"""Focused CHAT/CA parser for speaker timing and auditable references."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_HIDDEN_BULLET = re.compile(r"\x15([^\x15]*)\x15")
_VISIBLE_BULLET = re.compile(r"·([^·]*)·")
_ANY_BULLET = re.compile(r"\x15[^\x15]*\x15|·[^·]*·")
_MAIN_TIER = re.compile(r"^\*([A-Za-z0-9_]+):(.*)$")
_HEADER = re.compile(r"^@([^:]+):\s*(.*)$")


@dataclass(frozen=True)
class ChatTimedSegment:
    start_ms: int
    end_ms: int
    raw_text: str
    scoring_text: str


@dataclass(frozen=True)
class ChatUtterance:
    speaker: str
    raw_text: str
    scoring_text: str
    timed_segments: tuple[ChatTimedSegment, ...] = field(default_factory=tuple)
    line_number: int = 0


@dataclass(frozen=True)
class ChatTranscript:
    media_name: str | None
    media_type: str | None
    participants: dict[str, dict[str, str]]
    utterances: tuple[ChatUtterance, ...]
    diagnostics: dict[str, Any]


def normalize_chat_text(raw_text: str) -> str:
    """Apply deterministic talkbank-ca-v1 lexical cleanup without rewriting words."""
    text = _ANY_BULLET.sub("", str(raw_text))
    # Parenthesized CHAT pauses and explanatory double-parenthesis comments.
    text = re.sub(r"\(\s*(?:\.+|h\.|\.h|\d+(?:\.\d+)?(?:\s*(?:s|sec))?)\s*\)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\(\(.*?\)\)", " ", text)
    # Explicit paralinguistic events are not lexical transcript content.
    text = re.sub(r"\[\s*=!.*?\]", " ", text)
    text = re.sub(r"&=[^\s<>]+", " ", text)
    text = re.sub(r"\[\s*(?:laugh(?:s|ing)?|laughter|cough(?:s|ing)?|sigh(?:s|ing)?|breath(?:ing)?)[^\]]*\]", " ", text, flags=re.IGNORECASE)
    # CA overlap marks and CHAT scoping delimiters; keep words between angle brackets.
    text = re.sub(r"\[(?:<|>)\]", " ", text)
    text = text.replace("<", "").replace(">", "")
    text = text.translate(str.maketrans("", "", "⌈⌉⌊⌋"))
    # CHAT retracing, pause/event, and utterance-link codes are structure, not words.
    text = re.sub(r"\[(?:/{1,3}|\\\\|\?)(?:\s*\d+)?\]", " ", text)
    text = re.sub(r"\[\s*x\s*\d+\s*\]", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<!\w)\+(?:/\.{1}|//\.?|///\.?|\.{3}|\^|,|\+)(?!\w)", " ", text)
    # Common CHAT word-form tags (e.g. word@o); preserve the lexical word.
    text = re.sub(r"(?<=\w)@(?:s|b|o|f|q|u|d|n|p|r|t|a|c|i)(?=\s|[.,?!;:]|$)", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _parse_time(payload: str) -> tuple[int, int] | None:
    value = payload.strip()
    # A leading dash means continuous playback in CHAT; it is not a negative time.
    if value.startswith("-"):
        value = value[1:]
    match = re.fullmatch(r"(\d+)_(\d+)", value)
    if not match:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    if end <= start:
        return None
    return start, end


def _is_lexical(text: str) -> bool:
    return any(character.isalnum() for character in text)


def _parse_participants(value: str) -> dict[str, dict[str, str]]:
    participants: dict[str, dict[str, str]] = {}
    entries = re.split(r",\s*(?=[A-Za-z0-9_]+\s+)", value.strip())
    for entry in entries:
        fields = entry.strip().split()
        if len(fields) < 2:
            continue
        speaker = fields[0]
        participants[speaker] = {
            "name": " ".join(fields[1:-1]) if len(fields) > 2 else "",
            "role": fields[-1],
        }
    return participants


def _parse_utterance(speaker: str, body: str, line_number: int) -> tuple[ChatUtterance, dict[str, int]]:
    raw_text = _ANY_BULLET.sub("", body)
    scoring_text = normalize_chat_text(raw_text)
    tokens = list(_HIDDEN_BULLET.finditer(body))
    tokens.extend(_VISIBLE_BULLET.finditer(body))
    tokens.sort(key=lambda match: match.start())

    timed_segments = []
    malformed = 0
    cursor = 0
    for token in tokens:
        chunk = body[cursor:token.start()]
        payload = token.group(1)
        parsed_time = _parse_time(payload)
        if parsed_time is None:
            malformed += 1
        else:
            chunk_text = normalize_chat_text(chunk)
            if _is_lexical(chunk_text):
                timed_segments.append(
                    ChatTimedSegment(
                        start_ms=parsed_time[0],
                        end_ms=parsed_time[1],
                        raw_text=chunk,
                        scoring_text=chunk_text,
                    )
                )
        cursor = token.end()

    # Text after a final bullet has no time alignment; do not infer one.
    trailing_text = body[cursor:]
    untimed_tail = int(bool(tokens) and _is_lexical(normalize_chat_text(trailing_text)))
    lexical = _is_lexical(scoring_text)
    no_bullet = int(not tokens and lexical)
    no_timing = int(lexical and not timed_segments)
    utterance = ChatUtterance(
        speaker=speaker,
        raw_text=raw_text,
        scoring_text=scoring_text,
        timed_segments=tuple(timed_segments),
        line_number=line_number,
    )
    counts = {
        "malformed_bullet_count": malformed,
        "utterances_without_bullet": no_bullet,
        "lexical_utterances_without_timing": no_timing,
        "untimed_lexical_chunks": untimed_tail,
        "multiple_bullet_utterances": int(len(tokens) > 1),
    }
    return utterance, counts


def parse_chat_text(text: str) -> ChatTranscript:
    media_name: str | None = None
    media_type: str | None = None
    participants: dict[str, dict[str, str]] = {}
    utterances = []
    skipped_non_speech_tiers = 0
    current_speaker: str | None = None
    current_line = 0
    current_body: list[str] = []

    def finish_current() -> None:
        nonlocal current_speaker, current_body, current_line
        if current_speaker is not None:
            utterance, counts = _parse_utterance(
                current_speaker, "\n".join(current_body), current_line
            )
            utterances.append((utterance, counts))
        current_speaker = None
        current_body = []

    for line_number, line in enumerate(str(text).splitlines(), start=1):
        main = _MAIN_TIER.match(line)
        if main:
            finish_current()
            current_speaker = main.group(1)
            current_body = [main.group(2)]
            current_line = line_number
            continue
        header = _HEADER.match(line)
        if header:
            finish_current()
            name, value = header.group(1).strip().lower(), header.group(2).strip()
            if name == "participants":
                participants = _parse_participants(value)
            elif name == "media":
                fields = [field.strip() for field in value.split(",")]
                if fields and fields[0]:
                    media_name = Path(fields[0]).stem
                if len(fields) > 1:
                    media_type = fields[1].lower()
            continue
        if line.startswith("%"):
            finish_current()
            skipped_non_speech_tiers += 1
            continue
        if line.startswith("@") or not line.strip():
            if line.startswith("@"):
                finish_current()
            continue
        if current_speaker is not None:
            current_body.append(line)

    finish_current()
    diagnostics: dict[str, Any] = {
        "speaker_tier_count": len(utterances),
        "lexical_speaker_tier_count": sum(_is_lexical(item.scoring_text) for item, _ in utterances),
        "skipped_non_speech_tiers": skipped_non_speech_tiers,
    }
    for key in (
        "malformed_bullet_count",
        "utterances_without_bullet",
        "lexical_utterances_without_timing",
        "untimed_lexical_chunks",
        "multiple_bullet_utterances",
    ):
        diagnostics[key] = sum(counts[key] for _, counts in utterances)
    return ChatTranscript(
        media_name=media_name,
        media_type=media_type,
        participants=participants,
        utterances=tuple(item for item, _ in utterances),
        diagnostics=diagnostics,
    )


def parse_chat_file(path: Path) -> ChatTranscript:
    return parse_chat_text(Path(path).read_text(encoding="utf-8-sig"))
