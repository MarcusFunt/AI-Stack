"""Dataset-aware summary and paired comparison table construction."""

from __future__ import annotations

import itertools
from typing import Any

from .bootstrap import DEFAULT_BOOTSTRAP_SAMPLES, DEFAULT_BOOTSTRAP_SEED, paired_bootstrap_wer
from .evidence import model_dataset_evidence


def dataset_info(result: dict[str, Any]) -> dict[str, Any]:
    info = result.get("dataset")
    if isinstance(info, dict):
        return info
    return {"name": "private-manifest", "class": "PRIVATE-USER-PROVIDED"}


def is_successful_model(model: dict[str, Any]) -> bool:
    """Recognize current and legacy successful rows without ranking failed runs."""
    status = model.get("status")
    if status not in (None, "success"):
        return False
    if status is None and int(model.get("failure_count", 0) or 0) > 0:
        return False
    return model.get("content_wer") is not None


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
        evidence = model_dataset_evidence(result, alias)
        reference_semantics = dataset.get("reference_semantics", "record_transcript")
        model_rows.append({
            "dataset": dataset.get("name", "unknown"),
            "dataset_class": dataset.get("class", "unknown"),
            "model": alias,
            "status": model.get("status", "success" if is_successful_model(model) else "failed"),
            "failure_class": (model.get("failure") or {}).get("class"),
            "failure_message": (model.get("failure") or {}).get("message"),
            "evidence_status": evidence["status"],
            "evidence_decisive": evidence["decisive"],
            "evidence_source": evidence["source"],
            "evidence_note": evidence["note"],
            "model_revision": evidence["model_revision"],
            "reference_semantics": reference_semantics,
            "test_split_status": dataset.get("test_split_status"),
            "speaker_split_relation": dataset.get("speaker_split_relation"),
            "chronological_single_stream_wer": (
                model.get("content_wer")
                if reference_semantics == "chronological_single_stream" else None
            ),
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
        recording = records.get(record_id, {})
        metadata = recording.get("metadata", {}) or {}
        bootstrap_group = str(metadata.get("bootstrap_group", "")).strip()
        bootstrap_group_type = str(metadata.get("bootstrap_group_type", "")).strip()
        if not bootstrap_group:
            speaker_id = str(metadata.get("speaker_id", "")).strip()
            if speaker_id:
                bootstrap_group = speaker_id
                bootstrap_group_type = "speaker"
            else:
                bootstrap_group = str(recording.get("source_recording", record_id)).strip() or record_id
                bootstrap_group_type = "recording"
        units.append({
            "id": record_id,
            "source_recording": recording.get("source_recording", record_id),
            "bootstrap_group": bootstrap_group,
            "bootstrap_group_type": bootstrap_group_type or "recording",
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
    aliases = sorted(
        alias for alias, model in result.get("models", {}).items()
        if is_successful_model(model)
    )
    rows = []
    for model_a, model_b in itertools.combinations(aliases, 2):
        units = _paired_units(result, model_a, model_b)
        bootstrap = paired_bootstrap_wer(
            units, model_a, model_b, samples=samples, seed=seed
        )
        group = bootstrap.get("group_bootstrap") or {}
        evidence_a = model_dataset_evidence(result, model_a)
        evidence_b = model_dataset_evidence(result, model_b)
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
            "group_unit": group.get("group_type"),
            "group_count": group.get("group_count"),
            "group_status": group.get("status"),
            "group_ci95_low": (group.get("ci95") or [None, None])[0],
            "group_ci95_high": (group.get("ci95") or [None, None])[1],
            "evidence_status_a": evidence_a["status"],
            "evidence_decisive_a": evidence_a["decisive"],
            "evidence_status_b": evidence_b["status"],
            "evidence_decisive_b": evidence_b["decisive"],
        })
    return rows
