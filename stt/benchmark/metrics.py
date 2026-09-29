from __future__ import annotations

import itertools
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

FILLERS = {"øh", "øhm", "uh", "uhm", "eh", "hmm", "mm"}


def _strip_punctuation(text: str) -> str:
    chars = []
    for index, ch in enumerate(text):
        category = unicodedata.category(ch)
        previous = text[index - 1] if index else ""
        following = text[index + 1] if index + 1 < len(text) else ""
        if ch in {"'", "’"} and previous.isalnum() and following.isalnum():
            chars.append("'")
        elif category.startswith(("P", "S")):
            chars.append(" ")
        else:
            chars.append(ch)
    return re.sub(r"\s+", " ", "".join(chars)).strip()


def _canonicalize_danish_numbers(text: str) -> str:
    try:
        from num2words import num2words
        from text_to_num import alpha2digit
    except ImportError as exc:
        raise RuntimeError(
            "Danish number normalization requires num2words and text2num"
        ) from exc

    protected = (
        text.replace(" en ", " zzzenarticlezzz ")
        .replace(" et ", " zzzetarticlezzz ")
    )
    protected = re.sub(r"^en\b", "zzzenarticlezzz", protected)
    protected = re.sub(r"^et\b", "zzzetarticlezzz", protected)
    protected = re.sub(r"\ben$", "zzzenarticlezzz", protected)
    protected = re.sub(r"\bet$", "zzzetarticlezzz", protected)
    converted = alpha2digit(protected, "da")
    converted = converted.replace("zzzenarticlezzz", "en").replace(
        "zzzetarticlezzz", "et"
    )

    def expand(match: re.Match[str]) -> str:
        return str(num2words(int(match.group(0)), lang="da"))

    return re.sub(r"(?<!\w)\d+(?!\w)", expand, converted)


def normalize_text(
    text: str,
    *,
    remove_fillers: bool = False,
    canonicalize_numbers: bool = False,
) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(?<=\d)[.,](?=\d)", "", text)
    text = text.lower()
    text = _strip_punctuation(text)
    if canonicalize_numbers:
        text = _canonicalize_danish_numbers(text)
        text = _strip_punctuation(text)
    words = text.split()
    if remove_fillers:
        words = [word for word in words if word not in FILLERS]
    return " ".join(words)


@dataclass(frozen=True)
class ErrorStats:
    errors: int
    reference_units: int

    @property
    def rate(self) -> float:
        if self.reference_units == 0:
            return 0.0 if self.errors == 0 else 1.0
        return self.errors / self.reference_units


def edit_distance(reference: list[str], hypothesis: list[str]) -> int:
    previous = list(range(len(hypothesis) + 1))
    for i, ref_item in enumerate(reference, start=1):
        current = [i]
        for j, hyp_item in enumerate(hypothesis, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[j] + 1,
                    previous[j - 1] + (ref_item != hyp_item),
                )
            )
        previous = current
    return previous[-1]


def word_error_stats(
    reference: str,
    hypothesis: str,
    *,
    remove_fillers: bool = False,
    canonicalize_numbers: bool = False,
) -> ErrorStats:
    ref = normalize_text(
        reference,
        remove_fillers=remove_fillers,
        canonicalize_numbers=canonicalize_numbers,
    ).split()
    hyp = normalize_text(
        hypothesis,
        remove_fillers=remove_fillers,
        canonicalize_numbers=canonicalize_numbers,
    ).split()
    return ErrorStats(edit_distance(ref, hyp), len(ref))


def char_error_stats(
    reference: str,
    hypothesis: str,
    *,
    remove_fillers: bool = False,
    canonicalize_numbers: bool = False,
) -> ErrorStats:
    ref = list(
        normalize_text(
            reference,
            remove_fillers=remove_fillers,
            canonicalize_numbers=canonicalize_numbers,
        ).replace(" ", "")
    )
    hyp = list(
        normalize_text(
            hypothesis,
            remove_fillers=remove_fillers,
            canonicalize_numbers=canonicalize_numbers,
        ).replace(" ", "")
    )
    return ErrorStats(edit_distance(ref, hyp), len(ref))


def _active_sets(segments: list[dict], frame_s: float, frame_count: int) -> list[set[str]]:
    frames: list[set[str]] = [set() for _ in range(frame_count)]
    for segment in segments:
        start = max(0, int(math.floor(float(segment["start"]) / frame_s)))
        end = min(frame_count, int(math.ceil(float(segment["end"]) / frame_s)))
        speaker = str(segment["speaker"])
        for idx in range(start, end):
            frames[idx].add(speaker)
    return frames


def _collar_mask(segments: list[dict], frame_s: float, frame_count: int, collar_s: float) -> list[bool]:
    ignored = [False] * frame_count
    radius = int(math.ceil(collar_s / frame_s))
    if radius <= 0:
        return ignored
    for segment in segments:
        for boundary in (float(segment["start"]), float(segment["end"])):
            center = int(round(boundary / frame_s))
            lo, hi = max(0, center - radius), min(frame_count, center + radius + 1)
            ignored[lo:hi] = [True] * (hi - lo)
    return ignored


def _der_counts(
    reference_frames: list[set[str]],
    hypothesis_frames: list[set[str]],
    mapping: dict[str, str | None],
    ignored: list[bool],
) -> tuple[int, int, int, int]:
    miss = false_alarm = confusion = reference_speaker_frames = 0
    for idx, (ref, hyp) in enumerate(zip(reference_frames, hypothesis_frames)):
        if ignored[idx]:
            continue
        mapped = {mapping.get(speaker) for speaker in hyp}
        mapped.discard(None)
        correct = len(ref & mapped)
        reference_speaker_frames += len(ref)
        miss += max(0, len(ref) - len(mapped))
        false_alarm += max(0, len(mapped) - len(ref))
        confusion += max(0, min(len(ref), len(mapped)) - correct)
    return miss, false_alarm, confusion, reference_speaker_frames


def diarization_error(
    reference_segments: list[dict],
    hypothesis_segments: list[dict],
    *,
    duration_s: float,
    frame_s: float = 0.01,
    collar_s: float = 0.25,
) -> dict:
    frame_count = max(1, int(math.ceil(duration_s / frame_s)))
    ref_frames = _active_sets(reference_segments, frame_s, frame_count)
    hyp_frames = _active_sets(hypothesis_segments, frame_s, frame_count)
    ignored = _collar_mask(reference_segments, frame_s, frame_count, collar_s)

    ref_speakers = sorted({str(s["speaker"]) for s in reference_segments})
    hyp_speakers = sorted({str(s["speaker"]) for s in hypothesis_segments})
    candidate_targets: list[str | None] = ref_speakers + [None] * max(0, len(hyp_speakers) - len(ref_speakers))

    if not hyp_speakers:
        mappings = [{}]
    else:
        mappings = (
            dict(zip(hyp_speakers, perm))
            for perm in set(itertools.permutations(candidate_targets, len(hyp_speakers)))
        )

    best = None
    best_mapping: dict[str, str | None] = {}
    for mapping in mappings:
        counts = _der_counts(ref_frames, hyp_frames, mapping, ignored)
        errors = counts[0] + counts[1] + counts[2]
        if best is None or errors < best[0]:
            best = (errors, counts)
            best_mapping = mapping

    assert best is not None
    miss, false_alarm, confusion, denominator = best[1]
    rate = (miss + false_alarm + confusion) / denominator if denominator else 0.0
    return {
        "der": rate,
        "miss": miss * frame_s,
        "false_alarm": false_alarm * frame_s,
        "confusion": confusion * frame_s,
        "reference_speaker_time": denominator * frame_s,
        "mapping": best_mapping,
        "collar_s": collar_s,
        "frame_s": frame_s,
    }


def aggregate_error_stats(stats: Iterable[ErrorStats]) -> ErrorStats:
    stats = list(stats)
    return ErrorStats(sum(item.errors for item in stats), sum(item.reference_units for item in stats))
