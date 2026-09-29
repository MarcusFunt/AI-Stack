from __future__ import annotations

import asyncio
import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


class GatewayRealtimeVoiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = patch.dict(os.environ, {
            "AI_API_KEY": "test-only-gateway-key",
            "SUPERVISOR_TOKEN": "test-only-supervisor-token",
            "LLAMA_API_KEY": "test-only-llama-key",
            "CONFIG_PATH": str(Path("config/models.json").resolve()),
            "VOICE_URL": "http://voice-test:8000",
        })
        cls.environment.start()
        cls.gateway = importlib.import_module("gateway.app")
        cls.client = TestClient(cls.gateway.app)

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("gateway.app", None)
        cls.environment.stop()

    def test_session_create_is_authenticated_traced_and_rewrites_internal_websocket_url(self):
        observed = {}
        original = self.gateway.httpx.AsyncClient.post

        async def fake_post(client, url, **kwargs):
            observed["url"] = url
            observed["headers"] = kwargs["headers"]
            observed["payload"] = kwargs["json"]
            return self.gateway.httpx.Response(200, json={
                "id": "session-123",
                "client_secret": {"value": "one-use-ticket", "expires_at": 2000},
                "ws_url": "ws://voice:8000/ws/session-123",
            })

        self.gateway.httpx.AsyncClient.post = fake_post
        try:
            unauthorized = self.client.post("/v1/realtime/sessions", json={})
            self.assertEqual(unauthorized.status_code, 401)

            response = self.client.post(
                "/v1/realtime/sessions",
                headers={
                    "Authorization": "Bearer test-only-gateway-key",
                    "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
                },
                json={"language": "en"},
            )
        finally:
            self.gateway.httpx.AsyncClient.post = original

        self.assertEqual(response.status_code, 200)
        self.assertEqual(observed["url"], "http://voice-test:8000/sessions")
        self.assertEqual(observed["headers"]["Authorization"], "Bearer test-only-gateway-key")
        self.assertTrue(observed["headers"]["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-"))
        self.assertEqual(observed["payload"], {"language": "en"})
        result = response.json()
        self.assertEqual(result["ws_url"], "ws://testserver/v1/realtime?session_id=session-123")
        self.assertEqual(result["client_secret"]["value"], "one-use-ticket")
        self.assertNotIn("voice:8000", result["ws_url"])

    def test_gateway_rejects_non_english_session_before_proxying(self):
        response = self.client.post(
            "/v1/realtime/sessions",
            headers={"Authorization": "Bearer test-only-gateway-key"},
            json={"language": "da"},
        )

        self.assertEqual(response.status_code, 400)

    def test_voice_evaluation_forwarding_is_authenticated_and_fail_open(self):
        observed = {}
        original = self.gateway.httpx.AsyncClient.post

        async def fake_post(client, url, **kwargs):
            observed["url"] = url
            observed["headers"] = kwargs["headers"]
            observed["json"] = kwargs["json"]
            return self.gateway.httpx.Response(201, json={"accepted": True})

        self.gateway.httpx.AsyncClient.post = fake_post
        try:
            response = self.client.post(
                "/internal/voice-evaluations",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                json={"event_type": "voice.turn.completed", "trace_id": "a" * 32},
            )
        finally:
            self.gateway.httpx.AsyncClient.post = original

        self.assertEqual(response.status_code, 202)
        self.assertEqual(observed["url"], "http://eval-router:8000/v1/voice-evaluations")
        self.assertEqual(observed["headers"]["Authorization"], "Bearer test-only-gateway-key")
        self.assertEqual(observed["json"]["event_type"], "voice.turn.completed")

    def test_websocket_bridge_keeps_ticket_out_of_url_and_relays_frames(self):
        observed = {}

        class FakeUpstream:
            def __init__(self):
                self.messages = asyncio.Queue()

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def send(self, value):
                observed.setdefault("sent", []).append(value)
                await self.messages.put("voice:" + value if isinstance(value, str) else b"voice-audio")

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not hasattr(self, "yielded"):
                    self.yielded = True
                    return await self.messages.get()
                raise StopAsyncIteration

        fake_upstream = FakeUpstream()
        original = self.gateway.websockets.connect

        def fake_connect(url, **kwargs):
            observed["url"] = url
            observed["subprotocols"] = kwargs["subprotocols"]
            return fake_upstream

        self.gateway.websockets.connect = fake_connect
        try:
            protocols = ["ai-stack.voice.v1", "ai-stack.ticket.short-lived-ticket"]
            with self.client.websocket_connect(
                "/v1/realtime?session_id=session-123",
                subprotocols=protocols,
            ) as websocket:
                websocket.send_text("client-frame")
                self.assertEqual(websocket.receive_text(), "voice:client-frame")
        finally:
            self.gateway.websockets.connect = original

        self.assertEqual(observed["url"], "ws://voice-test:8000/ws/session-123")
        self.assertEqual(observed["subprotocols"], protocols)
        self.assertEqual(observed["sent"], ["client-frame"])


if __name__ == "__main__":
    unittest.main()
