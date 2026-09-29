from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from core.context import TraceContext

_TRACER = None


class _NoopSpan:
    """Span-shaped fallback used when tracing is not configured or fails."""

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def get_span_context(self):
        return None

    def end(self) -> None:
        return None


_NOOP_SPAN = _NoopSpan()


def initialize_tracing(service_name: str = "ai-stack") -> bool:
    """Configure OTLP tracing when an endpoint is supplied; return False on failure."""
    global _TRACER
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        _TRACER = trace.get_tracer(service_name)
        return True
    except Exception:
        _TRACER = None
        return False


def _parent_context(parent: TraceContext | None):
    if parent is None or parent.span_id is None:
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.context import Context
        from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState

        span_context = SpanContext(
            trace_id=int(parent.trace_id, 16),
            span_id=int(parent.span_id, 16),
            is_remote=True,
            trace_flags=TraceFlags(int(parent.trace_flags, 16)),
            trace_state=TraceState(),
        )
        return trace.set_span_in_context(NonRecordingSpan(span_context), Context())
    except (ImportError, AttributeError, TypeError, ValueError):
        return None


def _tracer_or_default(tracer=None):
    if tracer is not None:
        return tracer
    if _TRACER is not None:
        return _TRACER
    try:
        from opentelemetry import trace
        return trace.get_tracer("ai-stack")
    except Exception:
        return None


def start_span_handle(
    name: str,
    *,
    parent: TraceContext | None = None,
    attributes: Mapping[str, Any] | None = None,
    tracer=None,
):
    """Start a span whose lifetime can be ended by a later streaming callback."""
    active_tracer = _tracer_or_default(tracer)
    if active_tracer is None:
        return _NOOP_SPAN
    kwargs: dict[str, Any] = {"attributes": dict(attributes or {})}
    parent_context = _parent_context(parent)
    if parent_context is not None:
        kwargs["context"] = parent_context
    try:
        return active_tracer.start_span(name, **kwargs)
    except Exception:
        return _NOOP_SPAN


def end_span_handle(span, error: BaseException | None = None) -> None:
    """End a span handle, recording an error when streaming failed."""
    if error is not None:
        try:
            from opentelemetry.trace import Status, StatusCode

            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, type(error).__name__))
        except Exception:
            pass
    try:
        span.end()
    except Exception:
        pass


@contextmanager
def start_span(
    name: str,
    *,
    parent: TraceContext | None = None,
    attributes: Mapping[str, Any] | None = None,
    tracer=None,
) -> Iterator[Any]:
    """Start a span when possible and fail open without masking application errors."""
    active_tracer = _tracer_or_default(tracer)
    if active_tracer is None:
        yield _NOOP_SPAN
        return

    kwargs: dict[str, Any] = {"attributes": dict(attributes or {})}
    parent_context = _parent_context(parent)
    if parent_context is not None:
        kwargs["context"] = parent_context
    try:
        manager = active_tracer.start_as_current_span(name, **kwargs)
        span = manager.__enter__()
    except Exception:
        yield _NOOP_SPAN
        return

    try:
        yield span
    except BaseException as error:
        try:
            manager.__exit__(type(error), error, error.__traceback__)
        except BaseException:
            pass
        raise
    else:
        try:
            manager.__exit__(None, None, None)
        except Exception:
            pass


def current_trace_context(parent: TraceContext | None, span) -> TraceContext:
    """Build the canonical context from a real span, or a local child fallback."""
    if parent is None:
        parent = TraceContext()
    try:
        span_context = span.get_span_context()
        if span_context is None or not span_context.is_valid:
            return parent.child()
        # With only the OpenTelemetry API installed/configured, a no-op tracer
        # may hand back the parent NonRecordingSpan rather than a distinct child.
        # Treat that as "no span was created" so downstream propagation still
        # gets a valid child span id within the same trace.
        if (
            parent.span_id is not None
            and int(span_context.span_id) == int(parent.span_id, 16)
        ):
            return parent.child()
        trace_flags = getattr(span_context, "trace_flags", None)
        if trace_flags is None:
            flags = parent.trace_flags
        elif hasattr(trace_flags, "sampled"):
            flags = "01" if trace_flags.sampled else "00"
        else:
            flags = f"{int(trace_flags):02x}"
        return TraceContext(
            trace_id=f"{int(span_context.trace_id):032x}",
            span_id=f"{int(span_context.span_id):016x}",
            parent_span_id=parent.span_id,
            request_id=parent.request_id,
            session_id=parent.session_id,
            baggage=parent.baggage,
            trace_flags=flags,
        )
    except (AttributeError, TypeError, ValueError):
        return parent.child()
