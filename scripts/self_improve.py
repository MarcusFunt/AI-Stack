from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_lab.improvement import (
    ImprovementFeature,
    evaluate_feature_baseline,
    evaluate_feature_candidate,
    load_feature_catalog,
)
from agent_lab.sandbox_client import SandboxClient


LAB_URL = "http://127.0.0.1:8770"
EVAL_URL = "http://127.0.0.1:8771"
MIRROR = ROOT / "data" / "agent-lab" / "repo.git"
TERMINAL = {"passed", "failed", "cancelled", "error"}


def api(method: str, url: str, payload=None, timeout: int = 30):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} -> {exc.code}: {body}") from exc


def run(command, cwd=ROOT, timeout=600):
    return subprocess.run(
        [str(part) for part in command],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )


def git(*args: str) -> str:
    proc = run(["git", *args], timeout=120)
    if proc.returncode:
        raise RuntimeError(proc.stdout)
    return proc.stdout.strip()


def wait_api(url: str, timeout_s: int = 1800):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        record = api("GET", url)
        if record.get("status") in TERMINAL:
            return record
        time.sleep(2)
    raise TimeoutError(f"timed out waiting for {url}")
def refresh_mirror() -> None:
    proc = run([
        "git", "--git-dir", str(MIRROR), "fetch", "--prune", str(ROOT),
        "+refs/heads/*:refs/heads/*",
    ], timeout=120)
    if proc.returncode:
        raise RuntimeError(proc.stdout)


def _baseline_evaluation(base_commit: str) -> dict:
    run_id = uuid.uuid4().hex
    proc = run([
        "git", "--git-dir", str(MIRROR), "update-ref",
        f"refs/agent-lab/candidates/{run_id}", base_commit,
    ])
    if proc.returncode:
        raise RuntimeError(proc.stdout)
    created = api("POST", f"{EVAL_URL}/evaluations", {
        "run_id": run_id,
        "candidate_commit": base_commit,
        "base_commit": base_commit,
        "suite": "agent-lab-selfmod-v1",
        "model": "local-fast",
        "token_budget": 60000,
        "wall_time_seconds": 1800,
    })
    evaluation = wait_api(
        f"{EVAL_URL}/evaluations/{created['id']}",
        timeout_s=2000,
    )
    if evaluation["status"] not in {"passed", "failed"}:
        raise RuntimeError(f"baseline evaluation ended as {evaluation['status']}")
    result = evaluation.get("result") or {}
    if int(result.get("passed_cases", 0)) <= 0:
        raise RuntimeError("refusing to establish a zero-quality sealed baseline")
    return evaluation


def _case_statuses(evaluation: dict) -> dict[str, str]:
    result = evaluation.get("result") or {}
    return {
        str(item.get("case")): str(item.get("status"))
        for item in result.get("case_results") or []
        if item.get("case")
    }


def ensure_reference(base_commit: str, log) -> dict:
    try:
        current = api("GET", f"{EVAL_URL}/references/agent-lab-selfmod-v1")
        reference = current.get("reference") or {}
        if (
            reference.get("base_commit") == base_commit
            and reference.get("model") == "local-fast"
        ):
            return current
    except RuntimeError:
        pass

    refresh_mirror()
    first = _baseline_evaluation(base_commit)
    second = _baseline_evaluation(base_commit)
    if _case_statuses(first) != _case_statuses(second):
        raise RuntimeError(
            "sealed baseline is unstable across repeated deterministic evaluations"
        )
    reference = api(
        "POST",
        f"{EVAL_URL}/evaluations/{first['id']}/set-reference",
    )
    ref = reference["reference"]
    log(
        f"sealed stable reference {ref['passed_cases']}/{ref['total_cases']} "
        "confirmed across 2 runs"
    )
    return reference


def task_payload(feature: ImprovementFeature, base_commit: str, feedback: str):
    objective = feature.objective
    if feedback:
        objective += (
            "\n\nPrevious attempt feedback:\n"
            + feedback[-4000:]
            + "\nImplement the general semantics; do not special-case examples."
        )
    return {
        "repository": "ai-stack",
        "base_ref": base_commit,
        "task_type": "python",
        "objective": objective,
        "required_harnesses": ["python-unit"],
        "require_failing_baseline": False,
        "allow_test_edits": False,
        "context_include": list(feature.context_include),
        "context_exclude": list(feature.context_exclude),
        "edit_include": list(feature.edit_include),
        "edit_exclude": list(feature.edit_exclude),
        "budget": {
            "max_iterations": 4,
            "wall_time_minutes": 20,
            "model_tokens": 60000,
        },
    }
def create_workspace(feature: ImprovementFeature, base_commit: str) -> dict:
    return api("POST", f"{LAB_URL}/runs", {
        "task": task_payload(feature, base_commit, ""),
        "auto_start": False,
    }, timeout=120)


def execute_candidate(
    feature: ImprovementFeature,
    base_commit: str,
    feedback: str,
) -> dict:
    created = api("POST", f"{LAB_URL}/runs", {
        "task": task_payload(feature, base_commit, feedback),
        "auto_start": False,
    }, timeout=120)
    api("POST", f"{LAB_URL}/runs/{created['id']}/execute")
    return wait_api(f"{LAB_URL}/runs/{created['id']}", timeout_s=1500)


def materialize_branch(branch: str, candidate: str) -> None:
    git("fetch", str(MIRROR), candidate)
    git("branch", "-f", branch, "FETCH_HEAD")


def sealed_stability(run_id: str, repeats: int) -> list[dict]:
    rows = []
    for _ in range(repeats):
        created = api("POST", f"{LAB_URL}/runs/{run_id}/evaluate")
        evaluation_id = created.get("id")
        if not evaluation_id:
            existing = api("GET", f"{LAB_URL}/runs/{run_id}/evaluations")
            evaluation_id = existing[0]["id"]
        rows.append(wait_api(
            f"{EVAL_URL}/evaluations/{evaluation_id}",
            timeout_s=2000,
        ))
    return rows
def diff_stats(base: str, candidate: str) -> dict:
    proc = run([
        "git", "diff", "--numstat", base, candidate,
    ], timeout=120)
    if proc.returncode:
        return {"error": proc.stdout}
    files = 0
    insertions = 0
    deletions = 0
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        files += 1
        if parts[0].isdigit():
            insertions += int(parts[0])
        if parts[1].isdigit():
            deletions += int(parts[1])
    return {
        "files": files,
        "insertions": insertions,
        "deletions": deletions,
        "changed_lines": insertions + deletions,
    }


def feedback_from_gate(gate: dict) -> str:
    public = next(
        (c for c in gate["checks"] if c["id"] == "candidate-public-passes"),
        None,
    )
    hidden = next(
        (c for c in gate["checks"] if c["id"] == "candidate-hidden-passes"),
        None,
    )
    regression = next(
        (c for c in gate["checks"] if c["id"] == "candidate-full-regression"),
        None,
    )
    if public and public["status"] == "block":
        return "Public feature validation failed:\n" + str(
            public["evidence"].get("output", "")
        )
    if regression and regression["status"] == "block":
        return "Existing repository tests regressed. Preserve backward compatibility."
    if hidden and hidden["status"] == "block":
        return "A hidden semantic validation failed. Re-read the objective and generalize."
    return "Candidate failed an external gate; make the implementation more conservative."
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--catalog",
        default=str(ROOT / "config" / "self-improve-features.json"),
    )
    parser.add_argument("--base-ref", default="HEAD")
    parser.add_argument("--duration-hours", type=float, default=8.0)
    args = parser.parse_args()

    features = load_feature_catalog(args.catalog)
    base_commit = git("rev-parse", f"{args.base_ref}^{{commit}}")
    started = datetime.now()
    deadline = time.time() + max(0.5, args.duration_hours) * 3600
    stamp = started.strftime("%Y%m%d-%H%M%S")
    out = ROOT / "data" / "state" / f"self-improve-v2-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    log_path = out / "campaign.log"
    summary_path = out / "summary.json"

    def log(message: str) -> None:
        line = f"[{datetime.now().isoformat()}] {message}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    sandbox = SandboxClient(ROOT / "data" / "agent-lab" / "sandbox")
    if not sandbox.healthy():
        raise RuntimeError("Agent Lab sandbox is not healthy")
    ensure_reference(base_commit, log)
    branch_prefix = f"agent-nightly-v2/{stamp}"
    summary = {
        "base_commit": base_commit,
        "catalog": str(args.catalog),
        "started_at": started.isoformat(),
        "features": [],
    }
    for index, feature in enumerate(features, start=1):
        if time.time() > deadline - 1200:
            log("deadline guard reached; stopping before next feature")
            break
        baseline = create_workspace(feature, base_commit)
        feedback = ""
        feature_result = {
            "slug": feature.slug,
            "baseline_run_id": baseline["id"],
            "attempts": [],
            "qualified_branch": None,
        }
        log(f"feature {index}/{len(features)} {feature.slug}")
        baseline_gate = evaluate_feature_baseline(
            sandbox,
            base_run_id=baseline["id"],
            public_validator=feature.public_validator,
            hidden_validator=feature.hidden_validator,
        ).as_dict()
        feature_result["baseline_gate"] = baseline_gate
        if not baseline_gate["eligible"]:
            log(f"skipping {feature.slug}: validators do not discriminate baseline")
            summary["features"].append(feature_result)
            summary_path.write_text(
                json.dumps(summary, indent=2),
                encoding="utf-8",
            )
            try:
                api("POST", f"{LAB_URL}/runs/{baseline['id']}/cleanup")
            except Exception:
                pass
            continue

        for attempt in range(1, feature.max_attempts + 1):
            if time.time() > deadline - 900:
                break
            candidate = execute_candidate(feature, base_commit, feedback)
            result = candidate.get("result") or {}
            commit = result.get("candidate_commit")
            branch = f"{branch_prefix}/{index:02d}-{feature.slug}-a{attempt}"
            evidence = {
                "attempt": attempt,
                "run_id": candidate["id"],
                "run_status": candidate["status"],
                "candidate_commit": commit,
                "branch": None,
            }
            if not commit:
                feedback = "Agent did not produce a passing candidate."
                evidence["error"] = candidate.get("error") or feedback
                feature_result["attempts"].append(evidence)
                try:
                    api("POST", f"{LAB_URL}/runs/{candidate['id']}/cleanup")
                except Exception:
                    pass
                continue

            materialize_branch(branch, commit)
            evidence["branch"] = branch
            gate = evaluate_feature_candidate(
                sandbox,
                base_run_id=baseline["id"],
                candidate_run_id=candidate["id"],
                public_validator=feature.public_validator,
                hidden_validator=feature.hidden_validator,
            ).as_dict()
            evidence["feature_gate"] = gate
            evidence["diff"] = diff_stats(base_commit, commit)
            if gate["eligible"]:
                evaluations = sealed_stability(
                    candidate["id"],
                    feature.sealed_repeats,
                )
                evidence["sealed_evaluations"] = [
                    {
                        "id": item["id"],
                        "status": item["status"],
                        "result": item.get("result"),
                    }
                    for item in evaluations
                ]
                sealed_ok = all(item["status"] == "passed" for item in evaluations)
                review = api(
                    "GET",
                    f"{LAB_URL}/runs/{candidate['id']}/promotion-review",
                )
                evidence["promotion_review"] = review
                promotion_ok = bool(review.get("eligible_for_manual_promotion"))
            else:
                sealed_ok = False
                promotion_ok = False

            evidence["qualified"] = gate["eligible"] and sealed_ok and promotion_ok
            feature_result["attempts"].append(evidence)
            (out / f"{index:02d}-{feature.slug}-a{attempt}.json").write_text(
                json.dumps(evidence, indent=2),
                encoding="utf-8",
            )
            try:
                api("POST", f"{LAB_URL}/runs/{candidate['id']}/cleanup")
            except Exception:
                pass
            if evidence["qualified"]:
                feature_result["qualified_branch"] = branch
                log(f"qualified {feature.slug} -> {branch}")
                break
            feedback = feedback_from_gate(gate)
            log(f"retrying {feature.slug}: {feedback.splitlines()[0]}")

        summary["features"].append(feature_result)
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        try:
            api("POST", f"{LAB_URL}/runs/{baseline['id']}/cleanup")
        except Exception:
            pass

    summary["finished_at"] = datetime.now().isoformat()
    summary["qualified_branches"] = [
        item["qualified_branch"]
        for item in summary["features"]
        if item["qualified_branch"]
    ]
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log(f"campaign complete; qualified={len(summary['qualified_branches'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
