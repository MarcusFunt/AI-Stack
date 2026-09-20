import tempfile
import unittest
from pathlib import Path

from agent_lab.worker.context import collect_repository_context


class RepositoryContextTests(unittest.TestCase):
    def test_hidden_generated_and_model_data_are_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "visible.py").write_text("VISIBLE = 1\n", encoding="utf-8")
            hidden = root / ".agent_lab_holdout"
            hidden.mkdir()
            (hidden / "oracle.py").write_text("SECRET = 42\n", encoding="utf-8")
            data = root / "data"
            data.mkdir()
            (data / "state.json").write_text('{"token":"secret"}', encoding="utf-8")
            modules = root / "node_modules"
            modules.mkdir()
            (modules / "dep.js").write_text("DO_NOT_SEND = true\n", encoding="utf-8")

            context = collect_repository_context(root)
            self.assertIn("VISIBLE = 1", context)
            self.assertNotIn("SECRET = 42", context)
            self.assertNotIn('"token":"secret"', context)
            self.assertNotIn("DO_NOT_SEND", context)
    def test_context_respects_byte_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("a = '" + ("x" * 4000) + "'\n", encoding="utf-8")
            (root / "b.py").write_text("b = '" + ("y" * 4000) + "'\n", encoding="utf-8")

            context = collect_repository_context(root, max_bytes=1500)
            self.assertLessEqual(len(context.encode("utf-8")), 1500)
            self.assertIn("===== a.py =====", context)
            self.assertNotIn("===== b.py =====", context)

    def test_binary_and_unsupported_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.py").write_text("OK = True\n", encoding="utf-8")
            (root / "bad.py").write_bytes(b"\xff\xfe\xfd")
            (root / "image.png").write_bytes(b"not-an-image")

            context = collect_repository_context(root)
            self.assertIn("OK = True", context)
            self.assertNotIn("bad.py", context)
            self.assertNotIn("image.png", context)
    def test_objective_prioritizes_relevant_file_before_large_earlier_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "aaa.md").write_text("A" * 5000, encoding="utf-8")
            target = root / "agent_lab" / "worker"
            target.mkdir(parents=True)
            (target / "model.py").write_text("TARGET = True\n", encoding="utf-8")

            context = collect_repository_context(
                root,
                max_bytes=1200,
                objective="Improve agent_lab/worker/model.py usage accounting",
            )
            target_marker = "===== agent_lab/worker/model.py ====="
            filler_marker = "===== aaa.md ====="
            self.assertIn(target_marker, context)
            self.assertIn("TARGET = True", context)
            self.assertIn(filler_marker, context)
            self.assertLess(context.index(target_marker), context.index(filler_marker))

    def test_objective_keeps_deterministic_fallback_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "b.py").write_text("B = 1\n", encoding="utf-8")
            (root / "a.py").write_text("A = 1\n", encoding="utf-8")

            context = collect_repository_context(root, objective="unrelated objective")
            self.assertLess(context.index("===== a.py ====="), context.index("===== b.py ====="))

    def test_context_include_and_exclude_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "agent_lab" / "worker"
            tests = root / "agent_lab" / "tests"
            evaluator = root / "agent_eval"
            worker.mkdir(parents=True)
            tests.mkdir(parents=True)
            evaluator.mkdir()
            (worker / "model.py").write_text("VISIBLE = 1\n", encoding="utf-8")
            (tests / "test_model.py").write_text("TEST = 1\n", encoding="utf-8")
            (evaluator / "secret.py").write_text("SECRET = 1\n", encoding="utf-8")

            context = collect_repository_context(
                root,
                include=["agent_lab"],
                exclude=["agent_lab/tests"],
            )
            self.assertIn("VISIBLE = 1", context)
            self.assertNotIn("TEST = 1", context)
            self.assertNotIn("SECRET = 1", context)


if __name__ == "__main__":
    unittest.main()
