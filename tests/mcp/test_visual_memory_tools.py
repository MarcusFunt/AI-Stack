from __future__ import annotations

import asyncio
import base64
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp"))
with patch.dict(os.environ, {
    "MCP_API_KEY": "test-mcp-api-key",
    "MCP_URL_TOKEN": "test-mcp-url-token",
    "AI_API_KEY": "test-ai-api-key",
}):
    import app as service_app


def test_mcp_registers_visual_memory_tools():
    names = {tool.name for tool in service_app.mcp._tool_manager.list_tools()}
    assert {"index_visual_frame", "search_visual_memory", "analyze_visual"} <= names


def test_index_visual_frame_validates_and_forwards_authenticated_gateway_multipart(monkeypatch):
    observed = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"frame_id": "frame-1", "duplicate": False}

    async def fake_request(method, path, **kwargs):
        observed.update(method=method, path=path, **kwargs)
        return FakeResponse()

    monkeypatch.setattr(service_app, "gateway_audio_request", fake_request)
    image = base64.b64encode(b"frame-bytes").decode("ascii")
    context = SimpleNamespace(request_id="mcp-visual-index")
    result = asyncio.run(service_app.index_visual_frame(
        image,
        namespace="game:demo",
        filename="frame.png",
        content_type="image/png",
        source="game",
        session_id="session-1",
        sequence_id="match-2",
        sequence_number=3,
        tags=["combat"],
        metadata={"scene": "arena"},
        ctx=context,
    ))

    assert result["frame_id"] == "frame-1"
    assert observed["method"] == "POST"
    assert observed["path"] == "/v1/visual-memory/index"
    assert observed["files"]["image"][1] == b"frame-bytes"
    assert dict(observed["data"])["namespace"] == "game:demo"
    assert dict(observed["data"])["sequence_number"] == "3"
    assert observed["trace_context"].request_id == "mcp-visual-index"


def test_mcp_visual_upload_uses_authenticated_gateway_and_trace_headers(monkeypatch):
    observed = {}

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {"ok": True}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, url, *, headers, json=None, data=None, files=None):
            observed.update(method=method, url=url, headers=dict(headers), files=files)
            return FakeResponse()

    monkeypatch.setattr(service_app.httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    response = asyncio.run(service_app.gateway_audio_request(
        "POST",
        "/v1/visual-memory/index",
        files={"image": ("frame.png", b"frame-bytes", "image/png")},
        trace_context=service_app.TraceContext(request_id="mcp-upload-trace"),
    ))

    assert response.json()["ok"] is True
    assert observed["url"] == service_app.GATEWAY_URL + "/v1/visual-memory/index"
    assert observed["headers"]["Authorization"] == "Bearer test-ai-api-key"
    assert observed["headers"]["X-Request-ID"] == "mcp-upload-trace"


def test_search_visual_memory_uses_gateway_text_and_image_routes(monkeypatch):
    observed = []

    async def fake_json(method, path, **kwargs):
        observed.append(("json", method, path, kwargs))
        return {"matches": [{"id": "text-frame"}]}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"matches": [{"id": "image-frame"}]}

    async def fake_multipart(method, path, **kwargs):
        observed.append(("multipart", method, path, kwargs))
        return FakeResponse()

    monkeypatch.setattr(service_app, "gateway_request", fake_json)
    monkeypatch.setattr(service_app, "gateway_audio_request", fake_multipart)
    ctx = SimpleNamespace(request_id="mcp-visual-search")
    text_result = asyncio.run(service_app.search_visual_memory(
        query="a scoreboard", namespace="game:demo", top_k=4, ctx=ctx
    ))
    image_result = asyncio.run(service_app.search_visual_memory(
        image_base64=base64.b64encode(b"query image").decode("ascii"),
        namespace="game:demo", top_k=4, ctx=ctx,
    ))

    assert text_result["matches"][0]["id"] == "text-frame"
    assert image_result["matches"][0]["id"] == "image-frame"
    assert observed[0][2] == "/v1/visual-memory/search/text"
    assert observed[1][2] == "/v1/visual-memory/search/image"
    assert observed[1][3]["files"]["image"][1] == b"query image"


def test_analyze_visual_forwards_context_to_detailed_gateway_and_rejects_bad_inputs(monkeypatch):
    observed = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"analysis": {"text": "Looks playable."}}

    async def fake_request(method, path, **kwargs):
        observed.update(method=method, path=path, **kwargs)
        return FakeResponse()

    monkeypatch.setattr(service_app, "gateway_audio_request", fake_request)
    image = base64.b64encode(b"image bytes").decode("ascii")
    result = asyncio.run(service_app.analyze_visual(
        image,
        filename="screen.png",
        namespace="game:demo",
        analysis_profile="game",
        context={"scene": "arena", "frame": 20, "telemetry": {"health": 65}},
        prompt="Is the health bar readable?",
        ctx=SimpleNamespace(request_id="mcp-visual-analyze"),
    ))

    assert result["analysis"]["text"] == "Looks playable."
    assert observed["path"] == "/v1/vision/analyze-detailed"
    assert observed["files"]["image"][1] == b"image bytes"
    assert dict(observed["data"])["analysis_profile"] == "game"
    assert '"health":65' in dict(observed["data"])["context_json"]

    with pytest.raises(ValueError, match="base64"):
        asyncio.run(service_app.analyze_visual("not base64", ctx=SimpleNamespace(request_id="bad")))


def test_search_and_compare_arguments_are_validated_before_gateway_calls(monkeypatch):
    calls = []

    async def fake_request(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("invalid MCP arguments must not reach the gateway")

    monkeypatch.setattr(service_app, "gateway_request", fake_request)
    monkeypatch.setattr(service_app, "gateway_audio_request", fake_request)
    ctx = SimpleNamespace(request_id="mcp-invalid")

    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(service_app.search_visual_memory(namespace="game:demo", ctx=ctx))
    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(service_app.search_visual_memory(
            query="scoreboard", image_base64=base64.b64encode(b"x").decode(), namespace="game:demo", ctx=ctx
        ))
    with pytest.raises(ValueError, match="reference"):
        asyncio.run(service_app.analyze_visual(
            base64.b64encode(b"x").decode(), reference_id="frame-1", reference_image_base64=base64.b64encode(b"y").decode(),
            ctx=ctx,
        ))

    assert calls == []
