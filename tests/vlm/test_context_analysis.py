import io
import json
from copy import deepcopy
from types import SimpleNamespace

import torch
from fastapi.testclient import TestClient
from PIL import Image

from vlm import app as vlm_module


def png_bytes(color):
    stream = io.BytesIO()
    image = Image.new("RGB", (32, 24), color)
    image.putpixel((0, 0), (255, 255, 255))
    image.save(stream, format="PNG")
    return stream.getvalue()


class FakeBatch(dict):
    def to(self, _device):
        return self


class FakeProcessor:
    def __init__(self):
        self.messages = None

    def apply_chat_template(self, messages, **_kwargs):
        self.messages = messages
        return FakeBatch(input_ids=torch.tensor([[1, 2]]))

    def batch_decode(self, _ids, **_kwargs):
        return ["The current page has a comparison banner."]


class FakeModel:
    device = "cpu"
    generation_config = SimpleNamespace()

    def generate(self, **_kwargs):
        return torch.tensor([[1, 2, 3, 4]])


def client_with_fake_model(monkeypatch):
    processor = FakeProcessor()
    monkeypatch.setattr(vlm_module, "get_model", lambda: (FakeModel(), processor))
    return TestClient(vlm_module.app), processor


def test_context_endpoint_sends_current_crops_and_references_in_one_model_call(monkeypatch):
    client, processor = client_with_fake_model(monkeypatch)
    context = {"url": "https://example.test/cart", "viewport": {"width": 1280, "height": 720}}
    response = client.post(
        "/v1/vision/analyze-context",
        data={"analysis_profile": "website", "context_json": json.dumps(context), "prompt": "Compare the layout."},
        files=[
            ("current_image", ("current.png", png_bytes("red"), "image/png")),
            ("crop_images", ("header.webp", png_bytes("blue"), "image/png")),
            ("reference_images", ("prior.webp", png_bytes("green"), "image/png")),
        ],
    )

    assert response.status_code == 200
    assert response.json()["text"] == "The current page has a comparison banner."
    content = processor.messages[0]["content"]
    assert sum(item["type"] == "image" for item in content) == 3
    assert any("example.test/cart" in item.get("text", "") for item in content)
    assert response.json()["images_received"] == {"current": 1, "crops": 1, "references": 1}


def test_context_endpoint_rejects_invalid_profile_json_and_excess_images(monkeypatch):
    client, _processor = client_with_fake_model(monkeypatch)
    image = png_bytes("red")

    invalid_profile = client.post(
        "/v1/vision/analyze-context",
        data={"analysis_profile": "arbitrary", "context_json": "{}"},
        files={"current_image": ("current.png", image, "image/png")},
    )
    invalid_json = client.post(
        "/v1/vision/analyze-context",
        data={"analysis_profile": "generic", "context_json": "{"},
        files={"current_image": ("current.png", image, "image/png")},
    )
    excess_crops = client.post(
        "/v1/vision/analyze-context",
        data={"analysis_profile": "game", "context_json": "{}"},
        files=[("current_image", ("current.png", image, "image/png"))]
        + [("crop_images", (f"crop-{index}.png", image, "image/png")) for index in range(7)],
    )

    assert invalid_profile.status_code == 400
    assert invalid_json.status_code == 400
    assert excess_crops.status_code == 413


def test_context_endpoint_rejects_more_than_four_references(monkeypatch):
    client, _processor = client_with_fake_model(monkeypatch)
    image = png_bytes("red")
    response = client.post(
        "/v1/vision/analyze-context",
        data={"analysis_profile": "generic", "context_json": "{}"},
        files=[("current_image", ("current.png", image, "image/png"))]
        + [("reference_images", (f"reference-{index}.png", image, "image/png")) for index in range(5)],
    )
    assert response.status_code == 413


def test_context_endpoint_preserves_old_single_image_route(monkeypatch):
    client, processor = client_with_fake_model(monkeypatch)
    response = client.post(
        "/v1/vision/analyze",
        data={"prompt": "Describe this scene."},
        files={"image": ("current.png", png_bytes("red"), "image/png")},
    )
    assert response.status_code == 200
    assert sum(item["type"] == "image" for item in processor.messages[0]["content"]) == 1
