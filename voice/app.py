from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import secrets
import time
import wave
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

from voice.metrics import METRICS, VoiceMetrics
from voice.providers import GatewayVoiceProviders
from voice.session import SentenceChunker, VoiceSessionRegistry, visible_assistant_text
from voice.turn_detection import TurnEvent, VoiceTurnDetector

MAX_SESSION_BODY_BYTES = 16 * 1024
MAX_AUDIO_MESSAGE_BYTES = 128 * 1024
VOICE_SAMPLE_RATE = 16_000
VOICE_SUBPROTOCOL = "ai-stack.voice.v1"
VOICE_TICKET_PREFIX = "ai-stack.ticket."
VAD_SILENCE_SECONDS = float(os.getenv("VOICE_VAD_END_SILENCE_SECONDS", "0.65"))
VAD_SPEECH_THRESHOLD = float(os.getenv("VOICE_VAD_SPEECH_THRESHOLD", "420"))
MAX_TURN_SECONDS = float(os.getenv("VOICE_MAX_TURN_SECONDS", "90"))
MAX_SESSION_SECONDS = float(os.getenv("VOICE_SESSION_MAX_SECONDS", "3600"))


def _principal(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def _pcm_wav(audio: bytes, sample_rate: int = VOICE_SAMPLE_RATE) -> bytes:
    result = io.BytesIO()
    with wave.open(result, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(audio)
    return result.getvalue()


def _event(event_type: str, **data: Any) -> dict[str, Any]:
    return {"type": event_type, **data}


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
    make_providers = provider_factory or GatewayVoiceProviders
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
    def metrics() -> PlainTextResponse:
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
        language = body.get("language", "en")
        if language != "en":
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
        provider = None
        response_task: asyncio.Task | None = None
        response_state: dict[str, Any] | None = None
        response_cancel_reason: str | None = None
        evaluation_tasks: set[asyncio.Task] = set()
        detector = VoiceTurnDetector(
            sample_rate=VOICE_SAMPLE_RATE,
            speech_threshold=VAD_SPEECH_THRESHOLD,
            end_silence_seconds=VAD_SILENCE_SECONDS,
            max_turn_seconds=MAX_TURN_SECONDS,
        )
        session.state = "LISTENING"
        active_metrics.active_sessions += 1

        async def send(event_type: str, **data: Any) -> None:
            await websocket.send_json(_event(event_type, **data))

        def trace_id() -> str:
            parts = (session.traceparent or "").split("-")
            if len(parts) == 4 and len(parts[1]) == 32 and any(ch != "0" for ch in parts[1]):
                return parts[1].lower()
            return uuid4().hex

        def enqueue_evaluation(
            turn: dict[str, Any],
            event_type: str,
            *,
            interrupted: bool = False,
            truncation_recorded: bool = False,
            audio_integrity_ok: bool = True,
        ) -> None:
            elapsed_ms = lambda key: (
                round((turn[key] - turn["started_at"]) * 1000, 2)
                if turn.get(key) is not None
                else None
            )
            payload = {
                "event_id": str(uuid4()),
                "session_id": session.id,
                "turn_id": turn["id"],
                "trace_id": trace_id(),
                "event_type": event_type,
                "duration_ms": round((time.perf_counter() - turn["started_at"]) * 1000, 2),
                "time_to_first_transcript_ms": elapsed_ms("first_transcript_at"),
                "time_to_first_token_ms": elapsed_ms("first_token_at"),
                "time_to_first_audio_ms": elapsed_ms("first_audio_at"),
                "audio_input_bytes": turn["audio_input_bytes"],
                "audio_output_bytes": turn["audio_output_bytes"],
                "interrupted": interrupted,
                "truncation_recorded": truncation_recorded,
                "audio_integrity_ok": audio_integrity_ok,
                "stt_provider": session.stt_provider,
                "llm_provider": session.llm_provider,
                "tts_provider": session.tts_provider,
            }

            async def deliver() -> None:
                reporter = getattr(provider, "report_turn", None)
                if callable(reporter):
                    try:
                        await reporter(payload)
                    except Exception:
                        return

            task = asyncio.create_task(deliver())
            evaluation_tasks.add(task)
            task.add_done_callback(evaluation_tasks.discard)

        async def cancel_response(reason: str) -> None:
            nonlocal response_task, response_state, response_cancel_reason
            task = response_task
            current = response_state
            if task is None or task.done():
                response_task = None
                response_state = None
                return
            response_cancel_reason = reason
            if current is not None:
                current["interrupted"] = True
                current["cancel_reason"] = reason
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            response_task = None
            response_state = None

        async def transcribe_and_respond(audio: bytes) -> None:
            nonlocal response_state
            started_at = time.perf_counter()
            turn_stats = {
                "id": str(uuid4()),
                "started_at": started_at,
                "audio_input_bytes": len(audio),
                "audio_output_bytes": 0,
                "first_transcript_at": None,
                "first_token_at": None,
                "first_audio_at": None,
            }
            try:
                session.state = "TRANSCRIBING"
                transcript = await provider.transcribe(_pcm_wav(audio))
                turn_stats["first_transcript_at"] = time.perf_counter()
                full_text = transcript["text"].strip()
                transcript_id = str(uuid4())
                segments = transcript.get("segments") or []
                partials = [segment.get("text", "") for segment in segments if isinstance(segment, dict)]
                partials = [part for part in partials if isinstance(part, str) and part]
                if not partials and full_text:
                    partials = [full_text]
                accumulated = ""
                for part in partials:
                    accumulated += part
                    await send(
                        "conversation.item.input_audio_transcription.delta",
                        item_id=transcript_id,
                        delta=part,
                    )
                await send(
                    "conversation.item.input_audio_transcription.completed",
                    item_id=transcript_id,
                    transcript=full_text,
                    language="en",
                )
                if not full_text:
                    session.state = "LISTENING"
                    enqueue_evaluation(turn_stats, "voice.turn.empty")
                    return

                session.current_user_turn += 1
                session.history.append({"role": "user", "content": full_text})
                session.current_assistant_turn += 1
                response_id = "resp_" + secrets.token_hex(12)
                response_state = {
                    "id": response_id,
                    "turn_id": turn_stats["id"],
                    "generated_text": "",
                    "emitted_text": "",
                    "emitted_audio_bytes": 0,
                    "output_sample_rate": 24_000,
                    "output_channels": 1,
                    "interrupted": False,
                    "first_audio_at": None,
                }
                await send("response.created", response_id=response_id)
                session.state = "GENERATING"
                chunker = SentenceChunker()

                async def speak(chunk: str) -> None:
                    pcm, sample_rate, channels = await provider.synthesize(chunk)
                    if not pcm:
                        return
                    await send(
                        "response.audio.delta",
                        response_id=response_id,
                        delta=base64.b64encode(pcm).decode("ascii"),
                        sample_rate=sample_rate,
                        channels=channels,
                        text=chunk,
                    )
                    response_state["emitted_text"] += chunk + " "
                    response_state["emitted_audio_bytes"] += len(pcm)
                    response_state["output_sample_rate"] = sample_rate
                    response_state["output_channels"] = channels
                    turn_stats["audio_output_bytes"] = response_state["emitted_audio_bytes"]
                    session.audio_output_bytes += len(pcm)
                    if response_state["first_audio_at"] is None:
                        first_audio_at = time.perf_counter()
                        response_state["first_audio_at"] = first_audio_at
                        turn_stats["first_audio_at"] = first_audio_at
                        delay = first_audio_at - started_at
                        active_metrics.observe_first_audio(delay)

                async for delta in provider.chat_deltas(session.history):
                    if turn_stats["first_token_at"] is None:
                        turn_stats["first_token_at"] = time.perf_counter()
                    response_state["generated_text"] += delta
                    await send("response.output_text.delta", response_id=response_id, delta=delta)
                    for chunk in chunker.feed(delta):
                        session.state = "SPEAKING"
                        await speak(chunk)
                for chunk in chunker.feed("", final=True):
                    session.state = "SPEAKING"
                    await speak(chunk)

                assistant_text = visible_assistant_text(
                    response_state["generated_text"],
                    response_state["emitted_text"].strip(),
                    interrupted=False,
                )
                if assistant_text:
                    session.history.append({"role": "assistant", "content": assistant_text})
                await send(
                    "response.done",
                    response_id=response_id,
                    status="completed",
                    output_text=assistant_text,
                )
                active_metrics.turns += 1
                enqueue_evaluation(turn_stats, "voice.turn.completed", audio_integrity_ok=bool(turn_stats["audio_output_bytes"]))
            except asyncio.CancelledError:
                current = response_state
                if current is not None:
                    response_state["interrupted"] = True
                    audio_ms = round(
                        response_state["emitted_audio_bytes"]
                        / max(1, response_state["output_sample_rate"] * response_state["output_channels"] * 2)
                        * 1000
                    )
                    await send("response.cancelled", response_id=current["id"], reason=current.get("cancel_reason", response_cancel_reason or "client_cancelled"))
                    if response_state["emitted_text"].strip():
                        await send(
                            "conversation.item.truncated",
                            response_id=response_state["id"],
                            content_index=0,
                            audio_end_ms=audio_ms,
                        )
                        session.history.append({
                            "role": "assistant",
                            "content": visible_assistant_text(
                                response_state["generated_text"],
                                response_state["emitted_text"].strip(),
                                interrupted=True,
                            ),
                        })
                    truncation_recorded = not bool(current["emitted_audio_bytes"]) or bool(current["emitted_text"].strip())
                    active_metrics.interruptions += 1
                else:
                    truncation_recorded = True
                enqueue_evaluation(
                    turn_stats,
                    "voice.turn.interrupted",
                    interrupted=True,
                    truncation_recorded=truncation_recorded,
                    audio_integrity_ok=True,
                )
                raise
            except Exception as exc:
                await send("error", code="voice_turn_failed", message=str(exc)[:256])
                enqueue_evaluation(
                    turn_stats,
                    "voice.turn.failed",
                    audio_integrity_ok=False,
                    truncation_recorded=True,
                )
            finally:
                session.state = "LISTENING"
                if response_task is asyncio.current_task():
                    response_state = None

        async def handle_turn_event(event: TurnEvent) -> None:
            nonlocal response_task, response_state, response_cancel_reason
            if event.kind == "speech_started":
                await send("input_audio_buffer.speech_started", audio_start_ms=0)
                if response_task is not None and not response_task.done():
                    await cancel_response("barge_in")
                session.state = "LISTENING"
            elif event.kind == "speech_stopped" and event.audio:
                session.state = "TURN_PENDING"
                await send("input_audio_buffer.speech_stopped")
                if response_task is not None and not response_task.done():
                    await cancel_response("superseded_turn")
                response_state = None
                response_cancel_reason = None
                response_task = asyncio.create_task(transcribe_and_respond(event.audio))

        await send(
            "session.created",
            session_id=session.id,
            language="en",
            input_audio_format="pcm16",
            input_sample_rate=VOICE_SAMPLE_RATE,
        )
        await send("session.configured", language="en", turn_detection="server_vad")
        connected_at = time.monotonic()
        try:
            provider = make_providers(traceparent=session.traceparent, session_id=session.id)
            while True:
                remaining = MAX_SESSION_SECONDS - (time.monotonic() - connected_at)
                if remaining <= 0:
                    await send("session.timeout", session_id=session.id)
                    break
                try:
                    message = await asyncio.wait_for(websocket.receive(), timeout=remaining)
                except asyncio.TimeoutError:
                    await send("session.timeout", session_id=session.id)
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
                            await handle_turn_event(event)
                    except (ValueError, OverflowError) as exc:
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
                            await handle_turn_event(event)
                    except (ValueError, OverflowError) as exc:
                        await send("error", code="invalid_audio", message=str(exc))
                elif event_type == "input_audio_buffer.commit":
                    for event in detector.commit():
                        await handle_turn_event(event)
                elif event_type == "response.cancel":
                    await cancel_response("client_cancelled")
                elif event_type == "session.close":
                    await cancel_response("session_closed")
                    await send("session.closed", session_id=session.id)
                    break
                else:
                    await send("error", code="unsupported_event", event_type=event_type)
        except WebSocketDisconnect:
            pass
        finally:
            if response_task is not None and not response_task.done():
                await cancel_response("session_closed")
            if evaluation_tasks:
                await asyncio.wait(evaluation_tasks, timeout=2.5)
            if provider is not None:
                await provider.close()
            session.state = "CLOSED"
            sessions.remove(session.id)
            active_metrics.active_sessions = max(0, active_metrics.active_sessions - 1)

    return app


app = create_app()
