from __future__ import annotations

import io
import json
import os
import wave
from collections.abc import AsyncIterator
from typing import Protocol

import httpx


class RealtimeProvider(Protocol):
    """Transport-neutral capabilities required by the realtime runtime."""

    async def transcribe(self, wav_audio: bytes, *, traceparent: str | None = None) -> dict: ...

    def chat_deltas(
        self, history: list[dict[str, str]], *, traceparent: str | None = None
    ) -> AsyncIterator[str]: ...

    async def synthesize(
        self, text: str, *, traceparent: str | None = None
    ) -> tuple[bytes, int, int]: ...

    async def close(self) -> None: ...


class CascadedRealtimeProvider:
    """STT -> chat -> TTS provider that invokes models only through the gateway."""

    def __init__(self, *, traceparent: str | None, session_id: str) -> None:
        api_key = os.getenv("AI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("AI_API_KEY must be set")
        self.gateway_url = os.getenv("GATEWAY_URL", "http://gateway:8000").rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "X-AI-Stack-Profile": "voice",
            "X-Session-ID": session_id,
        }
        if traceparent:
            self.headers["traceparent"] = traceparent
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=5.0))

    async def close(self) -> None:
        await self.client.aclose()

    def _headers_for(self, traceparent: str | None) -> dict[str, str]:
        headers = dict(self.headers)
        if traceparent:
            headers["traceparent"] = traceparent
        return headers

    async def transcribe(self, wav_audio: bytes, *, traceparent: str | None = None) -> dict:
        response = await self.client.post(
            self.gateway_url + "/v1/audio/transcriptions",
            headers=self._headers_for(traceparent),
            files={"file": ("voice-turn.wav", wav_audio, "audio/wav")},
            data={"model": "local-stt", "language": "en", "response_format": "json"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise ValueError("gateway returned an invalid transcription response")
        return payload

    async def chat_deltas(
        self, history: list[dict[str, str]], *, traceparent: str | None = None
    ) -> AsyncIterator[str]:
        payload = {
            "model": "local-fast",
            "messages": history,
            "stream": True,
            "max_tokens": 256,
            "temperature": 0.65,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        async with self.client.stream(
            "POST",
            self.gateway_url + "/v1/chat/completions",
            headers=self._headers_for(traceparent),
            json=payload,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = event.get("choices") or []
                if not choices:
                    continue
                delta = (choices[0].get("delta") or {}).get("content")
                if isinstance(delta, str) and delta:
                    yield delta

    async def synthesize(
        self, text: str, *, traceparent: str | None = None
    ) -> tuple[bytes, int, int]:
        response = await self.client.post(
            self.gateway_url + "/v1/audio/speech",
            headers=self._headers_for(traceparent),
            json={"model": "local-tts", "input": text, "voice": "Aiden", "response_format": "wav"},
        )
        response.raise_for_status()
        try:
            with wave.open(io.BytesIO(response.content), "rb") as audio:
                if audio.getsampwidth() != 2:
                    raise ValueError("TTS output must be 16-bit PCM")
                channels = audio.getnchannels()
                sample_rate = audio.getframerate()
                pcm = audio.readframes(audio.getnframes())
        except (wave.Error, EOFError) as exc:
            raise ValueError("gateway returned invalid WAV audio") from exc
        if not pcm or channels not in {1, 2} or sample_rate < 8_000:
            raise ValueError("gateway returned empty or unsupported audio")
        return pcm, sample_rate, channels

    async def report_turn(self, event: dict) -> bool:
        try:
            response = await self.client.post(
                self.gateway_url + "/internal/voice-evaluations",
                headers=self.headers,
                json=event,
                timeout=2.0,
            )
            return response.status_code < 300
        except httpx.HTTPError:
            return False


# Compatibility for callers that still use the original adapter name.
GatewayVoiceProviders = CascadedRealtimeProvider
