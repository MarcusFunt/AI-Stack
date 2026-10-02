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
        original_host_agent = self.gateway.host_agent

        async def fake_post(client, url, **kwargs):
            observed["url"] = url
            observed["headers"] = kwargs["headers"]
            observed["payload"] = kwargs["json"]
            return self.gateway.httpx.Response(200, json={
                "id": "session-123",
                "client_secret": {"value": "one-use-ticket", "expires_at": 2000},
                "ws_url": "ws://voice:8000/ws/session-123",
            })

        async def fake_host_agent(method, path, payload=None, timeout=30):
            self.assertEqual((method, path, timeout), ("GET", "/tailscale/status", 3))
            return self._direct_route_status()

        self.gateway.httpx.AsyncClient.post = fake_post
        self.gateway.host_agent = fake_host_agent
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
            self.gateway.host_agent = original_host_agent

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

    def test_voice_smoke_is_an_authenticated_maintenance_action_with_idle_gpu_guard(self):
        observed = {}
        original_supervisor = self.gateway.supervisor
        original_host_agent = self.gateway.host_agent

        async def fake_supervisor(method, path, timeout=600):
            observed["supervisor"] = (method, path)
            return {"active_jobs": {"llm": 0, "tts": 0, "stt": 0}}

        async def fake_host_agent(method, path, payload=None, timeout=30):
            observed["operation"] = (method, path, payload)
            return {"id": "op-0123456789ab", "action": "voice-smoke", "state": "running"}

        self.gateway.supervisor = fake_supervisor
        self.gateway.host_agent = fake_host_agent
        try:
            response = self.client.post(
                "/control/maintenance/start",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                json={"action": "voice-smoke"},
            )
        finally:
            self.gateway.supervisor = original_supervisor
            self.gateway.host_agent = original_host_agent

        self.assertEqual(response.status_code, 200)
        self.assertEqual(observed["supervisor"], ("GET", "/status"))
        self.assertEqual(observed["operation"], ("POST", "/operations/start", {"action": "voice-smoke"}))

    def test_voice_smoke_maintenance_refuses_active_gpu_jobs(self):
        original_supervisor = self.gateway.supervisor
        original_host_agent = self.gateway.host_agent
        forwarded = []

        async def fake_supervisor(method, path, timeout=600):
            return {"active_jobs": {"tts": 1}}

        async def fake_host_agent(*args, **kwargs):
            forwarded.append(True)
            return {}

        self.gateway.supervisor = fake_supervisor
        self.gateway.host_agent = fake_host_agent
        try:
            response = self.client.post(
                "/control/maintenance/start",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                json={"action": "voice-smoke"},
            )
        finally:
            self.gateway.supervisor = original_supervisor
            self.gateway.host_agent = original_host_agent

        self.assertEqual(response.status_code, 409)
        self.assertIn("active AI jobs", response.json()["detail"])
        self.assertEqual(forwarded, [])

    def test_session_response_adds_same_origin_offer_url(self):
        original = self.gateway.httpx.AsyncClient.post
        original_host_agent = self.gateway.host_agent

        async def fake_post(client, url, **_kwargs):
            return self.gateway.httpx.Response(200, json={
                "id": "session-123",
                "client_secret": {"value": "one-use-ticket", "expires_at": 2000},
                "ws_url": "ws://voice:8000/ws/session-123",
                "offer_url": "http://voice:8000/sessions/session-123/offer",
            })

        async def fake_host_agent(method, path, payload=None, timeout=30):
            self.assertEqual((method, path, timeout), ("GET", "/tailscale/status", 3))
            return self._direct_route_status()

        self.gateway.httpx.AsyncClient.post = fake_post
        self.gateway.host_agent = fake_host_agent
        try:
            response = self.client.post(
                "/v1/realtime/sessions",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                json={"language": "en"},
            )
        finally:
            self.gateway.httpx.AsyncClient.post = original
            self.gateway.host_agent = original_host_agent

        result = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(result["offer_url"], "/v1/realtime/sessions/session-123/offer")
        self.assertNotIn("voice:8000", str(result))
        self.assertEqual(result["ice_route"], {"kind": "direct", "transport": None, "port": None})

    def _create_session_without_turn(self, network=None, host_agent_error=None):
        original_post = self.gateway.httpx.AsyncClient.post
        original_host_agent = self.gateway.host_agent

        async def fake_post(client, url, **_kwargs):
            return self.gateway.httpx.Response(200, json={
                "id": "session-direct-123",
                "client_secret": {"value": "short-lived-ticket", "expires_at": 2000},
                "ice_servers": [],
            })

        async def fake_host_agent(method, path, payload=None, timeout=30):
            self.assertEqual((method, path, timeout), ("GET", "/tailscale/status", 3))
            if host_agent_error:
                raise host_agent_error
            return network if network is not None else self._direct_route_status()

        self.gateway.httpx.AsyncClient.post = fake_post
        self.gateway.host_agent = fake_host_agent
        try:
            return self.client.post(
                "/v1/realtime/sessions",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                json={"language": "en"},
            )
        finally:
            self.gateway.httpx.AsyncClient.post = original_post
            self.gateway.host_agent = original_host_agent

    @staticmethod
    def _direct_route_status():
        return {
            "online": True,
            "route_state_available": True,
            "voice_turn_enabled": False,
        }

    def test_session_fails_clearly_when_host_agent_check_fails_without_turn(self):
        with self.assertLogs(self.gateway._LOGGER, level="WARNING") as route_logs:
            response = self._create_session_without_turn(
                host_agent_error=self.gateway.HTTPException(503, "host agent unavailable: private detail"),
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("host-agent check failed", response.json()["detail"].lower())
        self.assertNotIn("private detail", str(response.json()))
        log_output = "\n".join(route_logs.output)
        self.assertIn("session_id=session-direct-123", log_output)
        self.assertIn(f"request_id={response.headers['x-request-id']}", log_output)
        self.assertIn("reason=host-agent-unavailable", log_output)
        self.assertIn("host_agent_status=503", log_output)
        self.assertNotIn("private detail", log_output)

    def test_session_fails_clearly_when_turn_is_enabled_but_voice_has_no_turn_servers(self):
        response = self._create_session_without_turn(network=self._verified_turn_route())

        self.assertEqual(response.status_code, 503)
        self.assertIn("TURN is enabled", response.json()["detail"])
        self.assertIn("voice service returned no TURN relay", response.json()["detail"])

    def _create_turn_session(self, network=None, host_agent_error=None):
        original_post = self.gateway.httpx.AsyncClient.post
        original_host_agent = self.gateway.host_agent

        async def fake_post(client, url, **_kwargs):
            return self.gateway.httpx.Response(200, json={
                "id": "session-123",
                "client_secret": {"value": "short-lived-ticket", "expires_at": 2000},
                "ice_servers": [{
                    "urls": "turns:turn-check.invalid:8447?transport=tcp",
                    "username": "short-lived-username",
                    "credential": "short-lived-password",
                }],
            })

        async def fake_host_agent(method, path, payload=None, timeout=30):
            self.assertEqual((method, path, timeout), ("GET", "/tailscale/status", 3))
            if host_agent_error:
                raise host_agent_error
            return network

        self.gateway.httpx.AsyncClient.post = fake_post
        self.gateway.host_agent = fake_host_agent
        try:
            return self.client.post(
                "/v1/realtime/sessions",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                json={"language": "en"},
            )
        finally:
            self.gateway.httpx.AsyncClient.post = original_post
            self.gateway.host_agent = original_host_agent

    @staticmethod
    def _verified_turn_route(**overrides):
        status = {
            "online": True,
            "dns_name": "phone-call.tailnet.ts.net",
            "voice_turn_enabled": True,
            "voice_turn_route_present": True,
            "voice_turn_funnel_enabled": False,
            "voice_turn_listener_ready": True,
            "route_state_available": True,
        }
        status.update(overrides)
        return status

    def test_turn_session_fails_clearly_when_host_agent_check_fails(self):
        with self.assertLogs(self.gateway._LOGGER, level="WARNING") as route_logs:
            response = self._create_turn_session(
                host_agent_error=self.gateway.HTTPException(503, "host agent unavailable: private detail"),
            )

        self.assertEqual(response.status_code, 503)
        detail = response.json()["detail"]
        self.assertIn("TURN route could not be verified", detail)
        self.assertIn("retry", detail.lower())
        self.assertNotIn("coturn", str(response.json()).lower())
        self.assertNotIn("private detail", detail)
        self.assertNotIn("short-lived-password", str(response.json()))
        log_output = "\n".join(route_logs.output)
        self.assertIn("session_id=session-123", log_output)
        self.assertIn(f"request_id={response.headers['x-request-id']}", log_output)
        self.assertIn("reason=host-agent-unavailable", log_output)
        self.assertIn("host_agent_status=503", log_output)
        self.assertNotIn("private detail", log_output)
        self.assertNotIn("short-lived-password", log_output)
        self.assertNotIn("turn-check.invalid", log_output)

    def test_turn_session_fails_clearly_when_host_agent_reports_route_disabled(self):
        response = self._create_turn_session(
            network=self._verified_turn_route(voice_turn_enabled=False),
        )

        self.assertEqual(response.status_code, 503)
        self.assertIn("TURN route could not be verified", response.json()["detail"])

    def test_turn_session_fails_clearly_when_host_agent_hostname_is_invalid(self):
        response = self._create_turn_session(
            network=self._verified_turn_route(dns_name="bad host.example"),
        )

        self.assertEqual(response.status_code, 503)
        self.assertIn("TURN route could not be verified", response.json()["detail"])

    def test_turn_session_requires_listener_and_route_state_and_funnel_disabled(self):
        for overrides in (
            {"voice_turn_listener_ready": False},
            {"voice_turn_route_present": False},
            {"route_state_available": False},
            {"voice_turn_funnel_enabled": True},
        ):
            with self.subTest(overrides=overrides):
                response = self._create_turn_session(
                    network=self._verified_turn_route(**overrides),
                )

                self.assertEqual(response.status_code, 503)
                self.assertIn("TURN route could not be verified", response.json()["detail"])

    def test_session_turn_url_uses_the_authenticated_tailnet_hostname(self):
        original_post = self.gateway.httpx.AsyncClient.post
        original_host_agent = self.gateway.host_agent
        observed = {}

        async def fake_post(client, url, **_kwargs):
            return self.gateway.httpx.Response(200, json={
                "id": "session-123",
                "client_secret": {"value": "one-use-ticket", "expires_at": 2000},
                "ice_servers": [{
                    "urls": "turns:turn-check.invalid:8447?transport=tcp",
                    "username": "short-lived-username",
                    "credential": "short-lived-password",
                }],
            })

        async def fake_host_agent(method, path, payload=None, timeout=30):
            observed["request"] = (method, path)
            return self._verified_turn_route()

        self.gateway.httpx.AsyncClient.post = fake_post
        self.gateway.host_agent = fake_host_agent
        try:
            response = self.client.post(
                "/v1/realtime/sessions",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                json={"language": "en"},
            )
        finally:
            self.gateway.httpx.AsyncClient.post = original_post
            self.gateway.host_agent = original_host_agent

        self.assertEqual(response.status_code, 200)
        self.assertEqual(observed["request"], ("GET", "/tailscale/status"))
        self.assertEqual(
            response.json()["ice_servers"][0],
            {
                "urls": "turns:phone-call.tailnet.ts.net:8447?transport=tcp",
                "username": "short-lived-username",
                "credential": "short-lived-password",
            },
        )
        self.assertEqual(
            response.json()["ice_route"],
            {"kind": "tailnet-turn", "transport": "tls/tcp", "port": 8447},
        )

    def test_offer_proxy_authenticates_bounds_and_forwards_ticket_out_of_url(self):
        observed = {}
        original_post = self.gateway.httpx.AsyncClient.post

        async def fake_post(client, url, **kwargs):
            if kwargs["json"]["sdp"] == "oversized-answer":
                return self.gateway.httpx.Response(200, json={
                    "sdp": "x" * (128 * 1024),
                    "type": "answer",
                })
            observed["url"] = url
            observed["headers"] = kwargs["headers"]
            observed["json"] = kwargs["json"]
            return self.gateway.httpx.Response(200, json={
                "sdp": "v=0\r\nt=0 0\r\n",
                "type": "answer",
                "pc_id": "private-peer-id",
            })

        self.gateway.httpx.AsyncClient.post = fake_post
        try:
            unauthenticated = self.client.post(
                "/v1/realtime/sessions/session-123/offer",
                headers={"X-Voice-Session-Ticket": "one-use-ticket"},
                json={"type": "offer", "sdp": "v=0"},
            )
            oversized = self.client.post(
                "/v1/realtime/sessions/session-123/offer",
                headers={
                    "Authorization": "Bearer test-only-gateway-key",
                    "X-Voice-Session-Ticket": "one-use-ticket",
                },
                json={"type": "offer", "sdp": "x" * (128 * 1024)},
            )
            response = self.client.post(
                "/v1/realtime/sessions/session-123/offer",
                headers={
                    "Authorization": "Bearer test-only-gateway-key",
                    "X-Voice-Session-Ticket": "one-use-ticket",
                },
                json={"type": "offer", "sdp": "v=0\r\nt=0 0\r\n"},
            )
            oversized_answer = self.client.post(
                "/v1/realtime/sessions/session-123/offer",
                headers={
                    "Authorization": "Bearer test-only-gateway-key",
                    "X-Voice-Session-Ticket": "one-use-ticket",
                },
                json={"type": "offer", "sdp": "oversized-answer"},
            )
        finally:
            self.gateway.httpx.AsyncClient.post = original_post

        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(oversized_answer.status_code, 502)
        self.assertEqual(response.json(), {"sdp": "v=0\r\nt=0 0\r\n", "type": "answer"})
        self.assertEqual(observed["url"], "http://voice-test:8000/sessions/session-123/offer")
        self.assertEqual(observed["headers"]["Authorization"], "Bearer test-only-gateway-key")
        self.assertEqual(observed["headers"]["X-Voice-Session-Ticket"], "one-use-ticket")
        self.assertNotIn("one-use-ticket", observed["url"])
        self.assertEqual(observed["json"], {"type": "offer", "sdp": "v=0\r\nt=0 0\r\n"})

    def test_gateway_metrics_include_voice_webrtc_metrics(self):
        original_get = self.gateway.httpx.AsyncClient.get
        original_supervisor = self.gateway.supervisor

        async def fake_get(client, url, **_kwargs):
            body = (
                'ai_stack_voice_webrtc_offers_total{outcome="succeeded"} 4\n'
                if url == "http://voice-test:8000/metrics"
                else ""
            )
            return self.gateway.httpx.Response(
                200,
                text=body,
                request=self.gateway.httpx.Request("GET", url),
            )

        async def fake_supervisor(_method, path, **_kwargs):
            if path == "/status":
                return {"active_jobs": {}, "gpu_owner": "idle", "service_states": {}}
            raise AssertionError(f"unexpected supervisor route: {path}")

        self.gateway.httpx.AsyncClient.get = fake_get
        self.gateway.supervisor = fake_supervisor
        try:
            response = self.client.get(
                "/metrics",
                headers={"Authorization": "Bearer test-only-gateway-key"},
            )
        finally:
            self.gateway.httpx.AsyncClient.get = original_get
            self.gateway.supervisor = original_supervisor

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'ai_stack_voice_webrtc_offers_total{outcome="succeeded"} 4',
            response.text,
        )

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
