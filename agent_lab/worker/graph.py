from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from agent_lab.harnesses import HarnessRegistry
from agent_lab.schemas import TaskSpec
from .context import collect_repository_context
from .patches import PatchError, apply_exact_edits
from .state import AgentState


EventSink = Callable[[str, dict[str, Any]], None]


class AgentCancelled(RuntimeError):
    pass


class AgentBudgetExceeded(RuntimeError):
    pass


class AgentRunner:
    def __init__(
        self,
        task: TaskSpec,
        workspace: Path,
        registry: HarnessRegistry,
        model: Any,
        emit: EventSink | None = None,
        cancelled: Callable[[], bool] | None = None,
    ):
        self.task = task
        self.workspace = workspace
        self.registry = registry
        self.model = model
        self.emit = emit or (lambda _kind, _payload: None)
        self.cancelled = cancelled or (lambda: False)
        self.started_at = time.monotonic()
        self.graph = self._build_graph()

    def _remaining_wall_time_seconds(self) -> float:
        elapsed = time.monotonic() - self.started_at
        return max(0.0, self.task.budget.wall_time_minutes * 60 - elapsed)

    def _guard(self) -> None:
        if self.cancelled():
            raise AgentCancelled("run cancellation requested")
        if self._remaining_wall_time_seconds() <= 0:
            raise AgentBudgetExceeded("run wall-time budget exceeded")

    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node("inspect", self._inspect)
        graph.add_node("select_harness", self._select_harness)
        graph.add_node("baseline", self._evaluate)
        graph.add_node("propose", self._propose)
        graph.add_node("apply", self._apply)
        graph.add_node("evaluate", self._evaluate)
        graph.add_node("holdout", self._holdout)
        graph.add_node("finalize", self._finalize)
        graph.add_edge(START, "inspect")
        graph.add_edge("inspect", "select_harness")
        graph.add_edge("select_harness", "baseline")
        graph.add_conditional_edges(
            "baseline", self._route_baseline,
            {"propose": "propose", "finalize": "finalize"},
        )
        graph.add_edge("propose", "apply")
        graph.add_conditional_edges(
            "apply", self._route_apply,
            {
                "propose": "propose",
                "evaluate": "evaluate",
                "finalize": "finalize",
            },
        )
        graph.add_conditional_edges(
            "evaluate", self._route_evaluation,
            {"propose": "propose", "holdout": "holdout", "finalize": "finalize"},
        )
        graph.add_edge("holdout", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile()

    def _inspect(self, state: AgentState) -> dict:
        self._guard()
        context = collect_repository_context(self.workspace)
        self.emit("repository_inspected", {"context_chars": len(context)})
        return {"repo_context": context, "iteration": 0, "applied_edits": []}

    def _select_harness(self, state: AgentState) -> dict:
        self._guard()
        harness = self.registry.select(self.task, self.workspace)
        self.emit("harness_selected", {"harness": harness.manifest.id})
        return {"selected_harness": harness.manifest.id}

    def _evaluate(self, state: AgentState) -> dict:
        self._guard()
        harness_id = state["selected_harness"]
        harness = self.registry.get(harness_id)
        self.emit("harness_started", {"harness": harness_id})
        result = harness.execute(self.task, self.workspace).model_dump()
        self.emit(
            "harness_finished",
            {"harness": harness_id, "status": result["status"]},
        )
        return {"harness_result": result}

    def _holdout(self, state: AgentState) -> dict:
        self._guard()
        harness_id = state["selected_harness"]
        harness = self.registry.get(harness_id)
        self.emit("holdout_started", {"harness": harness_id})
        result = harness.execute_holdout(self.task, self.workspace)
        if result is None:
            payload = {"status": "skipped"}
            self.emit("holdout_finished", payload)
            return {"holdout_result": payload}
        payload = result.model_dump()
        self.emit("holdout_finished", {"status": payload["status"]})
        return {"holdout_result": payload}

    def _propose(self, state: AgentState) -> dict:
        self._guard()
        iteration = int(state.get("iteration", 0)) + 1
        context = collect_repository_context(self.workspace)
        remaining = self._remaining_wall_time_seconds()
        self.emit(
            "model_iteration_started",
            {"iteration": iteration, "remaining_wall_time_s": round(remaining, 3)},
        )
        try:
            proposal = self.model.propose_patch(
                self.task.objective,
                context,
                state.get("harness_result"),
                timeout_seconds=remaining,
            )
            edits = proposal.get("edits", [])
            summary = str(proposal.get("summary", ""))
            self.emit(
                "patch_proposed",
                {"iteration": iteration, "edit_count": len(edits), "summary": summary},
            )
            return {
                "iteration": iteration,
                "repo_context": context,
                "plan": summary,
                "proposed_edits": edits,
                "error": "",
                "budget_exhausted": False,
            }
        except Exception as exc:
            exhausted = self._remaining_wall_time_seconds() <= 0
            message = f"model proposal failed: {exc}"
            if exhausted:
                message += "; run wall-time budget exhausted"
            self.emit(
                "model_iteration_failed",
                {
                    "iteration": iteration,
                    "error": str(exc),
                    "budget_exhausted": exhausted,
                },
            )
            return {
                "iteration": iteration,
                "proposed_edits": [],
                "error": message,
                "budget_exhausted": exhausted,
            }

    def _apply(self, state: AgentState) -> dict:
        edits = state.get("proposed_edits", [])
        if state.get("budget_exhausted") or not edits:
            return {}
        self._guard()
        try:
            changed = apply_exact_edits(
                self.workspace,
                edits,
                allow_test_edits=self.task.allow_test_edits,
            )
        except (PatchError, OSError, UnicodeError) as exc:
            message = f"patch rejected: {exc}"
            self.emit("patch_rejected", {"error": str(exc)})
            return {
                "error": message,
                "proposed_edits": [],
                "harness_result": {
                    "status": "failed",
                    "checks": [
                        {
                            "id": "patch-application",
                            "status": "failed",
                            "message": message,
                        }
                    ],
                },
            }
        applied = list(state.get("applied_edits", [])) + changed
        self.emit("patch_applied", {"files": changed})
        return {"applied_edits": applied}

    def _route_baseline(self, state: AgentState) -> str:
        if state["harness_result"]["status"] == "passed":
            return "finalize" if self.task.require_failing_baseline else "propose"
        return "propose"

    def _route_apply(self, state: AgentState) -> str:
        if state.get("budget_exhausted"):
            return "finalize"
        if state.get("error") or not state.get("proposed_edits"):
            if int(state.get("iteration", 0)) < self.task.budget.max_iterations:
                return "propose"
            return "finalize"
        return "evaluate"

    def _route_evaluation(self, state: AgentState) -> str:
        if state["harness_result"]["status"] == "passed":
            return "holdout"
        if int(state.get("iteration", 0)) >= self.task.budget.max_iterations:
            return "finalize"
        return "propose"

    def _finalize(self, state: AgentState) -> dict:
        passed = state.get("harness_result", {}).get("status") == "passed"
        baseline_only = int(state.get("iteration", 0)) == 0
        baseline_unproven = baseline_only and self.task.require_failing_baseline and passed
        holdout = state.get("holdout_result", {})
        holdout_failed = holdout.get("status") == "failed"
        error = state.get("error", "")
        if state.get("budget_exhausted") and not error:
            error = "run wall-time budget exhausted"
        if baseline_unproven:
            error = "baseline harness already passed; objective is not proven by a failing regression"
        elif holdout_failed:
            error = "candidate passed visible checks but failed hidden holdout validation"
        status = "passed" if passed and not holdout_failed and not error else "failed"
        self.emit(
            "agent_finished",
            {"status": status, "iterations": state.get("iteration", 0)},
        )
        return {"final_status": status, "error": error}

    def run(self, run_id: str) -> AgentState:
        self.started_at = time.monotonic()
        initial: AgentState = {
            "run_id": run_id,
            "objective": self.task.objective,
            "task_type": self.task.task_type,
            "max_iterations": self.task.budget.max_iterations,
            "workspace": str(self.workspace),
        }
        return self.graph.invoke(initial)
