import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agent_lab.promotion import review_candidate
from agent_lab.schemas import RunRecord, RunStatus, TaskSpec
from agent_lab.worktrees import WorktreeManager


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.stdout.strip()


def make_source(root: Path, *, self_mod: bool = False) -> tuple[Path, str]:
    source = root / "source"
    source.mkdir()
    git(source, "init")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.invalid")
    target = source / (
        "agent_lab/worker/example.py" if self_mod else "feature.py"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("VALUE = 1\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "initial")
    return source, git(source, "rev-parse", "HEAD")


def passed_run(run_id, workspace, base, candidate):
    now = datetime.now(timezone.utc)
    return RunRecord(
        id=run_id,
        status=RunStatus.PASSED,
        task=TaskSpec(objective="Improve feature"),
        created_at=now,
        updated_at=now,
        workspace=str(workspace),
        base_commit=base,
        selected_harness="python-unit",
        result={
            "candidate_commit": candidate,
            "harness_result": {"status": "passed"},
            "holdout_result": {"status": "passed"},
        },
    )


class PromotionReviewTests(unittest.TestCase):
    def test_small_valid_candidate_is_eligible_for_manual_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, base = make_source(root)
            manager = WorktreeManager(source, root / "data")
            run_id = "1" * 32
            workspace, _ = manager.create(run_id)
            (workspace / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
            candidate = manager.save_candidate(workspace, run_id)

            review = review_candidate(
                passed_run(run_id, workspace, base, candidate), manager
            )
            self.assertTrue(review.eligible_for_manual_promotion)
            self.assertEqual(review.files_changed, ["feature.py"])
            self.assertFalse(
                any(check.status == "block" for check in review.checks)
            )

    def test_stale_base_blocks_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, base = make_source(root)
            manager = WorktreeManager(source, root / "data")
            run_id = "2" * 32
            workspace, _ = manager.create(run_id)
            (workspace / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
            candidate = manager.save_candidate(workspace, run_id)

            (source / "other.py").write_text("OTHER = True\n", encoding="utf-8")
            git(source, "add", ".")
            git(source, "commit", "-m", "source advanced")

            review = review_candidate(
                passed_run(run_id, workspace, base, candidate), manager
            )
            self.assertFalse(review.eligible_for_manual_promotion)
            freshness = next(c for c in review.checks if c.id == "base-freshness")
            self.assertEqual(freshness.status, "block")

    def test_rename_into_protected_test_path_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, base = make_source(root)
            manager = WorktreeManager(source, root / "data")
            run_id = "4" * 32
            workspace, _ = manager.create(run_id)
            (workspace / "feature.py").rename(workspace / "test_feature.py")
            candidate = manager.save_candidate(workspace, run_id)

            review = review_candidate(
                passed_run(run_id, workspace, base, candidate), manager
            )
            self.assertFalse(review.eligible_for_manual_promotion)
            protected = next(c for c in review.checks if c.id == "protected-paths")
            self.assertEqual(protected.status, "block")
            self.assertIn("test_feature.py", protected.message)

    def test_agent_lab_self_modification_requires_extra_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, base = make_source(root, self_mod=True)
            manager = WorktreeManager(source, root / "data")
            run_id = "3" * 32
            workspace, _ = manager.create(run_id)
            target = workspace / "agent_lab" / "worker" / "example.py"
            target.write_text("VALUE = 2\n", encoding="utf-8")
            candidate = manager.save_candidate(workspace, run_id)

            review = review_candidate(
                passed_run(run_id, workspace, base, candidate), manager
            )
            self.assertFalse(review.eligible_for_manual_promotion)
            self_check = next(c for c in review.checks if c.id == "self-modification")
            self.assertEqual(self_check.status, "block")

    def test_verified_self_evaluation_allows_experimental_worker_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, base = make_source(root, self_mod=True)
            manager = WorktreeManager(source, root / "data")
            run_id = "5" * 32
            workspace, _ = manager.create(run_id)
            target = workspace / "agent_lab" / "worker" / "example.py"
            target.write_text("VALUE = 2\n", encoding="utf-8")
            candidate = manager.save_candidate(workspace, run_id)
            evidence = {
                "verified": True,
                "evaluation": {
                    "run_id": run_id,
                    "candidate_commit": candidate,
                    "base_commit": base,
                    "status": "passed",
                },
                "attestation": {
                    "status": "passed",
                    "suite": "agent-lab-selfmod-v1",
                    "reference_hash": "r" * 64,
                    "comparison": {
                        "compatible": True,
                        "regressions": [],
                        "improvements": [],
                        "meets_reference": True,
                    },
                    "critical_checks": {
                        "runner_network_isolated": True,
                        "candidate_source_unchanged": True,
                    },
                    "case_results": [
                        {"case": "sealed-a", "status": "passed"}
                    ],
                },
            }
            review = review_candidate(
                passed_run(run_id, workspace, base, candidate),
                manager,
                self_eval_evidence=evidence,
            )
            self.assertTrue(review.eligible_for_manual_promotion)
            self_check = next(
                c for c in review.checks if c.id == "self-modification"
            )
            self.assertEqual(self_check.status, "pass")

    def test_root_trust_change_remains_blocked_even_with_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            git(source, "init")
            git(source, "config", "user.name", "Test")
            git(source, "config", "user.email", "test@example.invalid")
            target = source / "agent_lab" / "promotion.py"
            target.parent.mkdir(parents=True)
            target.write_text("VALUE = 1\n", encoding="utf-8")
            git(source, "add", ".")
            git(source, "commit", "-m", "initial")
            base = git(source, "rev-parse", "HEAD")
            manager = WorktreeManager(source, root / "data")
            run_id = "6" * 32
            workspace, _ = manager.create(run_id)
            (workspace / "agent_lab" / "promotion.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            candidate = manager.save_candidate(workspace, run_id)
            evidence = {
                "verified": True,
                "evaluation": {
                    "run_id": run_id,
                    "candidate_commit": candidate,
                    "base_commit": base,
                    "status": "passed",
                },
                "attestation": {
                    "status": "passed",
                    "suite": "agent-lab-selfmod-v1",
                    "reference_hash": "r" * 64,
                    "comparison": {
                        "compatible": True,
                        "regressions": [],
                        "improvements": [],
                        "meets_reference": True,
                    },
                    "critical_checks": {"x": True},
                    "case_results": [{"case": "x", "status": "passed"}],
                },
            }
            review = review_candidate(
                passed_run(run_id, workspace, base, candidate),
                manager,
                self_eval_evidence=evidence,
            )
            self.assertFalse(review.eligible_for_manual_promotion)
            self_check = next(
                c for c in review.checks if c.id == "self-modification"
            )
            self.assertEqual(self_check.status, "block")
            self.assertIn("root-of-trust", self_check.message)


if __name__ == "__main__":
    unittest.main()
