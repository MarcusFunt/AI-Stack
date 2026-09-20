from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from agent_lab.schemas import HarnessResult, TaskSpec


@dataclass(frozen=True)
class HarnessManifest:
    id: str
    version: str
    task_types: tuple[str, ...]
    description: str
    destructive: bool = False


class Harness(ABC):
    manifest: HarnessManifest

    @abstractmethod
    def applicability(self, task: TaskSpec, workspace: Path) -> float:
        raise NotImplementedError

    @abstractmethod
    def execute(self, task: TaskSpec, workspace: Path) -> HarnessResult:
        raise NotImplementedError

    def execute_holdout(self, task: TaskSpec, workspace: Path) -> HarnessResult | None:
        return None
