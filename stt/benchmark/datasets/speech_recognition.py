"""Single-speaker Hugging Face ASR test suites and stratification summaries."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable

from .audio import write_audio_16k_mono
from .base import DatasetAdapter, DatasetSpec
from .huggingface import load_dataset_split, resolve_dataset_revision, validate_commit_sha
from .manifest import write_manifest
from .provenance import build_lock_entry, file_sha256, load_lock, update_lock


CORAL = {
    "dataset": "coral-conversation-test",
    "repo": "CoRal-project/coral-v3",
    "config": "conversational",
    "license": "openrail",
    "url": "https://huggingface.co/datasets/CoRal-project/coral-v3",
}
NST = {
    "dataset": "nst-da-test",
    "repo": "alexandrainst/nst-da",
    "config": None,
    "license": "CC0-1.0",
    "url": "https://huggingface.co/datasets/alexandrainst/nst-da",
}
FLEURS = {
    "dataset": "fleurs-da-dk-test",
    "repo": "google/fleurs",
    "config": "da_dk",
    "license": "CC-BY-4.0",
    "url": "https://huggingface.co/datasets/google/fleurs",
}
REFERENCE_TRANSFORM = "asr-reference-raw-v1"
MIN_REFERENCE_WORDS = 1000


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


def _safe_id(value: Any) -> str:
    text = str(value).strip()
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")
    return normalized or "sample"


def _as_text(row: dict, fields: tuple[str, ...], sample_id: str) -> str:
    for field in fields:
        value = str(row.get(field) or "").strip()
        if value:
            return value
    raise ValueError(f"{sample_id}: reference text is empty")


def _categorical(row: dict, *fields: str) -> str | None:
    for field in fields:
        value = row.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _age_group(row: dict) -> str | None:
    explicit = _categorical(row, "age_group", "age_range")
    if explicit:
        return explicit
    value = row.get("age")
    if value is None:
        return None
    try:
        age = float(value)
    except (TypeError, ValueError):
        return str(value).strip() or None
    if age < 25:
        return "0-24"
    if age < 50:
        return "25-49"
    return "50+"


def _strata(row: dict, duration_s: float) -> dict[str, str]:
    dimensions = {
        "dialect": _categorical(row, "dialect"),
        "accent": _categorical(row, "accent_group", "accent", "non_native_accent"),
        "age_group": _age_group(row),
        "gender": _categorical(row, "gender", "sex"),
    }
    dialect = dimensions["dialect"]
    if dimensions["accent"] is None and dialect and "non-native" in dialect.lower():
        dimensions["accent"] = "non-native"
    if duration_s < 5:
        dimensions["duration_bucket"] = "<5s"
    elif duration_s <= 10:
        dimensions["duration_bucket"] = "5-10s"
    else:
        dimensions["duration_bucket"] = ">10s"
    return {key: value for key, value in dimensions.items() if value is not None}


def _recording_key(row: dict, index: int) -> str:
    for field in ("id", "file_name", "id_recording", "id_sentence"):
        if row.get(field) is not None and str(row[field]).strip():
            return str(row[field]).strip()
    return f"row-{index:06d}"


def _load_rows(
    loader: Callable[..., Iterable[dict]],
    repo_id: str,
    config: str | None,
    *,
    split: str,
    revision: str,
    cache_dir: Path | None,
) -> Iterable[dict]:
    options = {"split": split, "revision": revision, "cache_dir": cache_dir}
    if config:
        return loader(repo_id, config, **options)
    return loader(repo_id, **options)


def prepare_single_speaker_suite(
    spec: DatasetSpec,
    *,
    repo_id: str,
    config: str | None,
    text_fields: tuple[str, ...],
    output_dir: Path,
    lock_path: Path | None = None,
    revision: str | None = None,
    cache_dir: Path | None = None,
    dataset_loader: Callable[..., Iterable[dict]] | None = None,
    revision_resolver: Callable[[str], str] | None = None,
    minimum_reference_words: int = MIN_REFERENCE_WORDS,
) -> Path:
    """Prepare a pinned test split while keeping its original transcript text."""
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = Path(lock_path) if lock_path else output_dir.parent / "dataset-lock.json"
    old_entry = load_lock(lock_path)["datasets"].get(spec.dataset)
    if revision:
        pinned_revision = validate_commit_sha(revision)
    elif old_entry:
        pinned_revision = validate_commit_sha(old_entry["revision"])
    else:
        resolver = revision_resolver or resolve_dataset_revision
        pinned_revision = validate_commit_sha(resolver(repo_id))
    if old_entry and old_entry.get("revision") != pinned_revision:
        raise ValueError(
            f"dataset {spec.dataset} revision changed from {old_entry.get('revision')} "
            f"to {pinned_revision}; use a new lock file for an intentional upgrade"
        )

    loader = dataset_loader or load_dataset_split
    rows = _load_rows(
        loader,
        repo_id,
        config,
        split="test",
        revision=pinned_revision,
        cache_dir=cache_dir,
    )
    records = []
    prepared_files = []
    strata_counts: dict[str, dict[str, dict[str, int]]] = {}
    reference_words = total_duration = 0

    for index, row in enumerate(rows, start=1):
        source_id = _recording_key(row, index)
        record_id = f"{spec.dataset}-{index:06d}-{_safe_id(source_id)}"
        text = _as_text(row, text_fields, record_id)
        audio_path = output_dir / "audio" / f"{record_id}.wav"
        duration_s = write_audio_16k_mono(row.get("audio"), audio_path, record_id)
        words = len(text.split())
        reference_words += words
        total_duration += duration_s
        groups = _strata(row, duration_s)
        for dimension, value in groups.items():
            entry = strata_counts.setdefault(dimension, {}).setdefault(
                value, {"recordings": 0, "reference_words": 0}
            )
            entry["recordings"] += 1
            entry["reference_words"] += words

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        speaker_id = _categorical(row, "speaker_id", "id_speaker")
        records.append({
            "id": record_id,
            "dataset": spec.dataset,
            "dataset_class": spec.dataset_class,
            "source_recording": source_id,
            "audio": audio_path.relative_to(output_dir).as_posix(),
            "speaker_count": 1,
            "text": text,
            "segments": [],
            "metadata": {
                "source_hash": file_sha256(audio_path),
                "transcript_hash": digest,
                "source_url": spec.source_url,
                "source_license": spec.license,
                "reference_transform": REFERENCE_TRANSFORM,
                "source_revision": pinned_revision,
                "source_split": "test",
                "source_config": config or "default",
                "source_recording_id": source_id,
                "speaker_id": speaker_id or "",
                "duration_s": round(duration_s, 6),
                "strata": groups,
            },
        })
        prepared_files.append(audio_path)

    if not records:
        raise ValueError(f"{spec.dataset}: test split contains no usable recordings")
    manifest_path = write_manifest(output_dir / "manifest.jsonl", records)
    validation = {
        "recording_count": len(records),
        "reference_word_count": reference_words,
        "total_audio_s": round(total_duration, 6),
        "source_split": "test",
        "source_config": config or "default",
        "source_revision": pinned_revision,
    }
    validation_path = output_dir / "dataset-validation.json"
    validation_path.write_text(
        json.dumps(validation, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    for categories in strata_counts.values():
        for category in categories.values():
            category["status"] = (
                "included" if category["reference_words"] >= minimum_reference_words
                else "insufficient_n"
            )
    strata_path = output_dir / "strata-summary.json"
    strata_path.write_text(
        json.dumps({
            "minimum_reference_words": minimum_reference_words,
            "strata": strata_counts,
        }, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    lock_root = Path(lock_path).resolve().parent
    entry = build_lock_entry(
        dataset=spec.dataset,
        dataset_class=spec.dataset_class,
        source_url=spec.source_url,
        license=spec.license,
        revision=pinned_revision,
        files=prepared_files + [manifest_path, validation_path, strata_path],
        root=lock_root,
        preparation_code_git_sha=_git_revision(),
        reference_transform=REFERENCE_TRANSFORM,
    )
    update_lock(lock_path, entry)
    return manifest_path


class _HfAsrTestAdapter(DatasetAdapter):
    repo_id: str
    config: str | None
    text_fields: tuple[str, ...]

    def prepare(self, source_path: Path | None, output_dir: Path, **options: Any) -> Path:
        if source_path is not None:
            options.setdefault("cache_dir", Path(source_path))
        return prepare_single_speaker_suite(
            self.spec,
            repo_id=self.repo_id,
            config=self.config,
            text_fields=self.text_fields,
            output_dir=output_dir,
            **options,
        )


class CoRalConversationTestAdapter(_HfAsrTestAdapter):
    spec = DatasetSpec(
        dataset=CORAL["dataset"],
        dataset_class="HELD-OUT-IN-DOMAIN",
        source_url=CORAL["url"],
        license=CORAL["license"],
    )
    repo_id = CORAL["repo"]
    config = CORAL["config"]
    text_fields = ("text",)


class NstDanishTestAdapter(_HfAsrTestAdapter):
    spec = DatasetSpec(
        dataset=NST["dataset"],
        dataset_class="HELD-OUT-IN-DOMAIN",
        source_url=NST["url"],
        license=NST["license"],
    )
    repo_id = NST["repo"]
    config = NST["config"]
    text_fields = ("text",)


class FleursDanishTestAdapter(_HfAsrTestAdapter):
    spec = DatasetSpec(
        dataset=FLEURS["dataset"],
        dataset_class="HELD-OUT-IN-DOMAIN",
        source_url=FLEURS["url"],
        license=FLEURS["license"],
    )
    repo_id = FLEURS["repo"]
    config = FLEURS["config"]
    text_fields = ("raw_transcription", "transcription")
