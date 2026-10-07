import importlib
import json
import os
import sys
from email import policy
from email.parser import BytesParser
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from fastapi.responses import Response
from fastapi.testclient import TestClient
from PIL import Image


def png_bytes(color="red"):
    stream = BytesIO()
    image = Image.new("RGB", (32, 24), color)
    image.putpixel((0, 0), (255, 255, 255))
    image.save(stream, format="PNG")
    return stream.getvalue()


def parse_multipart(body, content_type):
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    )
    values = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        values.setdefault(name, []).append((part.get_filename(), part.get_payload(decode=True)))
    return values


class TestDetailedVisualAnalysisGateway:
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
        cls.gateway.VISUAL_METRICS.reset()
        cls.client = TestClient(cls.gateway.app)

    @classmethod
    def teardown_class(cls):
        sys.modules.pop("gateway.app", None)
        cls.environment.stop()

    def test_detailed_endpoint_retrieves_evidence_and_sends_original_current_image_once(self):
        calls = []
        qwen_calls_before = self.gateway.VISUAL_METRICS.counter("qwen_escalations_total")
        current_image = png_bytes("red")
        reference_bytes = b"reference-image-bytes"
        crop_bytes = b"selected-crop-bytes"
        original_forward = self.gateway.forward_buffered

        async def fake_forward(service, path, request, body=None, content_type=None, **kwargs):
            calls.append((service, path, request.state.invocation, body, content_type))
            if path == "/v1/visual-memory/index":
                return Response(json.dumps({"frame_id": "current-1", "observation_id": "obs-1",
                                            "duplicate": False, "model": "google/embeddinggemma-2",
                                            "revision": "pinned"}), media_type="application/json")
            if path == "/v1/visual-memory/search/image":
                form = parse_multipart(body, content_type)
                parent = form.get("parent_id", [(None, b"")])[0][1].decode()
                if parent == "current-1":
                    matches = [{"id": "current-1", "vector_id": 1, "parent_id": "current-1", "score": 0.91,
                                "crop": {"type": "center", "bbox": {"x": .25, "y": .25, "w": .5, "h": .5}}}]
                else:
                    matches = [{"id": "prior-1", "score": 0.88, "parent_id": None,
                                "metadata": {"namespace": "website:demo"}, "temporal_context": []}]
                return Response(json.dumps({"matches": matches, "model": "google/embeddinggemma-2",
                                            "revision": "pinned", "dimension": 768}), media_type="application/json")
            if path == "/v1/visual-memory/search/text":
                return Response(json.dumps({"matches": [{"id": "code-1", "score": 0.82,
                    "content_hash": "sha-code", "text": "header nav { position: fixed; }",
                    "metadata": {"source_path": "src/header.css", "start_line": 3, "end_line": 9}}]}),
                    media_type="application/json")
            if path == "/v1/visual-memory/context-pack":
                pack = json.loads(body)
                return Response(json.dumps({
                    "current_frame": pack["current_frame"],
                    "retrieved": pack["retrieved"],
                    "crops": pack["crops"],
                    "structured_context": pack["structured_context"],
                    "text_context": pack["text_context"],
                    "temporal_context": pack["temporal_context"],
                }), media_type="application/json")
            if path == "/v1/visual-memory/assets/read":
                payload = json.loads(body)
                return Response(crop_bytes if payload.get("crop_type") else reference_bytes, media_type="image/webp")
            if path == "/v1/vision/analyze-context":
                fields = parse_multipart(body, content_type)
                assert fields["current_image"][0][1] == current_image
                assert fields["crop_images"][0][1] == crop_bytes
                assert fields["reference_images"][0][1] == reference_bytes
                assert b"Do not switch to the compact mobile view" in fields["context_json"][0][1]
                return Response(json.dumps({"model": "Qwen/Qwen3-VL-4B-Instruct", "text": "The title is overlapped."}),
                                media_type="application/json")
            raise AssertionError(f"unexpected worker call {service} {path}")

        self.gateway.forward_buffered = fake_forward
        try:
            response = self.client.post(
                "/v1/vision/analyze-detailed",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                data={"analysis_profile": "website", "namespace": "website:demo", "code_namespace": "code:demo",
                      "context_json": '{"url":"https://example.test","note":"Do not switch to the compact mobile view"}',
                      "prompt": "Find the overlapping title.", "max_new_tokens": "64"},
                files={"image": ("current.png", current_image, "image/png")},
            )
        finally:
            self.gateway.forward_buffered = original_forward

        assert response.status_code == 200
        payload = response.json()
        assert payload["analysis"]["text"] == "The title is overlapped."
        assert payload["current_frame"]["id"] == "current-1"
        assert payload["evidence"][0]["id"] == "prior-1"
        assert payload["evidence"][0]["score"] == 0.88
        assert payload["text_evidence"][0]["metadata"]["source_path"] == "src/header.css"
        assert len([call for call in calls if call[1] == "/v1/vision/analyze-context"]) == 1
        assert all(call[0] in {"visual-memory", "vlm"} for call in calls)
        assert self.gateway.VISUAL_METRICS.counter("qwen_escalations_total") == qwen_calls_before + 1

    def test_metadata_only_retention_reaches_index_and_missing_images_are_omitted_from_vlm(self):
        calls = []
        current_image = png_bytes("blue")
        original_forward = self.gateway.forward_buffered

        async def fake_forward(service, path, request, body=None, content_type=None, **kwargs):
            calls.append(path)
            if path == "/v1/visual-memory/index":
                form = parse_multipart(body, content_type)
                assert form["retention_policy"][0][1] == b"metadata-only"
                return Response(json.dumps({"frame_id": "current-only-metadata", "observation_id": "obs-current",
                                            "duplicate": False, "model": "google/embeddinggemma-2",
                                            "revision": "pinned"}), media_type="application/json")
            if path == "/v1/visual-memory/search/image":
                form = parse_multipart(body, content_type)
                parent = form.get("parent_id", [(None, b"")])[0][1].decode()
                if parent == "current-only-metadata":
                    matches = [{"id": parent, "vector_id": 11,
                                "crop": {"type": "center", "bbox": {"x": 0, "y": 0, "w": 1, "h": 1}}}]
                else:
                    matches = [{"id": "prior-metadata-only", "score": 0.75,
                                "metadata": {"namespace": "website:demo"}}]
                return Response(json.dumps({"matches": matches}), media_type="application/json")
            if path == "/v1/visual-memory/context-pack":
                pack = json.loads(body)
                return Response(json.dumps({
                    "current_frame": pack["current_frame"], "retrieved": pack["retrieved"],
                    "crops": pack["crops"], "text_context": [], "temporal_context": [],
                    "structured_context": pack["structured_context"],
                }), media_type="application/json")
            if path == "/v1/visual-memory/assets/read":
                return Response(b'{"detail":"visual image bytes are not retained"}', status_code=404,
                                media_type="application/json")
            if path == "/v1/vision/analyze-context":
                form = parse_multipart(body, content_type)
                assert form["current_image"][0][1] == current_image
                assert "crop_images" not in form
                assert "reference_images" not in form
                return Response(json.dumps({"model": "Qwen/Qwen3-VL-4B-Instruct", "text": "Analysis completed."}),
                                media_type="application/json")
            raise AssertionError(f"unexpected worker call {service} {path}")

        self.gateway.forward_buffered = fake_forward
        try:
            response = self.client.post(
                "/v1/vision/analyze-detailed",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                data={"analysis_profile": "website", "namespace": "website:demo",
                      "retention_policy": "metadata-only", "prompt": "Summarize this page."},
                files={"image": ("current.png", current_image, "image/png")},
            )
        finally:
            self.gateway.forward_buffered = original_forward

        assert response.status_code == 200
        assert response.json()["analysis"]["text"] == "Analysis completed."
        assert "/v1/vision/analyze-context" in calls

    def test_detailed_endpoint_rejects_invalid_profiles_and_context_without_worker_calls(self):
        calls = []
        original_forward = self.gateway.forward_buffered

        async def fake_forward(*args, **kwargs):
            calls.append(args)
            return Response(b"{}", media_type="application/json")

        self.gateway.forward_buffered = fake_forward
        try:
            invalid_profile = self.client.post(
                "/v1/vision/analyze-detailed",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                data={"analysis_profile": "arbitrary", "context_json": "{}"},
                files={"image": ("current.png", png_bytes(), "image/png")},
            )
            invalid_context = self.client.post(
                "/v1/vision/analyze-detailed",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                data={"analysis_profile": "game", "context_json": "{"},
                files={"image": ("current.png", png_bytes(), "image/png")},
            )
            invalid_skip_policy = self.client.post(
                "/v1/vision/analyze-detailed",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                data={"analysis_profile": "game", "skip_if_identical_frame": "sometimes"},
                files={"image": ("current.png", png_bytes(), "image/png")},
            )
        finally:
            self.gateway.forward_buffered = original_forward

        assert invalid_profile.status_code == 400
        assert invalid_context.status_code == 400
        assert invalid_skip_policy.status_code == 400
        assert calls == []

    def test_explicit_exact_duplicate_policy_skips_qwen_and_records_avoided_escalation(self):
        calls = []
        avoided_before = self.gateway.VISUAL_METRICS.counter("qwen_escalations_avoided_total")
        qwen_before = self.gateway.VISUAL_METRICS.counter("qwen_escalations_total")
        original_forward = self.gateway.forward_buffered

        async def fake_forward(service, path, request, body=None, content_type=None, **kwargs):
            calls.append((service, path))
            assert path == "/v1/visual-memory/index"
            return Response(json.dumps({
                "frame_id": "known-frame",
                "observation_id": "known-observation",
                "duplicate": True,
                "duplicate_level": "identical",
                "model": "google/embeddinggemma-2",
                "revision": "pinned",
            }), media_type="application/json")

        self.gateway.forward_buffered = fake_forward
        try:
            response = self.client.post(
                "/v1/vision/analyze-detailed",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                data={"analysis_profile": "website", "namespace": "website:demo",
                      "skip_if_identical_frame": "true", "prompt": "Describe the page."},
                files={"image": ("current.png", png_bytes(), "image/png")},
            )
        finally:
            self.gateway.forward_buffered = original_forward

        assert response.status_code == 200
        payload = response.json()
        assert payload["analysis_skipped"] is True
        assert payload["skip_reason"] == "identical_frame_already_indexed"
        assert payload["current_frame"]["id"] == "known-frame"
        assert calls == [("visual-memory", "/v1/visual-memory/index")]
        assert self.gateway.VISUAL_METRICS.counter("qwen_escalations_avoided_total") == avoided_before + 1
        assert self.gateway.VISUAL_METRICS.counter("qwen_escalations_total") == qwen_before

    def test_compare_accepts_uploaded_reference_and_sends_both_original_images(self):
        calls = []
        current_image = png_bytes("red")
        reference_image = png_bytes("blue")
        original_forward = self.gateway.forward_buffered

        async def fake_forward(service, path, request, body=None, content_type=None, **kwargs):
            calls.append((service, path))
            if path == "/v1/visual-memory/index":
                return Response(json.dumps({"frame_id": "current-compare", "observation_id": "obs-compare",
                                            "duplicate": False, "model": "google/embeddinggemma-2",
                                            "revision": "pinned"}), media_type="application/json")
            if path == "/v1/visual-memory/search/image":
                return Response(json.dumps({"matches": [], "model": "google/embeddinggemma-2",
                                            "revision": "pinned", "dimension": 768}), media_type="application/json")
            if path == "/v1/visual-memory/context-pack":
                pack = json.loads(body)
                return Response(json.dumps({
                    "current_frame": pack["current_frame"], "retrieved": pack["retrieved"],
                    "crops": [], "structured_context": pack["structured_context"],
                    "text_context": [], "temporal_context": [],
                }), media_type="application/json")
            if path == "/v1/vision/analyze-context":
                form = parse_multipart(body, content_type)
                assert form["current_image"][0][1] == current_image
                assert form["reference_images"][0][1] == reference_image
                assert b"Compare the current image" in form["prompt"][0][1]
                return Response(json.dumps({"model": "Qwen/Qwen3-VL-4B-Instruct", "text": "The header moved."}),
                                media_type="application/json")
            raise AssertionError(f"unexpected worker call {service} {path}")

        self.gateway.forward_buffered = fake_forward
        try:
            response = self.client.post(
                "/v1/vision/compare",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                data={"analysis_profile": "website", "namespace": "website:demo"},
                files=[
                    ("image", ("current.png", current_image, "image/png")),
                    ("reference", ("reference.png", reference_image, "image/png")),
                ],
            )
        finally:
            self.gateway.forward_buffered = original_forward

        assert response.status_code == 200
        assert response.json()["analysis"]["text"] == "The header moved."
        assert len([call for call in calls if call[1] == "/v1/vision/analyze-context"]) == 1

    def test_compare_requires_exactly_one_reference_before_worker_calls(self):
        calls = []
        original_forward = self.gateway.forward_buffered

        async def fake_forward(*args, **kwargs):
            calls.append(args)
            return Response(b"{}", media_type="application/json")

        self.gateway.forward_buffered = fake_forward
        try:
            missing = self.client.post(
                "/v1/vision/compare",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                files={"image": ("current.png", png_bytes(), "image/png")},
            )
            both = self.client.post(
                "/v1/vision/compare",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                data={"reference_id": "saved-frame"},
                files=[
                    ("image", ("current.png", png_bytes(), "image/png")),
                    ("reference", ("reference.png", png_bytes("blue"), "image/png")),
                ],
            )
        finally:
            self.gateway.forward_buffered = original_forward

        assert missing.status_code == 400
        assert both.status_code == 400
        assert calls == []

    def test_legacy_vision_route_forwards_the_original_multipart_contract(self):
        raw_seen = {}
        original_forward = self.gateway.forward_buffered
        image = png_bytes()

        async def fake_forward(service, path, request, body=None, **kwargs):
            raw_seen.update(service=service, path=path, body=body)
            return Response(b'{"text":"legacy"}', media_type="application/json")

        self.gateway.forward_buffered = fake_forward
        try:
            response = self.client.post(
                "/v1/vision/analyze",
                headers={"Authorization": "Bearer test-only-gateway-key"},
                data={"prompt": "Describe this image.", "max_new_tokens": "20"},
                files={"image": ("legacy.png", image, "image/png")},
            )
        finally:
            self.gateway.forward_buffered = original_forward

        assert response.status_code == 200
        assert raw_seen["service"] == "vlm"
        assert raw_seen["path"] == "/v1/vision/analyze"
        assert b"legacy.png" in raw_seen["body"]
        assert image in raw_seen["body"]
