import json
import importlib
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.responses import Response
from fastapi.testclient import TestClient
from PIL import Image

from core.context import TraceContext
from core.invocation import Invocation, InvocationOperation, InvocationSource, Modality
from core.router import InvocationRouter
from gateway.adapters.embedding import EmbeddingAdapter


class TestGatewayEmbeddingEndpoint:
    @classmethod
    def setup_class(cls):
        cls.environment = patch.dict(os.environ, {
            "AI_API_KEY": "test-only-gateway-key",
            "SUPERVISOR_TOKEN": "test-only-supervisor-token",
            "LLAMA_API_KEY": "test-only-llama-key",
            "CONFIG_PATH": str(Path("config/models.json").resolve()),
        })
        cls.environment.start()
        cls.gateway = importlib.import_module("gateway.app")
        cls.client = TestClient(cls.gateway.app)

    @classmethod
    def teardown_class(cls):
        sys.modules.pop("gateway.app", None)
        cls.environment.stop()

    def test_text_embedding_route_authenticates_and_uses_registry_forwarding(self):
        observed = {}
        original = self.gateway.forward_buffered

        async def fake_forward(service, path, request, body=None, **kwargs):
            observed["service"] = service
            observed["path"] = path
            observed["invocation"] = request.state.invocation
            observed["body"] = json.loads(body)
            return Response(content=b'{"ok":true}', media_type="application/json")

        self.gateway.forward_buffered = fake_forward
        try:
            response = self.client.post(
                "/v1/embeddings/text",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                json={"inputs": ["settings dialog"], "dimensions": 768},
            )
        finally:
            self.gateway.forward_buffered = original

        assert response.status_code == 200
        assert observed["service"] == "visual-memory"
        assert observed["path"] == "/v1/embeddings/text"
        assert observed["invocation"].operation is InvocationOperation.EMBED
        assert observed["body"]["inputs"] == ["settings dialog"]

    def test_image_embedding_route_forwards_original_multipart_body(self):
        observed = {}
        original = self.gateway.forward_buffered
        image_bytes = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(image_bytes, format="PNG")

        async def fake_forward(service, path, request, body=None, **kwargs):
            observed["service"] = service
            observed["path"] = path
            observed["invocation"] = request.state.invocation
            observed["body"] = body
            return Response(content=b'{"ok":true}', media_type="application/json")

        self.gateway.forward_buffered = fake_forward
        try:
            response = self.client.post(
                "/v1/embeddings/image",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                files={"image": ("screen.png", image_bytes.getvalue(), "image/png")},
                data={"vision_token_budget": "560"},
            )
        finally:
            self.gateway.forward_buffered = original

        assert response.status_code == 200
        assert observed["service"] == "visual-memory"
        assert observed["path"] == "/v1/embeddings/image"
        assert observed["invocation"].operation is InvocationOperation.EMBED
        assert b"screen.png" in observed["body"]
        assert image_bytes.getvalue() in observed["body"]

    def test_embedding_route_rejects_missing_gateway_authentication(self):
        response = self.client.post("/v1/embeddings/text", json={"inputs": ["dashboard"]})

        assert response.status_code == 401


def test_text_embedding_adapter_uses_canonical_embed_operation_without_raw_text():
    adapter = EmbeddingAdapter()
    invocation = adapter.to_text(
        {"inputs": ["mobile navigation overlaps the title"], "instruction": "task: search result | query:"},
        TraceContext(request_id="embed-1"),
    )

    assert invocation.operation is InvocationOperation.EMBED
    assert invocation.source is InvocationSource.INTERNAL
    assert invocation.modality == {Modality.TEXT}
    assert invocation.input.data == {"input_count": 1, "instruction_present": True}
    assert "mobile navigation" not in repr(invocation)


def test_image_embedding_adapter_records_upload_metadata_without_image_bytes():
    upload = SimpleNamespace(filename="screen.png", content_type="image/png", size=456)

    invocation = EmbeddingAdapter().to_image(
        {"image": upload, "instruction": "find a red error banner", "vision_token_budget": "560"},
        TraceContext(request_id="embed-2"),
    )

    assert invocation.operation is InvocationOperation.EMBED
    assert invocation.modality == {Modality.IMAGE}
    assert invocation.input.data["image"] == {
        "filename": "screen.png",
        "mime_type": "image/png",
        "size_bytes": 456,
    }
    assert "image bytes" not in repr(invocation)


def test_visual_embedding_alias_routes_through_registry_and_vision_stays_qwen():
    config_path = Path("config/models.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    router = InvocationRouter(config["models"], aliases=config["aliases"])

    visual = router.resolve(EmbeddingAdapter().to_text({"inputs": ["dashboard"]}, TraceContext()))
    vision = router.resolve(
        Invocation(
            operation=InvocationOperation.ANALYZE,
            requested_model="vision",
        )
    )

    assert visual.model_id == "local-visual-embedding"
    assert visual.provider_id == "visual-memory"
    assert vision.model_id == "local-vlm"
    assert vision.provider_id == "vlm"
