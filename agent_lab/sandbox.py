from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any


ID_RE = re.compile(r"^[a-f0-9]{32}$")
RUNS_ROOT = Path(os.environ.get("AGENT_LAB_RUNS_ROOT", "/runs")).resolve()
JOBS_ROOT = Path(os.environ.get("AGENT_LAB_JOBS_ROOT", "/jobs")).resolve()
REQUESTS = JOBS_ROOT / "requests"
RESULTS = JOBS_ROOT / "results"
HEARTBEAT = JOBS_ROOT / "heartbeat.json"


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload), encoding="utf-8")
    temp.replace(path)


def drop_to_unprivileged() -> None:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        return
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)


def execute_python_unit(
    run_id: str, timeout_s: int, suite: str = "public"
) -> dict[str, Any]:
    if not ID_RE.fullmatch(run_id):
        raise ValueError("invalid run id")
    workspace = (RUNS_ROOT / run_id / "workspace").resolve()
    try:
        workspace.relative_to(RUNS_ROOT)
    except ValueError as exc:
        raise ValueError("workspace escapes runs root") from exc
    if not workspace.is_dir():
        raise FileNotFoundError(f"workspace not found: {run_id}")

    if suite == "public":
        command = ["python", "-m", "unittest", "discover", "-v"]
    elif suite == "holdout":
        holdout = workspace / ".agent_lab_holdout"
        if not holdout.is_dir():
            return {
                "returncode": 0,
                "output": "NO_HOLDOUT_SUITE",
                "duration_s": 0.0,
                "skipped": True,
            }
        command = [
            "python", "-m", "unittest", "discover", "-v",
            "-s", ".agent_lab_holdout",
        ]
    else:
        raise ValueError(f"unsupported test suite: {suite}")

    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    started = time.monotonic()
    proc = subprocess.run(
        command,
        cwd=workspace,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=max(1, min(int(timeout_s), 600)),
        preexec_fn=drop_to_unprivileged,
    )
    return {
        "returncode": proc.returncode,
        "output": proc.stdout[-20_000:],
        "duration_s": round(time.monotonic() - started, 3),
    }


def process_request(path: Path) -> None:
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
        job_id = str(request.get("job_id", ""))
        if not ID_RE.fullmatch(job_id):
            raise ValueError("invalid job id")
        if request.get("kind") != "python-unit":
            raise ValueError("unsupported sandbox job kind")
        payload = execute_python_unit(
            str(request.get("run_id", "")),
            int(request.get("timeout_s", 600)),
            str(request.get("suite", "public")),
        )
    except subprocess.TimeoutExpired as exc:
        payload = {
            "error": f"test process timed out after {exc.timeout}s",
            "returncode": 124,
            "output": "",
            "duration_s": float(exc.timeout),
        }
        job_id = path.stem
    except Exception as exc:
        payload = {"error": str(exc)}
        job_id = path.stem

    if ID_RE.fullmatch(job_id):
        atomic_json(RESULTS / f"{job_id}.json", payload)
    path.unlink(missing_ok=True)


def main() -> None:
    REQUESTS.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(JOBS_ROOT, 0o700)
        os.chmod(REQUESTS, 0o700)
        os.chmod(RESULTS, 0o700)
    except OSError:
        pass

    while True:
        atomic_json(HEARTBEAT, {"ts": time.time()})
        for request in sorted(REQUESTS.glob("*.json")):
            process_request(request)
        time.sleep(0.2)


if __name__ == "__main__":
    main()
