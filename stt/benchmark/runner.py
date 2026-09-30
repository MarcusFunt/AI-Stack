from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
import tempfile
import time
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .diarization import NemotronDiarizer
from .metrics import (
    aggregate_error_stats,
    char_error_stats,
    diarization_error,
    diarization_error_regions,
    word_error_stats,
)
from .models import MODEL_SPECS, load_adapter
from .revisions import parse_revision_overrides
from .evaluation_protocol import evaluation_protocol
from .datasets.provenance import canonical_json_sha256, file_sha256
from .analysis.evidence import build_model_dataset_evidence


def package_versions() -> dict[str, str]:
    names = [
        "torch", "transformers", "librosa", "num2words", "text2num",
        "huggingface-hub", "tokenizers", "accelerate", "safetensors", "sentencepiece",
    ]
    versions = {"python": platform.python_version()}
    for name in names:
        try:
            versions[name] = version(name)
        except PackageNotFoundError:
            versions[name] = "not-installed"
    return versions


def load_manifest(path: Path) -> list[dict]:
    rows = []
    seen_ids = set()
    root = path.parent.resolve()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = json.loads(line)
        item.setdefault("id", f"recording-{number:03d}")
        if item["id"] in seen_ids:
            raise ValueError(f"{path}:{number}: duplicate recording id: {item['id']}")
        seen_ids.add(item["id"])
        audio = Path(item["audio"])
        if not audio.is_absolute():
            audio = root / audio
        audio = audio.resolve()
        if not audio.exists():
            raise FileNotFoundError(f"{path}:{number}: audio not found: {audio}")
        segments = sorted(
            item.get("segments", []),
            key=lambda x: (float(x["start"]), float(x["end"])),
        )
        for segment in segments:
            if float(segment["end"]) <= float(segment["start"]):
                raise ValueError(f"{path}:{number}: segment end must be greater than start")
            if "speaker" not in segment:
                raise ValueError(f"{path}:{number}: every segment needs a speaker")
            segment.setdefault("text", "")
        item["segments"] = segments
        item["has_speaker_transcripts"] = bool(segments) and all(
            str(segment.get("text", "")).strip() for segment in segments
        )
        item["audio_path"] = str(audio)
        item["speaker_count"] = int(item.get("speaker_count", 3))
        top_level_text = str(item.get("text") or "").strip()
        item["reference_text"] = (
            top_level_text
            or " ".join(str(s.get("text", "")) for s in segments).strip()
        )
        if not item["reference_text"]:
            raise ValueError(
                f"{path}:{number}: recording requires reference text for ASR scoring"
            )
        rows.append(item)
    if not rows:
        raise ValueError("manifest contains no recordings")
    if any(row.get("schema_version") == 2 for row in rows):
        from .datasets.manifest import validate_manifest
        validate_manifest(rows, audio_root=root)
    return rows


def audio_duration(path: str) -> float:
    import librosa
    return float(librosa.get_duration(path=path))


def _dataset_info(records: list[dict], manifest_path: Path) -> dict:
    names = {str(record.get("dataset", "")).strip() for record in records if record.get("dataset")}
    classes = {
        str(record.get("dataset_class", "")).strip()
        for record in records if record.get("dataset_class")
    }
    if len(names) > 1 or len(classes) > 1:
        raise ValueError("one benchmark manifest must contain a single dataset and dataset class")
    metadata = [record.get("metadata", {}) for record in records]
    values = {
        field: sorted({str(item.get(field, "")).strip() for item in metadata if item.get(field)})
        for field in (
            "source_url", "source_license", "source_revision", "reference_transform",
            "reference_semantics", "test_split_status", "speaker_split_relation",
        )
    }
    lock_candidate = manifest_path.resolve().parents[2] / "dataset-lock.json"
    lock_payload = (
        json.loads(lock_candidate.read_text(encoding="utf-8"))
        if lock_candidate.is_file() else None
    )
    dataset_name = next(iter(names), "private-manifest")
    lock_entry = (
        (lock_payload or {}).get("datasets", {}).get(dataset_name)
        if isinstance(lock_payload, dict) else None
    )
    reference_semantics = values["reference_semantics"][0] if values["reference_semantics"] else (
        "chronological_single_stream" if dataset_name == "samtalebank-sam3" else "record_transcript"
    )
    return {
        "name": dataset_name,
        "class": next(iter(classes), "PRIVATE-USER-PROVIDED"),
        "source_url": values["source_url"][0] if values["source_url"] else None,
        "source_license": values["source_license"][0] if values["source_license"] else None,
        "source_revisions": values["source_revision"],
        "reference_transforms": values["reference_transform"],
        "reference_semantics": reference_semantics,
        "test_split_status": values["test_split_status"][0] if values["test_split_status"] else None,
        "speaker_split_relation": values["speaker_split_relation"][0] if values["speaker_split_relation"] else None,
        "manifest": str(manifest_path),
        "manifest_sha256": (
            hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            if manifest_path.is_file() else None
        ),
        "dataset_lock": str(lock_candidate) if lock_candidate.is_file() else None,
        "dataset_lock_sha256": file_sha256(lock_candidate) if lock_candidate.is_file() else None,
        "dataset_lock_entry": lock_entry,
        "dataset_lock_entry_sha256": (
            canonical_json_sha256(lock_entry) if isinstance(lock_entry, dict) else None
        ),
    }


def _run_reference_diarization(
    records: list[dict],
    durations: dict[str, float],
    revision: str | None,
    *,
    collar_s: float = 0.25,
    diarizer_factory=None,
) -> dict:
    reference_records = [record for record in records if record["segments"]]
    result = {
        "revision": None,
        "predicted_by_id": {},
        "der_by_id": {},
        "der_025_by_id": {},
        "der_0_by_id": {},
        "regions_by_id": {},
    }
    if not reference_records:
        return result

    diarizer = (diarizer_factory or NemotronDiarizer)(revision=revision)
    try:
        for record in reference_records:
            predicted = diarizer.diarize(record["audio_path"])
            record_id = record["id"]
            result["predicted_by_id"][record_id] = predicted
            result["der_by_id"][record_id] = diarization_error(
                record["segments"], predicted,
                duration_s=durations[record_id], collar_s=collar_s,
            )
            result["der_025_by_id"][record_id] = diarization_error(
                record["segments"], predicted,
                duration_s=durations[record_id], collar_s=0.25,
            )
            result["der_0_by_id"][record_id] = diarization_error(
                record["segments"], predicted,
                duration_s=durations[record_id], collar_s=0.0,
            )
            result["regions_by_id"][record_id] = diarization_error_regions(
                record["segments"], predicted,
                duration_s=durations[record_id], collar_s=0.25,
            )
        result["revision"] = diarizer.revision
    finally:
        diarizer.unload()
    return result


def _aggregate_der(scores: dict[str, dict]) -> dict:
    if not scores:
        return {"global": None, "macro": None, "miss": None, "false_alarm": None, "confusion": None}
    totals = {
        key: sum(float(score.get(key, 0.0)) for score in scores.values())
        for key in ("miss", "false_alarm", "confusion", "reference_speaker_time")
    }
    error_time = totals["miss"] + totals["false_alarm"] + totals["confusion"]
    values = [float(score["der"]) for score in scores.values()]
    return {
        "global": error_time / totals["reference_speaker_time"] if totals["reference_speaker_time"] else 0.0,
        "macro": sum(values) / len(values),
        "miss": totals["miss"],
        "false_alarm": totals["false_alarm"],
        "confusion": totals["confusion"],
        "reference_speaker_time": totals["reference_speaker_time"],
    }


def _is_oom_error(error: BaseException) -> bool:
    return (
        "outofmemory" in type(error).__name__.replace("_", "").lower()
        or "memoryerror" in type(error).__name__.lower()
        or "out of memory" in str(error).lower()
    )


def merge_turns(segments: list[dict], *, gap_s: float = 0.35) -> list[dict]:
    merged: list[dict] = []
    for segment in sorted(segments, key=lambda x: (float(x["start"]), float(x["end"]))):
        current = dict(segment)
        if (
            merged
            and merged[-1]["speaker"] == current["speaker"]
            and float(current["start"]) - float(merged[-1]["end"]) <= gap_s
        ):
            merged[-1]["end"] = max(float(merged[-1]["end"]), float(current["end"]))
        else:
            merged.append(current)
    return merged


def render_turns(record: dict, segments: list[dict], directory: Path) -> list[dict]:
    import librosa
    import soundfile as sf

    audio, sr = librosa.load(record["audio_path"], sr=16000, mono=True)
    rendered = []
    for index, segment in enumerate(merge_turns(segments)):
        start = max(0, int(float(segment["start"]) * sr))
        end = min(len(audio), int(float(segment["end"]) * sr))
        if end - start < int(0.20 * sr):
            continue
        path = directory / f"{record['id']}-{index:04d}-spk{segment['speaker']}.wav"
        sf.write(path, audio[start:end], sr)
        rendered.append({**segment, "path": str(path)})
    return rendered


def speaker_attributed_stats(
    adapter,
    records: list[dict],
    diarization_by_id: dict[str, list[dict]],
    mappings: dict[str, dict],
    batch_size: int,
) -> dict:
    all_stats = []
    per_recording = {}
    with tempfile.TemporaryDirectory(prefix="ai-stack-stt-turns-") as tmp:
        tmpdir = Path(tmp)
        for record in records:
            if not record["has_speaker_transcripts"]:
                continue
            turns = render_turns(record, diarization_by_id[record["id"]], tmpdir)
            if not turns:
                continue
            texts = adapter.transcribe([turn["path"] for turn in turns], batch_size)
            if len(texts) != len(turns):
                raise RuntimeError(
                    f"adapter returned {len(texts)} transcripts for {len(turns)} diarized turns"
                )
            predicted_by_speaker: dict[str, list[str]] = defaultdict(list)
            mapping = mappings.get(record["id"], {})
            for turn, text in zip(turns, texts):
                mapped = mapping.get(str(turn["speaker"]))
                if mapped is not None:
                    predicted_by_speaker[str(mapped)].append(text)

            reference_by_speaker: dict[str, list[str]] = defaultdict(list)
            for segment in record["segments"]:
                reference_by_speaker[str(segment["speaker"])].append(
                    str(segment.get("text", ""))
                )

            record_stats = []
            for speaker in sorted(reference_by_speaker):
                stat = word_error_stats(
                    " ".join(reference_by_speaker[speaker]),
                    " ".join(predicted_by_speaker.get(speaker, [])),
                    remove_fillers=True,
                    canonicalize_numbers=True,
                )
                record_stats.append(stat)
                all_stats.append(stat)
            aggregate = aggregate_error_stats(record_stats)
            per_recording[record["id"]] = aggregate.rate

    if not all_stats:
        return {"speaker_attributed_wer": None, "per_recording": per_recording}
    total = aggregate_error_stats(all_stats)
    return {"speaker_attributed_wer": total.rate, "per_recording": per_recording}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark Danish ASR + 3-speaker diarization on local recordings."
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--models", nargs="+", default=["edda", "saga2", "hviske"])
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument(
        "--beam-size",
        type=int,
        default=5,
        help="Edda beam size; other adapters use their native decoder.",
    )
    parser.add_argument("--collar", type=float, default=0.25)
    parser.add_argument(
        "--revision",
        action="append",
        default=[],
        metavar="ALIAS=REVISION",
        help=(
            "Pin a Hub model ref. Repeat for edda, saga2, hviske, or nemotron. "
            "Resolved commit SHAs are always written to results.json."
        ),
    )
    parser.add_argument("--no-speaker-attributed", action="store_true")
    args = parser.parse_args()

    try:
        requested_revisions = parse_revision_overrides(args.revision)
    except ValueError as exc:
        parser.error(str(exc))
    allowed_revision_aliases = set(MODEL_SPECS) | {"nemotron"}
    unknown_revision_aliases = sorted(set(requested_revisions) - allowed_revision_aliases)
    if unknown_revision_aliases:
        parser.error(
            "unknown revision aliases: " + ", ".join(unknown_revision_aliases)
        )

    unknown = sorted(set(args.models) - set(MODEL_SPECS))
    if unknown:
        parser.error(f"unknown models: {', '.join(unknown)}")

    args.output.mkdir(parents=True, exist_ok=True)
    records = load_manifest(args.manifest)
    durations = {record["id"]: audio_duration(record["audio_path"]) for record in records}
    empty_audio = [record_id for record_id, duration in durations.items() if duration <= 0]
    if empty_audio:
        raise ValueError(f"recordings have no audio duration: {', '.join(empty_audio)}")
    total_audio_s = sum(durations.values())

    diarization_result = _run_reference_diarization(
        records,
        durations,
        requested_revisions.get("nemotron"),
        collar_s=args.collar,
    )
    diarization_by_id = diarization_result["predicted_by_id"]
    der_by_id = diarization_result["der_by_id"]
    der_025_by_id = diarization_result["der_025_by_id"]
    der_0_by_id = diarization_result["der_0_by_id"]
    der_regions_by_id = diarization_result["regions_by_id"]
    mappings = {
        record_id: score["mapping"] for record_id, score in der_by_id.items()
    }

    der_default = _aggregate_der(der_by_id)
    der_standard = _aggregate_der(der_025_by_id)
    der_strict = _aggregate_der(der_0_by_id)
    overlap_error = sum(
        float(item["overlap_miss"] + item["overlap_false_alarm"] + item["overlap_confusion"])
        for item in der_regions_by_id.values()
    )
    overlap_reference_time = sum(
        float(item["overlap_reference_speaker_time"])
        for item in der_regions_by_id.values()
    )
    non_overlap_error = sum(
        float(item["non_overlap_miss"] + item["non_overlap_false_alarm"] + item["non_overlap_confusion"])
        for item in der_regions_by_id.values()
    )
    non_overlap_reference_time = sum(
        float(item["non_overlap_reference_speaker_time"])
        for item in der_regions_by_id.values()
    )
    exact_speaker_counts = sum(
        len({segment["speaker"] for segment in record["segments"]})
        == len(set(segment["speaker"] for segment in diarization_by_id[record["id"]]))
        for record in records
        if record["segments"]
    )
    speaker_count_rows = sum(bool(record["segments"]) for record in records)

    reference_semantics = next(
        (record.get("metadata", {}).get("reference_semantics") for record in records
         if record.get("metadata", {}).get("reference_semantics")),
        "chronological_single_stream" if any(
            record.get("dataset") == "samtalebank-sam3" for record in records
        ) else "record_transcript",
    )
    protocol = evaluation_protocol(
        args.collar,
        reference_semantics,
        speaker_attributed=not args.no_speaker_attributed,
    )
    results = {
        "schema_version": 3,
        "evaluation_protocol": protocol,
        "evaluation_protocol_sha256": canonical_json_sha256(protocol),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "manifest": str(args.manifest),
        "total_audio_s": total_audio_s,
        "dataset": _dataset_info(records, args.manifest),
        "config": {
            "models": list(args.models),
            "batch_size": args.batch_size,
            "beam_size": args.beam_size,
            "collar_s": args.collar,
            "speaker_attributed": not args.no_speaker_attributed,
            "normalization": "danish-content-v1",
            "requested_revisions": requested_revisions,
        },
        "runtime_versions": package_versions(),
        "recordings": [
            {
                "id": record["id"],
                "duration_s": durations[record["id"]],
                "speaker_count": record["speaker_count"],
                "source_recording": record.get("source_recording", record["id"]),
                "reference_speakers": sorted(
                    {str(segment["speaker"]) for segment in record["segments"]}
                    or ({str(record.get("metadata", {}).get("speaker_id"))}
                        if record.get("metadata", {}).get("speaker_id") else set())
                ),
                "reference_text": record["reference_text"],
                "metadata": record.get("metadata", {}),
            }
            for record in records
        ],
        "diarization": {
            "model": NemotronDiarizer.model_id,
            "revision": diarization_result["revision"],
            "status": (
                "scored" if der_by_id else "not_run_no_reference_segments"
            ),
            "per_recording": der_by_id,
            "collar_025_per_recording": der_025_by_id,
            "collar_0_per_recording": der_0_by_id,
            "overlap_regions_per_recording": der_regions_by_id,
            "predicted_segments": diarization_by_id,
            "global_der": der_default["global"],
            "macro_der": der_default["macro"],
            "global_der_collar_025": der_standard["global"],
            "macro_der_collar_025": der_standard["macro"],
            "global_der_collar_0": der_strict["global"],
            "macro_der_collar_0": der_strict["macro"],
            "miss_s_collar_025": der_standard["miss"],
            "false_alarm_s_collar_025": der_standard["false_alarm"],
            "confusion_s_collar_025": der_standard["confusion"],
            "speaker_count_accuracy": (
                exact_speaker_counts / speaker_count_rows if speaker_count_rows else None
            ),
            "overlap_region_der": overlap_error / overlap_reference_time if overlap_reference_time else None,
            "non_overlap_der": non_overlap_error / non_overlap_reference_time if non_overlap_reference_time else None,
        },
        "models": {},
    }

    for alias in args.models:
        adapter = None
        failure_class = None
        failure_message = None
        failure_traceback = None
        oom_failure = False
        elapsed = 0.0
        try:
            try:
                adapter = load_adapter(
                    alias,
                    beam_size=args.beam_size,
                    revision=requested_revisions.get(alias),
                )
            except Exception as exc:
                hypotheses = [""] * len(records)
                failure_class = type(exc).__name__
                failure_message = str(exc)
                failure_traceback = traceback.format_exc()
                oom_failure = _is_oom_error(exc)
            else:
                started = time.perf_counter()
                try:
                    hypotheses = adapter.transcribe(
                        [record["audio_path"] for record in records], args.batch_size
                    )
                    if len(hypotheses) != len(records):
                        raise RuntimeError(
                            f"{alias} returned {len(hypotheses)} transcripts for {len(records)} recordings"
                        )
                except Exception as exc:
                    hypotheses = [""] * len(records)
                    failure_class = type(exc).__name__
                    failure_message = str(exc)
                    failure_traceback = traceback.format_exc()
                    oom_failure = _is_oom_error(exc)
                elapsed = time.perf_counter() - started

            if failure_class:
                results["models"][alias] = {
                    "repo": MODEL_SPECS[alias]["repo"],
                    "license": MODEL_SPECS[alias]["license"],
                    "revision": getattr(adapter, "revision", None) or requested_revisions.get(alias),
                    "requested_revision": requested_revisions.get(alias),
                    "status": "failed",
                    "failure": {
                        "class": failure_class,
                        "message": failure_message or "",
                        "traceback": failure_traceback or "",
                    },
                    "content_wer": None,
                    "content_errors": None,
                    "content_reference_words": None,
                    "verbatim_wer": None,
                    "verbatim_errors": None,
                    "verbatim_reference_words": None,
                    "cer": None,
                    "cer_errors": None,
                    "cer_reference_characters": None,
                    "speaker_attributed_wer": None,
                    "elapsed_s": elapsed,
                    "rtf": None,
                    "failure_count": len(records),
                    "oom_count": len(records) if oom_failure else 0,
                    "empty_output_count": len(records),
                    "hallucination_on_silence_count": None,
                    "peak_vram_mb": None,
                    "per_recording": {
                        record["id"]: {
                            "hypothesis": "",
                            "scored": False,
                            "failure_class": failure_class,
                            "failure_message": failure_message or "",
                        }
                        for record in records
                    },
                }
                continue

            content_stats = []
            verbatim_stats = []
            cer_stats = []
            per_recording = {}

            for record, hypothesis in zip(records, hypotheses):
                content = word_error_stats(
                    record["reference_text"],
                    hypothesis,
                    remove_fillers=True,
                    canonicalize_numbers=True,
                )
                verbatim = word_error_stats(
                    record["reference_text"],
                    hypothesis,
                    remove_fillers=False,
                    canonicalize_numbers=True,
                )
                cer = char_error_stats(
                    record["reference_text"],
                    hypothesis,
                    remove_fillers=True,
                    canonicalize_numbers=True,
                )
                content_stats.append(content)
                verbatim_stats.append(verbatim)
                cer_stats.append(cer)
                recording_result = {
                    "content_wer": content.rate,
                    "content_errors": content.errors,
                    "content_reference_words": content.reference_units,
                    "verbatim_wer": verbatim.rate,
                    "verbatim_errors": verbatim.errors,
                    "verbatim_reference_words": verbatim.reference_units,
                    "cer": cer.rate,
                    "cer_errors": cer.errors,
                    "cer_reference_characters": cer.reference_units,
                    "hypothesis": hypothesis,
                }
                if failure_class:
                    recording_result["failure_class"] = failure_class
                per_recording[record["id"]] = recording_result

            content_total = aggregate_error_stats(content_stats)
            verbatim_total = aggregate_error_stats(verbatim_stats)
            cer_total = aggregate_error_stats(cer_stats)
            model_result = {
                "repo": MODEL_SPECS[alias]["repo"],
                "license": MODEL_SPECS[alias]["license"],
                "revision": getattr(adapter, "revision", None),
                "requested_revision": requested_revisions.get(alias),
                "status": "success",
                "failure": None,
                "content_wer": content_total.rate,
                "content_errors": content_total.errors,
                "content_reference_words": content_total.reference_units,
                "verbatim_wer": verbatim_total.rate,
                "verbatim_errors": verbatim_total.errors,
                "verbatim_reference_words": verbatim_total.reference_units,
                "cer": cer_total.rate,
                "cer_errors": cer_total.errors,
                "cer_reference_characters": cer_total.reference_units,
                "elapsed_s": elapsed,
                "rtf": elapsed / total_audio_s if total_audio_s and not failure_class else None,
                "failure_count": len(records) if failure_class else 0,
                "oom_count": len(records) if oom_failure else 0,
                "empty_output_count": sum(not str(value).strip() for value in hypotheses),
                "hallucination_on_silence_count": None,
                "peak_vram_mb": None,
                "per_recording": per_recording,
            }

            if adapter is not None and not failure_class and not args.no_speaker_attributed and mappings:
                model_result.update(
                    speaker_attributed_stats(
                        adapter,
                        records,
                        diarization_by_id,
                        mappings,
                        args.batch_size,
                    )
                )
            results["models"][alias] = model_result
        finally:
            if adapter is not None:
                adapter.unload()

    der_values = [item["der"] for item in der_by_id.values()]
    for alias, model_result in results["models"].items():
        model_result["evidence"] = build_model_dataset_evidence(results, alias)
    results["diarization"]["macro_der"] = (
        sum(der_values) / len(der_values) if der_values else None
    )
    der_reference_time = sum(
        item["reference_speaker_time"] for item in der_by_id.values()
    )
    der_error_time = sum(
        item["miss"] + item["false_alarm"] + item["confusion"]
        for item in der_by_id.values()
    )
    results["diarization"]["global_der"] = (
        der_error_time / der_reference_time if der_reference_time else None
    )
    ranking = sorted(
        (
            alias for alias, row in results["models"].items()
            if row.get("status") == "success" and row.get("content_wer") is not None
        ),
        key=lambda alias: results["models"][alias]["content_wer"],
    )
    failed_aliases = [
        alias for alias, row in results["models"].items()
        if row.get("status") == "failed"
    ]
    results["ranking_by_content_wer"] = ranking
    (args.output / "results.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    with (args.output / "summary.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "rank",
                "model",
                "status",
                "content_wer",
                "verbatim_wer",
                "cer",
                "speaker_attributed_wer",
                "rtf",
                "failure_class",
                "failure_message",
            ]
        )
        for rank, alias in enumerate(ranking, start=1):
            row = results["models"][alias]
            writer.writerow(
                [
                    rank,
                    alias,
                    row.get("status", "success"),
                    row["content_wer"],
                    row["verbatim_wer"],
                    row["cer"],
                    row.get("speaker_attributed_wer"),
                    row["rtf"],
                    None,
                    None,
                ]
            )
        for alias in failed_aliases:
            row = results["models"][alias]
            writer.writerow([
                "", alias, "failed", row.get("content_wer"), row.get("verbatim_wer"),
                row.get("cer"), row.get("speaker_attributed_wer"), row.get("rtf"),
                (row.get("failure") or {}).get("class"),
                (row.get("failure") or {}).get("message"),
            ])

    lines = [
        "# Danish STT benchmark",
        "",
        f"Audio: {total_audio_s / 60:.1f} min across {len(records)} recording(s)",
        (
            f"Nemotron global DER: {results['diarization']['global_der']:.3%}"
            if der_values
            else "Nemotron DER: no reference segments supplied"
        ),
        "",
    ]
    if ranking:
        lines.extend([
            "| Rank | Model | Content WER | Verbatim WER | CER | Speaker WER | RTF |",
            "|---:|---|---:|---:|---:|---:|---:|",
        ])
        for rank, alias in enumerate(ranking, start=1):
            row = results["models"][alias]
            speaker = row.get("speaker_attributed_wer")
            rtf = row.get("rtf")
            rtf_cell = f"{rtf:.3f}" if rtf is not None else "n/a"
            speaker_cell = f"{speaker:.2%}" if speaker is not None else "n/a"
            lines.append(
                f"| {rank} | {alias} | {row['content_wer']:.2%} | "
                f"{row['verbatim_wer']:.2%} | {row['cer']:.2%} | "
                f"{speaker_cell} | {rtf_cell} |"
            )
    else:
        lines.append("No successful model produced scored results.")
    if failed_aliases:
        lines.extend([
            "",
            "## Failed candidates",
            "",
            "Failed runs are unscored and excluded from ranking.",
            "",
            "| Model | Failure |",
            "|---|---|",
        ])
        for alias in failed_aliases:
            failure = results["models"][alias]["failure"]
            lines.append(f"| {alias} | {failure['class']}: {failure['message']} |")

    (args.output / "summary.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    print("\n".join(lines))
    return 1 if failed_aliases else 0


if __name__ == "__main__":
    raise SystemExit(main())
