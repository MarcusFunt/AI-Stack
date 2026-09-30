"""SamtaleBank window construction and local media preparation primitives."""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import shutil
import statistics
import subprocess
import wave
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from .base import DatasetAdapter, DatasetSpec
from .chat import parse_chat_file
from .manifest import write_manifest
from .provenance import build_lock_entry, file_sha256, preparation_code_provenance, update_lock


def _validated_segments(segments: Iterable[dict]) -> list[dict]:
    result = []
    for index, source in enumerate(segments, start=1):
        try:
            start = float(source["start"])
            end = float(source["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"segment {index} needs numeric start and end") from exc
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError(f"segment {index} has invalid timestamps")
        speaker = str(source.get("speaker", "")).strip()
        if not speaker:
            raise ValueError(f"segment {index} has a blank speaker")
        text = str(source.get("scoring_text", source.get("text", ""))).strip()
        if not text:
            continue
        result.append({**source, "start": start, "end": end, "speaker": speaker, "text": text})
    return sorted(result, key=lambda item: (item["start"], item["end"], item["speaker"]))


def window_metrics(segments: Iterable[dict], start_s: float, end_s: float) -> dict:
    start_s, end_s = float(start_s), float(end_s)
    if not math.isfinite(start_s) or not math.isfinite(end_s) or start_s < 0 or end_s <= start_s:
        raise ValueError("window must have finite start < end")
    duration = end_s - start_s
    events: dict[float, list[tuple[str, int]]] = defaultdict(list)
    turn_durations = []
    for segment in _validated_segments(segments):
        start = max(start_s, segment["start"])
        end = min(end_s, segment["end"])
        if end <= start:
            continue
        speaker = segment["speaker"]
        events[start].append((speaker, 1))
        events[end].append((speaker, -1))
        turn_durations.append(end - start)

    points = sorted(events)
    active: dict[str, int] = defaultdict(int)
    speech_s = overlap_s = 0.0
    speaker_time: dict[str, float] = defaultdict(float)
    for index, point in enumerate(points[:-1]):
        for speaker, delta in events[point]:
            active[speaker] += delta
            if active[speaker] <= 0:
                active.pop(speaker, None)
        next_point = points[index + 1]
        interval_s = next_point - point
        current = [speaker for speaker, count in active.items() if count > 0]
        if current:
            speech_s += interval_s
            for speaker in current:
                speaker_time[speaker] += interval_s
            if len(current) >= 2:
                overlap_s += interval_s

    speaker_total = sum(speaker_time.values())
    dominant_fraction = max(speaker_time.values(), default=0.0) / speaker_total if speaker_total else 0.0
    return {
        "speech_s": speech_s,
        "overlap_s": overlap_s,
        "speech_ratio": speech_s / duration,
        "overlap_ratio": overlap_s / duration,
        "speaker_time_s": dict(sorted(speaker_time.items())),
        "dominant_speaker_fraction": dominant_fraction,
        "median_turn_duration_s": statistics.median(turn_durations) if turn_durations else 0.0,
        "turn_count": len(turn_durations),
    }


def _safe_boundary(point: float, segments: list[dict]) -> bool:
    return not any(
        segment["start"] < point - 1e-9 and segment["end"] > point + 1e-9
        for segment in segments
    )


def _silence_margin(point: float, segments: list[dict]) -> float:
    previous = [segment["end"] for segment in segments if segment["end"] <= point]
    following = [segment["start"] for segment in segments if segment["start"] >= point]
    left_gap = point - max(previous) if previous else point
    right_gap = min(following) - point if following else 0.0
    return min(left_gap, right_gap)


def build_windows(
    segments: Iterable[dict],
    *,
    duration_s: float,
    target_s: float = 90.0,
    min_s: float = 60.0,
    max_s: float = 120.0,
    min_speaker_s: float = 3.0,
    seed: int = 20260930,
) -> list[dict]:
    duration_s = float(duration_s)
    if not math.isfinite(duration_s) or duration_s <= 0:
        raise ValueError("source duration must be positive and finite")
    if not 0 < min_s <= target_s <= max_s:
        raise ValueError("window lengths must satisfy 0 < min_s <= target_s <= max_s")
    if min_speaker_s < 0:
        raise ValueError("min_speaker_s must not be negative")

    items = _validated_segments(segments)
    if not items:
        return []
    if any(segment["end"] > duration_s + 0.02 for segment in items):
        raise ValueError("a reference segment extends beyond source audio duration")

    boundaries = sorted(
        {0.0, duration_s}
        | {segment[edge] for segment in items for edge in ("start", "end")}
    )
    randomizer = random.Random(seed)
    windows = []
    cursor = 0.0
    rejected_speaker_count = 0
    rejected_speaker_time = 0

    while cursor + min_s <= duration_s + 1e-9:
        starts = [
            point for point in boundaries
            if cursor - 1e-9 <= point <= min(cursor + 30.0, duration_s - min_s) + 1e-9
            and _safe_boundary(point, items)
        ]
        candidates = []
        for start in starts:
            ends = [
                point for point in boundaries
                if start + min_s - 1e-9 <= point <= min(start + max_s, duration_s) + 1e-9
                and point > start + 1e-9
                and _safe_boundary(point, items)
            ]
            for end in ends:
                inside = [
                    segment for segment in items
                    if segment["start"] >= start - 1e-9 and segment["end"] <= end + 1e-9
                ]
                stats = window_metrics(inside, start, end)
                speaker_times = stats["speaker_time_s"]
                if len(speaker_times) != 3:
                    rejected_speaker_count += 1
                    continue
                if any(value + 1e-9 < min_speaker_s for value in speaker_times.values()):
                    rejected_speaker_time += 1
                    continue
                score = (
                    round(start - cursor, 6),
                    round(abs((end - start) - target_s), 6),
                    round(-_silence_margin(end, items), 6),
                    round(end, 6),
                )
                candidates.append((score, start, end, inside, stats))

        if not candidates:
            next_points = [point for point in boundaries if point >= cursor + 30.0 - 1e-9]
            if not next_points:
                break
            cursor = min(next_points)
            continue

        best_score = min(candidate[0] for candidate in candidates)
        tied = [candidate for candidate in candidates if candidate[0] == best_score]
        _, start, end, inside, stats = randomizer.choice(tied)
        relative_segments = []
        for segment in inside:
            relative = dict(segment)
            relative["start"] = max(0.0, segment["start"] - start)
            relative["end"] = min(end - start, segment["end"] - start)
            relative_segments.append(relative)
        relative_segments.sort(key=lambda item: (item["start"], item["end"], item["speaker"]))
        text = " ".join(segment["text"] for segment in relative_segments if segment["text"]).strip()
        window_id = f"samtalebank-sam3-{int(round(start * 1000)):09d}-{int(round(end * 1000)):09d}"
        windows.append(
            {
                "id": window_id,
                "start_s": start,
                "end_s": end,
                "duration_s": end - start,
                "speaker_count": len(stats["speaker_time_s"]),
                "text": text,
                "segments": relative_segments,
                "metadata": {
                    "window_start_source_s": start,
                    "window_end_source_s": end,
                    "speech_ratio": stats["speech_ratio"],
                    "overlap_ratio": stats["overlap_ratio"],
                    "speech_s": stats["speech_s"],
                    "overlap_s": stats["overlap_s"],
                    "speaker_time_s": stats["speaker_time_s"],
                    "dominant_speaker_fraction": stats["dominant_speaker_fraction"],
                    "median_turn_duration_s": stats["median_turn_duration_s"],
                    "turn_count": stats["turn_count"],
                },
            }
        )
        cursor = end

    if windows:
        windows[0]["_selection_diagnostics"] = {
            "windows_rejected_missing_speaker": rejected_speaker_count,
            "windows_rejected_low_speaker_time": rejected_speaker_time,
        }
    return windows


class TalkBankAccessError(RuntimeError):
    """The local Sam3 materials are not available for preparation."""


def _wave_info(path: Path) -> tuple[int, int, int, int]:
    try:
        with wave.open(str(path), "rb") as handle:
            return (
                handle.getnchannels(),
                handle.getsampwidth(),
                handle.getframerate(),
                handle.getnframes(),
            )
    except (wave.Error, EOFError, OSError) as exc:
        raise ValueError(f"not a readable PCM WAV: {path}") from exc


def _validate_benchmark_wav(path: Path) -> float:
    channels, width, rate, frames = _wave_info(path)
    if channels != 1 or width != 2 or rate != 16000 or frames <= 0:
        raise ValueError(
            f"expected non-empty mono 16 kHz PCM16 WAV, got channels={channels}, "
            f"sample_width={width}, sample_rate={rate}, frames={frames}: {path}"
        )
    return frames / float(rate)


def extract_audio_to_wav(source_path: Path, output_path: Path) -> Path:
    """Extract the complete source audio track as mono 16 kHz PCM16 without gain changes."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to extract Sam3 video audio; install ffmpeg and retry")
    source_path, output_path = Path(source_path), Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-y",
        str(output_path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()[-800:]
        raise RuntimeError(f"ffmpeg could not extract audio from {source_path}: {detail}")
    _validate_benchmark_wav(output_path)
    return output_path


def cut_wav_window(source_path: Path, output_path: Path, start_s: float, end_s: float) -> Path:
    """Cut an exact sample-aligned interval from a prepared mono 16 kHz PCM16 WAV."""
    source_path, output_path = Path(source_path), Path(output_path)
    start_s, end_s = float(start_s), float(end_s)
    if not math.isfinite(start_s) or not math.isfinite(end_s) or start_s < 0 or end_s <= start_s:
        raise ValueError("audio window must have finite start < end")
    channels, width, rate, frames = _wave_info(source_path)
    if channels != 1 or width != 2 or rate != 16000:
        raise ValueError("source WAV must be mono 16 kHz PCM16")
    start_frame = int(round(start_s * rate))
    end_frame = int(round(end_s * rate))
    if start_frame < 0 or end_frame <= start_frame or end_frame > frames:
        raise ValueError("audio window is outside source WAV bounds")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(source_path), "rb") as source:
        params = source.getparams()
        source.setpos(start_frame)
        frames_data = source.readframes(end_frame - start_frame)
    with wave.open(str(output_path), "wb") as output:
        output.setparams(params)
        output.writeframes(frames_data)
    return output_path


def _access_message(source_path: Path) -> str:
    return (
        "SamtaleBank Sam3 files were not found at the local source path. Register or log in to "
        "TalkBank, accept the applicable SamtaleBank ground rules, download the Sam3 transcript "
        "archive and its linked media, extract them locally, then rerun with "
        f"-SourcePath '{source_path}'. The preparation command uses local files and does not "
        "bypass TalkBank access controls."
    )


def _find_media(transcript_path: Path, source_root: Path, media_by_stem: dict[str, list[Path]]) -> Path:
    parsed = parse_chat_file(transcript_path)
    if not parsed.media_name:
        raise ValueError(f"{transcript_path}: @Media header is required")
    candidates = media_by_stem.get(parsed.media_name.lower(), [])
    same_directory = [item for item in candidates if item.parent == transcript_path.parent]
    if len(same_directory) == 1:
        return same_directory[0]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise FileNotFoundError(
            f"{transcript_path}: media '{parsed.media_name}' named in @Media was not found under {source_root}"
        )
    raise ValueError(f"{transcript_path}: multiple media files match @Media '{parsed.media_name}'")


def _source_revision(files: list[Path], source_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(source_root).as_posix().lower()):
        relative = path.relative_to(source_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_sha256(path).encode("ascii"))
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _git_revision() -> str:
    repository_root = Path(__file__).resolve().parents[3]
    try:
        result = subprocess.run(
            ["git", "-C", str(repository_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _safe_stem(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-_") or "recording"


def prepare_samtalebank(
    source_path: Path,
    output_dir: Path,
    *,
    lock_path: Path | None = None,
    source_license: str = "CC-BY-NC-SA-3.0",
    source_url: str = "https://talkbank.org/samtale/access/Sam3.html",
    doi: str = "10.21415/T5X60H",
    seed: int = 20260930,
) -> Path:
    """Prepare all eligible Sam3 conversation windows from a local extracted corpus tree."""
    if source_path is None:
        raise TalkBankAccessError(_access_message(Path("(not provided)")))
    source_root = Path(source_path).resolve()
    output_dir = Path(output_dir).resolve()
    if not source_root.exists() or not source_root.is_dir():
        raise TalkBankAccessError(_access_message(source_path))
    transcript_files = sorted(
        (path for path in source_root.rglob("*.cha") if path.is_file()),
        key=lambda item: item.relative_to(source_root).as_posix().lower(),
    )
    if not transcript_files:
        raise TalkBankAccessError(_access_message(source_path))

    media_extensions = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".wav", ".mp3", ".m4a"}
    media_files = [
        path for path in source_root.rglob("*")
        if path.is_file() and path.suffix.lower() in media_extensions
    ]
    media_by_stem: dict[str, list[Path]] = defaultdict(list)
    for path in media_files:
        media_by_stem[path.stem.lower()].append(path)

    output_dir.mkdir(parents=True, exist_ok=True)
    audio_dir = output_dir / "audio"
    parsed_sources = []
    all_source_files = []
    diagnostics = defaultdict(int)
    for transcript_path in transcript_files:
        parsed = parse_chat_file(transcript_path)
        media_path = _find_media(transcript_path, source_root, media_by_stem)
        media_type = parsed.media_type or ("video" if media_path.suffix.lower() in {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg"} else "audio")
        if media_type not in {"audio", "video"}:
            raise ValueError(f"{transcript_path}: unsupported @Media type {media_type!r}")
        all_source_files.extend([transcript_path, media_path])
        for key, value in parsed.diagnostics.items():
            if isinstance(value, (int, float)):
                diagnostics[key] += int(value)

        timed_segments = []
        for utterance in parsed.utterances:
            for segment in utterance.timed_segments:
                timed_segments.append(
                    {
                        "start": segment.start_ms / 1000.0,
                        "end": segment.end_ms / 1000.0,
                        "speaker": utterance.speaker,
                        "text": segment.scoring_text,
                        "scoring_text": segment.scoring_text,
                        "raw_text": segment.raw_text,
                    }
                )
        parsed_sources.append((transcript_path, media_path, parsed, timed_segments))

    lexical_lines = diagnostics["lexical_speaker_tier_count"]
    untimed_lines = diagnostics["lexical_utterances_without_timing"]
    if lexical_lines and untimed_lines / lexical_lines > 0.01:
        raise ValueError(
            f"{untimed_lines}/{lexical_lines} lexical speaker tiers lack usable timing "
            f"({untimed_lines / lexical_lines:.1%}); inspect Sam3 transcript timing before preparation"
        )

    rows = []
    prepared_files = []
    validation = {
        "dataset": "samtalebank-sam3",
        "source_files": len(set(all_source_files)),
        "parsed_speaker_tiers": diagnostics["speaker_tier_count"],
        "lexical_speaker_tier_count": lexical_lines,
        "lexical_utterances_without_timing": untimed_lines,
        "skipped_non_speech_tiers": diagnostics["skipped_non_speech_tiers"],
        "utterances_with_no_media_bullet": diagnostics["utterances_without_bullet"],
        "malformed_bullet_lines": diagnostics["malformed_bullet_count"],
        "multiple_bullet_utterances": diagnostics["multiple_bullet_utterances"],
        "windows_rejected_and_reason": {
            "missing_one_or_more_speakers": 0,
            "less_than_3_seconds_per_speaker": 0,
            "no_eligible_window": 0,
        },
        "reference_words": 0,
        "overlap_seconds": 0.0,
        "total_speaker_time_s": 0.0,
        "windows": 0,
    }

    for source_index, (transcript_path, media_path, parsed, timed_segments) in enumerate(parsed_sources):
        recording_rel = transcript_path.relative_to(source_root).with_suffix("").as_posix()
        recording_name = _safe_stem(recording_rel.replace("/", "-"))
        source_audio = audio_dir / f"{recording_name}-source.wav"
        if (parsed.media_type or "").lower() == "video" or media_path.suffix.lower() != ".wav":
            extract_audio_to_wav(media_path, source_audio)
        else:
            channels, width, rate, frames = _wave_info(media_path)
            if channels == 1 and width == 2 and rate == 16000:
                source_audio.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(media_path, source_audio)
            else:
                extract_audio_to_wav(media_path, source_audio)
        duration_s = _validate_benchmark_wav(source_audio)
        source_hash = file_sha256(media_path)
        transcript_hash = file_sha256(transcript_path)
        windows = build_windows(
            timed_segments,
            duration_s=duration_s,
            seed=seed + source_index,
        )
        if not windows:
            validation["windows_rejected_and_reason"]["no_eligible_window"] += 1
            continue
        selection = windows[0].pop("_selection_diagnostics", {})
        validation["windows_rejected_and_reason"]["missing_one_or_more_speakers"] += selection.get(
            "windows_rejected_missing_speaker", 0
        )
        validation["windows_rejected_and_reason"]["less_than_3_seconds_per_speaker"] += selection.get(
            "windows_rejected_low_speaker_time", 0
        )

        for window in windows:
            start_s, end_s = window["start_s"], window["end_s"]
            window_id = (
                f"samtalebank-sam3-{recording_name}-"
                f"{int(round(start_s * 1000)):09d}-{int(round(end_s * 1000)):09d}"
            )
            output_audio = audio_dir / f"{recording_name}-{int(round(start_s * 1000)):09d}-{int(round(end_s * 1000)):09d}.wav"
            cut_wav_window(source_audio, output_audio, start_s, end_s)
            prepared_files.append(output_audio)
            segments = [
                {
                    "start": segment["start"],
                    "end": segment["end"],
                    "speaker": segment["speaker"],
                    "text": segment["text"],
                    "raw_text": segment.get("raw_text", segment["text"]),
                }
                for segment in window["segments"]
            ]
            metadata = {
                **window["metadata"],
                "bootstrap_group": recording_rel,
                "bootstrap_group_type": "source_recording",
                "reference_semantics": "chronological_single_stream",
                "source_license": source_license,
                "source_url": source_url,
                "source_hash": source_hash,
                "transcript_hash": transcript_hash,
                "reference_transform": "talkbank-ca-v1",
            }
            rows.append(
                {
                    "schema_version": 2,
                    "id": window_id,
                    "dataset": "samtalebank-sam3",
                    "dataset_class": "PRIMARY-INDEPENDENTISH",
                    "source_recording": recording_rel,
                    "audio": output_audio.relative_to(output_dir).as_posix(),
                    "speaker_count": 3,
                    "text": window["text"],
                    "segments": segments,
                    "metadata": metadata,
                }
            )
            validation["windows"] += 1
            validation["reference_words"] += len(window["text"].split())
            validation["overlap_seconds"] += window["metadata"]["overlap_s"]
            validation["total_speaker_time_s"] += sum(window["metadata"]["speaker_time_s"].values())
        prepared_files.append(source_audio)

    if not rows:
        (output_dir / "dataset-validation.json").write_text(
            json.dumps(validation, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        raise ValueError(
            "No eligible Sam3 windows were prepared. Each window needs 60–120 seconds, "
            "all three reference speakers, and at least 3 seconds of speech per speaker."
        )

    manifest_path = write_manifest(output_dir / "manifest.jsonl", rows)
    validation_path = output_dir / "dataset-validation.json"
    validation["overlap_seconds"] = round(validation["overlap_seconds"], 6)
    validation["total_speaker_time_s"] = round(validation["total_speaker_time_s"], 6)
    validation_path.write_text(
        json.dumps(validation, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    lock_path = Path(lock_path) if lock_path else (
        Path(__file__).resolve().parents[3] / "data" / "stt-benchmark" / "dataset-lock.json"
    )
    lock_root = lock_path.resolve().parent
    lock_files = [path for path in set(prepared_files + [manifest_path, validation_path]) if path.is_file()]
    preparation_code = preparation_code_provenance()
    entry = build_lock_entry(
        dataset="samtalebank-sam3",
        dataset_class="PRIMARY-INDEPENDENTISH",
        source_url=source_url,
        doi=doi,
        license=source_license,
        revision=_source_revision(list(set(all_source_files)), source_root),
        files=lock_files,
        root=lock_root,
        preparation_code_git_sha=preparation_code["git_sha"],
        preparation_code=preparation_code,
        reference_transform="talkbank-ca-v1",
    )
    entry["source_files_relative_to"] = "SourcePath"
    for source_file in set(all_source_files):
        source_relative = source_file.relative_to(source_root).as_posix()
        entry["files"].append({
            "path": f"source/{source_relative}",
            "sha256": file_sha256(source_file),
        })
    entry["files"].sort(key=lambda item: item["path"].lower())
    update_lock(lock_path, entry)
    return manifest_path



class SamtaleBankSam3Adapter(DatasetAdapter):
    spec = DatasetSpec(
        dataset="samtalebank-sam3",
        dataset_class="PRIMARY-INDEPENDENTISH",
        source_url="https://talkbank.org/samtale/access/Sam3.html",
        license="CC-BY-NC-SA-3.0",
        doi="10.21415/T5X60H",
    )

    def prepare(
        self, source_path: Path | None, output_dir: Path, **options: Any
    ) -> Path:
        return prepare_samtalebank(source_path, output_dir, **options)
