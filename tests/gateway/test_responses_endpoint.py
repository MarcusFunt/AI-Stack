import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.responses import Response, StreamingResponse
from fastapi.testclient import TestClient


class ResponsesEndpointTests(unittest.TestCase):
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

    def test_buffered_response_maps_through_router_and_returns_responses_shape(self):
        observed = {}
        original = self.gateway.forward_buffered

        async def fake_forward(service, path, request, body=None, **kwargs):
            observed["service"] = service
            observed["path"] = path
            observed["invocation"] = request.state.invocation
            observed["payload"] = json.loads(body)
            return Response(content=json.dumps({
                "id": "chatcmpl_1",
                "model": "local-fast",
                "choices": [{"message": {"role": "assistant", "content": "hello"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            }), media_type="application/json")

        self.gateway.forward_buffered = fake_forward
        try:
            response = self.client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                json={
                    "model": "fast",
                    "instructions": "Be brief",
                    "input": "hi",
                    "metadata": {"ticket": "42"},
                    "max_output_tokens": 32,
                },
            )
        finally:
            self.gateway.forward_buffered = original

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["object"], "response")
        self.assertEqual(result["output"][0]["content"][0]["text"], "hello")
        self.assertEqual(result["usage"]["total_tokens"], 6)
        self.assertEqual(result["metadata"], {"ticket": "42"})
        self.assertEqual(observed["service"], "llm")
        self.assertEqual(observed["path"], "/v1/chat/completions")
        self.assertEqual(observed["invocation"].source.value, "openai_responses")
        self.assertEqual(observed["payload"]["model"], "local-fast")
        self.assertEqual(observed["payload"]["messages"][0]["role"], "system")
        self.assertNotIn("metadata", observed["payload"])

    def test_streaming_maps_chat_sse_into_responses_events(self):
        original = self.gateway.forward_streaming

        async def fake_chunks():
            yield b'data: {"choices":[{"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}\n\n'
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            yield b"data: [DONE]\n\n"

        async def fake_forward(service, path, request, body):
            return StreamingResponse(fake_chunks(), media_type="text/event-stream")

        self.gateway.forward_streaming = fake_forward
        try:
            response = self.client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                json={"input": "hi", "stream": True},
            )
        finally:
            self.gateway.forward_streaming = original

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"].split(";")[0], "text/event-stream")
        self.assertIn("event: response.output_text.delta", response.text)
        self.assertIn('"delta":"Hello"', response.text)
        self.assertIn("event: response.completed", response.text)

    def test_rejects_unimplemented_server_state_and_keeps_auth(self):
        stateful = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-only-gateway-key"},
            json={"input": "hi", "previous_response_id": "resp_1"},
        )
        self.assertEqual(stateful.status_code, 400)
        self.assertIn("include conversation history", stateful.json()["detail"])

        unauthorized = self.client.post("/v1/responses", json={"input": "hi"})
        self.assertEqual(unauthorized.status_code, 401)


if __name__ == "__main__":
    unittest.main()
