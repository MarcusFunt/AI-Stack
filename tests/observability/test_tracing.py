import unittest
from contextlib import contextmanager
from types import SimpleNamespace

from core.context import TraceContext
from core.invocation import Invocation, InvocationInput, InvocationSource, ModelPolicy
from observability.openinference import invocation_attributes
from observability.tracing import current_trace_context, start_span


class FakeSpan:
    def __init__(self):
        self.attributes = {}
        self.parent = None
        self.context = SimpleNamespace(
            trace_id=int("4bf92f3577b34da6a3ce929d0e0e4736", 16),
            span_id=int("00f067aa0ba902b8", 16),
            trace_flags=SimpleNamespace(sampled=True),
            is_valid=True,
        )

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def get_span_context(self):
        return self.context


class FakeTracer:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.span = FakeSpan()
        self.started = []

    @contextmanager
    def start_as_current_span(self, name, **kwargs):
        if self.fail:
            raise RuntimeError("trace backend offline")
        self.started.append((name, kwargs))
        yield self.span


class TracingTests(unittest.TestCase):
    def test_start_span_records_attributes_and_preserves_user_exceptions(self):
        tracer = FakeTracer()
        parent = TraceContext()
        with start_span("gateway.chat", parent=parent, attributes={"ai_stack.transport": "openai_chat"}, tracer=tracer) as span:
            span.set_attribute("test.attribute", "value")

        self.assertEqual(tracer.started[0][0], "gateway.chat")
        self.assertEqual(tracer.started[0][1]["attributes"]["ai_stack.transport"], "openai_chat")
        self.assertEqual(span.attributes["test.attribute"], "value")
        with self.assertRaisesRegex(RuntimeError, "application error"):
            with start_span("gateway.chat", tracer=tracer):
                raise RuntimeError("application error")

    def test_span_creation_failure_fails_open_and_returns_child_context(self):
        parent = TraceContext()
        with start_span("gateway.chat", parent=parent, tracer=FakeTracer(fail=True)) as span:
            context = current_trace_context(parent, span)

        self.assertEqual(context.trace_id, parent.trace_id)
        self.assertEqual(context.parent_span_id, parent.span_id)
        self.assertNotEqual(context.span_id, parent.span_id)

    def test_span_context_uses_actual_span_ids_when_available(self):
        parent = TraceContext(request_id="request-1")
        context = current_trace_context(parent, FakeSpan())

        self.assertEqual(context.trace_id, "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertEqual(context.span_id, "00f067aa0ba902b8")
        self.assertEqual(context.parent_span_id, parent.span_id)
        self.assertEqual(context.request_id, "request-1")

    def test_openinference_attributes_exclude_prompt_content(self):
        invocation = Invocation(
            source=InvocationSource.OPENAI_CHAT,
            requested_model="local-fast",
            model_policy=ModelPolicy(profile="voice-fast"),
            input=InvocationInput(text="sensitive prompt text"),
            metadata={"prompt": "also sensitive"},
        )

        attributes = invocation_attributes(invocation, provider="llama.cpp", service="llm")

        self.assertEqual(attributes["gen_ai.operation.name"], "generate")
        self.assertEqual(attributes["gen_ai.request.model"], "local-fast")
        self.assertEqual(attributes["ai_stack.transport"], "openai_chat")
        self.assertEqual(attributes["ai_stack.profile"], "voice-fast")
        self.assertNotIn("sensitive prompt text", repr(attributes))
        self.assertNotIn("also sensitive", repr(attributes))


if __name__ == "__main__":
    unittest.main()
