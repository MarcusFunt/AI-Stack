from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any


class SandboxError(RuntimeError):
    pass


class SandboxClient:
    def __init__(self, queue_root: str | Path):
        self.root = Path(queue_root)
        self.requests = self.root / "requests"
        self.results = self.root / "results"
        self.requests.mkdir(parents=True, exist_ok=True)
        self.results.mkdir(parents=True, exist_ok=True)

    def _atomic_json(self, path: Path, payload: dict[str, Any]) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload), encoding="utf-8")
        temp.replace(path)

    def python_unit(
        self, run_id: str, timeout_s: int, suite: str = "public"
    ) -> dict[str, Any]:
        if suite not in {"public", "holdout"}:
            raise SandboxError(f"unsupported test suite: {suite}")
        job_id = uuid.uuid4().hex
        request = self.requests / f"{job_id}.json"
        result = self.results / f"{job_id}.json"
        self._atomic_json(
            request,
            {
                "job_id": job_id,
                "run_id": run_id,
                "kind": "python-unit",
                "suite": suite,
                "timeout_s": max(1, min(int(timeout_s), 600)),
            },
        )
        deadline = time.monotonic() + max(15, min(int(timeout_s), 600) + 15)
        while time.monotonic() < deadline:
            if result.exists():
                try:
                    payload = json.loads(result.read_text(encoding="utf-8"))
                finally:
                    result.unlink(missing_ok=True)
                if payload.get("error"):
                    raise SandboxError(str(payload["error"]))
                return payload
            time.sleep(0.1)
        request.unlink(missing_ok=True)
        raise SandboxError(f"sandbox job {job_id} timed out waiting for a result")

    def python_validator(
        self,
        run_id: str,
        code: str,
        timeout_s: int = 120,
    ) -> dict[str, Any]:
        if not isinstance(code, str) or not code.strip():
            raise SandboxError("validator code is empty")
        if len(code.encode("utf-8")) > 50_000:
            raise SandboxError("validator code exceeds 50000 bytes")
        job_id = uuid.uuid4().hex
        request = self.requests / f"{job_id}.json"
        result = self.results / f"{job_id}.json"
        bounded_timeout = max(1, min(int(timeout_s), 600))
        self._atomic_json(
            request,
            {
                "job_id": job_id,
                "run_id": run_id,
                "kind": "python-validator",
                "code": code,
                "timeout_s": bounded_timeout,
            },
        )
        deadline = time.monotonic() + max(15, bounded_timeout + 15)
        while time.monotonic() < deadline:
            if result.exists():
                try:
                    payload = json.loads(result.read_text(encoding="utf-8"))
                finally:
                    result.unlink(missing_ok=True)
                if payload.get("error"):
                    raise SandboxError(str(payload["error"]))
                return payload
            time.sleep(0.1)
        request.unlink(missing_ok=True)
        raise SandboxError(f"sandbox job {job_id} timed out waiting for a result")

    def healthy(self, max_age_s: float = 10.0) -> bool:
        heartbeat = self.root / "heartbeat.json"
        try:
            payload = json.loads(heartbeat.read_text(encoding="utf-8"))
            age = time.time() - float(payload["ts"])
            return 0 <= age <= max_age_s
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return False
