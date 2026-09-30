"""Audio conversion helpers shared by Hugging Face dataset adapters."""

from __future__ import annotations

import wave
from io import BytesIO
from pathlib import Path
from typing import Any


def write_audio_16k_mono(audio: Any, output_path: Path, row_id: str) -> float:
    """Write HF audio as mono 16 kHz PCM16 without resampling or normalization."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = audio if isinstance(audio, dict) else {"array": audio}
    array = payload.get("array")
    rate = payload.get("sampling_rate", payload.get("sample_rate"))

    if array is None:
        encoded = payload.get("bytes")
        source_path = payload.get("path")
        stream = BytesIO(encoded) if encoded else (
            source_path if source_path and Path(source_path).is_file() else None
        )
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
        import numpy as np
        import soundfile as sf

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
        raise RuntimeError("Writing decoded dataset audio needs numpy and soundfile from stt/requirements-benchmark.txt") from exc
