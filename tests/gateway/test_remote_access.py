from __future__ import annotations

import asyncio
import json
import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


def safe_status():
    return {
        "observed_at_utc": "2026-10-24T12:00:00Z",
        "tailnet": {"state": "connected", "unattended": "enabled", "last_state_change_utc": "2026-10-24T11:00:00Z"},
        "ssh": {"service": "running", "firewall": "tailnet-only", "last_listener_check_utc": "2026-10-24T11:59:00Z"},
        "desktop": {"task": "running", "session": "interactive", "rdc": "ready", "denied_by_category": {"authorization": 2, "view_only": 1, "other": 0}},
        "human_fallback": "unknown",
        "host": {"uptime_seconds": 3600, "last_boot_utc": "2026-10-24T11:00:00Z"},
        "last_audit": {"timestamp": "2026-10-24T11:59:30Z", "category": "view_only"},
    }


class GatewayRemoteAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = patch.dict(os.environ, {
            "AI_API_KEY": "test-only-gateway-key",
            "SUPERVISOR_TOKEN": "test-only-supervisor-token",
            "LLAMA_API_KEY": "test-only-llama-key",
            "HOST_AGENT_TOKEN": "test-only-host-agent-token",
            "CONFIG_PATH": str(Path("config/models.json").resolve()),
        })
        cls.environment.start()
        cls.gateway = importlib.import_module("gateway.app")
        cls.client = TestClient(cls.gateway.app)

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("gateway.app", None)
        cls.environment.stop()

    def test_route_uses_gateway_auth_and_existing_host_agent_token(self):
        observed = {}
        original_request = self.gateway.httpx.AsyncClient.request

        async def fake_request(client, method, url, **kwargs):
            observed["method"] = method
            observed["url"] = url
            observed["headers"] = kwargs.get("headers", {})
            return self.gateway.httpx.Response(200, json=safe_status())

        self.gateway.httpx.AsyncClient.request = fake_request
        try:
            denied = self.client.get("/control/remote-access")
            response = self.client.get("/control/remote-access", headers={"Authorization": "Bearer test-only-gateway-key"})
        finally:
            self.gateway.httpx.AsyncClient.request = original_request

        self.assertEqual(denied.status_code, 401)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(observed["method"], "GET")
        self.assertEqual(observed["url"], self.gateway.HOST_AGENT_URL + "/remote-access/status")
        self.assertEqual(observed["headers"]["X-Host-Agent-Token"], "test-only-host-agent-token")
        self.assertEqual(response.json(), safe_status())

    def test_host_agent_unavailable_maps_to_503(self):
        original_request = self.gateway.httpx.AsyncClient.request

        async def fake_request(client, method, url, **kwargs):
            return self.gateway.httpx.Response(503, json={"detail": "remote access status unavailable"})

        self.gateway.httpx.AsyncClient.request = fake_request
        try:
            response = self.client.get("/control/remote-access", headers={"Authorization": "Bearer test-only-gateway-key"})
        finally:
            self.gateway.httpx.AsyncClient.request = original_request

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)

    def test_mqtt_summary_contains_only_bounded_remote_status_fields(self):
        status = safe_status() | {"identity": "operator@example.invalid", "address": "100.64.12.34", "raw_audit": "private audit line"}
        summary = self.gateway.remote_access_mqtt_summary(status, checked_at="2026-10-24T12:01:00Z")
        serialized = json.dumps(summary)
        for private_value in ("operator@example.invalid", "100.64.12.34", "private audit line"):
            self.assertNotIn(private_value, serialized)
        self.assertEqual(summary["ssh_state"], "running")
        self.assertEqual(summary["human_fallback"], "unknown")
        self.assertEqual(summary["denied_by_category"], {"authorization": 2, "view_only": 1, "other": 0})
        self.assertEqual(set(summary), {
            "checked_at_utc", "observed_at_utc", "tailnet_state", "unattended_state", "ssh_state",
            "ssh_firewall_state", "desktop_task", "desktop_session", "desktop_rdc", "human_fallback",
            "denied_by_category", "uptime_seconds", "last_boot_utc", "last_audit",
        })

    def test_mqtt_publishes_sanitized_remote_status_and_unknown_when_missing(self):
        class FakeMqtt:
            def __init__(self):
                self.messages = []

            def publish(self, topic, payload, **kwargs):
                self.messages.append((topic, payload))

        original_client = self.gateway.mqtt_client
        original_connected = self.gateway.mqtt_connected
        original_discovery = self.gateway.publish_home_assistant
        fake = FakeMqtt()
        self.gateway.mqtt_client = fake
        self.gateway.mqtt_connected = True
        self.gateway.publish_home_assistant = lambda settings, root: None
        try:
            self.gateway.publish_mqtt_snapshot({}, {}, remote_access=safe_status())
            self.gateway.publish_mqtt_snapshot({}, {}, remote_access=None)
        finally:
            self.gateway.mqtt_client = original_client
            self.gateway.mqtt_connected = original_connected
            self.gateway.publish_home_assistant = original_discovery

        messages = [payload for topic, payload in fake.messages if topic == "localai/marcus-computer/remote-access/status"]
        self.assertEqual(len(messages), 2)
        self.assertEqual(json.loads(messages[0])["ssh_state"], "running")
        self.assertEqual(json.loads(messages[1])["ssh_state"], "unknown")

    def test_mqtt_status_probe_uses_short_cache_and_failure_stays_unknown(self):
        observed = {"calls": 0}
        original_host_agent = self.gateway.host_agent

        async def fake_host_agent(method, path, payload=None, timeout=30):
            observed["calls"] += 1
            self.assertEqual((method, path, timeout), ("GET", "/remote-access/status", 12))
            return safe_status()

        self.gateway.REMOTE_ACCESS_MQTT_CACHE.update({"expires_at": 0.0, "status": None, "loaded": False})
        self.gateway.host_agent = fake_host_agent
        try:
            first = asyncio.run(self.gateway.remote_access_status_for_mqtt())
            second = asyncio.run(self.gateway.remote_access_status_for_mqtt())
        finally:
            self.gateway.host_agent = original_host_agent
            self.gateway.REMOTE_ACCESS_MQTT_CACHE.update({"expires_at": 0.0, "status": None, "loaded": False})

        self.assertEqual(first, safe_status())
        self.assertEqual(second, first)
        self.assertEqual(observed["calls"], 1)


if __name__ == "__main__":
    unittest.main()
