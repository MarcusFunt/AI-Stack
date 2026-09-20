from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .db import RunStore
from .harnesses import HarnessRegistry
from .schemas import RunRecord, RunStatus, TaskSpec
from .sandbox_client import SandboxClient
from .worker import AgentRunner
from .worker.model import ModelClient
from .worktrees import WorktreeManager


class AgentLabController:
    def __init__(self) -> None:
        source_repo = Path(os.environ.get("AGENT_LAB_SOURCE_REPO", "/repo"))
        data_root = Path(os.environ.get("AGENT_LAB_DATA_ROOT", "/data"))
        db_path = Path(os.environ.get("AGENT_LAB_DB", data_root / "agent-lab.sqlite3"))
        gateway_url = os.environ.get("GATEWAY_URL", "http://gateway:8000")
        api_key = os.environ.get("AI_API_KEY", "")
        model_name = os.environ.get("AGENT_LAB_MODEL", "local-fast")
        workers = int(os.environ.get("AGENT_LAB_MAX_WORKERS", "1"))

        self.store = RunStore(db_path)
        self.worktrees = WorktreeManager(source_repo, data_root)
        self.sandbox = SandboxClient(data_root / "sandbox")
        self.registry = HarnessRegistry(sandbox=self.sandbox)
        self.model = ModelClient(gateway_url, api_key, model_name)
        self.executor = ThreadPoolExecutor(max_workers=max(1, workers))
        self._active: set[str] = set()
        self._lock = threading.Lock()

    def health(self) -> dict[str, Any]:
        with self._lock:
            active = sorted(self._active)
        sandbox_ok = self.sandbox.healthy()
        return {
            "status": "ok" if sandbox_ok else "degraded",
            "service": "agent-lab",
            "version": "0.1.0",
            "active_runs": active,
            "sandbox": "ok" if sandbox_ok else "unavailable",
        }

    def create_run(self, task: TaskSpec, auto_start: bool = False) -> RunRecord:
        if task.repository != "ai-stack":
            raise ValueError(f"unknown repository alias: {task.repository}")
        run_id = uuid.uuid4().hex
        self.store.create_run(run_id, task)
        try:
            workspace, commit = self.worktrees.create(run_id, task.base_ref)
            run = self.store.update_run(
                run_id,
                status=RunStatus.READY,
                workspace=str(workspace),
                base_commit=commit,
            )
            self.store.add_event(
                run_id, "workspace_created",
                {"workspace": str(workspace), "base_commit": commit},
            )
        except Exception as exc:
            self.store.update_run(
                run_id, status=RunStatus.ERROR, error=f"workspace creation failed: {exc}"
            )
            self.store.add_event(run_id, "workspace_failed", {"error": str(exc)})
            raise
        if auto_start:
            self.start_run(run_id)
        return self.store.get_run(run_id)

    def start_run(self, run_id: str) -> RunRecord:
        run = self.store.get_run(run_id)
        if run.status != RunStatus.READY:
            raise ValueError(f"run {run_id} is {run.status.value}, expected ready")
        with self._lock:
            if run_id in self._active:
                raise ValueError(f"run {run_id} is already active")
            self._active.add(run_id)
        self.store.update_run(run_id, status=RunStatus.RUNNING, error=None)
        self.store.add_event(run_id, "run_started", {})
        self.executor.submit(self._execute, run_id)
        return self.store.get_run(run_id)

    def _execute(self, run_id: str) -> None:
        try:
            run = self.store.get_run(run_id)
            if not run.workspace:
                raise RuntimeError("run has no workspace")
            emit = lambda kind, payload: self.store.add_event(run_id, kind, payload)
            runner = AgentRunner(
                run.task, Path(run.workspace), self.registry, self.model, emit=emit
            )
            state = runner.run(run_id)
            final_status = state.get("final_status", "failed")
            result = {
                "final_status": final_status,
                "iterations": state.get("iteration", 0),
                "plan": state.get("plan", ""),
                "applied_edits": state.get("applied_edits", []),
                "harness_result": state.get("harness_result"),
                "error": state.get("error", ""),
            }
            selected = state.get("selected_harness")
            if final_status == "passed":
                candidate = self.worktrees.save_candidate(run.workspace, run_id)
                result["candidate_commit"] = candidate
                self.store.add_event(
                    run_id, "candidate_saved", {"candidate_commit": candidate}
                )
                status = RunStatus.PASSED
            else:
                status = RunStatus.FAILED
            self.store.update_run(
                run_id,
                status=status,
                selected_harness=selected,
                result=result,
                error=state.get("error") or None,
            )
        except Exception as exc:
            self.store.update_run(
                run_id, status=RunStatus.ERROR, error=f"execution failed: {exc}"
            )
            self.store.add_event(run_id, "run_error", {"error": str(exc)})
        finally:
            with self._lock:
                self._active.discard(run_id)

    def cancel_run(self, run_id: str) -> RunRecord:
        run = self.store.get_run(run_id)
        with self._lock:
            if run_id in self._active:
                raise ValueError("active-run cancellation is not implemented yet")
        if run.status != RunStatus.READY:
            raise ValueError(f"cannot cancel run in state {run.status.value}")
        self.store.update_run(run_id, status=RunStatus.CANCELLED)
        self.store.add_event(run_id, "run_cancelled", {})
        return self.store.get_run(run_id)

    def cleanup_workspace(self, run_id: str) -> RunRecord:
        run = self.store.get_run(run_id)
        with self._lock:
            if run_id in self._active:
                raise ValueError("cannot clean an active run")
        if run.status in {RunStatus.PREPARING, RunStatus.READY, RunStatus.RUNNING}:
            raise ValueError(f"cannot clean run in state {run.status.value}")
        self.worktrees.cleanup(run_id)
        self.store.update_run(run_id, workspace=None)
        self.store.add_event(run_id, "workspace_cleaned", {})
        return self.store.get_run(run_id)

    def list_harnesses(self) -> list[dict]:
        return self.registry.list()
