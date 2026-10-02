from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "compose.yaml"
TURN_IMAGE = "ai-stack-coturn:4.18.0"


class TurnRestartPolicyConfigTests(unittest.TestCase):
    def test_compose_config_selects_disabled_and_enabled_restart_policies(self):
        docker = shutil.which("docker")
        if docker is None:
            self.skipTest("Docker CLI is not installed")

        with tempfile.TemporaryDirectory(prefix="ai-stack-turn-compose-") as temp_dir:
            empty_env = Path(temp_dir) / "empty.env"
            empty_env.write_text("", encoding="utf-8")
            for policy in ("no", "unless-stopped"):
                env = os.environ.copy()
                for key in (
                    "TURN_RESTART_POLICY",
                    "TURN_PROXY_ENABLED",
                    "VOICE_TURN_SHARED_SECRET",
                    "VOICE_TURN_HOSTNAME",
                ):
                    env.pop(key, None)
                env["TURN_RESTART_POLICY"] = policy
                env["TURN_PROXY_ENABLED"] = "0" if policy == "no" else "1"

                result = subprocess.run(
                    [
                        docker,
                        "compose",
                        "--env-file",
                        str(empty_env),
                        "-f",
                        str(COMPOSE_PATH),
                        "config",
                        "--format",
                        "json",
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                    check=False,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, f"docker compose config failed for {policy}")
                services = json.loads(result.stdout)["services"]
                with self.subTest(policy=policy):
                    self.assertEqual(services["coturn"]["restart"], policy)
                    self.assertEqual(services["turn-proxy"]["restart"], policy)


@unittest.skipUnless(shutil.which("docker"), "Docker CLI is not installed")
class TurnRestartPolicyDockerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        info = subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=15)
        image = subprocess.run(
            ["docker", "image", "inspect", TURN_IMAGE], capture_output=True, check=False, timeout=15
        )
        if info.returncode != 0 or image.returncode != 0:
            raise unittest.SkipTest("Docker daemon and built TURN image are required for restart integration tests")

    @staticmethod
    def _disabled_coturn_command() -> str:
        compose = COMPOSE_PATH.read_text(encoding="utf-8")
        match = re.search(
            r"^  coturn:\n(?P<body>.*?)(?=^  [a-z0-9-]+:\n|^networks:\n)",
            compose,
            re.MULTILINE | re.DOTALL,
        )
        if match is None:
            raise AssertionError("coturn service is missing from compose.yaml")
        service = match.group("body")
        command_marker = "    command:\n      - |\n"
        if command_marker not in service:
            raise AssertionError("coturn startup command is missing")
        command = service.split(command_marker, 1)[1].split("\n    environment:", 1)[0]
        lines = command.splitlines()
        indent = min((len(line) - len(line.lstrip()) for line in lines if line.strip()), default=0)
        return "\n".join(line[indent:] for line in lines).replace("$$", "$")

    @staticmethod
    def _start(name: str, restart: str, command: str) -> None:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                name,
                "--restart",
                restart,
                "--env",
                "VOICE_TURN_SHARED_SECRET=",
                "--env",
                "VOICE_TURN_HOSTNAME=",
                "--entrypoint",
                "/bin/sh",
                TURN_IMAGE,
                "-ec",
                command,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        if result.returncode != 0:
            raise AssertionError(f"docker run failed for restart policy {restart}")

    @staticmethod
    def _inspect(name: str) -> tuple[str, int, int]:
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Status}}|{{.State.ExitCode}}|{{.RestartCount}}",
                name,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            raise AssertionError("docker inspect failed for TURN restart integration container")
        status, exit_code, restart_count = result.stdout.strip().split("|")
        return status, int(exit_code), int(restart_count)

    @staticmethod
    def _wait(name: str, predicate, timeout: float = 15.0) -> tuple[str, int, int]:
        deadline = time.monotonic() + timeout
        state = ("created", 0, 0)
        while time.monotonic() < deadline:
            state = TurnRestartPolicyDockerTests._inspect(name)
            if predicate(state):
                return state
            time.sleep(0.1)
        return state

    def _remove(self, name: str) -> None:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, check=False, timeout=15)

    def test_disabled_turn_exits_once_and_remains_stopped(self):
        name = "codex-turn-disabled-" + uuid.uuid4().hex[:10]
        try:
            self._start(name, "no", self._disabled_coturn_command())
            state = self._wait(name, lambda current: current[0] == "exited")
            self.assertEqual(state, ("exited", 0, 0))
        finally:
            self._remove(name)

    def test_enabled_runtime_failure_is_restarted_automatically(self):
        name = "codex-turn-enabled-" + uuid.uuid4().hex[:10]
        try:
            self._start(name, "unless-stopped", "sleep 10; exit 23")
            state = self._wait(name, lambda current: current[2] >= 1, timeout=25)
            self.assertGreaterEqual(state[2], 1, "enabled TURN runtime failure was not restarted")
        finally:
            self._remove(name)


if __name__ == "__main__":
    unittest.main()
