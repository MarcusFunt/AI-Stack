import importlib
import json
import os
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from fastapi.responses import Response
from fastapi.testclient import TestClient
from PIL import Image


def png_bytes():
    stream = BytesIO()
    Image.new("RGB", (16, 12), "purple").save(stream, format="PNG")
    return stream.getvalue()


class TestGatewayVisualMemoryRoutes:
    @classmethod
    def setup_class(cls):
        cls.environment = patch.dict(os.environ, {
            "AI_API_KEY": "test-only-gateway-key",
            "SUPERVISOR_TOKEN": "test-only-supervisor-token",
            "LLAMA_API_KEY": "test-only-llama-key",
            "CONFIG_PATH": str(Path("config/models.json").resolve()),
        })
        cls.environment.start()
        sys.modules.pop("gateway.app", None)
        cls.gateway = importlib.import_module("gateway.app")
        cls.client = TestClient(cls.gateway.app)

    @classmethod
    def teardown_class(cls):
        sys.modules.pop("gateway.app", None)
        cls.environment.stop()

    def test_text_index_uses_registry_provider_and_preserves_json(self):
        observed = {}
        original_forward = self.gateway.forward_buffered

        async def fake_forward(service, path, request, body=None, **kwargs):
            observed.update(service=service, path=path, body=body, invocation=request.state.invocation,
                            route=request.state.model_route)
            return Response('{"frame_id":"text-1"}', media_type="application/json")

        self.gateway.forward_buffered = fake_forward
        payload = {"text": "header navigation", "namespace": "code:demo", "source": "code"}
        try:
            response = self.client.post(
                "/v1/visual-memory/index/text",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                json=payload,
            )
        finally:
            self.gateway.forward_buffered = original_forward

        assert response.status_code == 200
        assert json.loads(observed["body"]) == payload
        assert observed["service"] == "visual-memory"
        assert observed["path"] == "/v1/visual-memory/index/text"
        assert observed["invocation"].operation.value == "embed"
        assert observed["route"].provider_id == "visual-memory"

    def test_image_index_requires_auth_and_forwards_one_bounded_image(self):
        calls = []
        original_forward = self.gateway.forward_buffered

        async def fake_forward(service, path, request, body=None, **kwargs):
            calls.append((service, path, body))
            return Response('{"frame_id":"image-1"}', media_type="application/json")

        self.gateway.forward_buffered = fake_forward
        image = png_bytes()
        try:
            unauthorized = self.client.post(
                "/v1/visual-memory/index",
                data={"namespace": "game:demo"},
                files={"image": ("frame.png", image, "image/png")},
            )
            authorized = self.client.post(
                "/v1/visual-memory/index",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                data={"namespace": "game:demo"},
                files={"image": ("frame.png", image, "image/png")},
            )
        finally:
            self.gateway.forward_buffered = original_forward

        assert unauthorized.status_code == 401
        assert authorized.status_code == 200
        assert len(calls) == 1
        assert calls[0][0:2] == ("visual-memory", "/v1/visual-memory/index")
        assert image in calls[0][2]

    def test_visual_memory_status_uses_embedding_registry_route(self):
        observed = {}
        original_forward = self.gateway.forward_buffered

        async def fake_forward(service, path, request, **kwargs):
            observed.update(service=service, path=path, route=request.state.model_route)
            return Response('{"status":"ok","loaded":false}', media_type="application/json")

        self.gateway.forward_buffered = fake_forward
        try:
            response = self.client.get(
                "/v1/visual-memory/status",
                headers={"Authorization": "Bearer test-only-gateway-key"},
            )
        finally:
            self.gateway.forward_buffered = original_forward

        assert response.status_code == 200
        assert response.json()["loaded"] is False
        assert observed["service"] == "visual-memory"
        assert observed["path"] == "/diagnostics"
        assert observed["route"].provider_id == "visual-memory"

    def test_capabilities_advertise_detailed_vision_and_visual_memory_routes(self):
        response = self.client.get(
            "/v1/capabilities",
            headers={"Authorization": "Bearer test-only-gateway-key"},
        )

        assert response.status_code == 200
        endpoints = response.json()["endpoints"]
        assert endpoints["vision"] == "/v1/vision/analyze"
        assert endpoints["vision_detailed"] == "/v1/vision/analyze-detailed"
        assert endpoints["vision_compare"] == "/v1/vision/compare"
        assert endpoints["visual_memory"]["search_text"] == "/v1/visual-memory/search/text"
