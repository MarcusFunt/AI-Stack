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

    def test_rejects_ambiguous_replacement_and_duplicate_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "example.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
            with self.assertRaisesRegex(PatchError, "exactly once"):
                apply_exact_edits(
                    root,
                    [{"path": "example.py", "old": "x = 1", "new": "x = 2"}],
                )
            (root / "example.py").write_text("x = 1\n", encoding="utf-8")
            with self.assertRaisesRegex(PatchError, "same file"):
                apply_exact_edits(
                    root,
                    [
                        {"path": "example.py", "old": "x = 1", "new": "x = 2"},
                        {"path": "example.py", "old": "x = 1", "new": "x = 3"},
                    ],
                )

    def test_rejects_excess_operations_and_unsupported_new_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            edits = [
                {"op": "create", "path": f"f{i}.py", "content": "x = 1\n"}
                for i in range(6)
            ]
            with self.assertRaisesRegex(PatchError, "more than 5"):
                apply_exact_edits(root, edits)
            with self.assertRaisesRegex(PatchError, "unsupported new-file type"):
                apply_exact_edits(
                    root,
                    [{"op": "create", "path": "payload.exe", "content": "nope"}],
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

    def test_edit_scope_blocks_outside_and_excluded_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "agent_lab" / "worker"
            worker.mkdir(parents=True)
            (worker / "model.py").write_text("x = 1\n", encoding="utf-8")
            evaluator = root / "agent_eval"
            evaluator.mkdir()
            (evaluator / "controller.py").write_text("x = 1\n", encoding="utf-8")

            changed = apply_exact_edits(
                root,
                [{
                    "path": "agent_lab/worker/model.py",
                    "old": "x = 1",
                    "new": "x = 2",
                }],
                include=["agent_lab/worker"],
                exclude=["agent_eval"],
            )
            self.assertEqual(changed, ["agent_lab/worker/model.py"])
            with self.assertRaisesRegex(PatchError, "outside allowed edit scope"):
                apply_exact_edits(
                    root,
                    [{
                        "path": "agent_eval/controller.py",
                        "old": "x = 1",
                        "new": "x = 2",
                    }],
                    include=["agent_lab/worker"],
                    exclude=["agent_eval"],
                )


if __name__ == "__main__":
    unittest.main()
