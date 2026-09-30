"""Audio conversion helpers shared by Hugging Face dataset adapters."""

from __future__ import annotations

import wave
from io import BytesIO
from math import gcd
from pathlib import Path
from typing import Any


def write_audio_16k_mono(audio: Any, output_path: Path, row_id: str) -> float:
    """Decode, downmix, and resample HF audio to mono 16 kHz PCM16."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = audio if isinstance(audio, dict) else {"array": audio}
    samples = payload.get("array")
    rate = payload.get("sampling_rate", payload.get("sample_rate"))

    if samples is None:
        encoded = payload.get("bytes")
        source_path = payload.get("path")
        stream = BytesIO(encoded) if encoded else (
            source_path if source_path and Path(source_path).is_file() else None
        )
        if stream is None:
            raise ValueError(f"{row_id}: dataset audio has no decoded array, bytes, or readable path")
        try:
            with wave.open(stream, "rb") as source:
                channels = source.getnchannels()
                rate = source.getframerate()
                width = source.getsampwidth()
                frame_count = source.getnframes()
                frames = source.readframes(frame_count)
        except (wave.Error, EOFError, AttributeError):
            frames = None
        else:
            if channels == 1 and rate == 16000 and width == 2:
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
                "Decoding Hugging Face audio needs soundfile from stt/requirements-benchmark.txt"
            ) from exc
        try:
            if encoded:
                samples, rate = sf.read(BytesIO(encoded), dtype="float32", always_2d=True)
            elif source_path and Path(source_path).is_file():
                samples, rate = sf.read(str(source_path), dtype="float32", always_2d=True)
            else:
                raise ValueError("no decodable audio payload")
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(f"{row_id}: could not decode Hugging Face audio") from exc

    if samples is None:
        raise ValueError(f"{row_id}: decoded audio is empty")
    try:
        import numpy as np
        import soundfile as sf
        from scipy.signal import resample_poly
    except ImportError as exc:
        raise RuntimeError(
            "Audio conversion needs numpy, scipy, and soundfile from stt/requirements-benchmark.txt"
        ) from exc

    source_rate = int(rate or 0)
    if source_rate <= 0:
        raise ValueError(f"{row_id}: audio sample rate must be positive")
    samples = np.asarray(samples)
    if samples.ndim == 2:
        if samples.shape[1] < 1:
            raise ValueError(f"{row_id}: dataset audio has no channels")
        if np.issubdtype(samples.dtype, np.integer):
            info = np.iinfo(samples.dtype)
            scale = float(max(abs(info.min), abs(info.max) + 1))
            samples = samples.astype(np.float32) / scale
        else:
            samples = samples.astype(np.float32, copy=False)
        samples = samples.mean(axis=1)
    elif samples.ndim == 1:
        if np.issubdtype(samples.dtype, np.integer):
            info = np.iinfo(samples.dtype)
            scale = float(max(abs(info.min), abs(info.max) + 1))
            samples = samples.astype(np.float32) / scale
        else:
            samples = samples.astype(np.float32, copy=False)
    else:
        raise ValueError(
            f"{row_id}: expected audio with samples and optional channels, got shape {samples.shape}"
        )
    if not samples.size:
        raise ValueError(f"{row_id}: dataset audio is empty")
    if not np.isfinite(samples).all():
        raise ValueError(f"{row_id}: dataset audio contains non-finite samples")
    if source_rate != 16000:
        divisor = gcd(source_rate, 16000)
        samples = resample_poly(
            samples, 16000 // divisor, source_rate // divisor
        ).astype(np.float32)
    sf.write(str(output_path), samples, 16000, subtype="PCM_16", format="WAV")
    return len(samples) / 16000.0
