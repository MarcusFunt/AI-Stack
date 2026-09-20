from __future__ import annotations

import ast
import time
from pathlib import Path

from agent_lab.schemas import HarnessCheck, HarnessResult, TaskSpec
from .base import Harness, HarnessManifest


class PythonSyntaxHarness(Harness):
    manifest = HarnessManifest(
        id="python-syntax",
        version="0.1.0",
        task_types=("syntax", "python-syntax"),
        description="Parses Python source files and reports syntax errors.",
    )

    def applicability(self, task: TaskSpec, workspace: Path) -> float:
        if task.task_type in self.manifest.task_types:
            return 1.0
        return 0.15 if any(workspace.rglob("*.py")) else 0.0

    def execute(self, task: TaskSpec, workspace: Path) -> HarnessResult:
        started = time.monotonic()
        failures: list[str] = []
        checked = 0
        for path in sorted(workspace.rglob("*.py")):
            rel = path.relative_to(workspace)
            if ".agent_lab_holdout" in rel.parts or "__pycache__" in rel.parts:
                continue
            checked += 1
            try:
                ast.parse(path.read_text(encoding="utf-8"), filename=rel.as_posix())
            except (SyntaxError, UnicodeError, OSError) as exc:
                failures.append(f"{rel.as_posix()}: {exc}")
        duration = round(time.monotonic() - started, 3)
        passed = checked > 0 and not failures
        return HarnessResult(
            harness_id=self.manifest.id,
            harness_version=self.manifest.version,
            status="passed" if passed else "failed",
            checks=[
                HarnessCheck(
                    id="python-parse",
                    status="passed" if passed else "failed",
                    duration_s=duration,
                    message="\n".join(failures) if failures else f"parsed {checked} files",
                )
            ],
            metrics={"files_checked": checked, "syntax_errors": len(failures)},
        )
