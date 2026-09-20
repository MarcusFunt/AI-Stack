from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def benchmark_root(data_root: str | Path) -> Path:
    return Path(data_root) / "benchmarks"


def _model_key(model: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in model)
    return cleaned or "unknown-model"


def _model_state_root(data_root: str | Path, model: str) -> Path:
    return benchmark_root(data_root) / "models" / _model_key(model)


def _load_path(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _load_named(data_root: str | Path, name: str) -> dict[str, Any] | None:
    return _load_path(benchmark_root(data_root) / name)


def _load_model_named(
    data_root: str | Path,
    model: str,
    name: str,
) -> dict[str, Any] | None:
    payload = _load_path(_model_state_root(data_root, model) / name)
    if payload is not None:
        return payload
    legacy = _load_named(data_root, name)
    if legacy is not None and legacy.get("model") == model:
        return legacy
    return None


def load_latest(
    data_root: str | Path,
    model: str | None = None,
) -> dict[str, Any] | None:
    if model is None:
        return _load_named(data_root, "latest.json")
    return _load_model_named(data_root, model, "latest.json")


def save_latest(data_root: str | Path, summary: dict[str, Any]) -> Path:
    model = str(summary.get("model") or "unknown-model")
    root = _model_state_root(data_root, model)
    root.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(summary, indent=2)
    path = root / "latest.json"
    path.write_text(payload, encoding="utf-8")
    global_latest = benchmark_root(data_root) / "latest.json"
    global_latest.parent.mkdir(parents=True, exist_ok=True)
    global_latest.write_text(payload, encoding="utf-8")
    return path


def load_reference(
    data_root: str | Path,
    model: str | None = None,
) -> dict[str, Any] | None:
    if model is None:
        return _load_named(data_root, "reference.json")
    return _load_model_named(data_root, model, "reference.json")


def save_reference(data_root: str | Path, summary: dict[str, Any]) -> Path:
    model = str(summary.get("model") or "unknown-model")
    root = _model_state_root(data_root, model)
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

    previous_ids = set(_case_ids(previous))
    current_ids = set(_case_ids(current))
    compatibility = (
        previous.get("suite") == current.get("suite")
        and previous.get("model") == current.get("model")
        and bool(current_ids)
        and current_ids.issubset(previous_ids)
    )
    if not compatibility:
        return {
            "compatible": False,
            "reason": "suite/model differs or previous benchmark does not cover current cases",
            "regressions": [],
            "improvements": [],
        }

    old = {
        str(item["case"]): item
        for item in previous.get("results", [])
        if isinstance(item, dict) and item.get("case") in current_ids
    }
    new = {
        str(item["case"]): item
        for item in current.get("results", [])
        if isinstance(item, dict) and item.get("case")
    }
    if set(old) != current_ids or set(new) != current_ids:
        return {
            "compatible": False,
            "reason": "benchmark results are missing one or more selected cases",
            "regressions": [],
            "improvements": [],
        }

    regressions = sorted(
        case
        for case in current_ids
        if old[case].get("status") == "passed"
        and new[case].get("status") != "passed"
    )
    improvements = sorted(
        case
        for case in current_ids
        if old[case].get("status") != "passed"
        and new[case].get("status") == "passed"
    )
    old_passed = sum(old[case].get("status") == "passed" for case in current_ids)
    new_passed = sum(new[case].get("status") == "passed" for case in current_ids)
    old_rate = old_passed / len(current_ids)
    new_rate = new_passed / len(current_ids)
    exact_case_set = previous_ids == current_ids
    old_duration = float(previous.get("duration_s", 0.0) or 0.0)
    new_duration = float(current.get("duration_s", 0.0) or 0.0)
    return {
        "compatible": True,
        "reason": "",
        "scope": "exact" if exact_case_set else "subset",
        "regressions": regressions,
        "improvements": improvements,
        "pass_rate_delta": round(new_rate - old_rate, 4),
        "duration_delta_s": (
            round(new_duration - old_duration, 3) if exact_case_set else None
        ),
        "no_regressions": not regressions,
    }
