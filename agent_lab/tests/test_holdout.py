import tempfile
import unittest
from pathlib import Path

from agent_lab.harnesses.base import Harness, HarnessManifest
from agent_lab.harnesses.registry import HarnessRegistry
from agent_lab.schemas import HarnessCheck, HarnessResult, TaskSpec
from agent_lab.worker.context import collect_repository_context
from agent_lab.worker.graph import AgentRunner


class HoldoutFailHarness(Harness):
    manifest = HarnessManifest(
        id="holdout-fail",
        version="0.1",
        task_types=("python",),
        description="test harness",
    )

    def applicability(self, task, workspace):
        return 1.0

    def execute(self, task, workspace):
        source = (workspace / "value.py").read_text(encoding="utf-8")
        passed = "return 2" in source
        return HarnessResult(
            harness_id=self.manifest.id,
            harness_version=self.manifest.version,
            status="passed" if passed else "failed",
            checks=[
                HarnessCheck(
                    id="visible",
                    status="passed" if passed else "failed",
                )
            ],
        )

    def execute_holdout(self, task, workspace):
        return HarnessResult(
            harness_id=self.manifest.id,
            harness_version=self.manifest.version,
            status="failed",
            checks=[HarnessCheck(id="hidden", status="failed")],
        )


class RepairModel:
    def propose_patch(self, objective, repo_context, prior_result):
        return {
            "summary": "repair visible behavior",
            "edits": [
                {
                    "path": "value.py",
                    "old": "return 1",
                    "new": "return 2",
                }
            ],
        }


class HoldoutTests(unittest.TestCase):
    def test_hidden_files_are_excluded_from_model_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "visible.py").write_text("VISIBLE = True\n", encoding="utf-8")
            hidden = root / ".agent_lab_holdout"
            hidden.mkdir()
            (hidden / "secret.py").write_text(
                "SECRET_EXPECTATION = 42\n", encoding="utf-8"
            )
            context = collect_repository_context(root)
            self.assertIn("VISIBLE = True", context)
            self.assertNotIn("SECRET_EXPECTATION", context)
            self.assertNotIn(".agent_lab_holdout", context)

    def test_hidden_failure_blocks_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "value.py").write_text(
                "def value():\n    return 1\n", encoding="utf-8"
            )
            task = TaskSpec(
                task_type="python",
                objective="Return 2.",
                required_harnesses=["holdout-fail"],
            )
            runner = AgentRunner(
                task,
                root,
                HarnessRegistry(harnesses=[HoldoutFailHarness()]),
                RepairModel(),
            )
            result = runner.run("f" * 32)
            self.assertEqual(result["harness_result"]["status"], "passed")
            self.assertEqual(result["holdout_result"]["status"], "failed")
            self.assertEqual(result["final_status"], "failed")
            self.assertIn("hidden holdout", result["error"])


if __name__ == "__main__":
    unittest.main()
