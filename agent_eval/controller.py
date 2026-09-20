from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import secrets
import shutil
import subprocess
import tarfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from .db import EvaluationStore
from .schemas import (
    Attestation,
    EvaluationCreate,
    EvaluationRecord,
    EvaluationStatus,
)
from .suite import get_suite, suite_hash


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


class EvaluatorController:
    def __init__(self) -> None:
        self.data_root = Path(os.environ.get("AGENT_EVAL_DATA_ROOT", "/data"))
        self.exchange = Path(os.environ.get("AGENT_EVAL_EXCHANGE", "/exchange"))
        self.repo = Path(os.environ.get("AGENT_EVAL_CANDIDATE_REPO", "/candidate.git"))
        self.gateway_url = os.environ.get("GATEWAY_URL", "http://gateway:8000").rstrip("/")
        self.api_key = os.environ.get("AI_API_KEY", "")
        self.source_hash = os.environ.get("AGENT_EVAL_SOURCE_HASH", "unknown")
        self.store = EvaluationStore(
            os.environ.get("AGENT_EVAL_DB", str(self.data_root / "evaluator.sqlite3"))
        )
        self.store.recover_inflight()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self._active: set[str] = set()
        self._cancel: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self.signing_key = self._load_or_create_signing_key()
        (self.data_root / "attestations").mkdir(parents=True, exist_ok=True)
        (self.exchange / "runner" / "requests").mkdir(parents=True, exist_ok=True)
        (self.exchange / "runner" / "results").mkdir(parents=True, exist_ok=True)

    def _load_or_create_signing_key(self) -> bytes:
        path = self.data_root / "signing.key"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(secrets.token_bytes(32))
            try:
                path.chmod(0o600)
            except OSError:
                pass
        value = path.read_bytes()
        if len(value) < 32:
            raise RuntimeError("evaluator signing key is too short")
        return value

    def runner_health(self) -> dict[str, Any]:
        path = self.exchange / "runner-heartbeat.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            age = time.time() - float(payload["ts"])
            return {
                "ok": 0 <= age < 10,
                "age_s": round(age, 3),
                "network_isolated": bool(payload.get("network_isolated")),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def health(self) -> dict[str, Any]:
        runner = self.runner_health()
        with self._lock:
            active = sorted(self._active)
        return {
            "status": "ok" if runner.get("ok") and runner.get("network_isolated") else "degraded",
            "service": "agent-evaluator",
            "version": __version__,
            "runner": runner,
            "active_evaluations": active,
        }

    def _git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-c", "safe.directory=*", "--git-dir", str(self.repo), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "git command failed")
        return proc.stdout.strip()

    def _candidate_ref_commit(self, run_id: str) -> str:
        return self._git(
            "rev-parse", f"refs/agent-lab/candidates/{run_id}^{{commit}}"
        )

    def _export_candidate(self, commit: str, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=False)
        proc = subprocess.run(
            [
                "git", "-c", "safe.directory=*",
                "--git-dir", str(self.repo),
                "archive", "--format=tar", commit, "agent_lab",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode(errors="replace").strip())
        with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as archive:
            for member in archive.getmembers():
                target = (destination / member.name).resolve()
                target.relative_to(destination.resolve())
            archive.extractall(destination)

    def create(self, request: EvaluationCreate) -> EvaluationRecord:
        if request.suite != "agent-lab-selfmod-v1":
            raise ValueError("unsupported sealed suite")
        if request.model != "local-fast":
            raise ValueError("v0.4 evaluator only permits local-fast")
        private_commit = self._candidate_ref_commit(request.run_id)
        if private_commit != request.candidate_commit:
            raise ValueError("private candidate ref does not match requested commit")
        evaluation_id = uuid.uuid4().hex
        record = self.store.create(
            evaluation_id=evaluation_id,
            run_id=request.run_id,
            candidate_commit=request.candidate_commit,
            base_commit=request.base_commit,
            suite=request.suite,
            suite_hash=suite_hash(request.suite),
            model=request.model,
            token_budget=request.token_budget,
            wall_time_seconds=request.wall_time_seconds,
        )
        with self._lock:
            self._active.add(evaluation_id)
            self._cancel[evaluation_id] = threading.Event()
        self.executor.submit(self._execute, evaluation_id)
        return record

    def cancel(self, evaluation_id: str) -> EvaluationRecord:
        record = self.store.get(evaluation_id)
        with self._lock:
            event = self._cancel.get(evaluation_id)
            if evaluation_id in self._active and event is not None:
                event.set()
                marker = (
                    self.exchange / "evaluations" / evaluation_id / "CANCEL"
                )
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("cancelled", encoding="utf-8")
                return self.store.update(
                    evaluation_id,
                    status=EvaluationStatus.CANCELLING,
                    error="evaluation cancellation requested",
                )
        if record.status == EvaluationStatus.QUEUED:
            return self.store.update(
                evaluation_id,
                status=EvaluationStatus.CANCELLED,
                error="evaluation cancelled before start",
            )
        raise ValueError(f"cannot cancel evaluation in state {record.status.value}")

    def _model_call(
        self,
        record: EvaluationRecord,
        request: dict[str, Any],
        remaining_tokens: int,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        messages = request.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("invalid model messages")
        if len(messages) > 16:
            raise ValueError("too many model messages")
        requested = int(request.get("max_tokens", 2048))
        max_tokens = max(32, min(requested, 2048, max(32, remaining_tokens // 2)))
        payload = {
            "model": record.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        with httpx.Client(timeout=600) as client:
            response = client.post(
                f"{self.gateway_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        content = str(body["choices"][0]["message"]["content"])
        usage = body.get("usage") or {}
        prompt_tokens = int(
            usage.get(
                "prompt_tokens",
                max(1, sum(len(str(m.get("content", ""))) for m in messages) // 4),
            )
        )
        completion_tokens = int(
            usage.get("completion_tokens", max(1, len(content) // 4))
        )
        total_tokens = int(
            usage.get("total_tokens", prompt_tokens + completion_tokens)
        )
        return {"content": content}, {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

    def _reference_path(self, suite: str) -> Path:
        safe = suite.replace("/", "_").replace("\\", "_")
        root = self.data_root / "references"
        root.mkdir(parents=True, exist_ok=True)
        return root / f"{safe}.json"

    def _load_reference(self, suite: str) -> dict[str, Any] | None:
        path = self._reference_path(suite)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _reference_hash(self, reference: dict[str, Any]) -> str:
        return hashlib.sha256(canonical_json(reference)).hexdigest()

    def _compare_to_reference(
        self,
        reference: dict[str, Any] | None,
        case_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        current = {
            str(item.get("case")): str(item.get("status"))
            for item in case_results
            if item.get("case")
        }
        current_passed = sum(status == "passed" for status in current.values())
        if reference is None:
            return {
                "compatible": False,
                "reason": "no sealed reference established",
                "regressions": [],
                "improvements": [],
                "reference_passed": None,
                "current_passed": current_passed,
                "meets_reference": False,
            }
        old = {
            str(item.get("case")): str(item.get("status"))
            for item in reference.get("case_results", [])
            if item.get("case")
        }
        if set(old) != set(current):
            return {
                "compatible": False,
                "reason": "sealed case set differs from reference",
                "regressions": [],
                "improvements": [],
                "reference_passed": sum(v == "passed" for v in old.values()),
                "current_passed": current_passed,
                "meets_reference": False,
            }
        regressions = sorted(
            case for case, status in current.items()
            if old.get(case) == "passed" and status != "passed"
        )
        improvements = sorted(
            case for case, status in current.items()
            if old.get(case) != "passed" and status == "passed"
        )
        reference_passed = sum(status == "passed" for status in old.values())
        return {
            "compatible": True,
            "reason": "",
            "regressions": regressions,
            "improvements": improvements,
            "reference_passed": reference_passed,
            "current_passed": current_passed,
            "meets_reference": (
                not regressions and current_passed >= reference_passed
            ),
        }

    def set_reference(self, evaluation_id: str) -> dict[str, Any]:
        record = self.store.get(evaluation_id)
        if record.status not in {
            EvaluationStatus.PASSED,
            EvaluationStatus.FAILED,
        }:
            raise ValueError("reference source evaluation must be terminal")
        if record.candidate_commit != record.base_commit:
            raise ValueError(
                "sealed reference may only be established from candidate == base"
            )
        result = record.result or {}
        critical = result.get("critical_checks") or {}
        cases = result.get("case_results") or []
        if not critical or not all(bool(value) for value in critical.values()):
            raise ValueError("reference source has failing critical checks")
        if not cases:
            raise ValueError("reference source has no case results")
        reference = {
            "schema_version": 1,
            "suite": record.suite,
            "suite_hash": record.suite_hash,
            "model": record.model,
            "evaluation_id": record.id,
            "candidate_commit": record.candidate_commit,
            "base_commit": record.base_commit,
            "case_results": [
                {
                    "case": item.get("case"),
                    "status": item.get("status"),
                }
                for item in cases
            ],
            "passed_cases": sum(
                item.get("status") == "passed" for item in cases
            ),
            "total_cases": len(cases),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        path = self._reference_path(record.suite)
        path.write_text(json.dumps(reference, indent=2), encoding="utf-8")
        return {
            "reference": reference,
            "reference_hash": self._reference_hash(reference),
        }

    def get_reference(self, suite: str) -> dict[str, Any]:
        reference = self._load_reference(suite)
        if reference is None:
            raise KeyError("no sealed evaluator reference")
        return {
            "reference": reference,
            "reference_hash": self._reference_hash(reference),
        }

    def _sign_attestation(self, attestation: dict[str, Any]) -> str:
        return hmac.new(
            self.signing_key,
            canonical_json(attestation),
            hashlib.sha256,
        ).hexdigest()

    def verify_attestation(self, evaluation_id: str) -> dict[str, Any]:
        record = self.store.get(evaluation_id)
        if not record.attestation_path or not record.attestation_signature:
            raise KeyError("evaluation has no attestation")
        path = Path(record.attestation_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        expected = self._sign_attestation(payload)
        verified = hmac.compare_digest(expected, record.attestation_signature)
        return {
            "verified": verified,
            "signature": record.attestation_signature,
            "attestation": payload,
        }

    def latest_passing_for_run(self, run_id: str) -> dict[str, Any]:
        for record in self.store.by_run(run_id, limit=100):
            if record.status != EvaluationStatus.PASSED:
                continue
            try:
                verified = self.verify_attestation(record.id)
            except KeyError:
                continue
            if verified["verified"]:
                return {
                    "evaluation": record.model_dump(mode="json"),
                    **verified,
                }
        raise KeyError("no verified passing evaluation for run")

    def _execute(self, evaluation_id: str) -> None:
        started = time.monotonic()
        try:
            record = self.store.get(evaluation_id)
            with self._lock:
                cancel_event = self._cancel[evaluation_id]
            self.store.update(evaluation_id, status=EvaluationStatus.PREPARING)

            private_commit = self._candidate_ref_commit(record.run_id)
            if private_commit != record.candidate_commit:
                raise RuntimeError("candidate ref changed after evaluation creation")

            eval_root = self.exchange / "evaluations" / evaluation_id
            if eval_root.exists():
                shutil.rmtree(eval_root)
            candidate_src = eval_root / "candidate-src"
            model_requests = eval_root / "model" / "requests"
            model_results = eval_root / "model" / "results"
            model_requests.mkdir(parents=True, exist_ok=True)
            model_results.mkdir(parents=True, exist_ok=True)
            try:
                model_requests.chmod(0o733)
                model_results.chmod(0o755)
            except OSError:
                pass
            self._export_candidate(record.candidate_commit, candidate_src)
            capability = secrets.token_urlsafe(24)

            runner_request = {
                "evaluation_id": evaluation_id,
                "candidate_src": str(candidate_src),
                "cases": get_suite(record.suite),
                "model_capability": capability,
            }
            runner_path = (
                self.exchange / "runner" / "requests" / f"{evaluation_id}.json"
            )
            temp = runner_path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(runner_request), encoding="utf-8")
            temp.replace(runner_path)
            self.store.update(evaluation_id, status=EvaluationStatus.RUNNING)

            usage = {
                "request_count": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
            handled: set[str] = set()
            runner_result = (
                self.exchange / "runner" / "results" / f"{evaluation_id}.json"
            )
            deadline = started + record.wall_time_seconds

            while time.monotonic() < deadline:
                if cancel_event.is_set():
                    raise InterruptedError("evaluation cancellation requested")
                for request_path in sorted(model_requests.glob("*.json")):
                    if request_path.name in handled:
                        continue
                    handled.add(request_path.name)
                    request_payload = json.loads(
                        request_path.read_text(encoding="utf-8")
                    )
                    request_id = str(request_payload.get("request_id", ""))
                    result_path = model_results / f"{request_id}.json"
                    try:
                        if request_payload.get("capability") != capability:
                            raise PermissionError("invalid model capability")
                        if usage["request_count"] >= 100:
                            raise RuntimeError("model request limit exceeded")
                        remaining = record.token_budget - usage["total_tokens"]
                        if remaining <= 0:
                            raise RuntimeError("model token budget exhausted")
                        response_payload, request_usage = self._model_call(
                            record, request_payload, remaining
                        )
                        usage["request_count"] += 1
                        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                            usage[key] += request_usage[key]
                        if usage["total_tokens"] > record.token_budget:
                            response_payload = {
                                "error": "model token budget exceeded"
                            }
                    except Exception as exc:
                        response_payload = {"error": str(exc)}
                    out_temp = result_path.with_suffix(".json.tmp")
                    out_temp.write_text(
                        json.dumps(response_payload), encoding="utf-8"
                    )
                    out_temp.replace(result_path)

                if runner_result.exists():
                    runner_payload = json.loads(
                        runner_result.read_text(encoding="utf-8")
                    )
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError("evaluation wall-time budget exceeded")

            critical = dict(runner_payload.get("critical_checks") or {})
            critical["model_token_budget_respected"] = (
                usage["total_tokens"] <= record.token_budget
            )
            case_results = list(runner_payload.get("case_results") or [])
            all_critical = bool(critical) and all(bool(v) for v in critical.values())
            reference = self._load_reference(record.suite)
            reference_compatible = (
                reference is not None
                and reference.get("base_commit") == record.base_commit
                and reference.get("suite_hash") == record.suite_hash
                and reference.get("model") == record.model
            )
            comparison = self._compare_to_reference(
                reference if reference_compatible else None,
                case_results,
            )
            if reference is not None and not reference_compatible:
                comparison["reason"] = (
                    "sealed reference does not match candidate base, suite, or model"
                )
            passed = (
                bool(case_results)
                and all_critical
                and bool(comparison.get("meets_reference"))
            )
            final_status = (
                EvaluationStatus.PASSED if passed else EvaluationStatus.FAILED
            )
            reference_hash = (
                self._reference_hash(reference)
                if reference_compatible and reference is not None
                else None
            )
            result = {
                "runner_status": runner_payload.get("status"),
                "case_results": case_results,
                "critical_checks": critical,
                "model_usage": usage,
                "passed_cases": sum(
                    item.get("status") == "passed" for item in case_results
                ),
                "total_cases": len(case_results),
                "comparison": comparison,
                "reference_hash": reference_hash,
                "duration_s": round(time.monotonic() - started, 3),
            }

            attestation = Attestation(
                evaluation_id=evaluation_id,
                run_id=record.run_id,
                candidate_commit=record.candidate_commit,
                base_commit=record.base_commit,
                evaluator_version=__version__,
                evaluator_source_hash=self.source_hash,
                suite=record.suite,
                suite_hash=record.suite_hash,
                model=record.model,
                status=final_status.value,
                created_at=datetime.now(timezone.utc).isoformat(),
                reference_hash=reference_hash,
                reference_evaluation_id=(
                    reference.get("evaluation_id")
                    if reference_compatible and reference is not None
                    else None
                ),
                comparison=comparison,
                case_results=case_results,
                critical_checks=critical,
                model_usage=usage,
                duration_s=result["duration_s"],
            ).model_dump(mode="json")
            signature = self._sign_attestation(attestation)
            attestation_path = (
                self.data_root / "attestations" / f"{evaluation_id}.json"
            )
            attestation_path.write_text(
                json.dumps(attestation, indent=2), encoding="utf-8"
            )
            self.store.update(
                evaluation_id,
                status=final_status,
                result=result,
                error=None if passed else "candidate-specific evaluation failed",
                attestation_path=str(attestation_path),
                attestation_signature=signature,
            )
        except InterruptedError as exc:
            self.store.update(
                evaluation_id,
                status=EvaluationStatus.CANCELLED,
                error=str(exc),
            )
        except Exception as exc:
            self.store.update(
                evaluation_id,
                status=EvaluationStatus.ERROR,
                error=str(exc),
            )
        finally:
            with self._lock:
                self._active.discard(evaluation_id)
                self._cancel.pop(evaluation_id, None)
