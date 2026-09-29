from __future__ import annotations

import gc
import os
import sys
from abc import ABC, abstractmethod
from pathlib import Path

MODEL_SPECS = {
    "edda": {"repo": "danish-foundation-models/edda-v0.1", "license": "Apache-2.0"},
    "saga2": {"repo": "capacit-ai/saga-2-m", "license": "Apache-2.0", "gated": True},
    "hviske": {"repo": "syvai/hviske-v6", "license": "CC-BY-NC-4.0"},
}


def _torch():
    import torch
    return torch


def _dtype():
    torch = _torch()
    if torch.cuda.is_available() and torch.cuda.is_bf16_supported():
        return torch.bfloat16
    return torch.float16 if torch.cuda.is_available() else torch.float32


def _token():
    return os.getenv("HF_TOKEN") or None


class ASRAdapter(ABC):
    alias: str
    repo_id: str

    @abstractmethod
    def transcribe(self, paths: list[str], batch_size: int) -> list[str]:
        raise NotImplementedError

    def unload(self) -> None:
        for name in ("model", "pipe", "processor", "asr"):
            if hasattr(self, name):
                setattr(self, name, None)
        gc.collect()
        torch = _torch()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


class EddaAdapter(ASRAdapter):
    alias = "edda"
    repo_id = MODEL_SPECS[alias]["repo"]

    def __init__(self, beam_size: int = 5):
        import torch
        from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline

        dtype = _dtype()
        self.processor = AutoProcessor.from_pretrained(self.repo_id, token=_token())
        self.model = AutoModelForSpeechSeq2Seq.from_pretrained(
            self.repo_id,
            torch_dtype=dtype,
            low_cpu_mem_usage=True,
            token=_token(),
        )
        if torch.cuda.is_available():
            self.model.to("cuda")
        self.pipe = pipeline(
            "automatic-speech-recognition",
            model=self.model,
            tokenizer=self.processor.tokenizer,
            feature_extractor=self.processor.feature_extractor,
            device=0 if torch.cuda.is_available() else -1,
            torch_dtype=dtype,
        )
        self.beam_size = beam_size

    def transcribe(self, paths: list[str], batch_size: int) -> list[str]:
        import librosa

        outputs = []
        generate_kwargs = {
            "language": "da",
            "task": "transcribe",
            "num_beams": self.beam_size,
        }
        for path in paths:
            duration_s = float(librosa.get_duration(path=path))
            kwargs = {
                "batch_size": max(1, batch_size),
                "return_timestamps": duration_s > 29.0,
                "generate_kwargs": generate_kwargs,
            }
            if duration_s > 29.0:
                kwargs.update({"chunk_length_s": 28, "stride_length_s": 3})
            result = self.pipe(path, **kwargs)
            outputs.append(str(result["text"]).strip())
        return outputs


def _snapshot(repo_id: str) -> Path:
    from huggingface_hub import snapshot_download

    path = Path(snapshot_download(repo_id=repo_id, token=_token()))
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)
    return path


class Saga2Adapter(ASRAdapter):
    alias = "saga2"
    repo_id = MODEL_SPECS[alias]["repo"]

    def __init__(self):
        repo = _snapshot(self.repo_id)
        from saga2 import load
        self.model = load(str(repo))

    def transcribe(self, paths: list[str], batch_size: int) -> list[str]:
        output = self.model.transcribe(paths, batch_size=max(1, batch_size))
        if isinstance(output, str):
            output = [output]
        return [str(item).strip() for item in output]


class HviskeAdapter(ASRAdapter):
    alias = "hviske"
    repo_id = MODEL_SPECS[alias]["repo"]

    def __init__(self):
        repo = _snapshot(self.repo_id)
        from processing_whisper_qwen import HviskeASR
        self.asr = HviskeASR.from_pretrained(str(repo))

    def transcribe(self, paths: list[str], batch_size: int) -> list[str]:
        output = self.asr.transcribe(paths, batch_size=max(1, batch_size))
        if isinstance(output, str):
            output = [output]
        return [str(item).strip() for item in output]


def load_adapter(alias: str, *, beam_size: int = 5) -> ASRAdapter:
    if alias == "edda":
        return EddaAdapter(beam_size=beam_size)
    if alias == "saga2":
        return Saga2Adapter()
    if alias == "hviske":
        return HviskeAdapter()
    raise ValueError(f"unknown ASR model alias: {alias}")
