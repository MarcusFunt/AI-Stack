from __future__ import annotations

from pathlib import PurePosixPath

from .schemas import PromotionReview, ReviewCheck, RunRecord, RunStatus
from .worktrees import WorktreeError, WorktreeManager


_PROTECTED_TOP_LEVEL = {"data", "models", "third_party"}
_ROOT_TRUST_PATHS = {
    "compose.yaml",
    "scripts/ai.ps1",
    "scripts/doctor.ps1",
    "scripts/source_hash.py",
    "agent_lab/app.py",
    "agent_lab/controller.py",
    "agent_lab/evaluator_client.py",
    "agent_lab/promotion.py",
    "agent_lab/sandbox.py",
    "agent_lab/sandbox_client.py",
    "agent_lab/schemas.py",
    "agent_lab/worktrees.py",
}
_EXPERIMENTAL_SELF_PREFIXES = (
    "agent_lab/worker/",
    "agent_lab/harnesses/",
)


def _protected_path(path: str) -> str | None:
    rel = PurePosixPath(path.replace("\\", "/"))
    parts = [part.lower() for part in rel.parts]
    name = rel.name.lower()
    if any(part == ".git" or part == ".agent_lab_holdout" for part in parts):
        return "Git/hidden-evaluation path"
    if parts and parts[0] in _PROTECTED_TOP_LEVEL:
        return f"protected top-level directory: {parts[0]}"
    if name == ".env" or name.startswith(".env."):
        return "environment/secret file"
    if "tests" in parts or name.startswith("test_") or name.endswith("_test.py"):
        return "test file"
    return None


def _self_modification_class(path: str) -> str | None:
    normalized = path.replace("\\", "/")
    if normalized in _ROOT_TRUST_PATHS:
        return "root-trust"
    if any(normalized.startswith(prefix) for prefix in _EXPERIMENTAL_SELF_PREFIXES):
        return "experimental"
    if normalized.startswith("agent_lab/"):
        return "root-trust"
    return None


def _valid_self_eval_evidence(
    evidence: dict | None,
    *,
    run_id: str,
    candidate_commit: str,
    base_commit: str,
) -> tuple[bool, str]:
    if not evidence:
        return False, "no verified candidate-specific evaluator evidence"
    if not evidence.get("verified"):
        return False, "candidate evaluator attestation is not verified"
    evaluation = evidence.get("evaluation") or {}
    attestation = evidence.get("attestation") or {}
    if evaluation.get("run_id") != run_id:
        return False, "evaluation run id does not match candidate run"
    if evaluation.get("candidate_commit") != candidate_commit:
        return False, "evaluation candidate commit does not match"
    if evaluation.get("base_commit") != base_commit:
        return False, "evaluation base commit does not match"
    if evaluation.get("status") != "passed" or attestation.get("status") != "passed":
        return False, "candidate-specific evaluation did not pass"
    if attestation.get("suite") != "agent-lab-selfmod-v1":
        return False, "unexpected evaluator suite"
    critical = attestation.get("critical_checks") or {}
    if not critical or not all(bool(value) for value in critical.values()):
        return False, "one or more evaluator critical checks failed"
    case_results = attestation.get("case_results") or []
    if not case_results:
        return False, "sealed evaluator returned no case results"
    comparison = attestation.get("comparison") or {}
    if not comparison.get("compatible"):
        return False, "sealed evaluator result is not reference-compatible"
    if comparison.get("regressions"):
        return False, "sealed evaluator detected regressions"
    if not comparison.get("meets_reference"):
        return False, "candidate does not meet sealed evaluator reference"
    if not attestation.get("reference_hash"):
        return False, "sealed evaluator attestation is not bound to a reference"
    return True, "verified sealed candidate-specific evaluation passed"


def review_candidate(
    run: RunRecord,
    worktrees: WorktreeManager,
    *,
    max_files: int = 10,
    max_changed_lines: int = 500,
    self_eval_evidence: dict | None = None,
) -> PromotionReview:
    checks: list[ReviewCheck] = []

    def check(check_id: str, status: str, message: str) -> None:
        checks.append(ReviewCheck(id=check_id, status=status, message=message))

    result = run.result or {}
    recorded_candidate = result.get("candidate_commit")
    base_commit = run.base_commit
    current_source: str | None = None
    files: list[str] = []
    insertions = 0
    deletions = 0

    if run.status == RunStatus.PASSED:
        check("terminal-status", "pass", "run completed with passed status")
    else:
        check(
            "terminal-status",
            "block",
            f"run status is {run.status.value}, not passed",
        )

    harness = result.get("harness_result") or {}
    if harness.get("status") == "passed":
        check("visible-validation", "pass", "selected harness passed")
    else:
        check("visible-validation", "block", "selected harness did not pass")

    holdout = result.get("holdout_result")
    if isinstance(holdout, dict) and holdout.get("status") == "passed":
        check("holdout-validation", "pass", "hidden holdout passed")
    elif isinstance(holdout, dict) and holdout.get("status") == "failed":
        check("holdout-validation", "block", "hidden holdout failed")
    else:
        check(
            "holdout-validation",
            "warn",
            "no passing hidden holdout evidence is attached to this run",
        )

    if not base_commit:
        check("base-commit", "block", "run has no recorded base commit")
    else:
        check("base-commit", "pass", base_commit)

    if not recorded_candidate:
        check("candidate-record", "block", "run has no recorded candidate commit")
    else:
        try:
            private_candidate = worktrees.candidate_commit(run.id)
            if private_candidate == recorded_candidate:
                check(
                    "candidate-record",
                    "pass",
                    "private candidate ref matches recorded commit",
                )
            else:
                check(
                    "candidate-record",
                    "block",
                    "private candidate ref does not match recorded commit",
                )
        except WorktreeError as exc:
            check("candidate-record", "block", f"candidate ref unavailable: {exc}")

    if base_commit:
        try:
            current_source = worktrees.current_source_commit("HEAD")
            if current_source == base_commit:
                check("base-freshness", "pass", "candidate is based on current source HEAD")
            else:
                check(
                    "base-freshness",
                    "block",
                    "source HEAD changed since the candidate run",
                )
        except WorktreeError as exc:
            check("base-freshness", "block", f"cannot resolve source HEAD: {exc}")

    if base_commit and recorded_candidate:
        try:
            stats = worktrees.candidate_diff_stats(base_commit, recorded_candidate)
            files = [str(path) for path in stats["files"]]
            insertions = int(stats["insertions"])
            deletions = int(stats["deletions"])
            statuses = dict(stats["statuses"])
            if not files:
                check("non-empty-diff", "block", "candidate contains no changes")
            else:
                check("non-empty-diff", "pass", f"{len(files)} files changed")

            if bool(stats["binary"]):
                check("binary-diff", "block", "candidate contains binary changes")
            else:
                check("binary-diff", "pass", "no binary changes")

            changed_lines = insertions + deletions
            if len(files) <= max_files and changed_lines <= max_changed_lines:
                check(
                    "diff-size",
                    "pass",
                    f"{len(files)} files, {changed_lines} changed lines",
                )
            else:
                check(
                    "diff-size",
                    "block",
                    f"{len(files)} files, {changed_lines} changed lines exceeds policy",
                )

            protected = [
                f"{path} ({reason})"
                for path in files
                if (reason := _protected_path(path)) is not None
            ]
            if protected:
                check("protected-paths", "block", "; ".join(protected))
            else:
                check("protected-paths", "pass", "no protected paths changed")

            deleted = [
                path for path, status in statuses.items()
                if str(status).startswith("D")
            ]
            if deleted:
                check(
                    "file-deletions",
                    "block",
                    "file deletions require explicit human review: " + ", ".join(deleted),
                )
            else:
                check("file-deletions", "pass", "no files deleted")

            self_changes = {
                path: _self_modification_class(path)
                for path in files
                if _self_modification_class(path) is not None
            }
            root_trust = [
                path for path, kind in self_changes.items()
                if kind == "root-trust"
            ]
            experimental = [
                path for path, kind in self_changes.items()
                if kind == "experimental"
            ]
            if root_trust:
                check(
                    "self-modification",
                    "block",
                    "root-of-trust files are always manual-only: "
                    + ", ".join(root_trust),
                )
            elif experimental:
                valid, message = _valid_self_eval_evidence(
                    self_eval_evidence,
                    run_id=run.id,
                    candidate_commit=str(recorded_candidate),
                    base_commit=str(base_commit),
                )
                check(
                    "self-modification",
                    "pass" if valid else "block",
                    message + ": " + ", ".join(experimental),
                )
            else:
                check("self-modification", "pass", "candidate does not alter Agent Lab")
        except WorktreeError as exc:
            check("candidate-diff", "block", f"cannot inspect candidate diff: {exc}")

    check(
        "automatic-promotion",
        "info",
        "automatic merge/promotion is disabled; this gate only qualifies manual review",
    )
    eligible = not any(item.status == "block" for item in checks)
    return PromotionReview(
        run_id=run.id,
        eligible_for_manual_promotion=eligible,
        candidate_commit=str(recorded_candidate) if recorded_candidate else None,
        base_commit=base_commit,
        current_source_commit=current_source,
        files_changed=files,
        insertions=insertions,
        deletions=deletions,
        checks=checks,
    )
