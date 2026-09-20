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


if __name__ == "__main__":
    unittest.main()
