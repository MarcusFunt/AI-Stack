from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def benchmark_root(data_root: str | Path) -> Path:
    return Path(data_root) / "benchmarks"


def _load_named(data_root: str | Path, name: str) -> dict[str, Any] | None:
    path = benchmark_root(data_root) / name
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_latest(data_root: str | Path) -> dict[str, Any] | None:
    return _load_named(data_root, "latest.json")


def load_reference(data_root: str | Path) -> dict[str, Any] | None:
    return _load_named(data_root, "reference.json")


def save_reference(data_root: str | Path, summary: dict[str, Any]) -> Path:
    root = benchmark_root(data_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "reference.json"
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return path


def list_history(data_root: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    root = benchmark_root(data_root)
    if not root.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json"), reverse=True):
        if path.name in {"latest.json", "reference.json"}:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items.append(
            {
                "file": path.name,
                "suite": payload.get("suite"),
                "model": payload.get("model"),
                "started_at": payload.get("started_at"),
                "cases": payload.get("cases"),
                "passed": payload.get("passed"),
                "failed": payload.get("failed"),
                "pass_rate": payload.get("pass_rate"),
                "duration_s": payload.get("duration_s"),
                "comparison": payload.get("comparison"),
            }
        )
        if len(items) >= limit:
            break
    return items


def _case_ids(summary: dict[str, Any]) -> list[str]:
    explicit = summary.get("case_ids")
    if isinstance(explicit, list):
        return sorted(str(item) for item in explicit)
    results = summary.get("results")
    if not isinstance(results, list):
        return []
    return sorted(
        str(item.get("case"))
        for item in results
        if isinstance(item, dict) and item.get("case")
    )


def compare_summaries(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> dict[str, Any]:
    if previous is None:
        return {
            "compatible": False,
            "reason": "no previous benchmark",
            "regressions": [],
            "improvements": [],
        }

    compatibility = (
        previous.get("suite") == current.get("suite")
        and previous.get("model") == current.get("model")
        and _case_ids(previous) == _case_ids(current)
        and bool(_case_ids(current))
    )
    if not compatibility:
        return {
            "compatible": False,
            "reason": "suite, model, or case set differs",
            "regressions": [],
            "improvements": [],
        }

    old = {
        str(item["case"]): item
        for item in previous.get("results", [])
        if isinstance(item, dict) and item.get("case")
    }
    new = {
        str(item["case"]): item
        for item in current.get("results", [])
        if isinstance(item, dict) and item.get("case")
    }
    regressions = sorted(
        case
        for case in new
        if old.get(case, {}).get("status") == "passed"
        and new[case].get("status") != "passed"
    )
    improvements = sorted(
        case
        for case in new
        if old.get(case, {}).get("status") != "passed"
        and new[case].get("status") == "passed"
    )
    old_rate = float(previous.get("pass_rate", 0.0) or 0.0)
    new_rate = float(current.get("pass_rate", 0.0) or 0.0)
    old_duration = float(previous.get("duration_s", 0.0) or 0.0)
    new_duration = float(current.get("duration_s", 0.0) or 0.0)
    return {
        "compatible": True,
        "reason": "",
        "regressions": regressions,
        "improvements": improvements,
        "pass_rate_delta": round(new_rate - old_rate, 4),
        "duration_delta_s": round(new_duration - old_duration, 3),
        "no_regressions": not regressions,
    }
