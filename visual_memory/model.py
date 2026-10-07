from __future__ import annotations

import importlib.metadata
import os
import threading
from pathlib import Path


MODEL_REPO = "google/embeddinggemma-2"
MODEL_REVISION = "914f7f89142e33e77833254d9c9b90c3cef7303b"
MODEL_PATH = Path(os.getenv("VISUAL_EMBED_MODEL_PATH", "/models/embeddinggemma-2"))
LOCAL_FILES_ONLY = True


def load_transformers_components(
    model_path: str | Path,
    *,
    revision: str = MODEL_REVISION,
    local_files_only: bool = LOCAL_FILES_ONLY,
    auto_config=None,
    auto_processor=None,
    auto_model=None,
    torch_module=None,
):
    if auto_config is None or auto_processor is None or auto_model is None:
        from transformers import AutoConfig, AutoModel, AutoProcessor

        auto_config = auto_config or AutoConfig
        auto_processor = auto_processor or AutoProcessor
        auto_model = auto_model or AutoModel
    if torch_module is None:
        import torch as torch_module

    model_path = str(model_path)
    config = auto_config.from_pretrained(
        model_path,
        revision=revision,
        local_files_only=local_files_only,
        audio_config=None,
    )
    processor = auto_processor.from_pretrained(
        model_path,
        revision=revision,
        local_files_only=local_files_only,
    )
    model = auto_model.from_pretrained(
        model_path,
        revision=revision,
        local_files_only=local_files_only,
        config=config,
        dtype=torch_module.float32,
    )
    model.to("cpu")
    model.eval()
    return model, processor, config


class EmbeddingGemma2Backend:
    def __init__(
        self,
        model_path: str | Path = MODEL_PATH,
        *,
        revision: str = MODEL_REVISION,
        local_files_only: bool = LOCAL_FILES_ONLY,
    ) -> None:
        self.model_path = Path(model_path)
        self.revision = revision
        self.local_files_only = local_files_only
        self._model = None
        self._processor = None
        self._torch = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _ensure_loaded(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    import torch
                    from transformers import AutoConfig, AutoModel, AutoProcessor

                    self._model, self._processor, self._config = load_transformers_components(
                        self.model_path,
                        revision=self.revision,
                        local_files_only=self.local_files_only,
                        auto_config=AutoConfig,
                        auto_processor=AutoProcessor,
                        auto_model=AutoModel,
                        torch_module=torch,
                    )
                    self._torch = torch
        return self._model, self._processor

    def _pool(self, inputs):
        model, _ = self._ensure_loaded()
        inputs = inputs.to("cpu")
        with self._torch.inference_mode():
            output = model(**inputs, return_dict=True)
            hidden = output.last_hidden_state.float()
            mask = inputs.get("attention_mask")
            if mask is None:
                pooled = hidden.mean(dim=1)
            else:
                weights = mask.to(hidden.device, dtype=hidden.dtype).unsqueeze(-1)
                pooled = (hidden * weights).sum(dim=1) / weights.sum(dim=1).clamp(min=1.0)
            return self._torch.nn.functional.normalize(pooled, p=2, dim=-1).cpu().tolist()

    def embed_text(self, inputs, *, instruction=None):
        _, processor = self._ensure_loaded()
        text = [f"{instruction.strip()} {value}" if instruction and instruction.strip() else value for value in inputs]
        encoded = processor(
            text=text,
            padding=True,
            truncation=True,
            max_length=8192,
            return_tensors="pt",
        )
        return self._pool(encoded)

    def embed_image(self, image, *, instruction=None, vision_token_budget=560):
        _, processor = self._ensure_loaded()
        image_kwargs = {"max_soft_tokens": vision_token_budget}
        if instruction and instruction.strip():
            encoded = processor(
                text=[f"{instruction.strip()} <|image|>"],
                images=[[image]],
                images_kwargs=image_kwargs,
                return_tensors="pt",
            )
        else:
            encoded = processor(
                images=[[image]],
                images_kwargs=image_kwargs,
                return_tensors="pt",
            )
        return self._pool(encoded)[0]

    def provenance(self) -> dict:
        try:
            transformers_version = importlib.metadata.version("transformers")
        except importlib.metadata.PackageNotFoundError:
            transformers_version = "unavailable"
        try:
            torch_version = importlib.metadata.version("torch")
        except importlib.metadata.PackageNotFoundError:
            torch_version = "unavailable"
        dtype = str(next(self._model.parameters()).dtype).replace("torch.", "") if self.loaded else "float32"
        return {
            "model": MODEL_REPO,
            "revision": self.revision,
            "transformers_version": transformers_version,
            "torch_version": torch_version,
            "dimension": 768,
            "loaded_encoders": ["text", "vision"],
            "dtype": dtype,
            "device": "cpu",
            "loaded": self.loaded,
            "local_files_only": self.local_files_only,
            "model_files_available": (self.model_path / "config.json").is_file(),
        }
