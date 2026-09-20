from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_lab.benchmark_history import (
    compare_summaries,
    load_latest,
    load_reference,
    save_reference,
)
from agent_lab.harnesses import HarnessRegistry
from agent_lab.sandbox_client import SandboxClient
from agent_lab.schemas import Budget, TaskSpec
from agent_lab.worker import AgentRunner
from agent_lab.worker.model import ModelClient


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    objective: str
    files: dict[str, str]
    public_tests: str | None = None
    holdout_tests: str | None = None
    task_type: str = "python"
    harness: str = "python-unit"
    max_iterations: int = 3


CASES: tuple[BenchmarkCase, ...] = (
    BenchmarkCase(
        id="add-operator",
        objective="Fix add(a, b) so it returns the mathematical sum of a and b.",
        files={"calculator.py": "def add(a, b):\n    return a - b\n"},
        public_tests="""import unittest
from calculator import add

class Tests(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(add(2, 3), 5)
""",
        holdout_tests="""import unittest
from calculator import add

class Holdout(unittest.TestCase):
    def test_negative(self):
        self.assertEqual(add(-4, 7), 3)

    def test_zero(self):
        self.assertEqual(add(0, 0), 0)
""",
    ),
    BenchmarkCase(
        id="clamp-bounds",
        objective="Fix clamp(value, low, high) so values below low return low, values above high return high, and in-range values are unchanged.",
        files={
            "clamp_utils.py": "def clamp(value, low, high):\n    return min(low, max(high, value))\n"
        },
        public_tests="""import unittest
from clamp_utils import clamp

class Tests(unittest.TestCase):
    def test_inside(self):
        self.assertEqual(clamp(5, 0, 10), 5)

    def test_above(self):
        self.assertEqual(clamp(20, 0, 10), 10)
""",
        holdout_tests="""import unittest
from clamp_utils import clamp

class Holdout(unittest.TestCase):
    def test_below(self):
        self.assertEqual(clamp(-5, 0, 10), 0)

    def test_boundary(self):
        self.assertEqual(clamp(10, 0, 10), 10)
""",
    ),
    BenchmarkCase(
        id="mean-division",
        objective="Fix mean(values) so it returns the arithmetic mean as a float and raises ValueError for an empty input.",
        files={
            "stats.py": """def mean(values):
    if not values:
        raise ValueError("empty")
    return sum(values) // len(values)
"""
        },
        public_tests="""import unittest
from stats import mean

class Tests(unittest.TestCase):
    def test_fractional_mean(self):
        self.assertEqual(mean([1, 2]), 1.5)
""",
        holdout_tests="""import unittest
from stats import mean

class Holdout(unittest.TestCase):
    def test_negative(self):
        self.assertEqual(mean([-2, 1]), -0.5)

    def test_empty(self):
        with self.assertRaises(ValueError):
            mean([])
""",
    ),
    BenchmarkCase(
        id="normalize-email",
        objective="Fix normalize_email(value) so it removes surrounding whitespace and lowercases the address.",
        files={"text_utils.py": "def normalize_email(value):\n    return value.strip()\n"},
        public_tests="""import unittest
from text_utils import normalize_email

class Tests(unittest.TestCase):
    def test_case(self):
        self.assertEqual(normalize_email(" User@Example.COM "), "user@example.com")
""",
        holdout_tests="""import unittest
from text_utils import normalize_email

class Holdout(unittest.TestCase):
    def test_already_clean(self):
        self.assertEqual(normalize_email("a@b.dk"), "a@b.dk")
""",
    ),
    BenchmarkCase(
        id="is-even",
        objective="Fix is_even(value) so it returns True exactly when an integer is even.",
        files={"parity.py": "def is_even(value):\n    return value % 2 == 1\n"},
        public_tests="""import unittest
from parity import is_even

class Tests(unittest.TestCase):
    def test_even(self):
        self.assertTrue(is_even(8))
""",
        holdout_tests="""import unittest
from parity import is_even

class Holdout(unittest.TestCase):
    def test_odd(self):
        self.assertFalse(is_even(7))

    def test_negative_even(self):
        self.assertTrue(is_even(-4))
""",
    ),
    BenchmarkCase(
        id="safe-divide",
        objective="Fix safe_divide(a, b) so it returns a / b normally and returns 0.0 when b is zero instead of raising.",
        files={"division.py": "def safe_divide(a, b):\n    return a / b\n"},
        public_tests="""import unittest
from division import safe_divide

class Tests(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(safe_divide(5, 0), 0.0)
""",
        holdout_tests="""import unittest
from division import safe_divide

class Holdout(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(safe_divide(9, 3), 3)

    def test_negative(self):
        self.assertEqual(safe_divide(-4, 2), -2)
""",
    ),
    BenchmarkCase(
        id="first-or-none",
        objective="Fix first_or_none(items) so it returns the first element when present and None for an empty sequence.",
        files={"collections_utils.py": "def first_or_none(items):\n    return items[0]\n"},
        public_tests="""import unittest
from collections_utils import first_or_none

class Tests(unittest.TestCase):
    def test_empty(self):
        self.assertIsNone(first_or_none([]))
""",
        holdout_tests="""import unittest
from collections_utils import first_or_none

class Holdout(unittest.TestCase):
    def test_value(self):
        self.assertEqual(first_or_none([0, 2]), 0)

    def test_tuple(self):
        self.assertEqual(first_or_none(("x", "y")), "x")
""",
    ),
    BenchmarkCase(
        id="word-count",
        objective="Fix count_words(text) so it counts whitespace-separated words correctly even with repeated spaces, tabs, or surrounding whitespace.",
        files={"words.py": "def count_words(text):\n    return len(text.split(' '))\n"},
        public_tests="""import unittest
from words import count_words

class Tests(unittest.TestCase):
    def test_repeated_spaces(self):
        self.assertEqual(count_words("one   two"), 2)
""",
        holdout_tests="""import unittest
from words import count_words

class Holdout(unittest.TestCase):
    def test_tabs(self):
        self.assertEqual(count_words("one\\ttwo\\nthree"), 3)

    def test_empty(self):
        self.assertEqual(count_words("   "), 0)
""",
    ),
    BenchmarkCase(
        id="stable-dedupe",
        objective="Fix dedupe(items) so duplicate values are removed while preserving the order of first occurrence.",
        files={"dedupe.py": "def dedupe(items):\n    return list(set(items))\n"},
        public_tests="""import unittest
from dedupe import dedupe

class Tests(unittest.TestCase):
    def test_order(self):
        self.assertEqual(dedupe([3, 1, 3, 2]), [3, 1, 2])
""",
        holdout_tests="""import unittest
from dedupe import dedupe

class Holdout(unittest.TestCase):
    def test_strings(self):
        self.assertEqual(dedupe(["b", "a", "b", "c", "a"]), ["b", "a", "c"])

    def test_empty(self):
        self.assertEqual(dedupe([]), [])
""",
    ),
    BenchmarkCase(
        id="syntax-colon",
        objective="Repair the Python syntax error without changing the intended behavior of greet(name). It should return 'Hello, ' followed by the name.",
        files={"greeting.py": "def greet(name)\n    return 'Hello, ' + name\n"},
        task_type="python-syntax",
        harness="python-syntax",
        max_iterations=2,
    ),
    BenchmarkCase(
        id="create-slug-module",
        objective="Create slug_utils.py implementing slugify(text). It must lowercase text, trim surrounding whitespace, replace runs of whitespace with a single hyphen, remove punctuation, and collapse repeated hyphens.",
        files={},
        public_tests="""import unittest
from slug_utils import slugify

class Tests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello World"), "hello-world")
""",
        holdout_tests="""import unittest
from slug_utils import slugify

class Holdout(unittest.TestCase):
    def test_punctuation_and_spaces(self):
        self.assertEqual(slugify("  Hello,   World!  "), "hello-world")

    def test_existing_hyphen(self):
        self.assertEqual(slugify("Already-Slug"), "already-slug")
""",
    ),
    BenchmarkCase(
        id="create-backoff-module",
        objective="Create retry_policy.py implementing backoff_seconds(attempt, base=0.5, cap=8.0). Return min(cap, base * 2**attempt). Raise ValueError for a negative attempt.",
        files={},
        public_tests="""import unittest
from retry_policy import backoff_seconds

class Tests(unittest.TestCase):
    def test_growth(self):
        self.assertEqual(backoff_seconds(3), 4.0)
""",
        holdout_tests="""import unittest
from retry_policy import backoff_seconds

class Holdout(unittest.TestCase):
    def test_cap(self):
        self.assertEqual(backoff_seconds(10), 8.0)

    def test_custom_base(self):
        self.assertEqual(backoff_seconds(2, base=1.0, cap=10.0), 4.0)

    def test_negative(self):
        with self.assertRaises(ValueError):
            backoff_seconds(-1)
""",
    ),
    BenchmarkCase(
        id="create-and-repair",
        objective="Repair greet(name) so it returns the prefix from helpers.py followed by the original name. Create helpers.py with GREETING_PREFIX = 'Hello, ' and update greeting.py to use it without uppercasing the name.",
        files={
            "greeting.py": """def greet(name):
    return "Hello, " + name.upper()
"""
        },
        public_tests="""import unittest
from greeting import greet

class Tests(unittest.TestCase):
    def test_preserves_case(self):
        self.assertEqual(greet("Ada"), "Hello, Ada")
""",
        holdout_tests="""import unittest
from greeting import greet
from helpers import GREETING_PREFIX

class Holdout(unittest.TestCase):
    def test_helper_contract(self):
        self.assertEqual(GREETING_PREFIX, "Hello, ")

    def test_lowercase_name(self):
        self.assertEqual(greet("linus"), "Hello, linus")
""",
    ),
)


def _validate_cases(cases: list[BenchmarkCase]) -> None:
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"duplicate benchmark case id: {case.id}")
        seen.add(case.id)
        if case.public_tests:
            try:
                ast.parse(case.public_tests, filename=f"{case.id}/test_regression.py")
            except SyntaxError as exc:
                raise ValueError(
                    f"invalid public tests for {case.id}: {exc}"
                ) from exc
        if case.holdout_tests:
            try:
                ast.parse(
                    case.holdout_tests,
                    filename=f"{case.id}/.agent_lab_holdout/test_holdout.py",
                )
            except SyntaxError as exc:
                raise ValueError(
                    f"invalid holdout tests for {case.id}: {exc}"
                ) from exc


def _write_workspace(workspace: Path, case: BenchmarkCase) -> None:
    workspace.mkdir(parents=True, exist_ok=False)
    for rel, content in case.files.items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if case.public_tests:
        (workspace / "test_regression.py").write_text(case.public_tests, encoding="utf-8")
    if case.holdout_tests:
        hidden = workspace / ".agent_lab_holdout"
        hidden.mkdir()
        (hidden / "test_holdout.py").write_text(case.holdout_tests, encoding="utf-8")


def _case_result(
    case: BenchmarkCase,
    run_id: str,
    state: dict[str, Any],
    elapsed: float,
) -> dict[str, Any]:
    public = state.get("harness_result") or {}
    holdout = state.get("holdout_result") or {"status": "not-run"}
    return {
        "case": case.id,
        "run_id": run_id,
        "status": state.get("final_status", "failed"),
        "iterations": state.get("iteration", 0),
        "public_status": public.get("status"),
        "holdout_status": holdout.get("status"),
        "files_changed": sorted(set(state.get("applied_edits", []))),
        "error": state.get("error", ""),
        "duration_s": round(elapsed, 3),
    }


def run_suite(
    data_root: Path,
    gateway_url: str,
    api_key: str,
    model_name: str,
    selected: set[str] | None = None,
    max_cases: int | None = None,
    keep_workspaces: bool = False,
    fail_on_regression: bool = False,
) -> dict[str, Any]:
    sandbox = SandboxClient(data_root / "sandbox")
    if not sandbox.healthy():
        raise RuntimeError("Agent Lab sandbox is not healthy")
    registry = HarnessRegistry(sandbox=sandbox)
    model = ModelClient(gateway_url, api_key, model_name)

    cases = [case for case in CASES if not selected or case.id in selected]
    if max_cases is not None:
        cases = cases[:max_cases]
    if not cases:
        raise ValueError("no benchmark cases selected")
    _validate_cases(cases)

    previous = load_reference(data_root) or load_latest(data_root)
    full_case_ids = {case.id for case in CASES}
    is_full_suite = {case.id for case in cases} == full_case_ids
    suite_started = time.monotonic()
    results: list[dict[str, Any]] = []
    for case in cases:
        run_id = uuid.uuid4().hex
        run_root = data_root / "runs" / run_id
        workspace = run_root / "workspace"
        _write_workspace(workspace, case)
        task = TaskSpec(
            repository="ai-stack",
            task_type=case.task_type,
            objective=case.objective,
            required_harnesses=[case.harness],
            budget=Budget(
                max_iterations=case.max_iterations,
                wall_time_minutes=5,
                model_tokens=20_000,
            ),
        )
        started = time.monotonic()
        events: list[dict[str, Any]] = []
        try:
            runner = AgentRunner(
                task,
                workspace,
                registry,
                model,
                emit=lambda kind, payload: events.append(
                    {"kind": kind, "payload": payload}
                ),
            )
            state = runner.run(run_id)
            result = _case_result(
                case, run_id, state, time.monotonic() - started
            )
            result["events"] = events
        except Exception as exc:
            result = {
                "case": case.id,
                "run_id": run_id,
                "status": "error",
                "iterations": 0,
                "public_status": None,
                "holdout_status": None,
                "files_changed": [],
                "error": str(exc),
                "duration_s": round(time.monotonic() - started, 3),
                "events": events,
            }
        results.append(result)
        print(
            f"[{result['status'].upper():6}] {case.id:20} "
            f"iterations={result['iterations']} "
            f"public={result['public_status']} holdout={result['holdout_status']} "
            f"{result['duration_s']:.2f}s",
            flush=True,
        )
        if not keep_workspaces:
            shutil.rmtree(run_root, ignore_errors=True)

    passed = sum(item["status"] == "passed" for item in results)
    summary = {
        "suite": "agent-lab-core-v2",
        "model": model_name,
        "case_ids": [case.id for case in cases],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "duration_s": round(time.monotonic() - suite_started, 3),
        "cases": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "pass_rate": round(passed / len(results), 4),
        "results": results,
    }
    summary["comparison"] = compare_summaries(previous, summary)
    comparison = summary["comparison"]
    reference_ok = (
        is_full_suite
        and summary["failed"] == 0
        and (
            not comparison.get("compatible")
            or not comparison.get("regressions")
        )
    )
    summary["reference_updated"] = reference_ok
    out_root = data_root / "benchmarks"
    out_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = out_root / f"{stamp}-{model_name}.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out_root / "latest.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    if reference_ok:
        save_reference(data_root, summary)
    print(
        f"BENCHMARK {passed}/{len(results)} passed "
        f"({summary['pass_rate'] * 100:.1f}%) in {summary['duration_s']:.2f}s"
    )
    if comparison.get("compatible"):
        print(
            "COMPARE "
            f"regressions={len(comparison['regressions'])} "
            f"improvements={len(comparison['improvements'])} "
            f"pass_rate_delta={comparison['pass_rate_delta']:+.4f}"
        )
    print(f"RESULT {out}")
    if fail_on_regression and summary["failed"]:
        failed_cases = [
            item["case"] for item in results if item.get("status") != "passed"
        ]
        raise RuntimeError(
            "benchmark failures: " + ", ".join(failed_cases)
        )
    if fail_on_regression and comparison.get("regressions"):
        raise RuntimeError(
            "benchmark regressions: " + ", ".join(comparison["regressions"])
        )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Agent Lab's local repair benchmark")
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--keep-workspaces", action="store_true")
    parser.add_argument("--fail-on-regression", action="store_true")
    parser.add_argument("--model", default=os.environ.get("AGENT_LAB_MODEL", "local-fast"))
    args = parser.parse_args()
    run_suite(
        data_root=Path(os.environ.get("AGENT_LAB_DATA_ROOT", "/data")),
        gateway_url=os.environ.get("GATEWAY_URL", "http://gateway:8000"),
        api_key=os.environ["AI_API_KEY"],
        model_name=args.model,
        selected=set(args.cases) if args.cases else None,
        max_cases=args.max_cases,
        keep_workspaces=args.keep_workspaces,
        fail_on_regression=args.fail_on_regression,
    )


if __name__ == "__main__":
    main()
