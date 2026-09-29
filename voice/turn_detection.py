from __future__ import annotations

import math
import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class TurnEvent:
    kind: str
    audio: bytes | None = None


class VoiceTurnDetector:
    """Small PCM16 energy VAD with silence hangover and bounded turn buffers."""

    def __init__(
        self,
        *,
        sample_rate: int = 16_000,
        speech_threshold: float = 420.0,
        end_silence_seconds: float = 0.65,
        max_turn_seconds: float = 90.0,
    ) -> None:
        if sample_rate <= 0 or speech_threshold <= 0 or end_silence_seconds <= 0 or max_turn_seconds <= 0:
            raise ValueError("VAD configuration values must be positive")
        self.sample_rate = sample_rate
        self.speech_threshold = speech_threshold
        self.end_silence_seconds = end_silence_seconds
        self.max_turn_bytes = int(sample_rate * 2 * max_turn_seconds)
        self._active = False
        self._silence_seconds = 0.0
        self._chunks: list[bytes] = []
        self._buffered_bytes = 0

    @property
    def active(self) -> bool:
        return self._active

    def feed(self, pcm16: bytes) -> list[TurnEvent]:
        if not pcm16:
            return []
        if len(pcm16) % 2:
            raise ValueError("PCM16 audio frames must contain whole samples")
        if len(pcm16) > self.max_turn_bytes:
            raise OverflowError("voice audio frame exceeds configured turn limit")
        frame_seconds = len(pcm16) / (self.sample_rate * 2)
        if self._active and self._buffered_bytes + len(pcm16) > self.max_turn_bytes:
            raise OverflowError("voice turn exceeds configured audio limit")

        samples = struct.unpack(f"<{len(pcm16) // 2}h", pcm16)
        rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
        speaking = rms >= self.speech_threshold
        events: list[TurnEvent] = []

        if speaking:
            if not self._active:
                self._active = True
                events.append(TurnEvent("speech_started"))
            self._silence_seconds = 0.0
            self._chunks.append(pcm16)
            self._buffered_bytes += len(pcm16)
            return events

        if not self._active:
            return events

        self._chunks.append(pcm16)
        self._buffered_bytes += len(pcm16)
        self._silence_seconds += frame_seconds
        if self._silence_seconds >= self.end_silence_seconds:
            events.extend(self.commit())
        return events

    def commit(self) -> list[TurnEvent]:
        if not self._active:
            return []
        audio = b"".join(self._chunks)
        self._active = False
        self._silence_seconds = 0.0
        self._chunks.clear()
        self._buffered_bytes = 0
        return [TurnEvent("speech_stopped", audio)]
