from __future__ import annotations

from dataclasses import dataclass
from math import gcd

import numpy as np
from scipy.signal import resample_poly


_SUPPORTED_SAMPLE_RATES = frozenset({16_000, 24_000, 32_000, 44_100, 48_000})
_SUPPORTED_CHANNELS = frozenset({1, 2})


@dataclass(frozen=True, slots=True)
class AudioPCMFrame:
    data: bytes
    sample_rate: int
    channels: int


def float_to_pcm16(samples: np.ndarray) -> bytes:
    """Encode normalized mono float samples as little-endian signed PCM16."""
    values = np.asarray(samples)
    if values.ndim != 1 or values.size == 0:
        raise ValueError("audio samples must be a non-empty one-dimensional array")
    if not np.issubdtype(values.dtype, np.number) or not np.isfinite(values).all():
        raise ValueError("audio samples must contain finite numeric values")

    scaled = np.rint(values.astype(np.float64, copy=False) * 32_768.0)
    clipped = np.clip(scaled, -32_768, 32_767).astype("<i2")
    return clipped.tobytes()


def _decode_pcm16(data: bytes, *, sample_rate: int, channels: int) -> np.ndarray:
    if not isinstance(data, (bytes, bytearray, memoryview)) or not data:
        raise ValueError("PCM16 audio must be non-empty bytes")
    if sample_rate not in _SUPPORTED_SAMPLE_RATES:
        raise ValueError(f"unsupported sample rate: {sample_rate}")
    if channels not in _SUPPORTED_CHANNELS:
        raise ValueError(f"unsupported channel count: {channels}")
    if len(data) % 2:
        raise ValueError("PCM16 audio must have an even byte length")

    samples = np.frombuffer(data, dtype="<i2")
    if samples.size % channels:
        raise ValueError("PCM16 sample count must be divisible by channel count")
    return samples.reshape((-1, channels)).astype(np.float64)


def _mono_resampled(data: bytes, *, sample_rate: int, channels: int, target_rate: int) -> bytes:
    frames = _decode_pcm16(data, sample_rate=sample_rate, channels=channels)
    mono = frames.mean(axis=1) if channels == 2 else frames[:, 0]
    normalized = mono / 32_768.0
    if sample_rate != target_rate:
        divisor = gcd(sample_rate, target_rate)
        normalized = resample_poly(
            normalized,
            target_rate // divisor,
            sample_rate // divisor,
        )
    return float_to_pcm16(normalized)


def convert_input_audio(data: bytes, *, sample_rate: int, channels: int) -> bytes:
    """Convert browser PCM16 input to mono 16 kHz PCM16."""
    return _mono_resampled(
        data, sample_rate=sample_rate, channels=channels, target_rate=16_000
    )


def convert_output_audio(
    data: bytes, *, sample_rate: int, channels: int
) -> AudioPCMFrame:
    """Convert provider PCM16 output to mono 48 kHz PCM16."""
    return AudioPCMFrame(
        data=_mono_resampled(
            data, sample_rate=sample_rate, channels=channels, target_rate=48_000
        ),
        sample_rate=48_000,
        channels=1,
    )
