from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from agent_lab.harnesses import HarnessRegistry
from agent_lab.schemas import TaskSpec
from .context import collect_repository_context
from .patches import PatchError, apply_exact_edits
from .state import AgentState


EventSink = Callable[[str, dict[str, Any]], None]


class AgentRunner:
    def __init__(
        self,
        task: TaskSpec,
        workspace: Path,
        registry: HarnessRegistry,
        model: Any,
        emit: EventSink | None = None,
    ):
        self.task = task
        self.workspace = workspace
        self.registry = registry
        self.model = model
        self.emit = emit or (lambda _kind, _payload: None)
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node("inspect", self._inspect)
        graph.add_node("select_harness", self._select_harness)
        graph.add_node("baseline", self._evaluate)
        graph.add_node("propose", self._propose)
        graph.add_node("apply", self._apply)
        graph.add_node("evaluate", self._evaluate)
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
            {"evaluate": "evaluate", "finalize": "finalize"},
        )
        graph.add_conditional_edges(
            "evaluate", self._route_evaluation,
            {"propose": "propose", "finalize": "finalize"},
        )
        graph.add_edge("finalize", END)
        return graph.compile()

    def _inspect(self, state: AgentState) -> dict:
        context = collect_repository_context(self.workspace)
        self.emit("repository_inspected", {"context_chars": len(context)})
        return {"repo_context": context, "iteration": 0, "applied_edits": []}

    def _select_harness(self, state: AgentState) -> dict:
        harness = self.registry.select(self.task, self.workspace)
        self.emit("harness_selected", {"harness": harness.manifest.id})
        return {"selected_harness": harness.manifest.id}

    def _evaluate(self, state: AgentState) -> dict:
        harness_id = state["selected_harness"]
        harness = self.registry.get(harness_id)
        self.emit("harness_started", {"harness": harness_id})
        result = harness.execute(self.task, self.workspace).model_dump()
        self.emit(
            "harness_finished",
            {"harness": harness_id, "status": result["status"]},
        )
        return {"harness_result": result}

    def _propose(self, state: AgentState) -> dict:
        iteration = int(state.get("iteration", 0)) + 1
        context = collect_repository_context(self.workspace)
        self.emit("model_iteration_started", {"iteration": iteration})
        try:
            proposal = self.model.propose_patch(
                self.task.objective, context, state.get("harness_result")
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
            }
        except Exception as exc:
            self.emit("model_iteration_failed", {"iteration": iteration, "error": str(exc)})
            return {
                "iteration": iteration,
                "proposed_edits": [],
                "error": f"model proposal failed: {exc}",
            }

    def _apply(self, state: AgentState) -> dict:
        edits = state.get("proposed_edits", [])
        if not edits:
            return {}
        try:
            changed = apply_exact_edits(self.workspace, edits)
        except (PatchError, OSError, UnicodeError) as exc:
            self.emit("patch_rejected", {"error": str(exc)})
            return {"error": f"patch rejected: {exc}", "proposed_edits": []}
        applied = list(state.get("applied_edits", [])) + changed
        self.emit("patch_applied", {"files": changed})
        return {"applied_edits": applied}

    def _route_baseline(self, state: AgentState) -> str:
        if state["harness_result"]["status"] == "passed":
            return "finalize" if self.task.require_failing_baseline else "propose"
        return "propose"

    def _route_apply(self, state: AgentState) -> str:
        if state.get("error") or not state.get("proposed_edits"):
            return "finalize"
        return "evaluate"

    def _route_evaluation(self, state: AgentState) -> str:
        if state["harness_result"]["status"] == "passed":
            return "finalize"
        if int(state.get("iteration", 0)) >= self.task.budget.max_iterations:
            return "finalize"
        return "propose"

    def _finalize(self, state: AgentState) -> dict:
        passed = state.get("harness_result", {}).get("status") == "passed"
        baseline_only = int(state.get("iteration", 0)) == 0
        baseline_unproven = baseline_only and self.task.require_failing_baseline and passed
        error = state.get("error", "")
        if baseline_unproven:
            error = "baseline harness already passed; objective is not proven by a failing regression"
        status = "passed" if passed and not error else "failed"
        self.emit(
            "agent_finished",
            {"status": status, "iterations": state.get("iteration", 0)},
        )
        return {"final_status": status, "error": error}

    def run(self, run_id: str) -> AgentState:
        initial: AgentState = {
            "run_id": run_id,
            "objective": self.task.objective,
            "task_type": self.task.task_type,
            "max_iterations": self.task.budget.max_iterations,
            "workspace": str(self.workspace),
        }
        return self.graph.invoke(initial)
