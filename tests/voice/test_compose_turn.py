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
        self.assertLess(command.index("exit 0"), command.index("exec python3 /usr/local/bin/coturn_supervisor.py -- turnserver"))
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

    def test_coturn_and_proxy_have_protocol_healthchecks_and_restart_recovery(self):
        compose_path = Path(__file__).resolve().parents[2] / "compose.yaml"
        compose = compose_path.read_text(encoding="utf-8")
        coturn = self._service(compose, "coturn")
        service = self._service(compose, "turn-proxy")

        self.assertIn("restart: ${TURN_RESTART_POLICY:-no}", coturn)
        self.assertIn("stop_grace_period: 20s", coturn)
        self.assertIn("healthcheck:\n      test: [\"CMD\",\"python3\",\"/usr/local/bin/turn_proxy_health.py\",\"--healthcheck\"]", coturn)
        self.assertIn("coturn_supervisor.py -- turnserver", coturn)
        self.assertIn("restart: ${TURN_RESTART_POLICY:-no}", service)
        self.assertIn("condition: service_healthy\n        restart: true", service)
        self.assertIn("healthcheck:\n      test: [\"CMD\",\"python\",\"/app/turn_proxy/proxy.py\",\"--healthcheck\"]", service)
        proxy_path = Path(__file__).resolve().parents[2] / "voice" / "turn_proxy" / "proxy.py"
        proxy_source = proxy_path.read_text(encoding="utf-8")
        self.assertIn("_DNS_RESOLUTION_ATTEMPTS = 8", proxy_source)
        self.assertIn("time.sleep(_DNS_RETRY_INTERVAL_SECONDS)", proxy_source)
        self.assertIn("_watch_upstream", proxy_source)
        self.assertIn("stun_binding_ready", proxy_source)

    def test_ai_ps1_disables_restart_loop_without_complete_turn_configuration(self):
        script_path = Path(__file__).resolve().parents[2] / "scripts" / "ai.ps1"
        script = script_path.read_text(encoding="utf-8")

        self.assertIn('$turnRestartPolicy = "no"', script)
        self.assertIn('$turnRestartPolicy = "unless-stopped"', script)
        enabled_branch = script.split(
            'if ($env:VOICE_TURN_SHARED_SECRET -and $env:VOICE_TURN_HOSTNAME) {', 1
        )[1].split("}", 1)[0]
        self.assertIn('$turnProxyEnabled = "1"', enabled_branch)
        self.assertIn('$turnRestartPolicy = "unless-stopped"', enabled_branch)
        self.assertIn('SetEnvironmentVariable("TURN_RESTART_POLICY", $turnRestartPolicy, "Process")', script)
        self.assertIn('SetEnvironmentVariable("TURN_RESTART_POLICY", $previousTurnRestartPolicy, "Process")', script)

    def test_coturn_healthcheck_image_uses_pinned_coturn_and_local_stun_probe(self):
        root = Path(__file__).resolve().parents[2]
        compose = (root / "compose.yaml").read_text(encoding="utf-8")
        service = self._service(compose, "coturn")
        dockerfile = (root / "voice" / "turn_proxy" / "Dockerfile.coturn").read_text(encoding="utf-8")

        self.assertIn("dockerfile: Dockerfile.coturn", service)
        self.assertIn("FROM coturn/coturn:4.18.0", dockerfile)
        self.assertIn("apt-get install -y --no-install-recommends python3", dockerfile)
        self.assertIn("COPY proxy.py /usr/local/bin/turn_proxy_health.py", dockerfile)
        self.assertIn("COPY coturn_supervisor.py /usr/local/bin/coturn_supervisor.py", dockerfile)

    def test_turn_proxy_receives_no_shared_secret(self):
        compose_path = Path(__file__).resolve().parents[2] / "compose.yaml"
        compose = compose_path.read_text(encoding="utf-8")
        service = self._service(compose, "turn-proxy")

        self.assertIn("TURN_PROXY_ENABLED", service)
        self.assertNotIn("VOICE_TURN_SHARED_SECRET", service)
        self.assertNotIn("VOICE_TURN_HOSTNAME", service)

    def test_topology_keeps_coturn_private_and_proxy_as_only_dual_network_component(self):
        compose_path = Path(__file__).resolve().parents[2] / "compose.yaml"
        compose = compose_path.read_text(encoding="utf-8")
        coturn = self._service(compose, "coturn")
        proxy = self._service(compose, "turn-proxy")

        self.assertIn("networks: [voice]", coturn)
        self.assertIn("networks: [turn-publish, voice]", proxy)
        self.assertIn("voice:\n    name: ai-stack-voice-net\n    internal: true", compose)
        self.assertEqual(compose.count("networks: [turn-publish, voice]"), 1)
        self.assertNotIn("VOICE_TURN_SHARED_SECRET", proxy)
        self.assertNotIn("VOICE_TURN_HOSTNAME", proxy)
        self.assertIn('"127.0.0.1:3478:3478/tcp"', proxy)
        self.assertNotIn("3478/udp", compose)
        self.assertNotIn("49160", compose.split("networks:", 1)[0])

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

        self.assertIn('if ($Service -ne "coturn") { Start-ControlPlane }', script)
        self.assertIn("$env:VOICE_TURN_SHARED_SECRET -and $env:VOICE_TURN_HOSTNAME", script)
        self.assertIn('"up","--build","-d","coturn","turn-proxy"', script)
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
