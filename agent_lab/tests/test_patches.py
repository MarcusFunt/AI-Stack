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

    def test_create_operation_writes_new_source_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            changed = apply_exact_edits(
                root,
                [
                    {
                        "op": "create",
                        "path": "pkg/helper.py",
                        "content": "def answer():\n    return 42\n",
                    }
                ],
            )
            self.assertEqual(changed, ["pkg/helper.py"])
            self.assertEqual(
                (root / "pkg" / "helper.py").read_text(encoding="utf-8"),
                "def answer():\n    return 42\n",
            )

    def test_validation_is_atomic_before_any_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.py"
            second = root / "second.py"
            first.write_text("value = 1\n", encoding="utf-8")
            second.write_text("value = 2\n", encoding="utf-8")
            with self.assertRaises(PatchError):
                apply_exact_edits(
                    root,
                    [
                        {
                            "op": "replace",
                            "path": "first.py",
                            "old": "value = 1",
                            "new": "value = 10",
                        },
                        {
                            "op": "replace",
                            "path": "second.py",
                            "old": "missing text",
                            "new": "value = 20",
                        },
                    ],
                )
            self.assertEqual(first.read_text(encoding="utf-8"), "value = 1\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "value = 2\n")

    def test_tests_and_holdouts_are_protected_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test_feature.py").write_text("x = 1\n", encoding="utf-8")
            hidden = root / ".agent_lab_holdout"
            hidden.mkdir()
            (hidden / "oracle.py").write_text("x = 1\n", encoding="utf-8")
            for path in ("test_feature.py", ".agent_lab_holdout/oracle.py"):
                with self.assertRaises(PatchError):
                    apply_exact_edits(
                        root,
                        [
                            {
                                "op": "replace",
                                "path": path,
                                "old": "x = 1",
                                "new": "x = 2",
                            }
                        ],
                    )

    def test_test_edits_require_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "test_feature.py"
            target.write_text("x = 1\n", encoding="utf-8")
            changed = apply_exact_edits(
                root,
                [
                    {
                        "op": "replace",
                        "path": "test_feature.py",
                        "old": "x = 1",
                        "new": "x = 2",
                    }
                ],
                allow_test_edits=True,
            )
            self.assertEqual(changed, ["test_feature.py"])
            self.assertEqual(target.read_text(encoding="utf-8"), "x = 2\n")


if __name__ == "__main__":
    unittest.main()
