from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from .invocation import Invocation


class InvocationEventType(str, Enum):
    INVOCATION_STARTED = "invocation.started"
    STARTED = "invocation.started"
    INVOCATION_ACCEPTED = "invocation.accepted"
    INVOCATION_CANCEL_REQUESTED = "invocation.cancel_requested"
    INVOCATION_CANCELLED = "invocation.cancelled"
    INVOCATION_COMPLETED = "invocation.completed"
    COMPLETED = "invocation.completed"
    INVOCATION_FAILED = "invocation.failed"
    INPUT_TEXT = "input.text"
    INPUT_AUDIO_DELTA = "input.audio.delta"
    INPUT_AUDIO_COMMITTED = "input.audio.committed"
    INPUT_IMAGE = "input.image"
    TRANSCRIPT_PARTIAL = "transcript.partial"
    TRANSCRIPT_FINAL = "transcript.final"
    TRANSCRIPT_FAILED = "transcript.failed"
    RESPONSE_TEXT_DELTA = "response.text.delta"
    RESPONSE_TEXT_COMPLETED = "response.text.completed"
    RESPONSE_REASONING_DELTA = "response.reasoning.delta"
    RESPONSE_REASONING_COMPLETED = "response.reasoning.completed"
    TOOL_REQUESTED = "tool.requested"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    SPEECH_STARTED = "speech.started"
    SPEECH_AUDIO_DELTA = "speech.audio.delta"
    SPEECH_COMPLETED = "speech.completed"
    SPEECH_CANCELLED = "speech.cancelled"
    RESOURCE_LEASE_REQUESTED = "resource.lease.requested"
    RESOURCE_LEASE_GRANTED = "resource.lease.granted"
    RESOURCE_MODEL_LOADING = "resource.model.loading"
    RESOURCE_MODEL_READY = "resource.model.ready"
    RESOURCE_MODEL_EVICTED = "resource.model.evicted"
    EVAL_SCREEN_REQUESTED = "eval.screen.requested"
    EVAL_SCREEN_COMPLETED = "eval.screen.completed"
    EVAL_DEEP_REQUESTED = "eval.deep.requested"
    EVAL_DEEP_COMPLETED = "eval.deep.completed"
    EVAL_FAILED = "eval.failed"


def _trace_id(value: str) -> str:
    if not isinstance(value, str) or len(value) != 32:
        raise ValueError("trace_id must be a 32-character hexadecimal identifier")
    try:
        numeric = int(value, 16)
    except ValueError as exc:
        raise ValueError("trace_id must be a 32-character hexadecimal identifier") from exc
    if numeric == 0:
        raise ValueError("trace_id must not be all zeroes")
    return value.lower()


@dataclass(frozen=True, slots=True)
class InvocationEvent:
    event_id: str
    invocation_id: str
    trace_id: str
    type: str
    timestamp: datetime
    sequence: int
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "event_id", str(UUID(str(self.event_id))))
            object.__setattr__(self, "invocation_id", str(UUID(str(self.invocation_id))))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("event_id and invocation_id must be UUIDs") from exc
        object.__setattr__(self, "trace_id", _trace_id(self.trace_id))
        if isinstance(self.type, Enum):
            object.__setattr__(self, "type", self.type.value)
        if not isinstance(self.type, str) or not self.type.strip():
            raise ValueError("event type must be a non-empty string")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("event timestamp must be timezone-aware")
        if self.sequence < 1:
            raise ValueError("event sequence must be positive")
        object.__setattr__(self, "data", dict(self.data))


class InvocationEventSequencer:
    """Assign ordered, unique events for one invocation."""

    def __init__(self, invocation: Invocation, *, start: int = 0) -> None:
        if not isinstance(invocation, Invocation):
            raise TypeError("InvocationEventSequencer requires an Invocation")
        if start < 0:
            raise ValueError("start must be non-negative")
        self._invocation = invocation
        self._sequence = start
        self._lock = threading.Lock()

    def emit(self, event_type: InvocationEventType | str, data: dict[str, Any] | None = None) -> InvocationEvent:
        value = event_type.value if isinstance(event_type, Enum) else event_type
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        return InvocationEvent(
            event_id=str(uuid4()),
            invocation_id=self._invocation.id,
            trace_id=self._invocation.trace_context.trace_id,
            type=value,
            timestamp=datetime.now(timezone.utc),
            sequence=sequence,
            data={} if data is None else data,
        )
