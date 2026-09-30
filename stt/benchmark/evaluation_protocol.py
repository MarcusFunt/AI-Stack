"""Versioned definitions for benchmark metrics and comparison semantics."""

from __future__ import annotations

from typing import Any


PROTOCOL_ID = "danish-stt-evaluation-v1"


def evaluation_protocol(
    collar_s: float,
    reference_semantics: str,
    *,
    speaker_attributed: bool = True,
) -> dict[str, Any]:
    return {
        "protocol_id": PROTOCOL_ID,
        "result_schema_version": 3,
        "reference_semantics": str(reference_semantics),
        "content_normalization": "danish-content-v1",
        "verbatim_normalization": "danish-verbatim-v1",
        "cer_normalization": "danish-cer-v1",
        "der_frame_ms": 10,
        "der_standard_collar_s": float(collar_s),
        "der_strict_collar_s": 0.0,
        "der_overlap": "included",
        "speaker_turn_merge_gap_s": 0.35,
        "speaker_mapping": "per_recording_permutation_optimal",
        "speaker_attributed_metrics_enabled": bool(speaker_attributed),
    }
