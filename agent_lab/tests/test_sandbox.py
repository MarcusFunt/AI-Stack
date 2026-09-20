import os
import tempfile
import unittest
from pathlib import Path

import agent_lab.sandbox as sandbox


class SandboxTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("AGENT_LAB_SANDBOX_TEST") == "1",
        "run in the agent-lab-sandbox container",
    )
    def test_python_tests_run_without_controller_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            workspace = runs / ("e" * 32) / "workspace"
            workspace.mkdir(parents=True)
            for path in (root, runs, workspace.parent, workspace):
                path.chmod(0o755)

            (workspace / "test_environment.py").write_text(
                "import os\n"
                "import unittest\n\n"
                "class EnvironmentTests(unittest.TestCase):\n"
                "    def test_no_gateway_secret(self):\n"
                "        self.assertNotIn('AI_API_KEY', os.environ)\n",
                encoding="utf-8",
            )
            (workspace / "test_environment.py").chmod(0o644)

            previous_root = sandbox.RUNS_ROOT
            previous_secret = os.environ.get("AI_API_KEY")
            sandbox.RUNS_ROOT = runs.resolve()
            os.environ["AI_API_KEY"] = "must-not-reach-tests"
            try:
                result = sandbox.execute_python_unit("e" * 32, 30)
            finally:
                sandbox.RUNS_ROOT = previous_root
                if previous_secret is None:
                    os.environ.pop("AI_API_KEY", None)
                else:
                    os.environ["AI_API_KEY"] = previous_secret

            self.assertEqual(result["returncode"], 0, result["output"])
            self.assertIn("OK", result["output"])


if __name__ == "__main__":
    unittest.main()
