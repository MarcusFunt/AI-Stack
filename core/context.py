from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from uuid import uuid4

_TRACE_ID = re.compile(r"^[0-9a-fA-F]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-fA-F]{16}$")
_TRACE_FLAGS = re.compile(r"^[0-9a-fA-F]{2}$")


def _valid_hex_id(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{label} must be a hexadecimal W3C trace identifier")
    normalized = value.lower()
    if set(normalized) == {"0"}:
        raise ValueError(f"{label} must not be all zeroes")
    return normalized


@dataclass(frozen=True, slots=True)
class TraceContext:
    """Trace identifiers and baggage carried across adapters and providers."""

    trace_id: str = field(default_factory=lambda: uuid4().hex)
    span_id: str | None = field(default_factory=lambda: secrets.token_hex(8))
    parent_span_id: str | None = None
    request_id: str = field(default_factory=lambda: str(uuid4()))
    session_id: str | None = None
    baggage: dict[str, str] = field(default_factory=dict)
    trace_flags: str = "01"

    def __post_init__(self) -> None:
        object.__setattr__(self, "trace_id", _valid_hex_id(self.trace_id, _TRACE_ID, "trace_id"))
        if self.span_id is not None:
            object.__setattr__(self, "span_id", _valid_hex_id(self.span_id, _SPAN_ID, "span_id"))
        if self.parent_span_id is not None:
            object.__setattr__(self, "parent_span_id", _valid_hex_id(self.parent_span_id, _SPAN_ID, "parent_span_id"))
        if not isinstance(self.trace_flags, str) or not _TRACE_FLAGS.fullmatch(self.trace_flags):
            raise ValueError("trace_flags must be two hexadecimal characters")
        object.__setattr__(self, "trace_flags", self.trace_flags.lower())
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValueError("request_id must be a non-empty string")
        if self.session_id is not None and not isinstance(self.session_id, str):
            raise ValueError("session_id must be a string or None")
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in self.baggage.items()):
            raise ValueError("trace baggage keys and values must be strings")
        object.__setattr__(self, "baggage", dict(self.baggage))

    def child(self, *, session_id: str | None = None, baggage: dict[str, str] | None = None) -> TraceContext:
        """Return a new span in the same trace, parented to this span."""
        merged_baggage = dict(self.baggage)
        if baggage:
            merged_baggage.update(baggage)
        return TraceContext(
            trace_id=self.trace_id,
            span_id=secrets.token_hex(8),
            parent_span_id=self.span_id,
            request_id=self.request_id,
            session_id=self.session_id if session_id is None else session_id,
            baggage=merged_baggage,
            trace_flags=self.trace_flags,
        )
