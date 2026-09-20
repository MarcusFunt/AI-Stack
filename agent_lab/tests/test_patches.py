import tempfile
import unittest
from pathlib import Path

from agent_lab.worker.patches import PatchError, apply_exact_edits


class PatchTests(unittest.TestCase):
    def test_exact_edit_is_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "example.py"
            target.write_text("value = 1\n", encoding="utf-8")
            changed = apply_exact_edits(
                root,
                [{"path": "example.py", "old": "value = 1", "new": "value = 2"}],
            )
            self.assertEqual(changed, ["example.py"])
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")

    def test_escape_and_secret_paths_are_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("SECRET=x\n", encoding="utf-8")
            with self.assertRaises(PatchError):
                apply_exact_edits(
                    root,
                    [{"path": ".env", "old": "SECRET=x", "new": "SECRET=y"}],
                )
            with self.assertRaises(PatchError):
                apply_exact_edits(
                    root,
                    [{"path": "../outside.py", "old": "x", "new": "y"}],
                )


if __name__ == "__main__":
    unittest.main()
