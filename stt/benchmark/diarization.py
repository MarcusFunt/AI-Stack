from __future__ import annotations

import gc
from .revisions import hf_token, resolve_model_revision


class NemotronDiarizer:
    model_id = "nvidia/Nemotron-3-Diarization"

    def __init__(self, revision: str | None = None):
        import torch
        from transformers import AutoModelForAudioFrameClassification, AutoProcessor

        self.revision = resolve_model_revision(self.model_id, revision)
        dtype = torch.bfloat16 if torch.cuda.is_available() and torch.cuda.is_bf16_supported() else "auto"
        self.processor = AutoProcessor.from_pretrained(
            self.model_id, revision=self.revision, token=hf_token()
        )
        self.model = AutoModelForAudioFrameClassification.from_pretrained(
            self.model_id,
            revision=self.revision,
            device_map="auto",
            torch_dtype=dtype,
            token=hf_token(),
        )

    def diarize(self, audio_path: str) -> list[dict]:
        import torch
        from transformers.audio_utils import load_audio

        sampling_rate = self.processor.feature_extractor.sampling_rate
        audio = load_audio(audio_path, sampling_rate=sampling_rate)
        inputs = self.processor(audio, sampling_rate=sampling_rate).to(
            self.model.device, dtype=self.model.dtype
        )
        with torch.inference_mode():
            logits = self.model(**inputs).logits
        raw = self.processor.extract_speaker_dict(logits, inputs.attention_mask)[0]
        segments = []
        for item in raw:
            speaker = int(item["Speaker"])
            start, end = float(item["Start"]), float(item["End"])
            if end > start:
                segments.append({"start": start, "end": end, "speaker": str(speaker)})
        return segments

    def unload(self) -> None:
        self.model = None
        self.processor = None
        gc.collect()
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
