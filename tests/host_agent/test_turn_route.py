from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from scripts import host_agent


TAILNET_NAME = "voice-host.example.ts.net"
TURN_KEY = f"{TAILNET_NAME}:8447"


def status_json():
    return json.dumps({
        "Self": {
            "DNSName": TAILNET_NAME + ".",
            "Online": True,
            "TailscaleIPs": ["100.64.0.10"],
        }
    })


def route_config(*, dashboard=False, turn=False, funnel=False):
    tcp = {}
    if turn:
        tcp[TURN_KEY] = {
            "TCPForward": "127.0.0.1:3478",
            "TerminateTLS": TAILNET_NAME,
        }
    web = {}
    if dashboard:
        web[f"{TAILNET_NAME}:8443"] = {
            "Handlers": {"/": {"Proxy": "127.0.0.1:3000"}}
        }
    allow_funnel = {TURN_KEY: True} if funnel else {}
    return {"Web": web, "TCP": tcp, "AllowFunnel": allow_funnel}


class FakeTailscale:
    def __init__(self, *, dashboard=False, turn=False, unsupported=False):
        self.config = route_config(dashboard=dashboard, turn=turn)
        self.unsupported = unsupported
        self.calls = []

    def run(self, args, **_kwargs):
        self.calls.append(list(args))
        if args[1:3] == ["status", "--json"]:
            return {"ok": True, "code": 0, "stdout": status_json(), "stderr": ""}
        if args[1:4] == ["serve", "status", "--json"]:
            return {"ok": True, "code": 0, "stdout": json.dumps(self.config), "stderr": ""}
        if args[1:3] == ["serve", "status"]:
            return {"ok": True, "code": 0, "stdout": "Serve running", "stderr": ""}
        if args[1] == "serve" and "--tls-terminated-tcp=8447" in args:
            if self.unsupported:
                return {"ok": False, "code": 2, "stdout": "", "stderr": "unknown flag"}
            if "off" in args:
                self.config["TCP"].pop(TURN_KEY, None)
            else:
                self.config["TCP"][TURN_KEY] = {
                    "TCPForward": "127.0.0.1:3478",
                    "TerminateTLS": TAILNET_NAME,
                }
        if args[1] == "serve" and "--https=8443" in args:
            if "off" in args:
                self.config["Web"].pop(f"{TAILNET_NAME}:8443", None)
            else:
                self.config["Web"][f"{TAILNET_NAME}:8443"] = {
                    "Handlers": {"/": {"Proxy": "127.0.0.1:3000"}}
                }
        return {"ok": True, "code": 0, "stdout": "", "stderr": ""}


class VoiceTurnRouteTests(unittest.TestCase):
    def test_existing_https_8446_route_is_preserved_and_not_detected_as_voice_turn(self):
        cli = FakeTailscale()
        existing_key = f"{TAILNET_NAME}:8446"
        existing_route = {"Handlers": {"/": {"Proxy": "127.0.0.1:8087"}}}
        cli.config["Web"][existing_key] = existing_route

        with (
            patch.object(host_agent, "tailscale_exe", return_value="tailscale.exe"),
            patch.object(host_agent, "run", side_effect=cli.run),
        ):
            status = host_agent.tailscale_status()
            host_agent.configure_tailscale({
                "dashboard_enabled": False,
                "mcp_mode": "off",
                "voice_turn_enabled": False,
            })

        self.assertFalse(status["voice_turn_enabled"])
        self.assertEqual(cli.config["Web"][existing_key], existing_route)

    def test_voice_turn_route_uses_tls_terminated_tcp_and_never_enables_funnel(self):
        cli = FakeTailscale()
        with (
            patch.object(host_agent, "tailscale_exe", return_value="tailscale.exe"),
            patch.object(host_agent, "run", side_effect=cli.run),
            patch.object(host_agent, "voice_turn_listener_ready", return_value=True, create=True),
        ):
            result = host_agent.configure_tailscale({
                "dashboard_enabled": False,
                "mcp_mode": "off",
                "voice_turn_enabled": True,
            })

        self.assertIn(
            ["tailscale.exe", "serve", "--tls-terminated-tcp=8447", "--bg", "--yes", "tcp://127.0.0.1:3478"],
            cli.calls,
        )
        self.assertFalse(any(call[1] == "funnel" and "--bg" in call for call in cli.calls))
        self.assertTrue(result["status"]["voice_turn_enabled"])

    def test_disabling_voice_turn_removes_only_the_turn_route(self):
        cli = FakeTailscale(dashboard=True, turn=True)
        with (
            patch.object(host_agent, "tailscale_exe", return_value="tailscale.exe"),
            patch.object(host_agent, "run", side_effect=cli.run),
        ):
            result = host_agent.configure_tailscale({
                "dashboard_enabled": True,
                "mcp_mode": "off",
                "voice_turn_enabled": False,
            })

        self.assertIn(
            ["tailscale.exe", "serve", "--tls-terminated-tcp=8447", "off"], cli.calls
        )
        self.assertTrue(result["status"]["dashboard_enabled"])
        self.assertFalse(result["status"]["voice_turn_enabled"])

    def test_turn_route_stays_disabled_when_tls_terminated_tcp_is_unsupported(self):
        cli = FakeTailscale(unsupported=True)
        with (
            patch.object(host_agent, "tailscale_exe", return_value="tailscale.exe"),
            patch.object(host_agent, "run", side_effect=cli.run),
            patch.object(host_agent, "voice_turn_listener_ready", return_value=True, create=True),
        ):
            result = host_agent.configure_tailscale({
                "dashboard_enabled": False,
                "mcp_mode": "off",
                "voice_turn_enabled": True,
            })

        self.assertFalse(result["ok"])
        self.assertFalse(result["status"]["voice_turn_enabled"])
        self.assertTrue(any("--tls-terminated-tcp=8447" in call for call in cli.calls))
        self.assertFalse(any(call[1] == "funnel" and "--bg" in call for call in cli.calls))

    def test_turn_status_requires_exact_loopback_tls_target_and_no_funnel(self):
        cli = FakeTailscale()
        cli.config = route_config(turn=True, funnel=True)
        with (
            patch.object(host_agent, "tailscale_exe", return_value="tailscale.exe"),
            patch.object(host_agent, "run", side_effect=cli.run),
        ):
            result = host_agent.tailscale_status()

        self.assertFalse(result["voice_turn_enabled"])


if __name__ == "__main__":
    unittest.main()
