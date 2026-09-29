import unittest

from core.context import TraceContext
from core.invocation import InvocationSource, Modality
from gateway.adapters.openai_chat import OpenAIChatAdapter


class OpenAIChatAdapterTests(unittest.TestCase):
    def test_maps_chat_request_without_leaking_transport_auth_into_principal(self):
        context = TraceContext(request_id="req-chat")
        invocation = OpenAIChatAdapter().to_invocation({
            "model": "fast",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "max_tokens": 32,
            "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
        }, context)

        self.assertEqual(invocation.source, InvocationSource.OPENAI_CHAT)
        self.assertEqual(invocation.modality, {Modality.TEXT})
        self.assertEqual(invocation.requested_model, "fast")
        self.assertEqual(invocation.trace_context.request_id, "req-chat")
        self.assertEqual(invocation.input.messages[0]["content"], "hi")
        self.assertEqual(invocation.principal.id, "gateway-client")
        self.assertEqual(invocation.tools[0].name, "lookup")
        self.assertTrue(invocation.options.stream)
        self.assertEqual(invocation.options.max_output_tokens, 32)

    def test_malformed_optional_fields_do_not_break_legacy_validation(self):
        invocation = OpenAIChatAdapter().to_invocation({"model": "fast", "messages": "bad", "temperature": "bad"})
        self.assertEqual(invocation.input.messages, ())
        self.assertIsNone(invocation.options.temperature)


if __name__ == "__main__":
    unittest.main()
