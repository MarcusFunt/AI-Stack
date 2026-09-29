from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from threading import RLock
from typing import Any, Mapping
from uuid import uuid4

from .invocation import Principal


class SessionType(str, Enum):
    CHAT = "chat"
    REALTIME_VOICE = "realtime_voice"
    AGENT = "agent"
    DEVICE = "device"


@dataclass(slots=True)
class Session:
    id: str
    session_type: SessionType
    principal: Principal
    conversation_state: list[dict[str, Any]] = field(default_factory=list)
    active_invocations: set[str] = field(default_factory=set)
    model_profile: str | None = None
    tool_permissions: set[str] = field(default_factory=set)
    transport_metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SessionManager:
    """Explicit, thread-safe in-memory session owner; persistence is adapter-owned."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = RLock()

    def create(self, session_type: SessionType | str, *, principal: Principal, model_profile: str | None = None, tool_permissions: set[str] | frozenset[str] = frozenset(), transport_metadata: Mapping[str, Any] | None = None) -> Session:
        kind = session_type if isinstance(session_type, SessionType) else SessionType(session_type)
        now = datetime.now(timezone.utc)
        session = Session(
            id=str(uuid4()),
            session_type=kind,
            principal=principal,
            model_profile=model_profile,
            tool_permissions=set(tool_permissions),
            transport_metadata=dict(transport_metadata or {}),
            created_at=now,
            last_activity=now,
        )
        with self._lock:
            self._sessions[session.id] = session
        return self._copy(session)

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            session = self._sessions.get(session_id)
            return None if session is None else self._copy(session)

    def start_invocation(self, session_id: str, invocation_id: str) -> None:
        with self._lock:
            session = self._require(session_id)
            session.active_invocations.add(invocation_id)
            self._touch(session)

    def finish_invocation(self, session_id: str, invocation_id: str) -> None:
        with self._lock:
            session = self._require(session_id)
            session.active_invocations.discard(invocation_id)
            self._touch(session)

    def append_message(self, session_id: str, message: Mapping[str, Any]) -> None:
        with self._lock:
            session = self._require(session_id)
            session.conversation_state.append(dict(message))
            self._touch(session)

    def delete(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def _require(self, session_id: str) -> Session:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise KeyError(f"unknown session: {session_id}") from exc

    @staticmethod
    def _touch(session: Session) -> None:
        session.last_activity = datetime.now(timezone.utc)

    @staticmethod
    def _copy(session: Session) -> Session:
        return copy.deepcopy(session)
