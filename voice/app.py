from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

from observability.propagation import extract_trace_context
from observability.tracing import initialize_tracing
from voice.metrics import METRICS, VoiceMetrics
from voice.providers import CascadedRealtimeProvider
from voice.runtime import RealtimeRuntime
from voice.session import VoiceSessionRegistry
from voice.turn_detection import VoiceTurnDetector

MAX_SESSION_BODY_BYTES = 16 * 1024
MAX_AUDIO_MESSAGE_BYTES = 128 * 1024
VOICE_SAMPLE_RATE = 16_000
VOICE_SUBPROTOCOL = "ai-stack.voice.v1"
VOICE_TICKET_PREFIX = "ai-stack.ticket."
VAD_SILENCE_SECONDS = float(os.getenv("VOICE_VAD_END_SILENCE_SECONDS", "0.65"))
VAD_SPEECH_THRESHOLD = float(os.getenv("VOICE_VAD_SPEECH_THRESHOLD", "420"))
MAX_TURN_SECONDS = float(os.getenv("VOICE_MAX_TURN_SECONDS", "90"))
MAX_SESSION_SECONDS = float(os.getenv("VOICE_SESSION_MAX_SECONDS", "3600"))
initialize_tracing("ai-stack-voice")


def _principal(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def _audio_message(message: dict[str, Any]) -> bytes:
    if message.get("type") != "input_audio_buffer.append":
        raise ValueError("expected input_audio_buffer.append")
    encoded = message.get("audio")
    if not isinstance(encoded, str):
        raise ValueError("audio must be a base64-encoded PCM16 chunk")
    try:
        audio = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("audio must be valid base64") from exc
    if not audio or len(audio) > MAX_AUDIO_MESSAGE_BYTES:
        raise ValueError("audio chunk must be between 1 byte and 128 KiB")
    return audio


def create_app(
    *,
    registry: VoiceSessionRegistry | None = None,
    metrics: VoiceMetrics | None = None,
    provider_factory: Callable[..., Any] | None = None,
    api_key: str | None = None,
) -> FastAPI:
    configured_key = (os.getenv("AI_API_KEY", "") if api_key is None else api_key).strip()
    if not configured_key:
        raise RuntimeError("AI_API_KEY must be set")

    sessions = registry or VoiceSessionRegistry(
        token_ttl_seconds=int(os.getenv("VOICE_SESSION_TOKEN_TTL_SECONDS", "60")),
        max_sessions=int(os.getenv("VOICE_MAX_SESSIONS", "8")),
    )
    active_metrics = metrics or METRICS
    make_providers = provider_factory or CascadedRealtimeProvider
    app = FastAPI(title="AI-Stack Realtime Voice", version="0.1.0")
    app.state.sessions = sessions

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "voice"}

    @app.get("/ready")
    async def ready() -> dict[str, str]:
        gateway_url = os.getenv("GATEWAY_URL", "http://gateway:8000").rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(gateway_url + "/health")
            response.raise_for_status()
        except Exception as exc:
            raise HTTPException(503, "gateway dependency is unavailable") from exc
        return {"status": "ready", "service": "voice"}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics_endpoint() -> PlainTextResponse:
        return PlainTextResponse(active_metrics.render(), media_type="text/plain; version=0.0.4")

    @app.post("/sessions")
    async def create_session(request: Request) -> dict[str, Any]:
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied, f"Bearer {configured_key}"):
            raise HTTPException(401, "invalid API key")
        raw = await request.body()
        if len(raw) > MAX_SESSION_BODY_BYTES:
            raise HTTPException(413, "session configuration is too large")
        try:
            body = json.loads(raw or b"{}")
        except Exception as exc:
            raise HTTPException(400, "session configuration must be JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "session configuration must be a JSON object")
        if body.get("language", "en") != "en":
            raise HTTPException(400, "realtime voice currently supports English only")

        session, token = sessions.create(
            principal=_principal(configured_key),
            traceparent=request.headers.get("traceparent"),
        )
        expires_at = int(session.token_expires_at)
        return {
            "id": session.id,
            "object": "realtime.session",
            "expires_at": expires_at,
            "client_secret": {"value": token, "expires_at": expires_at},
            "ws_url": f"ws://voice:8000/ws/{session.id}",
            "language": "en",
            "input_audio_format": "pcm16",
            "input_sample_rate": VOICE_SAMPLE_RATE,
            "output_audio_format": "pcm16",
        }

    @app.websocket("/ws/{session_id}")
    async def realtime_socket(websocket: WebSocket, session_id: str) -> None:
        offered_protocols = websocket.scope.get("subprotocols", [])
        ticket_protocol = next(
            (value for value in offered_protocols if value.startswith(VOICE_TICKET_PREFIX)),
            "",
        )
        token = ticket_protocol[len(VOICE_TICKET_PREFIX) :]
        if len(token) > 128 or VOICE_SUBPROTOCOL not in offered_protocols:
            await websocket.close(code=4401)
            return
        session = sessions.consume(session_id, token)
        if session is None:
            await websocket.close(code=4401)
            return

        await websocket.accept(subprotocol=VOICE_SUBPROTOCOL)
        detector = VoiceTurnDetector(
            sample_rate=VOICE_SAMPLE_RATE,
            speech_threshold=VAD_SPEECH_THRESHOLD,
            end_silence_seconds=VAD_SILENCE_SECONDS,
            max_turn_seconds=MAX_TURN_SECONDS,
        )
        session.state = "LISTENING"
        active_metrics.active_sessions += 1
        provider = None
        runtime: RealtimeRuntime | None = None

        async def send(event_type: str, **data: Any) -> None:
            await websocket.send_json({"type": event_type, "session_id": session.id, **data})

        session_trace_context = extract_trace_context(
            {"traceparent": session.traceparent or "", "x-session-id": session.id},
            session_id=session.id,
        )
        await send(
            "session.created",
            language="en",
            input_audio_format="pcm16",
            input_sample_rate=VOICE_SAMPLE_RATE,
        )
        await send("session.configured", language="en", turn_detection="server_vad")
        connected_at = time.monotonic()
        try:
            provider = make_providers(traceparent=session.traceparent, session_id=session.id)
            runtime = RealtimeRuntime(
                session=session,
                provider=provider,
                metrics=active_metrics,
                send_event=websocket.send_json,
                parent_trace_context=session_trace_context,
            )
            while True:
                remaining = MAX_SESSION_SECONDS - (time.monotonic() - connected_at)
                if remaining <= 0:
                    await send("session.timeout")
                    break
                try:
                    message = await asyncio.wait_for(websocket.receive(), timeout=remaining)
                except asyncio.TimeoutError:
                    await send("session.timeout")
                    break
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    audio = message["bytes"]
                    if not audio or len(audio) > MAX_AUDIO_MESSAGE_BYTES:
                        await send("error", code="audio_chunk_too_large")
                        continue
                    session.audio_input_bytes += len(audio)
                    try:
                        for event in detector.feed(audio):
                            await runtime.handle_turn_event(event)
                    except OverflowError as exc:
                        await send("input_audio_buffer.limit_exceeded", message=str(exc))
                    except ValueError as exc:
                        await send("error", code="invalid_audio", message=str(exc))
                    continue

                text = message.get("text")
                if not isinstance(text, str) or len(text) > MAX_AUDIO_MESSAGE_BYTES:
                    await send("error", code="invalid_message")
                    continue
                try:
                    control = json.loads(text)
                except json.JSONDecodeError:
                    await send("error", code="invalid_json")
                    continue
                if not isinstance(control, dict):
                    await send("error", code="invalid_message")
                    continue

                event_type = control.get("type")
                if event_type == "session.configure":
                    if control.get("language", "en") != "en":
                        await send("error", code="unsupported_language", message="English only")
                        continue
                    if control.get("input_audio_format", "pcm16") != "pcm16":
                        await send("error", code="unsupported_audio_format", message="Use pcm16")
                        continue
                    if control.get("sample_rate", VOICE_SAMPLE_RATE) != VOICE_SAMPLE_RATE:
                        await send("error", code="unsupported_sample_rate", message="Use 16000 Hz")
                        continue
                    await send("session.configured", language="en", turn_detection="server_vad")
                elif event_type == "input_audio_buffer.append":
                    try:
                        audio = _audio_message(control)
                        session.audio_input_bytes += len(audio)
                        for event in detector.feed(audio):
                            await runtime.handle_turn_event(event)
                    except OverflowError as exc:
                        await send("input_audio_buffer.limit_exceeded", message=str(exc))
                    except ValueError as exc:
                        await send("error", code="invalid_audio", message=str(exc))
                elif event_type == "input_audio_buffer.commit":
                    for event in detector.commit():
                        await runtime.handle_turn_event(event)
                elif event_type == "response.cancel":
                    await runtime.cancel_response("client_cancelled")
                elif event_type == "session.close":
                    await runtime.cancel_response("session_closed")
                    await send("session.closed")
                    break
                else:
                    await send("error", code="unsupported_event", event_type=event_type)
        except WebSocketDisconnect:
            pass
        finally:
            if runtime is not None:
                await runtime.close()
            if provider is not None:
                await provider.close()
            session.state = "CLOSED"
            sessions.remove(session.id)
            active_metrics.active_sessions = max(0, active_metrics.active_sessions - 1)

    return app


app = create_app()
