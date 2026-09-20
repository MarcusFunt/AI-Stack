import tempfile
import unittest
from pathlib import Path

from agent_lab.harnesses.registry import HarnessRegistry
from agent_lab.schemas import TaskSpec


class HarnessRegistryTests(unittest.TestCase):
    def test_python_syntax_selected_and_detects_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "broken.py"
            target.write_text("def greet(name)\n    return name\n", encoding="utf-8")
            task = TaskSpec(
                task_type="python-syntax",
                objective="Fix the syntax error.",
            )
            registry = HarnessRegistry()
            harness = registry.select(task, root)
            self.assertEqual(harness.manifest.id, "python-syntax")
            self.assertEqual(harness.execute(task, root).status, "failed")

            target.write_text("def greet(name):\n    return name\n", encoding="utf-8")
            self.assertEqual(harness.execute(task, root).status, "passed")


if __name__ == "__main__":
    unittest.main()
