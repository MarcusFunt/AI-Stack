"""Deterministic paired bootstrap confidence intervals for WER differences."""

from __future__ import annotations

import random
from typing import Any, Iterable


DEFAULT_BOOTSTRAP_SAMPLES = 5000
DEFAULT_BOOTSTRAP_SEED = 20260930


def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _wer(errors: int, reference_units: int) -> float:
    if reference_units:
        return errors / reference_units
    return 0.0 if errors == 0 else 1.0


def _draw_deltas(
    units: list[dict[str, Any]],
    model_a: str,
    model_b: str,
    *,
    samples: int,
    seed: int,
    group_key: str | None,
) -> list[float]:
    try:
        import numpy as np
    except ImportError:
        np = None

    rng = random.Random(seed) if np is None else np.random.default_rng(seed)
    if group_key:
        groups: dict[str, list[int]] = {}
        for index, unit in enumerate(units):
            value = str(unit.get(group_key, "")).strip()
            if not value and group_key == "bootstrap_group":
                value = str(unit.get("source_recording", "")).strip()
            if value:
                groups.setdefault(value, []).append(index)
        group_values = list(groups.values())
    else:
        group_values = []

    error_a = [int(unit[model_a]["errors"]) for unit in units]
    ref_a = [int(unit[model_a]["reference_units"]) for unit in units]
    error_b = [int(unit[model_b]["errors"]) for unit in units]
    ref_b = [int(unit[model_b]["reference_units"]) for unit in units]
    if np is not None:
        error_a, ref_a = np.asarray(error_a), np.asarray(ref_a)
        error_b, ref_b = np.asarray(error_b), np.asarray(ref_b)

    deltas = []
    unit_count = len(units)
    for _ in range(samples):
        if group_values:
            picks = (
                [rng.choice(group_values) for _ in range(len(group_values))]
                if np is None
                else [group_values[i] for i in rng.integers(0, len(group_values), len(group_values))]
            )
            indexes = [index for group in picks for index in group]
        elif np is None:
            indexes = [rng.randrange(unit_count) for _ in range(unit_count)]
        else:
            indexes = rng.integers(0, unit_count, unit_count)

        if np is None:
            errors_a = sum(error_a[index] for index in indexes)
            refs_a = sum(ref_a[index] for index in indexes)
            errors_b = sum(error_b[index] for index in indexes)
            refs_b = sum(ref_b[index] for index in indexes)
        else:
            errors_a = int(error_a[indexes].sum())
            refs_a = int(ref_a[indexes].sum())
            errors_b = int(error_b[indexes].sum())
            refs_b = int(ref_b[indexes].sum())
        deltas.append(_wer(errors_a, refs_a) - _wer(errors_b, refs_b))
    return deltas


def paired_bootstrap_wer(
    units: Iterable[dict[str, Any]],
    model_a: str,
    model_b: str,
    *,
    samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    group_key: str = "bootstrap_group",
) -> dict[str, Any]:
    """Return paired unit and optional source-group bootstrap deltas.

    Each unit maps both model names to `{errors, reference_units}`. The sampled
    rows are shared by both models. Delta is always WER(A) minus WER(B).
    """
    if samples < DEFAULT_BOOTSTRAP_SAMPLES:
        raise ValueError(f"samples must be at least {DEFAULT_BOOTSTRAP_SAMPLES}")
    selected = list(units)
    if not selected:
        raise ValueError("paired bootstrap needs at least one unit")
    for unit in selected:
        if model_a not in unit or model_b not in unit:
            raise ValueError("paired bootstrap requires both models on every unit")
        for model in (model_a, model_b):
            if int(unit[model]["reference_units"]) < 0:
                raise ValueError("paired bootstrap reference units cannot be negative")

    total_error_a = sum(int(unit[model_a]["errors"]) for unit in selected)
    total_ref_a = sum(int(unit[model_a]["reference_units"]) for unit in selected)
    total_error_b = sum(int(unit[model_b]["errors"]) for unit in selected)
    total_ref_b = sum(int(unit[model_b]["reference_units"]) for unit in selected)
    if total_ref_a <= 0 or total_ref_b <= 0:
        raise ValueError("paired bootstrap requires positive total reference units")
    point_delta = _wer(total_error_a, total_ref_a) - _wer(total_error_b, total_ref_b)

    deltas = _draw_deltas(
        selected, model_a, model_b, samples=samples, seed=seed, group_key=None
    )
    result: dict[str, Any] = {
        "model_a": model_a,
        "model_b": model_b,
        "unit_count": len(selected),
        "bootstrap_samples": samples,
        "seed": seed,
        "wer_a": _wer(total_error_a, total_ref_a),
        "wer_b": _wer(total_error_b, total_ref_b),
        "delta_wer": point_delta,
        "ci95": [_percentile(deltas, 0.025), _percentile(deltas, 0.975)],
    }
    result["ci_includes_zero"] = result["ci95"][0] <= 0 <= result["ci95"][1]

    group_types = {
        str(unit.get("bootstrap_group_type", "")).strip()
        for unit in selected if str(unit.get("bootstrap_group_type", "")).strip()
    }
    groups = {str(unit.get(group_key, "")).strip() for unit in selected}
    if group_key == "bootstrap_group":
        groups = {
            value or str(unit.get("source_recording", "")).strip()
            for unit in selected
            for value in [str(unit.get(group_key, "")).strip()]
        }
    groups.discard("")
    if 0 < len(groups) < len(selected):
        group_type = next(iter(group_types)) if len(group_types) == 1 else (
            "mixed" if group_types else "source_recording"
        )
        if len(groups) < 2:
            result["group_bootstrap"] = {
                "group_key": group_key,
                "group_type": group_type,
                "group_count": len(groups),
                "status": "insufficient_groups",
                "bootstrap_samples": samples,
                "seed": seed + 1,
                "ci95": None,
            }
            return result
        group_deltas = _draw_deltas(
            selected,
            model_a,
            model_b,
            samples=samples,
            seed=seed + 1,
            group_key=group_key,
        )
        result["group_bootstrap"] = {
            "group_key": group_key,
            "group_type": next(iter(group_types)) if len(group_types) == 1 else (
                "mixed" if group_types else "source_recording"
            ),
            "group_count": len(groups),
            "status": "scored",
            "bootstrap_samples": samples,
            "seed": seed + 1,
            "ci95": [
                _percentile(group_deltas, 0.025),
                _percentile(group_deltas, 0.975),
            ],
        }
    else:
        result["group_bootstrap"] = {
            "group_key": group_key,
            "group_type": next(iter(group_types)) if len(group_types) == 1 else (
                "mixed" if group_types else "source_recording"
            ),
            "group_count": len(groups),
            "status": "independent_units",
            "bootstrap_samples": samples,
            "seed": seed + 1,
            "ci95": None,
        }
    return result
