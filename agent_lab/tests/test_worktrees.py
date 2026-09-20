import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_lab.worktrees import WorktreeManager


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


class WorktreeTests(unittest.TestCase):
    def test_mirror_worktree_does_not_mutate_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            git(source, "init")
            git(source, "config", "user.name", "Test")
            git(source, "config", "user.email", "test@example.invalid")
            (source / "value.txt").write_text("one\n", encoding="utf-8")
            git(source, "add", ".")
            git(source, "commit", "-m", "initial")
            source_head = git(source, "rev-parse", "HEAD")

            manager = WorktreeManager(source, root / "agent-data")
            workspace, base = manager.create("a" * 32, "HEAD")
            self.assertEqual(base, source_head)
            (workspace / "value.txt").write_text("two\n", encoding="utf-8")
            candidate = manager.save_candidate(workspace, "a" * 32)

            self.assertNotEqual(candidate, source_head)
            self.assertEqual(git(source, "rev-parse", "HEAD"), source_head)
            self.assertEqual((source / "value.txt").read_text(), "one\n")

    def test_bare_source_resolves_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            git(source, "init")
            git(source, "config", "user.name", "Test")
            git(source, "config", "user.email", "test@example.invalid")
            (source / "value.txt").write_text("one\n", encoding="utf-8")
            git(source, "add", ".")
            git(source, "commit", "-m", "initial")
            expected = git(source, "rev-parse", "HEAD")
            bare = root / "source.git"
            subprocess.run(
                ["git", "clone", "--bare", str(source), str(bare)],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            manager = WorktreeManager(bare, root / "agent-data")
            workspace, commit = manager.create("c" * 32, "HEAD")
            self.assertEqual(commit, expected)
            self.assertEqual((workspace / "value.txt").read_text(), "one\n")


if __name__ == "__main__":
    unittest.main()
