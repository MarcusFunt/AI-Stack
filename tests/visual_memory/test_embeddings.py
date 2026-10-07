import math
import importlib
import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from visual_memory.embeddings import EmbeddingModel, normalize_vector, resolve_vision_token_budget
from visual_memory.image_processing import decode_image
from visual_memory.model import load_transformers_components


class FakeEmbedder:
    def __init__(self):
        self.image_budgets = []

    def embed_text(self, inputs, instruction=None):
        return [[3.0, 4.0] + [0.0] * 766 for _ in inputs]

    def embed_image(self, image, instruction=None, vision_token_budget=560):
        self.image_budgets.append(vision_token_budget)
        return [3.0, 4.0] + [0.0] * 766


def test_normalize_vector_returns_unit_length_768_dimension_vector():
    result = normalize_vector([3.0, 4.0] + [0.0] * 766, dimension=768)

    assert len(result) == 768
    assert result[0] == pytest.approx(0.6)
    assert result[1] == pytest.approx(0.8)
    assert math.sqrt(sum(value * value for value in result)) == pytest.approx(1.0)


def test_normalize_vector_rejects_zero_and_non_finite_values():
    with pytest.raises(ValueError, match="zero norm"):
        normalize_vector([0.0, 0.0], dimension=2)
    with pytest.raises(ValueError, match="finite"):
        normalize_vector([math.nan, 1.0], dimension=2)


def test_embedding_model_normalizes_text_and_image_embeddings():
    backend = FakeEmbedder()
    model = EmbeddingModel(backend)

    text = model.embed_text(["navigation overlaps the title"])
    image = model.embed_image(object(), vision_token_budget=280)

    assert len(text) == 1 and len(text[0]) == 768
    assert sum(value * value for value in text[0]) == pytest.approx(1.0)
    assert len(image) == 768
    assert sum(value * value for value in image) == pytest.approx(1.0)
    assert backend.image_budgets == [280]


@pytest.mark.parametrize(
    ("value", "expected_mode", "expected_budget"),
    [(280, "fast", 280), (560, "balanced", 560), (1120, "detail", 1120), (None, "balanced", 560)],
)
def test_resolve_vision_token_budget(value, expected_mode, expected_budget):
    assert resolve_vision_token_budget(value) == (expected_mode, expected_budget)


def test_resolve_vision_token_budget_rejects_unsupported_values():
    with pytest.raises(ValueError, match="vision token budget"):
        resolve_vision_token_budget(999)


def test_decode_image_rejects_invalid_bytes_oversize_and_pixel_limit():
    with pytest.raises(ValueError, match="invalid image"):
        decode_image(b"not an image")

    # A valid 1x1 PNG keeps this test independent of external image fixtures.
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0bIDAT"
        b"\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    with pytest.raises(ValueError, match="byte limit"):
        decode_image(png, max_bytes=len(png) - 1)
    with pytest.raises(ValueError, match="pixel limit"):
        decode_image(png, max_pixels=0)


def test_transformers_loader_is_local_and_disables_audio_tower():
    calls = {}

    class ConfigFactory:
        @staticmethod
        def from_pretrained(model_path, **kwargs):
            calls["config"] = (model_path, kwargs)
            return object()

    class ProcessorFactory:
        @staticmethod
        def from_pretrained(model_path, **kwargs):
            calls["processor"] = (model_path, kwargs)
            return "processor"

    class ModelFactory:
        @staticmethod
        def from_pretrained(model_path, **kwargs):
            calls["model"] = (model_path, kwargs)
            class LoadedModel:
                def to(self, device):
                    calls["device"] = device
                    return self

                def eval(self):
                    calls["eval"] = True

            return LoadedModel()

    model, processor, config = load_transformers_components(
        "/models/embeddinggemma-2",
        revision="immutable-sha",
        local_files_only=True,
        auto_config=ConfigFactory,
        auto_processor=ProcessorFactory,
        auto_model=ModelFactory,
    )

    assert model is not None
    assert processor == "processor"
    assert config is not None
    assert calls["device"] == "cpu"
    assert calls["eval"] is True
    assert calls["config"][1]["audio_config"] is None
    assert calls["processor"][1]["local_files_only"] is True
    assert calls["model"][1]["local_files_only"] is True
    assert calls["model"][1]["revision"] == "immutable-sha"


def _api_client(monkeypatch):
    service = importlib.import_module("visual_memory.app")
    runtime = SimpleNamespace(
        provenance=lambda: {
            "model": "google/embeddinggemma-2",
            "revision": "immutable-sha",
            "transformers_version": "5.19.0",
            "torch_version": "2.9.0",
            "dimension": 768,
            "loaded_encoders": ["text", "vision"],
            "dtype": "float32",
            "device": "cpu",
            "loaded": True,
            "local_files_only": True,
            "model_files_available": True,
        }
    )
    monkeypatch.setattr(service, "backend", runtime)
    monkeypatch.setattr(service, "embedding_model", EmbeddingModel(FakeEmbedder()))
    return TestClient(service.app)


def test_text_embedding_api_returns_normalized_vectors_and_model_provenance(monkeypatch):
    response = _api_client(monkeypatch).post(
        "/v1/embeddings/text",
        json={"inputs": ["mobile navigation overlaps the title"], "dimensions": 768},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["model"] == "google/embeddinggemma-2"
    assert result["revision"] == "immutable-sha"
    assert result["dimension"] == 768
    assert len(result["embeddings"][0]) == 768
    assert sum(value * value for value in result["embeddings"][0]) == pytest.approx(1.0)
    assert result["metadata"]["loaded_encoders"] == ["text", "vision"]
    assert result["metadata"]["device"] == "cpu"


def test_image_embedding_api_records_token_budget_mode_and_rejects_invalid_image(monkeypatch):
    client = _api_client(monkeypatch)
    png = io.BytesIO()
    Image.new("RGB", (2, 2), "blue").save(png, format="PNG")

    response = client.post(
        "/v1/embeddings/image",
        files={"image": ("frame.png", png.getvalue(), "image/png")},
        data={"vision_token_budget": "fast"},
    )
    invalid = client.post(
        "/v1/embeddings/image",
        files={"image": ("bad.bin", b"invalid", "application/octet-stream")},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["metadata"]["vision_token_budget"] == {"mode": "fast", "tokens": 280}
    assert len(result["embeddings"][0]) == 768
    assert invalid.status_code == 400
    assert "invalid image" in invalid.json()["detail"]
