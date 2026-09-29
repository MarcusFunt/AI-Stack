import unittest
from uuid import UUID

from core.context import TraceContext
from core.invocation import (
    Invocation,
    InvocationInput,
    InvocationOperation,
    InvocationOptions,
    InvocationSource,
    ModelPolicy,
    Modality,
    Principal,
)
from core.tools import ToolDefinition


class InvocationTests(unittest.TestCase):
    def test_invocation_has_transport_independent_defaults_and_round_trip_fields(self):
        invocation = Invocation(
            operation=InvocationOperation.GENERATE,
            modality={Modality.TEXT},
            source=InvocationSource.OPENAI_CHAT,
            principal=Principal(id="user-1", kind="user", scopes={"chat"}),
            requested_model="local-fast",
            model_policy=ModelPolicy(capability="chat", prefer_local=True),
            input=InvocationInput(text="hello"),
            tools=[ToolDefinition(name="lookup", input_schema={"type": "object"})],
            options=InvocationOptions(stream=True, max_output_tokens=16),
            metadata={"unknown": {"retained": True}},
        )

        self.assertIsInstance(UUID(invocation.id), UUID)
        self.assertEqual(invocation.source, InvocationSource.OPENAI_CHAT)
        self.assertEqual(invocation.modality, {Modality.TEXT})
        self.assertEqual(invocation.input.text, "hello")
        self.assertIsInstance(invocation.tools, list)
        self.assertTrue(invocation.options.stream)
        self.assertTrue(invocation.metadata["unknown"]["retained"])
        self.assertIsNotNone(invocation.created_at.tzinfo)
        self.assertIsInstance(UUID(invocation.trace_context.request_id), UUID)

    def test_invocation_rejects_invalid_uuid_identifiers(self):
        with self.assertRaises(ValueError):
            Invocation(id="not-a-uuid")

        with self.assertRaises(ValueError):
            Invocation(parent_invocation_id="also-not-a-uuid")

    def test_trace_context_uses_valid_w3c_ids_and_creates_child_span(self):
        root = TraceContext()
        child = root.child()

        self.assertRegex(root.trace_id, r"^[0-9a-f]{32}$")
        self.assertRegex(root.span_id, r"^[0-9a-f]{16}$")
        self.assertEqual(child.trace_id, root.trace_id)
        self.assertEqual(child.parent_span_id, root.span_id)
        self.assertNotEqual(child.span_id, root.span_id)
        self.assertEqual(child.request_id, root.request_id)

    def test_trace_context_rejects_invalid_w3c_ids(self):
        with self.assertRaises(ValueError):
            TraceContext(trace_id="bad")

        with self.assertRaises(ValueError):
            TraceContext(trace_id="0" * 32)

        with self.assertRaises(ValueError):
            TraceContext(span_id="0" * 16)


if __name__ == "__main__":
    unittest.main()
