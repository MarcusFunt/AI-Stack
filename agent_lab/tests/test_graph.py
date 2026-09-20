import tempfile
import unittest
from pathlib import Path

from agent_lab.harnesses import HarnessRegistry
from agent_lab.schemas import Budget, TaskSpec
from agent_lab.worker.graph import AgentCancelled, AgentRunner


class FakeModel:
    def __init__(self):
        self.calls = 0

    def propose_patch(self, objective, repo_context, prior_result):
        self.calls += 1
        return {
            "summary": "Replace subtraction with addition.",
            "edits": [
                {
                    "path": "calculator.py",
                    "old": "return a - b",
                    "new": "return a + b",
                }
            ],
        }


class RetryModel:
    def __init__(self):
        self.calls = 0
        self.feedback = []

    def propose_patch(self, objective, repo_context, prior_result):
        self.calls += 1
        self.feedback.append(prior_result)
        old = "not present" if self.calls == 1 else "return a - b"
        return {
            "summary": "retry exact patch",
            "edits": [
                {
                    "path": "calculator.py",
                    "old": old,
                    "new": "return a + b",
                }
            ],
        }


class GraphTests(unittest.TestCase):
    def test_agent_repairs_failing_python_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "calculator.py").write_text(
                "def add(a, b):\n    return a - b\n", encoding="utf-8"
            )
            (workspace / "test_calculator.py").write_text(
                "import unittest\n"
                "from calculator import add\n\n"
                "class Tests(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(add(2, 2), 4)\n",
                encoding="utf-8",
            )
            task = TaskSpec(
                task_type="python",
                objective="Fix add so the regression test passes.",
                budget=Budget(max_iterations=2),
            )
            model = FakeModel()
            runner = AgentRunner(
                task, workspace, HarnessRegistry(), model
            )
            result = runner.run("b" * 32)

            self.assertEqual(result["final_status"], "passed")
            self.assertEqual(result["iteration"], 1)
            self.assertEqual(model.calls, 1)
            self.assertIn("return a + b", (workspace / "calculator.py").read_text())

    def test_passing_baseline_is_not_false_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "calculator.py").write_text(
                "def add(a, b):\n    return a + b\n", encoding="utf-8"
            )
            (workspace / "test_calculator.py").write_text(
                "import unittest\nfrom calculator import add\n\n"
                "class Tests(unittest.TestCase):\n"
                "    def test_add(self): self.assertEqual(add(2, 2), 4)\n",
                encoding="utf-8",
            )
            model = FakeModel()
            task = TaskSpec(objective="Implement an unrelated feature.")
            result = AgentRunner(
                task, workspace, HarnessRegistry(), model
            ).run("d" * 32)

            self.assertEqual(result["final_status"], "failed")
            self.assertEqual(model.calls, 0)
            self.assertIn("baseline harness already passed", result["error"])
    def test_patch_rejection_is_feedback_and_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "calculator.py").write_text(
                "def add(a, b):\n    return a - b\n", encoding="utf-8"
            )
            (workspace / "test_calculator.py").write_text(
                "import unittest\nfrom calculator import add\n\n"
                "class Tests(unittest.TestCase):\n"
                "    def test_add(self): self.assertEqual(add(2, 2), 4)\n",
                encoding="utf-8",
            )
            task = TaskSpec(
                task_type="python",
                objective="Fix add.",
                budget=Budget(max_iterations=2),
            )
            model = RetryModel()
            result = AgentRunner(
                task, workspace, HarnessRegistry(), model
            ).run("e" * 32)
            self.assertEqual(result["final_status"], "passed")
            self.assertEqual(result["iteration"], 2)
            self.assertEqual(model.calls, 2)
            self.assertEqual(
                model.feedback[1]["checks"][0]["id"],
                "patch-application",
            )

    def test_cancellation_stops_before_agent_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "calculator.py").write_text(
                "def add(a, b):\n    return a - b\n", encoding="utf-8"
            )
            model = FakeModel()
            task = TaskSpec(objective="Fix the function.")
            runner = AgentRunner(
                task,
                workspace,
                HarnessRegistry(),
                model,
                cancelled=lambda: True,
            )
            with self.assertRaises(AgentCancelled):
                runner.run("c" * 32)
            self.assertEqual(model.calls, 0)


if __name__ == "__main__":
    unittest.main()
