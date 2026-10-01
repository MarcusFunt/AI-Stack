from __future__ import annotations

import json
import struct
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
    def __init__(self, *, dashboard=False, turn=False, funnel=False, unsupported=False, serve_status_unavailable=False):
        self.config = route_config(dashboard=dashboard, turn=turn, funnel=funnel)
        self.unsupported = unsupported
        self.serve_status_unavailable = serve_status_unavailable
        self.calls = []

    def run(self, args, **_kwargs):
        self.calls.append(list(args))
        if args[1:3] == ["status", "--json"]:
            return {"ok": True, "code": 0, "stdout": status_json(), "stderr": ""}
        if args[1:4] == ["serve", "status", "--json"]:
            if self.serve_status_unavailable:
                return {"ok": False, "code": 1, "stdout": "", "stderr": "Serve status unavailable"}
            return {"ok": True, "code": 0, "stdout": json.dumps(self.config), "stderr": ""}
        if args[1:3] == ["funnel", "--tls-terminated-tcp=8447"] and "off" in args:
            self.config["AllowFunnel"].pop(TURN_KEY, None)
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
    def test_status_fails_closed_when_tailscale_route_state_is_unknown(self):
        cli = FakeTailscale(turn=True, serve_status_unavailable=True)
        with (
            patch.object(host_agent, "tailscale_exe", return_value="tailscale.exe"),
            patch.object(host_agent, "run", side_effect=cli.run),
            patch.object(host_agent, "voice_turn_listener_ready", return_value=True),
        ):
            result = host_agent.tailscale_status()

        self.assertFalse(result["route_state_available"])
        self.assertFalse(result["voice_turn_enabled"])

    def test_status_fails_closed_when_route_target_is_not_exact_loopback(self):
        cli = FakeTailscale(turn=True)
        cli.config["TCP"][TURN_KEY]["TCPForward"] = "0.0.0.0:3478"
        with (
            patch.object(host_agent, "tailscale_exe", return_value="tailscale.exe"),
            patch.object(host_agent, "run", side_effect=cli.run),
            patch.object(host_agent, "voice_turn_listener_ready", return_value=True),
        ):
            result = host_agent.tailscale_status()

        self.assertTrue(result["voice_turn_route_present"])
        self.assertFalse(result["voice_turn_enabled"])

    def test_status_requires_a_valid_node_identity_before_route_state_is_available(self):
        def fake_run(args, **_kwargs):
            if args[1:3] == ["status", "--json"]:
                return {"ok": True, "code": 0, "stdout": "{}", "stderr": ""}
            if args[1:4] == ["serve", "status", "--json"]:
                return {"ok": True, "code": 0, "stdout": json.dumps(route_config()), "stderr": ""}
            return {"ok": True, "code": 0, "stdout": "Serve running", "stderr": ""}

        with (
            patch.object(host_agent, "tailscale_exe", return_value="tailscale.exe"),
            patch.object(host_agent, "run", side_effect=fake_run),
            patch.object(host_agent, "voice_turn_listener_ready", return_value=True),
        ):
            result = host_agent.tailscale_status()

        self.assertFalse(result["route_state_available"])

    def test_turn_enable_fails_closed_when_serve_state_cannot_be_read(self):
        cli = FakeTailscale(serve_status_unavailable=True)
        with (
            patch.object(host_agent, "tailscale_exe", return_value="tailscale.exe"),
            patch.object(host_agent, "run", side_effect=cli.run),
            patch.object(host_agent, "voice_turn_listener_ready", return_value=True),
        ):
            result = host_agent.configure_tailscale({
                "dashboard_enabled": False,
                "mcp_mode": "off",
                "voice_turn_enabled": True,
            })

        enable = ["tailscale.exe", "serve", "--tls-terminated-tcp=8447", "--bg", "--yes", "tcp://127.0.0.1:3478"]
        self.assertNotIn(enable, cli.calls)
        self.assertFalse(result["ok"])

    def test_turn_disable_does_not_report_success_when_serve_state_is_unknown(self):
        cli = FakeTailscale(funnel=True, serve_status_unavailable=True)
        with (
            patch.object(host_agent, "tailscale_exe", return_value="tailscale.exe"),
            patch.object(host_agent, "run", side_effect=cli.run),
            patch.object(host_agent, "voice_turn_listener_ready", return_value=True),
        ):
            result = host_agent.configure_tailscale({
                "dashboard_enabled": False,
                "mcp_mode": "off",
                "voice_turn_enabled": False,
            })

        self.assertIn(["tailscale.exe", "funnel", "--tls-terminated-tcp=8447", "off"], cli.calls)
        self.assertFalse(result["ok"])

    def test_listener_probe_requires_a_valid_stun_binding_response(self):
        request = b"\x00\x01\x00\x00\x21\x12\xa4\x42" + bytes(range(12))
        mapped_address = struct.pack("!HHBBH4s", 0x0020, 8, 0, 1, 3478, b"\x21\x12\xa4\x42")
        response = struct.pack("!HHI", 0x0101, len(mapped_address), 0x2112A442) + bytes(range(12)) + mapped_address

        class FakeSocket:
            def __init__(self):
                self.remaining = bytearray(response)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, _timeout):
                pass

            def sendall(self, payload):
                self.sent = payload

            def recv(self, size):
                if self.sent != request:
                    return b"bad response"
                part = self.remaining[:size]
                del self.remaining[:size]
                return bytes(part)

        with (
            patch.object(host_agent.socket, "create_connection", return_value=FakeSocket()),
            patch.object(host_agent.os, "urandom", return_value=bytes(range(12))),
        ):
            self.assertTrue(host_agent.voice_turn_listener_ready())

    def test_listener_probe_rejects_a_truncated_stun_body(self):
        request = b"\x00\x01\x00\x00\x21\x12\xa4\x42" + bytes(range(12))
        response = struct.pack("!HHI", 0x0101, 12, 0x2112A442) + bytes(range(12))

        class FakeSocket:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, _timeout):
                pass

            def sendall(self, _payload):
                self.remaining = bytearray(response)

            def recv(self, size):
                part = self.remaining[:size]
                del self.remaining[:size]
                return bytes(part)

        with (
            patch.object(host_agent.socket, "create_connection", return_value=FakeSocket()),
            patch.object(host_agent.os, "urandom", return_value=bytes(range(12))),
        ):
            self.assertFalse(host_agent.voice_turn_listener_ready())

    def test_listener_probe_uses_one_deadline_for_incremental_reads(self):
        response = (
            struct.pack("!HHI", 0x0101, 12, 0x2112A442)
            + bytes(range(12))
            + struct.pack("!HHBBH4s", 0x0020, 8, 0, 1, 3478, b"\x21\x12\xa4\x42")
        )

        class FakeSocket:
            def __init__(self):
                self.timeouts = []
                self.offset = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, timeout):
                self.timeouts.append(timeout)

            def sendall(self, _payload):
                pass

            def recv(self, _size):
                self.offset += 1
                now[0] += 0.2
                return response[self.offset - 1:self.offset]

        now = [0.0]
        fake_socket = FakeSocket()
        with (
            patch.object(host_agent.socket, "create_connection", return_value=fake_socket),
            patch.object(host_agent.os, "urandom", return_value=bytes(range(12))),
            patch.object(host_agent.time, "monotonic", side_effect=lambda: now[0]),
        ):
            self.assertFalse(host_agent.voice_turn_listener_ready(timeout=0.5))

        self.assertEqual(len(fake_socket.timeouts), 4)
        self.assertGreater(fake_socket.timeouts[1], fake_socket.timeouts[2])
        self.assertGreater(fake_socket.timeouts[2], fake_socket.timeouts[3])

    def test_listener_probe_rejects_a_non_stun_tcp_service(self):
        class FakeSocket:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, _timeout):
                pass

            def sendall(self, _payload):
                pass

            def recv(self, _size):
                return b"not coturn"

        with patch.object(host_agent.socket, "create_connection", return_value=FakeSocket()):
            self.assertFalse(host_agent.voice_turn_listener_ready())

    def test_listener_probe_fails_when_stun_stops_responding(self):
        class FakeSocket:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, _timeout):
                pass

            def sendall(self, _payload):
                pass

            def recv(self, _size):
                return b""

        with patch.object(host_agent.socket, "create_connection", return_value=FakeSocket()):
            self.assertFalse(host_agent.voice_turn_listener_ready())

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

    def test_voice_turn_enable_request_creates_private_route_when_stun_listener_is_ready(self):
        cli = FakeTailscale()
        with (
            patch.object(host_agent, "tailscale_exe", return_value="tailscale.exe"),
            patch.object(host_agent, "run", side_effect=cli.run),
            patch.object(host_agent, "voice_turn_listener_ready", return_value=True),
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
        self.assertTrue(result["ok"])
        self.assertTrue(result["status"]["voice_turn_enabled"])
        self.assertIn(TURN_KEY, cli.config["TCP"])

    def test_enabling_turn_removes_funnel_permission_before_adding_private_route(self):
        cli = FakeTailscale()
        cli.config["AllowFunnel"][TURN_KEY] = True
        with (
            patch.object(host_agent, "tailscale_exe", return_value="tailscale.exe"),
            patch.object(host_agent, "run", side_effect=cli.run),
            patch.object(host_agent, "voice_turn_listener_ready", return_value=True),
        ):
            result = host_agent.configure_tailscale({
                "dashboard_enabled": False,
                "mcp_mode": "off",
                "voice_turn_enabled": True,
            })

        funnel_off = ["tailscale.exe", "funnel", "--tls-terminated-tcp=8447", "off"]
        enable = ["tailscale.exe", "serve", "--tls-terminated-tcp=8447", "--bg", "--yes", "tcp://127.0.0.1:3478"]
        self.assertLess(cli.calls.index(funnel_off), cli.calls.index(enable))
        self.assertNotIn(TURN_KEY, cli.config["AllowFunnel"])
        self.assertTrue(result["status"]["voice_turn_enabled"])

    def test_voice_turn_enable_without_stun_listener_fails_closed_and_removes_existing_route(self):
        cli = FakeTailscale(turn=True)
        with (
            patch.object(host_agent, "tailscale_exe", return_value="tailscale.exe"),
            patch.object(host_agent, "run", side_effect=cli.run),
            patch.object(host_agent, "voice_turn_listener_ready", return_value=False),
        ):
            result = host_agent.configure_tailscale({
                "dashboard_enabled": False,
                "mcp_mode": "off",
                "voice_turn_enabled": True,
            })

        self.assertIn(["tailscale.exe", "serve", "--tls-terminated-tcp=8447", "off"], cli.calls)
        self.assertNotIn(
            ["tailscale.exe", "serve", "--tls-terminated-tcp=8447", "--bg", "--yes", "tcp://127.0.0.1:3478"],
            cli.calls,
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["status"]["voice_turn_enabled"])
        self.assertNotIn(TURN_KEY, cli.config["TCP"])
        self.assertTrue(any(step.get("code") == 409 for step in result["steps"]))

    def test_disabling_voice_turn_removes_only_the_turn_route(self):
        cli = FakeTailscale(dashboard=True, turn=True)
        cli.config["AllowFunnel"][TURN_KEY] = True
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
        self.assertIn(
            ["tailscale.exe", "funnel", "--tls-terminated-tcp=8447", "off"], cli.calls
        )
        self.assertTrue(result["status"]["dashboard_enabled"])
        self.assertFalse(result["status"]["voice_turn_enabled"])
        self.assertNotIn(TURN_KEY, cli.config["AllowFunnel"])

    def test_turn_route_fails_when_tls_terminated_tcp_is_unsupported(self):
        cli = FakeTailscale(unsupported=True)
        with (
            patch.object(host_agent, "tailscale_exe", return_value="tailscale.exe"),
            patch.object(host_agent, "run", side_effect=cli.run),
            patch.object(host_agent, "voice_turn_listener_ready", return_value=True),
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
            patch.object(host_agent, "voice_turn_listener_ready", return_value=True),
        ):
            result = host_agent.tailscale_status()

        self.assertFalse(result["voice_turn_enabled"])
        self.assertTrue(result["voice_turn_route_present"])
        self.assertTrue(result["voice_turn_funnel_enabled"])

    def test_status_reports_a_stale_route_even_when_the_listener_is_missing(self):
        cli = FakeTailscale(turn=True)
        with (
            patch.object(host_agent, "tailscale_exe", return_value="tailscale.exe"),
            patch.object(host_agent, "run", side_effect=cli.run),
            patch.object(host_agent, "voice_turn_listener_ready", return_value=False),
        ):
            result = host_agent.tailscale_status()

        self.assertFalse(result["voice_turn_enabled"])
        self.assertTrue(result["voice_turn_route_present"])


if __name__ == "__main__":
    unittest.main()
