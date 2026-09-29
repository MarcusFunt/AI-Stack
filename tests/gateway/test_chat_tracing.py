import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
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
            observed["invocation"] = getattr(request.state, "invocation", None)
            observed["payload"] = json.loads(body) if body is not None else None
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
                json={
                    "model": "fast",
                    "messages": [
                        {"role": "user", "content": "private"},
                        {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
                    ],
                    "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
                },
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
        self.assertEqual(observed["invocation"].requested_model, "fast")
        self.assertEqual(observed["invocation"].trace_context.trace_id, "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertEqual(observed["payload"]["model"], "local-fast")
        self.assertEqual(observed["payload"]["tools"][0]["function"]["name"], "lookup")
        self.assertEqual(observed["payload"]["messages"][1]["tool_calls"][0]["function"]["arguments"], "{}")

        invalid_model = self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-only-gateway-key"},
            json={"model": "does-not-exist", "messages": []},
        )
        self.assertEqual(invalid_model.status_code, 404)
        not_chat = self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-only-gateway-key"},
            json={"model": "local-stt", "messages": []},
        )
        self.assertEqual(not_chat.status_code, 400)

        unauthorized = self.client.post(
            "/v1/chat/completions",
            json={"model": "local-fast", "messages": []},
        )
        self.assertEqual(unauthorized.status_code, 401)

    def test_completed_invocation_is_screened_after_response_and_failures_stay_fail_open(self):
        from gateway import eval_client

        observed = []
        original_forward = self.gateway.forward_buffered
        original_report = eval_client.report_invocation_screen

        async def fake_forward(service, path, request, body=None, **kwargs):
            return Response(content=b'{"choices":[{"message":{"content":"ok"}}]}', media_type="application/json")

        async def failing_report(invocation, model_route, **kwargs):
            observed.append((invocation, model_route, kwargs))
            raise RuntimeError("evaluation service offline")

        self.gateway.forward_buffered = fake_forward
        eval_client.report_invocation_screen = failing_report
        try:
            response = self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                json={"model": "fast", "messages": [{"role": "user", "content": "private prompt"}]},
            )
        finally:
            self.gateway.forward_buffered = original_forward
            eval_client.report_invocation_screen = original_report

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(observed), 1)
        invocation, route, details = observed[0]
        self.assertEqual(invocation.source.value, "openai_chat")
        self.assertEqual(route.model_id, "local-fast")
        self.assertEqual(details["http_status"], 200)
        self.assertGreater(details["response_bytes"], 0)


    def test_streaming_chat_uses_routed_provider_and_keeps_legacy_response(self):
        observed = {}
        original = self.gateway.forward_streaming

        async def fake_forward(service, path, request, body):
            observed["service"] = service
            observed["path"] = path
            observed["invocation"] = request.state.invocation
            return Response(content=b"data: [DONE]\n\n", media_type="text/event-stream")

        self.gateway.forward_streaming = fake_forward
        try:
            response = self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                json={"model": "reasoning", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            )
        finally:
            self.gateway.forward_streaming = original

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "data: [DONE]\n\n")
        self.assertEqual(observed["service"], "reasoning")
        self.assertEqual(observed["path"], "/v1/chat/completions")
        self.assertTrue(observed["invocation"].options.stream)

    def test_audio_and_vision_routes_attach_canonical_invocations(self):
        observed = []
        original = self.gateway.forward_buffered

        async def fake_forward(service, path, request, body=None, **kwargs):
            observed.append({
                "service": service,
                "path": path,
                "invocation": getattr(request.state, "invocation", None),
                "body": body,
            })
            return Response(content=b"ok", media_type="application/octet-stream")

        self.gateway.forward_buffered = fake_forward
        headers = {"Authorization": "Bearer test-only-gateway-key"}
        try:
            transcription = self.client.post(
                "/v1/audio/transcriptions",
                headers=headers,
                files={"file": ("sample.wav", b"audio bytes", "audio/wav")},
                data={"language": "en", "response_format": "text"},
            )
            speech = self.client.post(
                "/v1/audio/speech",
                headers=headers,
                json={"model": "local-tts", "input": "hello", "voice": "alloy", "response_format": "wav"},
            )
            vision = self.client.post(
                "/v1/vision/analyze",
                headers=headers,
                files={"image": ("image.png", b"png bytes", "image/png")},
                data={"prompt": "describe", "max_new_tokens": "12"},
            )
        finally:
            self.gateway.forward_buffered = original

        self.assertEqual([transcription.status_code, speech.status_code, vision.status_code], [200, 200, 200])
        self.assertEqual([(item["service"], item["path"]) for item in observed], [
            ("stt", "/v1/audio/transcriptions"),
            ("tts", "/v1/audio/speech"),
            ("vlm", "/v1/vision/analyze"),
        ])
        self.assertEqual(observed[0]["invocation"].input.data["upload"]["mime_type"], "audio/wav")
        self.assertEqual(observed[1]["invocation"].input.text, "hello")
        self.assertEqual(observed[2]["invocation"].input.data["image"]["mime_type"], "image/png")
        self.assertIn(b"audio bytes", observed[0]["body"])
        self.assertIn(b"png bytes", observed[2]["body"])
        self.assertIn("traceparent", transcription.headers)
        self.assertIn("traceparent", speech.headers)
        self.assertIn("traceparent", vision.headers)

    def test_backend_failure_keeps_existing_http_error_semantics(self):
        original = self.gateway.forward_buffered

        async def fail_forward(*args, **kwargs):
            raise HTTPException(502, "llm upstream request failed")

        self.gateway.forward_buffered = fail_forward
        try:
            response = self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
            )
        finally:
            self.gateway.forward_buffered = original

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "llm upstream request failed")


if __name__ == "__main__":
    unittest.main()
