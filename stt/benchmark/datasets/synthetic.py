"""Preparation for the controlled exact-three-speaker synthetic suite."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import wave
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Iterable

from .base import DatasetAdapter, DatasetSpec
from .huggingface import load_dataset_split, resolve_dataset_revision, validate_commit_sha
from .manifest import write_manifest
from .provenance import build_lock_entry, file_sha256, load_lock, update_lock


DATASET_ID = "syvai/danish-diarization-bench"
DATASET_NAME = "diarization-k3"
SOURCE_URL = "https://huggingface.co/datasets/syvai/danish-diarization-bench"
SOURCE_LICENSE = "CC-BY-NC-4.0"
REFERENCE_TRANSFORM = "hf-segments-v1"


def _as_json(value: Any, label: str) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label} contains invalid JSON") from exc
    return value


def _source_families(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    parsed = _as_json(value, "sources")
    if isinstance(parsed, dict):
        values = parsed.values()
    elif isinstance(parsed, (list, tuple, set)):
        values = parsed
    else:
        values = [parsed]
    return sorted({str(item).strip() for item in values if str(item).strip()})


def _validated_segments(value: Any, row_id: str) -> list[dict]:
    parsed = _as_json(value, f"{row_id}.segments")
    if not isinstance(parsed, list) or not parsed:
        raise ValueError(f"{row_id}: segments must be a non-empty list")
    segments = []
    for index, item in enumerate(parsed, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"{row_id}: segment {index} must be an object")
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{row_id}: segment {index} needs numeric start and end") from exc
        speaker = str(item.get("speaker", "")).strip()
        text = str(item.get("text", "")).strip()
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError(f"{row_id}: segment {index} has invalid timestamps")
        if not speaker:
            raise ValueError(f"{row_id}: segment {index} has a blank speaker")
        if not text:
            raise ValueError(f"{row_id}: segment {index} has empty text")
        segments.append({"start": start, "end": end, "speaker": speaker, "text": text})
    return sorted(segments, key=lambda item: (item["start"], item["end"], item["speaker"]))


def _write_audio(audio: Any, output_path: Path, row_id: str) -> float:
    """Write decoded HF audio as mono 16 kHz PCM16 without resampling."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = audio if isinstance(audio, dict) else {"array": audio}
    array = payload.get("array")
    rate = payload.get("sampling_rate", payload.get("sample_rate"))
    if array is None:
        encoded = payload.get("bytes")
        source_path = payload.get("path")
        stream = BytesIO(encoded) if encoded else source_path if source_path and Path(source_path).is_file() else None
        if stream is not None:
            try:
                with wave.open(stream, "rb") as source:
                    channels = source.getnchannels()
                    rate = source.getframerate()
                    width = source.getsampwidth()
                    frame_count = source.getnframes()
                    frames = source.readframes(frame_count)
            except (wave.Error, EOFError):
                pass
            else:
                if channels != 1 or rate != 16000:
                    raise ValueError(f"{row_id}: expected mono 16 kHz audio")
                if width == 2:
                    with wave.open(str(output_path), "wb") as target:
                        target.setnchannels(1)
                        target.setsampwidth(2)
                        target.setframerate(16000)
                        target.writeframes(frames)
                    return frame_count / 16000.0

        try:
            import soundfile as sf
        except ImportError as exc:
            raise RuntimeError(
                "Decoding non-WAV dataset audio needs soundfile from stt/requirements-benchmark.txt"
            ) from exc
        try:
            if encoded:
                array, rate = sf.read(BytesIO(encoded), dtype="float32", always_2d=True)
            elif source_path and Path(source_path).is_file():
                array, rate = sf.read(str(source_path), dtype="float32", always_2d=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"{row_id}: could not decode Hugging Face audio") from exc

    if array is None or int(rate or 0) != 16000:
        raise ValueError(f"{row_id}: expected decoded 16 kHz audio")
    try:
        import soundfile as sf
        import numpy as np

        samples = np.asarray(array)
        if samples.ndim == 2 and samples.shape[1] == 1:
            samples = samples[:, 0]
        if samples.ndim != 1:
            raise ValueError(f"{row_id}: expected mono audio, got shape {samples.shape}")
        if not len(samples):
            raise ValueError(f"{row_id}: dataset audio is empty")
        sf.write(str(output_path), samples, 16000, subtype="PCM_16", format="WAV")
        return len(samples) / 16000.0
    except ImportError as exc:
        raise RuntimeError("Writing decoded dataset audio needs numpy from stt/requirements-benchmark.txt") from exc


def _git_revision() -> str:
    repository_root = Path(__file__).resolve().parents[3]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repository_root,
            check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "working-tree"
    revision = result.stdout.strip()
    return revision if len(revision) == 40 else "working-tree"


def prepare_diarization_k3(
    output_dir: Path,
    *,
    lock_path: Path | None = None,
    revision: str | None = None,
    cache_dir: Path | None = None,
    dataset_loader: Callable[..., Iterable[dict]] | None = None,
    revision_resolver: Callable[[str], str] | None = None,
) -> Path:
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = Path(lock_path) if lock_path else output_dir.parent / "dataset-lock.json"
    lock = load_lock(lock_path)
    old_entry = lock["datasets"].get(DATASET_NAME)

    if revision:
        pinned_revision = validate_commit_sha(revision)
    elif old_entry:
        pinned_revision = validate_commit_sha(old_entry["revision"])
    else:
        resolver = revision_resolver or resolve_dataset_revision
        pinned_revision = validate_commit_sha(resolver(DATASET_ID))

    if old_entry and old_entry.get("revision") != pinned_revision:
        raise ValueError(
            f"dataset {DATASET_NAME} revision changed from {old_entry.get('revision')} "
            f"to {pinned_revision}; use a new lock file for an intentional upgrade"
        )

    loader = dataset_loader or load_dataset_split
    rows = loader(
        DATASET_ID,
        split="test",
        revision=pinned_revision,
        cache_dir=cache_dir,
    )
    records = []
    validation = {"source_rows": 0, "prepared_k3_rows": 0, "filtered_non_k3_rows": 0}
    prepared_files: list[Path] = []
    seen_ids = set()

    for row in rows:
        validation["source_rows"] += 1
        try:
            speaker_count = int(row["num_speakers"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("dataset test row requires an integer num_speakers") from exc
        if speaker_count != 3:
            validation["filtered_non_k3_rows"] += 1
            continue

        row_id = str(row.get("id", "")).strip()
        if not row_id:
            raise ValueError("K=3 dataset row requires an id")
        if row_id in seen_ids:
            raise ValueError(f"duplicate K=3 sample id: {row_id}")
        seen_ids.add(row_id)
        segments = _validated_segments(row.get("segments"), row_id)
        actual_speakers = {segment["speaker"] for segment in segments}
        if len(actual_speakers) != 3:
            raise ValueError(f"{row_id}: K=3 row must contain three distinct speakers")

        safe_id = "".join(char if char.isalnum() or char in "-_" else "_" for char in row_id)
        audio_path = output_dir / "audio" / f"{safe_id}.wav"
        duration_s = _write_audio(row.get("audio"), audio_path, row_id)
        declared_duration = row.get("duration")
        if declared_duration is not None and abs(float(declared_duration) - duration_s) > 0.02:
            raise ValueError(f"{row_id}: declared duration differs from decoded WAV duration")
        if any(segment["end"] > duration_s + 0.02 for segment in segments):
            raise ValueError(f"{row_id}: segment timestamp exceeds audio duration")

        transcript = json.dumps(segments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        audio_hash = file_sha256(audio_path)
        transcript_hash = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
        text = " ".join(segment["text"] for segment in segments)
        row_sources = _source_families(row.get("sources"))
        records.append({
            "id": safe_id,
            "dataset": DATASET_NAME,
            "dataset_class": "CONTROLLED-SYNTHETIC",
            "source_recording": row_id,
            "audio": audio_path.relative_to(output_dir).as_posix(),
            "speaker_count": 3,
            "text": text,
            "segments": segments,
            "metadata": {
                "source_hash": audio_hash,
                "transcript_hash": transcript_hash,
                "source_url": SOURCE_URL,
                "source_license": SOURCE_LICENSE,
                "reference_transform": REFERENCE_TRANSFORM,
                "source_revision": pinned_revision,
                "source_dataset_families": row_sources,
            },
        })
        prepared_files.append(audio_path)
        validation["prepared_k3_rows"] += 1

    if not records:
        raise ValueError("the pinned test split contained no K=3 rows")

    manifest_path = write_manifest(output_dir / "manifest.jsonl", records)
    validation_path = output_dir / "dataset-validation.json"
    validation_path.write_text(
        json.dumps(validation, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    lock_root = Path(lock_path).resolve().parent
    lock_files = prepared_files + [manifest_path, validation_path]
    entry = build_lock_entry(
        dataset=DATASET_NAME,
        dataset_class="CONTROLLED-SYNTHETIC",
        source_url=SOURCE_URL,
        license=SOURCE_LICENSE,
        revision=pinned_revision,
        files=lock_files,
        root=lock_root,
        preparation_code_git_sha=_git_revision(),
        reference_transform=REFERENCE_TRANSFORM,
    )
    update_lock(lock_path, entry)
    return manifest_path


class DiarizationK3Adapter(DatasetAdapter):
    spec = DatasetSpec(
        dataset=DATASET_NAME,
        dataset_class="CONTROLLED-SYNTHETIC",
        source_url=SOURCE_URL,
        license=SOURCE_LICENSE,
    )

    def prepare(
        self, source_path: Path | None, output_dir: Path, **options: Any
    ) -> Path:
        if source_path is not None:
            options.setdefault("cache_dir", Path(source_path))
        return prepare_diarization_k3(output_dir, **options)
