"""SamtaleBank Sam3 error breakdowns by conversation conditions."""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any


def _overlap_bucket(value: float) -> str:
    if value <= 0.02:
        return "0-2%"
    if value <= 0.05:
        return "2-5%"
    if value <= 0.10:
        return "5-10%"
    return ">10%"


def _speech_bucket(value: float) -> str:
    if value < 0.50:
        return "<50%"
    if value <= 0.75:
        return "50-75%"
    return ">75%"


def sam3_stratified_metrics(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Aggregate WER and DER for available Sam3 difficulty metadata buckets."""
    dataset = result.get("dataset", {})
    if dataset.get("name") != "samtalebank-sam3":
        return []
    recordings = result.get("recordings", [])
    turn_values = [
        float(record.get("metadata", {}).get("median_turn_duration_s"))
        for record in recordings
        if record.get("metadata", {}).get("median_turn_duration_s") is not None
    ]
    median_turn = statistics.median(turn_values) if turn_values else None
    grouped: dict[tuple[str, str, str], dict[str, float]] = defaultdict(
        lambda: {"recordings": 0, "content_errors": 0, "reference_words": 0,
                 "der_error_s": 0.0, "der_reference_speaker_time_s": 0.0}
    )

    for record in recordings:
        record_id = record["id"]
        metadata = record.get("metadata", {})
        dimension_buckets: dict[str, str] = {}
        if metadata.get("overlap_ratio") is not None:
            dimension_buckets["overlap"] = _overlap_bucket(float(metadata["overlap_ratio"]))
        if metadata.get("speech_ratio") is not None:
            dimension_buckets["speech_density"] = _speech_bucket(float(metadata["speech_ratio"]))
        if metadata.get("median_turn_duration_s") is not None and median_turn is not None:
            duration = float(metadata["median_turn_duration_s"])
            dimension_buckets["turn_rate"] = "high" if duration <= median_turn else "low"
        if metadata.get("dominant_speaker_fraction") is not None:
            dominant = float(metadata["dominant_speaker_fraction"])
            dimension_buckets["speaker_balance"] = ">70% dominant" if dominant > 0.70 else "<=70% dominant"

        der = result.get("diarization", {}).get("collar_025_per_recording", {}).get(record_id, {})
        der_error = sum(float(der.get(key, 0.0)) for key in ("miss", "false_alarm", "confusion"))
        der_reference = float(der.get("reference_speaker_time", 0.0))
        for model, model_result in result.get("models", {}).items():
            scores = model_result.get("per_recording", {}).get(record_id, {})
            errors = int(scores.get("content_errors", 0))
            words = int(scores.get("content_reference_words", 0))
            for dimension, bucket in dimension_buckets.items():
                slot = grouped[(dimension, bucket, model)]
                slot["recordings"] += 1
                slot["content_errors"] += errors
                slot["reference_words"] += words
                slot["der_error_s"] += der_error
                slot["der_reference_speaker_time_s"] += der_reference

    rows = []
    for (dimension, bucket, model), totals in sorted(grouped.items()):
        rows.append({
            "dataset": "samtalebank-sam3",
            "dimension": dimension,
            "bucket": bucket,
            "model": model,
            "recordings": int(totals["recordings"]),
            "content_errors": int(totals["content_errors"]),
            "reference_words": int(totals["reference_words"]),
            "content_wer": (
                totals["content_errors"] / totals["reference_words"]
                if totals["reference_words"] else None
            ),
            "der_error_s": totals["der_error_s"],
            "der_reference_speaker_time_s": totals["der_reference_speaker_time_s"],
            "der": (
                totals["der_error_s"] / totals["der_reference_speaker_time_s"]
                if totals["der_reference_speaker_time_s"] else None
            ),
            "turn_rate_median_reference_duration_s": median_turn,
            "dominant_speaker_over_70pct": bucket == ">70% dominant" if dimension == "speaker_balance" else None,
        })
    return rows
