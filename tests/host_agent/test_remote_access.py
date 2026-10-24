from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from scripts import host_agent


def safe_status():
    return {
        "observed_at_utc": "2026-10-24T12:00:00Z",
        "tailnet": {"state": "connected", "unattended": "enabled", "last_state_change_utc": "2026-10-24T11:00:00Z"},
        "ssh": {"service": "running", "firewall": "tailnet-only", "last_listener_check_utc": "2026-10-24T11:59:00Z"},
        "desktop": {
            "task": "running", "session": "interactive", "rdc": "ready",
            "denied_by_category": {"authorization": 2, "view_only": 1, "other": 0, "private": "operator@example.invalid"},
            "screenshot": "private screenshot", "clipboard": "private clipboard",
        },
        "human_fallback": "unknown",
        "host": {"uptime_seconds": 3600, "last_boot_utc": "2026-10-24T11:00:00Z"},
        "last_audit": {"timestamp": "2026-10-24T11:59:30Z", "category": "view_only", "raw": "private audit line"},
        "identity": "operator@example.invalid",
        "address": "100.64.12.34",
    }


class RemoteAccessStatusHandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), host_agent.Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, token=None):
        headers = {"X-Host-Agent-Token": token} if token else {}
        request = urllib.request.Request(self.base_url + "/remote-access/status", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def setUp(self):
        with host_agent.REMOTE_ACCESS_STATUS_CACHE_LOCK:
            host_agent.REMOTE_ACCESS_STATUS_CACHE.update({"expires_at": 0.0, "value": None})

    def test_missing_or_wrong_token_is_rejected_before_probe(self):
        with patch.object(host_agent, "token", return_value="test-host-token"), patch.object(host_agent, "run") as run:
            self.assertEqual(self.request()[0], 401)
            self.assertEqual(self.request("wrong-token")[0], 401)
        run.assert_not_called()

    def test_authenticated_status_is_bounded_and_sanitized(self):
        def fake_run(args, *, timeout, max_output):
            self.assertLessEqual(timeout, 15)
            self.assertLessEqual(max_output, 8192)
            self.assertEqual(args[args.index("-File") + 1], str(host_agent.ROOT / "scripts" / "remote" / "Test-RemoteReadiness.ps1"))
            self.assertIn("-Json", args)
            return {"ok": True, "code": 0, "stdout": json.dumps({"status": safe_status()}), "stderr": "private stderr"}

        with patch.object(host_agent, "token", return_value="test-host-token"), patch.object(host_agent, "run", side_effect=fake_run) as run:
            code, payload = self.request("test-host-token")
            second_code, second_payload = self.request("test-host-token")

        self.assertEqual(code, 200)
        self.assertEqual(second_code, 200)
        self.assertEqual(second_payload, payload)
        self.assertEqual(payload["tailnet"]["state"], "connected")
        self.assertEqual(payload["desktop"]["denied_by_category"], {"authorization": 2, "view_only": 1, "other": 0})
        self.assertEqual(payload["last_audit"]["category"], "view_only")
        self.assertEqual(set(payload), {"observed_at_utc", "tailnet", "ssh", "desktop", "human_fallback", "host", "last_audit"})
        serialized = json.dumps(payload)
        for private_value in ("operator@example.invalid", "100.64.12.34", "private screenshot", "private clipboard", "private audit line", "private stderr"):
            self.assertNotIn(private_value, serialized)
        run.assert_called_once()

    def test_timeout_nonzero_exit_and_malformed_json_are_unavailable_without_details(self):
        failures = (
            subprocess.TimeoutExpired(cmd=["powershell.exe"], timeout=12),
            {"ok": False, "code": 1, "stdout": "", "stderr": "private command output"},
            {"ok": True, "code": 0, "stdout": "not-json private output", "stderr": ""},
            {"ok": True, "code": 0, "stdout": json.dumps({"status": []}), "stderr": ""},
        )
        for failure in failures:
            with self.subTest(failure=str(type(failure))):
                side_effect = failure if isinstance(failure, BaseException) else None
                with patch.object(host_agent, "token", return_value="test-host-token"), patch.object(host_agent, "run", side_effect=side_effect, return_value=failure):
                    code, payload = self.request("test-host-token")
                self.assertEqual(code, 503)
                self.assertEqual(payload, {"detail": "remote access status unavailable"})


if __name__ == "__main__":
    unittest.main()
