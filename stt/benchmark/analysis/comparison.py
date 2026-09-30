"""Dataset-aware summary and paired comparison table construction."""

from __future__ import annotations

import itertools
from typing import Any

from .bootstrap import DEFAULT_BOOTSTRAP_SAMPLES, DEFAULT_BOOTSTRAP_SEED, paired_bootstrap_wer


def dataset_info(result: dict[str, Any]) -> dict[str, Any]:
    info = result.get("dataset")
    if isinstance(info, dict):
        return info
    return {"name": "private-manifest", "class": "PRIVATE-USER-PROVIDED"}


def dataset_summary_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    dataset = dataset_info(result)
    recordings = result.get("recordings", [])
    speaker_ids = {
        str(speaker)
        for recording in recordings
        for speaker in recording.get("reference_speakers", [])
        if str(speaker).strip()
    }
    declared_speaker_count = max(
        [int(recording.get("speaker_count", 0)) for recording in recordings] or [0]
    )
    speaker_count = len(speaker_ids) if speaker_ids else declared_speaker_count
    model_rows = []
    for alias, model in result.get("models", {}).items():
        per_recording = model.get("per_recording", {})
        model_rows.append({
            "dataset": dataset.get("name", "unknown"),
            "dataset_class": dataset.get("class", "unknown"),
            "model": alias,
            "total_audio_s": result.get("total_audio_s"),
            "recording_count": len(recordings),
            "speaker_count": speaker_count,
            "reference_word_count": model.get("content_reference_words"),
            "content_wer": model.get("content_wer"),
            "verbatim_wer": model.get("verbatim_wer"),
            "cer": model.get("cer"),
            "speaker_attributed_wer": model.get("speaker_attributed_wer"),
            "rtf": model.get("rtf"),
            "failure_count": model.get("failure_count", 0),
            "oom_count": model.get("oom_count", 0),
            "empty_output_count": model.get(
                "empty_output_count",
                sum(not str(row.get("hypothesis", "")).strip() for row in per_recording.values()),
            ),
            "hallucination_on_silence_count": model.get("hallucination_on_silence_count"),
            "peak_vram_mb": model.get("peak_vram_mb"),
            "global_der": result.get("diarization", {}).get("global_der_collar_025"),
            "macro_der": result.get("diarization", {}).get("macro_der_collar_025"),
            "strict_der": result.get("diarization", {}).get("global_der_collar_0"),
            "miss_s": result.get("diarization", {}).get("miss_s_collar_025"),
            "false_alarm_s": result.get("diarization", {}).get("false_alarm_s_collar_025"),
            "confusion_s": result.get("diarization", {}).get("confusion_s_collar_025"),
            "speaker_count_accuracy": result.get("diarization", {}).get("speaker_count_accuracy"),
            "overlap_region_der": result.get("diarization", {}).get("overlap_region_der"),
            "non_overlap_der": result.get("diarization", {}).get("non_overlap_der"),
        })
    return model_rows


def _paired_units(result: dict[str, Any], model_a: str, model_b: str) -> list[dict[str, Any]]:
    a_rows = result["models"][model_a].get("per_recording", {})
    b_rows = result["models"][model_b].get("per_recording", {})
    records = {str(record["id"]): record for record in result.get("recordings", [])}
    if set(a_rows) != set(b_rows):
        raise ValueError(f"{model_a} and {model_b} do not have the same paired recording IDs")
    units = []
    for record_id in sorted(a_rows):
        a, b = a_rows[record_id], b_rows[record_id]
        for row in (a, b):
            if "content_errors" not in row or "content_reference_words" not in row:
                raise ValueError(
                    "results lack per-recording error counts; rerun with the current benchmark runner"
                )
        units.append({
            "id": record_id,
            "source_recording": records.get(record_id, {}).get("source_recording", record_id),
            model_a: {"errors": a["content_errors"], "reference_units": a["content_reference_words"]},
            model_b: {"errors": b["content_errors"], "reference_units": b["content_reference_words"]},
        })
    return units


def pairwise_comparison_rows(
    result: dict[str, Any],
    *,
    samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> list[dict[str, Any]]:
    dataset = dataset_info(result)
    aliases = sorted(result.get("models", {}))
    rows = []
    for model_a, model_b in itertools.combinations(aliases, 2):
        units = _paired_units(result, model_a, model_b)
        bootstrap = paired_bootstrap_wer(
            units, model_a, model_b, samples=samples, seed=seed
        )
        group = bootstrap.get("group_bootstrap") or {}
        rows.append({
            "dataset": dataset.get("name", "unknown"),
            "dataset_class": dataset.get("class", "unknown"),
            "sampling_unit": "recording/window",
            "model_a": model_a,
            "model_b": model_b,
            "unit_count": bootstrap["unit_count"],
            "bootstrap_samples": bootstrap["bootstrap_samples"],
            "seed": bootstrap["seed"],
            "wer_a": bootstrap["wer_a"],
            "wer_b": bootstrap["wer_b"],
            "delta_wer_a_minus_b": bootstrap["delta_wer"],
            "ci95_low": bootstrap["ci95"][0],
            "ci95_high": bootstrap["ci95"][1],
            "ci_includes_zero": bootstrap["ci_includes_zero"],
            "group_key": group.get("group_key"),
            "group_count": group.get("group_count"),
            "group_ci95_low": (group.get("ci95") or [None, None])[0],
            "group_ci95_high": (group.get("ci95") or [None, None])[1],
        })
    return rows
