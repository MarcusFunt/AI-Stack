"""Canonical schema-v2 JSONL manifests and reference-integrity checks."""

from __future__ import annotations

import json
import math
import re
import wave
from pathlib import Path
from typing import Iterable

from .base import DATASET_CLASSES

MANIFEST_SCHEMA_VERSION = 2
_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_AUDIO_TOLERANCE_S = 0.02


def _read_audio_duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            if rate <= 0:
                return 0.0
            return handle.getnframes() / float(rate)
    except (wave.Error, EOFError, OSError):
        return None


def _recording_text(row: dict) -> str:
    top_level = str(row.get("text") or "").strip()
    if top_level:
        return top_level
    segments = row.get("segments", [])
    return " ".join(
        str(segment.get("text", "")).strip()
        for segment in segments
        if str(segment.get("text", "")).strip()
    ).strip()


def validate_manifest(
    records: Iterable[dict], *, audio_root: Path, tolerance_s: float = _AUDIO_TOLERANCE_S
) -> dict:
    root = Path(audio_root).resolve()
    seen_ids = set()
    seen_windows = set()
    speakers = set()
    segment_count = reference_word_count = 0
    total_audio_s = 0.0
    normalized_records = list(records)
    if not normalized_records:
        raise ValueError("manifest contains no recordings")

    for index, row in enumerate(normalized_records, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"manifest row {index} must be an object")
        version = row.get("schema_version", 1)
        if isinstance(version, bool) or version not in (1, 2):
            raise ValueError(f"{row.get('id', index)}: unsupported schema_version {version!r}")
        recording_id = str(row.get("id") or (f"recording-{index:03d}" if version == 1 else "")).strip()
        if not recording_id:
            raise ValueError(f"manifest row {index} requires an id")
        if recording_id in seen_ids:
            raise ValueError(f"duplicate manifest id: {recording_id}")
        seen_ids.add(recording_id)

        audio_value = str(row.get("audio", "")).strip()
        if not audio_value:
            raise ValueError(f"{recording_id}: audio path is required")
        candidate = Path(audio_value)
        audio_path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        if version == 2:
            try:
                audio_path.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"{recording_id}: schema-v2 audio must stay under the manifest directory") from exc
        if not audio_path.is_file():
            raise FileNotFoundError(f"{recording_id}: audio not found: {audio_path}")
        duration_s = _read_audio_duration(audio_path)
        if version == 2 and (duration_s is None or duration_s <= 0):
            raise ValueError(f"{recording_id}: schema-v2 audio must be a non-empty PCM WAV")
        if duration_s is not None:
            if duration_s <= 0:
                raise ValueError(f"{recording_id}: audio duration must be greater than zero")
            total_audio_s += duration_s

        text = _recording_text(row)
        if not text:
            raise ValueError(f"{recording_id}: reference text is required")
        reference_word_count += len(text.split())

        segments = row.get("segments", [])
        if not isinstance(segments, list):
            raise ValueError(f"{recording_id}: segments must be a list")
        recording_speakers = set()
        for segment_index, segment in enumerate(segments, start=1):
            if not isinstance(segment, dict):
                raise ValueError(f"{recording_id}: segment {segment_index} must be an object")
            try:
                start = float(segment["start"])
                end = float(segment["end"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{recording_id}: segment {segment_index} needs numeric start and end") from exc
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                raise ValueError(f"{recording_id}: segment {segment_index} has invalid timestamps")
            if duration_s is not None and end > duration_s + tolerance_s:
                raise ValueError(f"{recording_id}: segment {segment_index} is outside audio duration")
            speaker = str(segment.get("speaker", "")).strip()
            if not speaker:
                raise ValueError(f"{recording_id}: segment {segment_index} has a blank speaker")
            recording_speakers.add(speaker)
            speakers.add(speaker)
            segment_count += 1
        if version == 2:
            dataset = str(row.get("dataset", "")).strip()
            dataset_class = str(row.get("dataset_class", "")).strip()
            if not dataset:
                raise ValueError(f"{recording_id}: dataset is required for schema v2")
            if dataset_class not in DATASET_CLASSES:
                raise ValueError(f"{recording_id}: unsupported dataset_class {dataset_class!r}")
            try:
                declared_speakers = int(row["speaker_count"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{recording_id}: speaker_count is required for schema v2") from exc
            if declared_speakers < 1:
                raise ValueError(f"{recording_id}: speaker_count must be positive")
            if dataset == "samtalebank-sam3" and (
                declared_speakers != 3 or len(recording_speakers) != 3
            ):
                raise ValueError(f"{recording_id}: Sam3 rows must contain exactly three reference speakers")

            metadata = row.get("metadata")
            if not isinstance(metadata, dict):
                raise ValueError(f"{recording_id}: metadata is required for schema v2")
            for field in ("source_hash", "transcript_hash"):
                if not _HASH_RE.fullmatch(str(metadata.get(field, ""))):
                    raise ValueError(f"{recording_id}: metadata.{field} must be a SHA-256 hex digest")
            for field in ("source_url", "source_license", "reference_transform"):
                if not str(metadata.get(field, "")).strip():
                    raise ValueError(f"{recording_id}: metadata.{field} is required")
            if "window_start_source_s" in metadata and "window_end_source_s" in metadata:
                window = (
                    dataset,
                    str(row.get("source_recording", "")),
                    round(float(metadata["window_start_source_s"]), 6),
                    round(float(metadata["window_end_source_s"]), 6),
                )
                if window in seen_windows:
                    raise ValueError(f"{recording_id}: duplicate source audio window")
                seen_windows.add(window)

    return {
        "recording_count": len(normalized_records),
        "segment_count": segment_count,
        "reference_word_count": reference_word_count,
        "speaker_count": len(speakers),
        "total_audio_s": total_audio_s,
    }


def write_manifest(path: Path, records: Iterable[dict]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for source in records:
        row = dict(source)
        row["schema_version"] = MANIFEST_SCHEMA_VERSION
        rows.append(row)
    validate_manifest(rows, audio_root=path.parent)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def load_manifest(path: Path) -> list[dict]:
    path = Path(path)
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_number}: manifest row must be an object")
        row.setdefault("schema_version", 1)
        if row.get("schema_version") == 1:
            row.setdefault("id", f"recording-{line_number:03d}")
        rows.append(row)
    validate_manifest(rows, audio_root=path.parent)
    return rows
