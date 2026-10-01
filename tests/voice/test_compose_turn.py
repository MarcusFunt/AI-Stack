from __future__ import annotations

import unittest
from pathlib import Path


class ComposeTurnServiceTests(unittest.TestCase):
    def test_missing_optional_turn_configuration_exits_cleanly_without_starting_coturn(self):
        compose_path = Path(__file__).resolve().parents[2] / "compose.yaml"
        compose = compose_path.read_text(encoding="utf-8")
        service = compose.split("  coturn:\n", 1)[1].split("\n  eval-router:\n", 1)[0]
        command_marker = "    command:\n      - |\n"
        self.assertIn(command_marker, service)
        command = service.split(command_marker, 1)[1].split("\n    environment:", 1)[0]

        self.assertIn('if [ -z "$${VOICE_TURN_SHARED_SECRET}" ]', command)
        self.assertIn('|| [ -z "$${VOICE_TURN_HOSTNAME}" ]', command)
        self.assertIn("exit 0", command)
        self.assertLess(command.index("exit 0"), command.index("exec turnserver"))
        self.assertIn('      - "127.0.0.1:3478:3478/tcp"', service)
        ports = service.split("    ports:\n", 1)[1].split("\n    networks:", 1)[0]
        self.assertNotIn("49160", ports)
        self.assertNotIn("/udp", ports)


if __name__ == "__main__":
    unittest.main()
