from __future__ import annotations

import argparse
import csv
import json
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .diarization import NemotronDiarizer
from .metrics import aggregate_error_stats, char_error_stats, diarization_error, word_error_stats
from .models import MODEL_SPECS, load_adapter
from .revisions import parse_revision_overrides


def package_versions() -> dict[str, str]:
    names = ["torch", "transformers", "librosa", "num2words", "text2num"]
    versions = {}
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
        item["reference_text"] = str(
            item.get("text") or " ".join(s["text"] for s in segments)
        ).strip()
        if not item["reference_text"]:
            raise ValueError(
                f"{path}:{number}: recording requires reference text for ASR scoring"
            )
        rows.append(item)
    if not rows:
        raise ValueError("manifest contains no recordings")
    return rows


def audio_duration(path: str) -> float:
    import librosa
    return float(librosa.get_duration(path=path))


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

    diarizer = NemotronDiarizer(revision=requested_revisions.get("nemotron"))
    diarization_by_id = {}
    der_by_id = {}
    mappings = {}
    try:
        for record in records:
            predicted = diarizer.diarize(record["audio_path"])
            diarization_by_id[record["id"]] = predicted
            if record["segments"]:
                score = diarization_error(
                    record["segments"],
                    predicted,
                    duration_s=durations[record["id"]],
                    collar_s=args.collar,
                )
                der_by_id[record["id"]] = score
                mappings[record["id"]] = score["mapping"]
    finally:
        diarizer.unload()

    results = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "manifest": str(args.manifest),
        "total_audio_s": total_audio_s,
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
            }
            for record in records
        ],
        "diarization": {
            "model": NemotronDiarizer.model_id,
            "revision": diarizer.revision,
            "per_recording": der_by_id,
            "predicted_segments": diarization_by_id,
        },
        "models": {},
    }

    for alias in args.models:
        adapter = load_adapter(
            alias,
            beam_size=args.beam_size,
            revision=requested_revisions.get(alias),
        )
        started = time.perf_counter()
        try:
            hypotheses = adapter.transcribe(
                [record["audio_path"] for record in records], args.batch_size
            )
            if len(hypotheses) != len(records):
                raise RuntimeError(
                    f"{alias} returned {len(hypotheses)} transcripts for {len(records)} recordings"
                )
            elapsed = time.perf_counter() - started
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
                per_recording[record["id"]] = {
                    "content_wer": content.rate,
                    "verbatim_wer": verbatim.rate,
                    "cer": cer.rate,
                    "hypothesis": hypothesis,
                }

            content_total = aggregate_error_stats(content_stats)
            verbatim_total = aggregate_error_stats(verbatim_stats)
            cer_total = aggregate_error_stats(cer_stats)
            model_result = {
                "repo": MODEL_SPECS[alias]["repo"],
                "license": MODEL_SPECS[alias]["license"],
                "revision": adapter.revision,
                "content_wer": content_total.rate,
                "verbatim_wer": verbatim_total.rate,
                "cer": cer_total.rate,
                "elapsed_s": elapsed,
                "rtf": elapsed / total_audio_s if total_audio_s else None,
                "per_recording": per_recording,
            }

            if not args.no_speaker_attributed and mappings:
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
            adapter.unload()

    der_values = [item["der"] for item in der_by_id.values()]
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
        results["models"],
        key=lambda alias: results["models"][alias]["content_wer"],
    )
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
                "content_wer",
                "verbatim_wer",
                "cer",
                "speaker_attributed_wer",
                "rtf",
            ]
        )
        for rank, alias in enumerate(ranking, start=1):
            row = results["models"][alias]
            writer.writerow(
                [
                    rank,
                    alias,
                    row["content_wer"],
                    row["verbatim_wer"],
                    row["cer"],
                    row.get("speaker_attributed_wer"),
                    row["rtf"],
                ]
            )

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
        "| Rank | Model | Content WER | Verbatim WER | CER | Speaker WER | RTF |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ]
    for rank, alias in enumerate(ranking, start=1):
        row = results["models"][alias]
        speaker = row.get("speaker_attributed_wer")
        if speaker is None:
            speaker_cell = "n/a"
        else:
            speaker_cell = f"{speaker:.2%}"
        lines.append(
            f"| {rank} | {alias} | {row['content_wer']:.2%} | "
            f"{row['verbatim_wer']:.2%} | {row['cer']:.2%} | "
            f"{speaker_cell} | {row['rtf']:.3f} |"
        )

    (args.output / "summary.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
