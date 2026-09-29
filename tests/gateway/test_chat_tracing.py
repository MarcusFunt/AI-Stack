import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.responses import Response
from fastapi.testclient import TestClient


class GatewayChatTracingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = patch.dict(os.environ, {
            "AI_API_KEY": "test-only-gateway-key",
            "SUPERVISOR_TOKEN": "test-only-supervisor-token",
            "LLAMA_API_KEY": "test-only-llama-key",
            "CONFIG_PATH": str(Path("config/models.json").resolve()),
        })
        cls.environment.start()
        cls.gateway = importlib.import_module("gateway.app")
        cls.client = TestClient(cls.gateway.app)

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("gateway.app", None)
        cls.environment.stop()

    def test_chat_exposes_propagated_trace_headers_without_calling_a_model(self):
        observed = {}
        original = self.gateway.forward_buffered

        async def fake_forward(service, path, request, body=None, **kwargs):
            observed["service"] = service
            observed["trace_context"] = getattr(request.state, "trace_context", None)
            return Response(content=b'{"ok":true}', media_type="application/json")

        self.gateway.forward_buffered = fake_forward
        try:
            response = self.client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": "Bearer test-only-gateway-key",
                    "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
                    "x-request-id": "request-42",
                },
                json={"model": "local-fast", "messages": [{"role": "user", "content": "private"}]},
            )
        finally:
            self.gateway.forward_buffered = original

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-request-id"], "request-42")
        self.assertEqual(response.headers["x-trace-id"], "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertTrue(response.headers["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-"))
        self.assertEqual(observed["service"], "llm")
        self.assertEqual(observed["trace_context"].trace_id, "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertEqual(observed["trace_context"].parent_span_id, "00f067aa0ba902b7")

        unauthorized = self.client.post(
            "/v1/chat/completions",
            json={"model": "local-fast", "messages": []},
        )
        self.assertEqual(unauthorized.status_code, 401)


if __name__ == "__main__":
    unittest.main()
