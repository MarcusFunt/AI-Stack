from __future__ import annotations

import re
import unittest
from pathlib import Path


class ComposeTurnServiceTests(unittest.TestCase):
    @staticmethod
    def _service(compose: str, name: str) -> str:
        match = re.search(
            rf"^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [a-z0-9-]+:\n|^networks:\n)",
            compose,
            re.MULTILINE | re.DOTALL,
        )
        if match is None:
            raise AssertionError(f"Compose service {name!r} is missing")
        return match.group("body")

    def test_missing_optional_turn_configuration_exits_cleanly_without_starting_coturn(self):
        compose_path = Path(__file__).resolve().parents[2] / "compose.yaml"
        compose = compose_path.read_text(encoding="utf-8")
        service = self._service(compose, "coturn")
        command_marker = "    command:\n      - |\n"
        self.assertIn(command_marker, service)
        command = service.split(command_marker, 1)[1].split("\n    environment:", 1)[0]

        self.assertIn('if [ -z "$${VOICE_TURN_SHARED_SECRET}" ]', command)
        self.assertIn('|| [ -z "$${VOICE_TURN_HOSTNAME}" ]', command)
        self.assertIn("exit 0", command)
        self.assertLess(command.index("exit 0"), command.index("exec turnserver"))
        self.assertIn("--no-udp", command)
        self.assertIn("--no-tcp-relay", command)
        self.assertIn("networks: [voice]", service)
        self.assertNotIn("    ports:\n", service)
        self.assertIn("voice:\n    name: ai-stack-voice-net\n    internal: true", compose)

    def test_coturn_has_no_published_ports(self):
        compose_path = Path(__file__).resolve().parents[2] / "compose.yaml"
        compose = compose_path.read_text(encoding="utf-8")
        service = self._service(compose, "coturn")

        self.assertNotIn("    ports:\n", service)

    def test_turn_proxy_publishes_only_loopback_tcp_and_uses_both_networks(self):
        compose_path = Path(__file__).resolve().parents[2] / "compose.yaml"
        compose = compose_path.read_text(encoding="utf-8")
        service = self._service(compose, "turn-proxy")

        ports = service.split("    ports:\n", 1)[1].split("\n    networks:", 1)[0]
        published_ports = [
            line.strip()[2:].strip().strip('"')
            for line in ports.splitlines()
            if line.lstrip().startswith("-")
        ]
        self.assertEqual(published_ports, ["127.0.0.1:3478:3478/tcp"])
        self.assertIn("networks: [turn-publish, voice]", service)
        self.assertIn("turn-publish:\n    name: ai-stack-turn-publish-net", compose)

    def test_turn_proxy_receives_no_shared_secret(self):
        compose_path = Path(__file__).resolve().parents[2] / "compose.yaml"
        compose = compose_path.read_text(encoding="utf-8")
        service = self._service(compose, "turn-proxy")

        self.assertIn("TURN_PROXY_ENABLED", service)
        self.assertNotIn("VOICE_TURN_SHARED_SECRET", service)
        self.assertNotIn("VOICE_TURN_HOSTNAME", service)

    def test_coturn_command_omits_removed_flags(self):
        compose_path = Path(__file__).resolve().parents[2] / "compose.yaml"
        compose = compose_path.read_text(encoding="utf-8")
        service = self._service(compose, "coturn")
        command_marker = "    command:\n      - |\n"
        command = service.split(command_marker, 1)[1].split("\n    environment:", 1)[0]

        self.assertIn("--no-tls", command)
        self.assertNotIn("--no-cli", command)
        self.assertNotIn("--no-dtls", command)

    def test_ai_ps1_starts_proxy_only_with_complete_turn_environment_and_stops_pair(self):
        script_path = Path(__file__).resolve().parents[2] / "scripts" / "ai.ps1"
        script = script_path.read_text(encoding="utf-8")

        self.assertIn("$env:VOICE_TURN_SHARED_SECRET -and $env:VOICE_TURN_HOSTNAME", script)
        self.assertIn('"up","-d","coturn","turn-proxy"', script)
        self.assertIn('"stop","coturn","turn-proxy"', script)
        self.assertIn('SetEnvironmentVariable("TURN_PROXY_ENABLED", $null, "Process")', script)

    def test_coturn_keeps_all_capabilities_dropped_except_bind_service(self):
        compose_path = Path(__file__).resolve().parents[2] / "compose.yaml"
        compose = compose_path.read_text(encoding="utf-8")
        service = self._service(compose, "coturn")

        self.assertIn("    security_opt:\n      - no-new-privileges:true", service)
        self.assertIn("    cap_drop:\n      - ALL", service)
        self.assertIn("    cap_add:\n      - NET_BIND_SERVICE", service)


if __name__ == "__main__":
    unittest.main()
