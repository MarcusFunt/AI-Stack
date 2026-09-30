from __future__ import annotations

import hashlib
import re
import secrets
import time
from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class RealtimeSession:
    id: str
    principal: str
    traceparent: str | None
    created_at: float
    token_digest: str
    token_expires_at: float
    state: str = "IDLE"
    conversation_id: str = field(default_factory=lambda: str(uuid4()))
    transport_type: str = "websocket"
    provider_type: str = "cascaded"
    history: list[dict[str, str]] = field(default_factory=list)
    current_user_turn: int = 0
    current_assistant_turn: int = 0
    stt_provider: str = "local-stt"
    llm_provider: str = "local-fast"
    tts_provider: str = "local-tts"
    pending_tool_calls: list[str] = field(default_factory=list)
    audio_input_bytes: int = 0
    audio_output_bytes: int = 0
    generation_counter: int = 0
    active_response_id: str | None = None
    active_response_generation_id: int | None = None
    active_playback_generation_id: int | None = None

    def begin_generation(self, response_id: str) -> int:
        self.generation_counter += 1
        self.active_response_id = response_id
        self.active_response_generation_id = self.generation_counter
        self.active_playback_generation_id = self.generation_counter
        return self.generation_counter

    def invalidate_generation(self) -> int:
        """Advance the generation before cancellation can yield control."""
        self.generation_counter += 1
        self.active_response_id = None
        self.active_response_generation_id = None
        self.active_playback_generation_id = None
        return self.generation_counter

    def generation_is_current(self, generation_id: int, response_id: str | None = None) -> bool:
        return (
            self.active_response_generation_id == generation_id
            and self.active_playback_generation_id == generation_id
            and (response_id is None or self.active_response_id == response_id)
        )

    def finish_generation(self, generation_id: int) -> bool:
        if not self.generation_is_current(generation_id):
            return False
        self.active_response_id = None
        self.active_response_generation_id = None
        self.active_playback_generation_id = None
        return True


# Backward-compatible name for existing imports and integrations.
VoiceSession = RealtimeSession


class VoiceSessionRegistry:
    """In-memory registry for bounded, one-use WebSocket session tickets."""

    def __init__(self, *, token_ttl_seconds: int = 60, max_sessions: int = 8) -> None:
        if token_ttl_seconds <= 0 or max_sessions <= 0:
            raise ValueError("session limits must be positive")
        self.token_ttl_seconds = token_ttl_seconds
        self.max_sessions = max_sessions
        self._sessions: dict[str, RealtimeSession] = {}
        self._connected: set[str] = set()

    def _remove_expired(self, now: float) -> None:
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if session_id not in self._connected and session.token_expires_at <= now
        ]
        for session_id in expired:
            self._sessions.pop(session_id, None)

    def create(
        self,
        *,
        principal: str,
        traceparent: str | None,
        now: float | None = None,
    ) -> tuple[RealtimeSession, str]:
        created_at = time.time() if now is None else float(now)
        self._remove_expired(created_at)
        if len(self._sessions) >= self.max_sessions:
            raise OverflowError("voice session capacity reached")

        token = secrets.token_urlsafe(32)
        session = RealtimeSession(
            id=str(uuid4()),
            principal=principal,
            traceparent=traceparent,
            created_at=created_at,
            token_digest=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            token_expires_at=created_at + self.token_ttl_seconds,
            history=[{
                "role": "system",
                "content": "You are a concise, helpful voice assistant. Respond naturally in English.",
            }],
        )
        self._sessions[session.id] = session
        return session, token

    def consume(
        self,
        session_id: str,
        token: str,
        *,
        now: float | None = None,
    ) -> RealtimeSession | None:
        current_time = time.time() if now is None else float(now)
        self._remove_expired(current_time)
        session = self._sessions.get(session_id)
        if session is None or session_id in self._connected or session.token_expires_at <= current_time:
            return None
        candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not secrets.compare_digest(candidate, session.token_digest):
            return None
        self._connected.add(session_id)
        return session

    def remove(self, session_id: str) -> None:
        self._connected.discard(session_id)
        self._sessions.pop(session_id, None)


class SentenceChunker:
    """Collect LLM deltas into sentence-sized chunks suitable for speech."""

    _BOUNDARY = re.compile(r"(?<=[.!?])(?:\s+|$)")

    def __init__(self, *, max_chars: int = 180) -> None:
        if max_chars < 8:
            raise ValueError("max_chars must be at least 8")
        self.max_chars = max_chars
        self._buffer = ""

    def feed(self, text: str, *, final: bool = False) -> list[str]:
        self._buffer += text
        chunks: list[str] = []

        while self._buffer:
            boundary = self._BOUNDARY.search(self._buffer)
            if boundary is not None and boundary.end() <= self.max_chars:
                chunk = self._buffer[: boundary.end()].strip()
                self._buffer = self._buffer[boundary.end() :].lstrip()
                if chunk:
                    chunks.append(chunk)
                continue

            if len(self._buffer) > self.max_chars:
                split_at = self._buffer.rfind(" ", 0, self.max_chars + 1)
                if split_at < 1:
                    split_at = self.max_chars
                chunk = self._buffer[:split_at].strip()
                self._buffer = self._buffer[split_at:].lstrip()
                if chunk:
                    chunks.append(chunk)
                continue
            break

        if final and self._buffer.strip():
            chunks.append(self._buffer.strip())
            self._buffer = ""
        return chunks


def visible_assistant_text(generated_text: str, emitted_audio_text: str, *, interrupted: bool) -> str:
    """Return only speech the client could have heard after a barge-in."""
    return emitted_audio_text if interrupted else generated_text
