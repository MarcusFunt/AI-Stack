from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    run_id: str
    objective: str
    task_type: str
    max_iterations: int
    iteration: int
    workspace: str
    repo_context: str
    selected_harness: str
    plan: str
    proposed_edits: list[dict[str, str]]
    applied_edits: list[str]
    harness_result: dict[str, Any]
    final_status: str
    error: str
