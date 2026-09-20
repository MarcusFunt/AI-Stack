from __future__ import annotations

import subprocess
import time
from pathlib import Path

from agent_lab.schemas import HarnessCheck, HarnessResult, TaskSpec
from .base import Harness, HarnessManifest


class PythonUnitHarness(Harness):
    def __init__(self, sandbox=None):
        self.sandbox = sandbox

    manifest = HarnessManifest(
        id="python-unit",
        version="0.1.0",
        task_types=("python", "bugfix", "general"),
        description="Runs Python unittest discovery in the isolated workspace.",
    )

    def applicability(self, task: TaskSpec, workspace: Path) -> float:
        score = 0.25
        if task.task_type in self.manifest.task_types:
            score += 0.4
        if any(workspace.rglob("*.py")):
            score += 0.25
        if (workspace / "tests").exists():
            score += 0.1
        return min(score, 1.0)

    def execute(self, task: TaskSpec, workspace: Path) -> HarnessResult:
        timeout_s = min(task.budget.wall_time_minutes * 60, 600)
        if self.sandbox is not None:
            run_id = workspace.parent.name
            payload = self.sandbox.python_unit(run_id, timeout_s)
            duration = float(payload.get("duration_s", 0.0))
            output = str(payload.get("output", ""))[-20_000:]
            returncode = int(payload.get("returncode", 1))
        else:
            started = time.monotonic()
            proc = subprocess.run(
                ["python", "-m", "unittest", "discover", "-v"],
                cwd=workspace,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout_s,
            )
            duration = round(time.monotonic() - started, 3)
            output = proc.stdout[-20_000:]
            returncode = proc.returncode
        no_tests = "Ran 0 tests" in output
        passed = returncode == 0 and not no_tests
        check = HarnessCheck(
            id="unittest",
            status="passed" if passed else "failed",
            duration_s=duration,
            message=output,
        )
        return HarnessResult(
            harness_id=self.manifest.id,
            harness_version=self.manifest.version,
            status="passed" if passed else "failed",
            checks=[check],
            metrics={"returncode": returncode, "duration_s": duration},
        )

    def execute_holdout(self, task: TaskSpec, workspace: Path) -> HarnessResult | None:
        holdout_dir = workspace / ".agent_lab_holdout"
        if not holdout_dir.is_dir():
            return None
        if self.sandbox is None:
            return None
        timeout_s = min(task.budget.wall_time_minutes * 60, 600)
        payload = self.sandbox.python_unit(
            workspace.parent.name, timeout_s, suite="holdout"
        )
        duration = float(payload.get("duration_s", 0.0))
        output = str(payload.get("output", ""))[-20_000:]
        returncode = int(payload.get("returncode", 1))
        no_tests = "Ran 0 tests" in output
        passed = returncode == 0 and not no_tests
        return HarnessResult(
            harness_id=self.manifest.id,
            harness_version=self.manifest.version,
            status="passed" if passed else "failed",
            checks=[
                HarnessCheck(
                    id="holdout-unittest",
                    status="passed" if passed else "failed",
                    duration_s=duration,
                    message=output,
                )
            ],
            metrics={"returncode": returncode, "duration_s": duration, "holdout": True},
        )
