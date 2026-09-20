from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any


EXCHANGE = Path(os.environ.get("AGENT_EVAL_EXCHANGE", "/exchange")).resolve()
RUNNER_ROOT = EXCHANGE / "runner"
REQUESTS = RUNNER_ROOT / "requests"
RESULTS = RUNNER_ROOT / "results"
HEARTBEAT = EXCHANGE / "runner-heartbeat.json"
JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(payload), encoding="utf-8")
    temp.replace(path)


def drop_to_unprivileged() -> None:
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)


def hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix().encode()
        digest.update(rel)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def make_candidate_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        try:
            path.chmod(0o555 if path.is_dir() else 0o444)
        except OSError:
            pass
    root.chmod(0o555)


def make_workspace_writable(root: Path) -> None:
    # The networkless runner intentionally has SETUID/SETGID but not CHOWN.
    # Candidate workspaces live only in the runner's tmpfs, so directory
    # write permission is granted with mode bits instead. Public tests are
    # independently hashed before/after candidate execution.
    for path in root.rglob("*"):
        if path.is_dir():
            path.chmod(0o777)
        else:
            path.chmod(0o644)
    root.chmod(0o777)


def network_isolated() -> bool:
    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=0.2):
            return False
    except OSError:
        return True


class LocalSandboxClient:
    def __init__(self, runs_root: Path):
        self.runs_root = runs_root

    def python_unit(self, run_id: str, timeout_s: int, suite: str = "public") -> dict:
        workspace = self.runs_root / run_id / "workspace"
        if suite == "holdout":
            hidden = workspace / ".agent_lab_holdout"
            if not hidden.is_dir():
                return {
                    "returncode": 0,
                    "output": "NO_HOLDOUT_SUITE",
                    "duration_s": 0.0,
                    "skipped": True,
                }
            command = [
                sys.executable, "-m", "unittest", "discover", "-v",
                "-s", ".agent_lab_holdout",
            ]
        else:
            command = [sys.executable, "-m", "unittest", "discover", "-v"]
        started = time.monotonic()
        proc = subprocess.run(
            command,
            cwd=workspace,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=max(1, min(int(timeout_s), 600)),
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": "/tmp",
                "TMPDIR": "/tmp",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
            },
        )
        return {
            "returncode": proc.returncode,
            "output": proc.stdout[-20_000:],
            "duration_s": round(time.monotonic() - started, 3),
        }


class QueueModelClient:
    def __init__(
        self,
        request_dir: Path,
        result_dir: Path,
        capability: str,
        timeout_s: int = 600,
    ):
        self.request_dir = request_dir
        self.result_dir = result_dir
        self.capability = capability
        self.timeout_s = timeout_s

    def propose_patch(self, objective, repo_context, prior_result):
        feedback = json.dumps(prior_result, ensure_ascii=False) if prior_result else "none"
        system = (
            "You are a coding agent in an isolated Git worktree. Return ONLY valid JSON. "
            "You may replace exact text in existing files shown in repository context, "
            "or create a small new source/config/documentation file when required. "
            "Never modify tests, hidden evaluation files, secrets, .git, or generated state."
        )
        user = f"""Objective:
{objective}

Previous harness result:
{feedback}

Repository context:
{repo_context}

Return JSON:
{{
  "summary": "short reasoning summary",
  "edits": [
    {{"op":"replace","path":"relative/file.py","old":"exact text","new":"replacement"}},
    {{"op":"create","path":"relative/new.py","content":"complete contents"}}
  ]
}}
Use only needed operations and at most five. Do not edit tests."""
        request_id = uuid.uuid4().hex
        request = self.request_dir / f"{request_id}.json"
        result = self.result_dir / f"{request_id}.json"
        atomic_json(
            request,
            {
                "request_id": request_id,
                "capability": self.capability,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": 2048,
            },
        )
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            if result.exists():
                payload = json.loads(result.read_text(encoding="utf-8"))
                if payload.get("error"):
                    raise RuntimeError(str(payload["error"]))
                raw = str(payload["content"])
                cleaned = JSON_FENCE.sub("", raw.strip()).strip()
                parsed = json.loads(cleaned)
                if not isinstance(parsed, dict) or not isinstance(parsed.get("edits", []), list):
                    raise ValueError("model returned invalid patch object")
                return parsed
            time.sleep(0.05)
        raise TimeoutError(f"model queue request {request_id} timed out")


def candidate_bootstrap(spec_path: Path) -> int:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    candidate_src = Path(spec["candidate_src"]).resolve()
    workspace = Path(spec["workspace"]).resolve()
    runs_root = workspace.parent.parent
    sys.path.insert(0, str(candidate_src))

    from agent_lab.harnesses import HarnessRegistry
    from agent_lab.schemas import Budget, TaskSpec
    from agent_lab.worker.graph import AgentRunner

    model = QueueModelClient(
        Path(spec["model_requests"]),
        Path(spec["model_results"]),
        str(spec["model_capability"]),
        int(spec.get("model_timeout_s", 600)),
    )
    sandbox = LocalSandboxClient(runs_root)
    registry = HarnessRegistry(sandbox=sandbox)
    task = TaskSpec(
        repository="ai-stack",
        task_type=spec.get("task_type", "python"),
        objective=spec["objective"],
        required_harnesses=[spec.get("harness", "python-unit")],
        require_failing_baseline=True,
        allow_test_edits=False,
        budget=Budget(
            max_iterations=int(spec.get("max_iterations", 3)),
            wall_time_minutes=max(1, int(spec.get("wall_time_minutes", 5))),
            model_tokens=int(spec.get("model_tokens", 20_000)),
        ),
    )
    state = AgentRunner(task, workspace, registry, model).run(spec["run_id"])
    result = {
        "final_status": state.get("final_status"),
        "iteration": state.get("iteration", 0),
        "applied_edits": state.get("applied_edits", []),
        "error": state.get("error", ""),
        "harness_status": (state.get("harness_result") or {}).get("status"),
        "ai_api_key_absent": "AI_API_KEY" not in os.environ,
    }
    print("AGENT_EVAL_RESULT=" + json.dumps(result, separators=(",", ":")))
    return 0


def run_process(command: list[str], cwd: Path, timeout_s: int, env: dict[str, str]) -> dict:
    started = time.monotonic()
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        preexec_fn=drop_to_unprivileged,
        start_new_session=True,
    )
    try:
        output, _ = proc.communicate(timeout=timeout_s)
        return {
            "returncode": proc.returncode,
            "output": output[-50_000:],
            "duration_s": round(time.monotonic() - started, 3),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        output, _ = proc.communicate()
        return {
            "returncode": 124,
            "output": output[-50_000:],
            "duration_s": round(time.monotonic() - started, 3),
            "timed_out": True,
        }


def oracle_unittest(workspace: Path, hidden: bool, timeout_s: int = 60) -> dict:
    command = [sys.executable, "-m", "unittest", "discover", "-v"]
    if hidden:
        command += ["-s", ".agent_eval_hidden"]
    result = run_process(
        command,
        workspace,
        timeout_s,
        {
            "PATH": os.environ.get("PATH", ""),
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        },
    )
    output = result["output"]
    result["passed"] = (
        result["returncode"] == 0
        and "Ran 0 tests" not in output
    )
    return result


def run_case(eval_root: Path, candidate_src: Path, case: dict, capability: str) -> dict:
    local_root = Path("/tmp/agent-eval") / eval_root.name / case["id"]
    if local_root.exists():
        shutil.rmtree(local_root)
    case_root = local_root
    run_id = uuid.uuid4().hex
    runs_root = case_root / "runs"
    workspace = runs_root / run_id / "workspace"
    workspace.mkdir(parents=True, exist_ok=False)

    for rel, content in case.get("files", {}).items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    public_tests = case.get("public_tests")
    public_test_path = workspace / "test_regression.py"
    if public_tests:
        public_test_path.write_text(public_tests, encoding="utf-8")
    make_workspace_writable(workspace)
    public_hash = hash_file(public_test_path) if public_test_path.exists() else None
    candidate_hash_before = hash_tree(candidate_src)

    public_spec_dir = case_root / "public"
    public_spec_dir.mkdir(parents=True, exist_ok=True)
    public_spec = public_spec_dir / "spec.json"
    atomic_json(
        public_spec,
        {
            "candidate_src": str(candidate_src),
            "workspace": str(workspace),
            "run_id": run_id,
            "objective": case["objective"],
            "task_type": case.get("task_type", "python"),
            "harness": case.get("harness", "python-unit"),
            "max_iterations": case.get("max_iterations", 3),
            "wall_time_minutes": 5,
            "model_tokens": 20_000,
            "model_requests": str(eval_root / "model" / "requests"),
            "model_results": str(eval_root / "model" / "results"),
            "model_capability": capability,
            "model_timeout_s": 600,
        },
    )
    public_spec.chmod(0o444)
    candidate = run_process(
        [sys.executable, __file__, "bootstrap", str(public_spec)],
        workspace,
        int(case.get("candidate_timeout_s", 180)),
        {
            "PATH": os.environ.get("PATH", ""),
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        },
    )
    parsed: dict[str, Any] = {}
    for line in candidate["output"].splitlines():
        if line.startswith("AGENT_EVAL_RESULT="):
            try:
                parsed = json.loads(line.split("=", 1)[1])
            except json.JSONDecodeError:
                parsed = {}
    candidate_hash_after = hash_tree(candidate_src)
    public_untampered = (
        public_hash is None
        or (
            public_test_path.exists()
            and hash_file(public_test_path) == public_hash
        )
    )

    public_oracle = oracle_unittest(workspace, hidden=False)
    hidden_tests = case.get("hidden_tests")
    hidden_oracle = {"passed": True, "skipped": True}
    if hidden_tests:
        hidden_dir = workspace / ".agent_eval_hidden"
        hidden_dir.mkdir(exist_ok=True)
        hidden_file = hidden_dir / "test_hidden.py"
        hidden_file.write_text(hidden_tests, encoding="utf-8")
        hidden_file.chmod(0o444)
        hidden_oracle = oracle_unittest(workspace, hidden=True)

    passed = (
        candidate["returncode"] == 0
        and parsed.get("final_status") == "passed"
        and public_untampered
        and candidate_hash_before == candidate_hash_after
        and bool(public_oracle.get("passed"))
        and bool(hidden_oracle.get("passed"))
        and bool(parsed.get("ai_api_key_absent"))
    )
    return {
        "case": case["id"],
        "status": "passed" if passed else "failed",
        "candidate": parsed,
        "candidate_process": {
            "returncode": candidate["returncode"],
            "timed_out": candidate["timed_out"],
            "duration_s": candidate["duration_s"],
        },
        "public_oracle": {
            "passed": public_oracle.get("passed", False),
            "returncode": public_oracle.get("returncode"),
        },
        "hidden_oracle": {
            "passed": hidden_oracle.get("passed", False),
            "returncode": hidden_oracle.get("returncode"),
            "skipped": hidden_oracle.get("skipped", False),
        },
        "public_tests_untampered": public_untampered,
        "candidate_source_unchanged": candidate_hash_before == candidate_hash_after,
    }


def process_job(path: Path) -> None:
    job_id = path.stem
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
        if request.get("evaluation_id") != job_id:
            raise ValueError("evaluation id mismatch")
        eval_root = EXCHANGE / "evaluations" / job_id
        candidate_src = Path(request["candidate_src"]).resolve()
        make_candidate_read_only(candidate_src)
        cases = request["cases"]
        results = [
            run_case(eval_root, candidate_src, case, request["model_capability"])
            for case in cases
        ]
        shutil.rmtree(
            Path("/tmp/agent-eval") / job_id,
            ignore_errors=True,
        )
        payload = {
            "evaluation_id": job_id,
            "status": "passed" if all(r["status"] == "passed" for r in results) else "failed",
            "case_results": results,
            "critical_checks": {
                "runner_network_isolated": network_isolated(),
                "all_public_tests_untampered": all(
                    r["public_tests_untampered"] for r in results
                ),
                "candidate_source_unchanged": all(
                    r["candidate_source_unchanged"] for r in results
                ),
                "candidate_has_no_gateway_secret": all(
                    bool(r["candidate"].get("ai_api_key_absent")) for r in results
                ),
            },
        }
    except Exception as exc:
        payload = {
            "evaluation_id": job_id,
            "status": "error",
            "error": str(exc),
            "case_results": [],
            "critical_checks": {},
        }
    atomic_json(RESULTS / f"{job_id}.json", payload)
    path.unlink(missing_ok=True)


def daemon() -> int:
    REQUESTS.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    try:
        REQUESTS.chmod(0o700)
        RESULTS.chmod(0o700)
    except OSError:
        pass
    while True:
        atomic_json(
            HEARTBEAT,
            {"ts": time.time(), "network_isolated": network_isolated()},
        )
        for path in sorted(REQUESTS.glob("*.json")):
            process_job(path)
        time.sleep(0.1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: runner_runtime.py daemon|bootstrap [spec]")
    if sys.argv[1] == "daemon":
        raise SystemExit(daemon())
    if sys.argv[1] == "bootstrap" and len(sys.argv) == 3:
        raise SystemExit(candidate_bootstrap(Path(sys.argv[2])))
    raise SystemExit("invalid command")
