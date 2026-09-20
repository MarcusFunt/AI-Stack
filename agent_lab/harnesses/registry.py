from __future__ import annotations

from pathlib import Path

from agent_lab.schemas import TaskSpec
from .base import Harness
from .python_unit import PythonUnitHarness


class HarnessRegistry:
    def __init__(self, harnesses: list[Harness] | None = None, sandbox=None):
        items = harnesses or [PythonUnitHarness(sandbox=sandbox)]
        self._harnesses = {item.manifest.id: item for item in items}

    def list(self) -> list[dict]:
        return [
            {
                "id": harness.manifest.id,
                "version": harness.manifest.version,
                "task_types": list(harness.manifest.task_types),
                "description": harness.manifest.description,
                "destructive": harness.manifest.destructive,
            }
            for harness in self._harnesses.values()
        ]

    def get(self, harness_id: str) -> Harness:
        try:
            return self._harnesses[harness_id]
        except KeyError as exc:
            raise KeyError(f"unknown harness: {harness_id}") from exc

    def select(self, task: TaskSpec, workspace: Path) -> Harness:
        if len(task.required_harnesses) > 1:
            raise RuntimeError("multiple required harnesses are not supported in v0.1")
        if task.required_harnesses:
            required = self.get(task.required_harnesses[0])
            if task.allowed_harnesses and required.manifest.id not in task.allowed_harnesses:
                raise RuntimeError("required harness is not in allowed_harnesses")
            return required
        candidates = list(self._harnesses.values())
        if task.allowed_harnesses:
            allowed = set(task.allowed_harnesses)
            candidates = [h for h in candidates if h.manifest.id in allowed]
        if not candidates:
            raise RuntimeError("no eligible harnesses")
        ranked = sorted(
            candidates,
            key=lambda h: h.applicability(task, workspace),
            reverse=True,
        )
        return ranked[0]
