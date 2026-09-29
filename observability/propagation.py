from __future__ import annotations

import re
from collections.abc import Mapping, MutableMapping
from urllib.parse import quote, unquote
from uuid import uuid4

from core.context import TraceContext

_TRACEPARENT = re.compile(r"^(00)-([0-9a-fA-F]{32})-([0-9a-fA-F]{16})-([0-9a-fA-F]{2})$")
_MAX_REQUEST_ID = 128
_MAX_BAGGAGE_BYTES = 8192


def _headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _baggage(value: str) -> dict[str, str]:
    if not value or len(value.encode("utf-8", errors="ignore")) > _MAX_BAGGAGE_BYTES:
        return {}
    result = {}
    for member in value.split(","):
        key, separator, item = member.strip().partition("=")
        if not separator or not key:
            continue
        key = unquote(key.strip())
        item = unquote(item.split(";", 1)[0].strip())
        if key and len(key) <= 256 and len(item) <= 1024:
            result[key] = item
    return result


def extract_trace_context(headers: Mapping[str, str], *, session_id: str | None = None) -> TraceContext:
    """Read W3C Trace Context and request metadata, falling back safely on invalid input."""
    normalized = _headers(headers)
    request_id = normalized.get("x-request-id", "").strip()[:_MAX_REQUEST_ID] or str(uuid4())
    session = session_id or normalized.get("x-session-id") or None
    baggage = _baggage(normalized.get("baggage", ""))
    match = _TRACEPARENT.fullmatch(normalized.get("traceparent", "").strip())
    if match:
        _, trace_id, span_id, flags = match.groups()
        try:
            return TraceContext(
                trace_id=trace_id,
                span_id=span_id,
                request_id=request_id,
                session_id=session,
                trace_flags=flags,
                baggage=baggage,
            )
        except ValueError:
            pass
    return TraceContext(request_id=request_id, session_id=session, baggage=baggage)


def inject_trace_context(headers: MutableMapping[str, str], context: TraceContext) -> None:
    """Inject W3C traceparent and baggage into an internal request or response."""
    if not isinstance(context, TraceContext):
        raise TypeError("context must be a TraceContext")
    active = context if context.span_id is not None else context.child()
    for key in tuple(headers):
        if str(key).lower() in {"traceparent", "x-request-id", "baggage"}:
            del headers[key]
    headers["traceparent"] = (
        f"00-{active.trace_id}-{active.span_id}-{active.trace_flags}"
    )
    headers["X-Request-ID"] = active.request_id
    if active.baggage:
        headers["baggage"] = ",".join(
            f"{quote(key, safe='')}={quote(value, safe='')}"
            for key, value in sorted(active.baggage.items())
        )
