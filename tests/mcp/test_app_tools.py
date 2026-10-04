from __future__ import annotations

import os
import sys
import asyncio
import base64
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp"))

with patch.dict(os.environ, {
    "MCP_API_KEY": "test-mcp-api-key",
    "MCP_URL_TOKEN": "test-mcp-url-token",
    "AI_API_KEY": "test-ai-api-key",
}):
    import app as service_app


class InboundMCPToolTests(unittest.TestCase):
    def test_inbound_mcp_exposes_ai_aliases_and_broker_tools(self):
        names = {tool.name for tool in service_app.mcp._tool_manager.list_tools()}

        self.assertTrue({
            "ai.generate",
            "ai.reason",
            "ai.models.list",
            "ai.system.status",
            "mcp_list_tools",
            "mcp_call_tool",
            "ask_local_ai",
        } <= names)

    def test_gateway_model_requests_include_inbound_mcp_trace_context(self):
        observed = {}
        original_client = service_app.httpx.AsyncClient

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": "hello"}}]}

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def request(self, method, url, *, headers, json=None):
                observed.update(method=method, url=url, headers=dict(headers), json=json)
                return FakeResponse()

        context = SimpleNamespace(
            request_context=SimpleNamespace(request=SimpleNamespace(headers={
                "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
                "x-request-id": "mcp-request-42",
            })),
            request_id="mcp-request-42",
        )
        service_app.httpx.AsyncClient = lambda **_kwargs: FakeClient()
        try:
            result = asyncio.run(service_app.ask_local_ai("hi", ctx=context))
        finally:
            service_app.httpx.AsyncClient = original_client

        self.assertEqual(result, "hello")
        self.assertEqual(observed["headers"]["traceparent"], "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
        self.assertEqual(observed["headers"]["X-Request-ID"], "mcp-request-42")

    def test_mcp_registers_speech_and_transcription_tools(self):
        names = {tool.name for tool in service_app.mcp._tool_manager.list_tools()}

        self.assertTrue({"local_ai_synthesize_speech", "local_ai_transcribe_audio"} <= names)

    def test_mcp_synthesis_passes_english_expressive_options_and_returns_audio(self):
        observed = {}
        original_client = service_app.httpx.AsyncClient

        class FakeResponse:
            status_code = 200
            content = b"fake mp3 data"
            headers = {"content-type": "audio/mpeg"}
            text = ""

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def request(self, method, url, *, headers, json=None, data=None, files=None):
                observed.update(method=method, url=url, headers=dict(headers), json=json, data=data, files=files)
                return FakeResponse()

        service_app.httpx.AsyncClient = lambda **_kwargs: FakeClient()
        try:
            result = asyncio.run(service_app.local_ai_synthesize_speech(
                "Hello, there.",
                voice="Vivian",
                instruct="Cheerfully, with a dramatic pause.",
                speed=0.9,
                response_format="mp3",
                ctx=SimpleNamespace(request_id="mcp-tts"),
            ))
        finally:
            service_app.httpx.AsyncClient = original_client

        self.assertEqual(observed["url"], service_app.GATEWAY_URL + "/v1/audio/speech")
        self.assertEqual(observed["json"]["language"], "English")
        self.assertEqual(observed["json"]["instruct"], "Cheerfully, with a dramatic pause.")
        self.assertEqual(result["mime_type"], "audio/mpeg")
        self.assertEqual(base64.b64decode(result["audio_base64"]), b"fake mp3 data")

    def test_mcp_transcription_uploads_audio_and_forwards_options(self):
        observed = {}
        original_client = service_app.httpx.AsyncClient

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = '{"text":"Hello."}'

            def json(self):
                return {"text": "Hello."}

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def request(self, method, url, *, headers, json=None, data=None, files=None):
                observed.update(method=method, url=url, headers=dict(headers), json=json, data=data, files=files)
                return FakeResponse()

        service_app.httpx.AsyncClient = lambda **_kwargs: FakeClient()
        try:
            result = asyncio.run(service_app.local_ai_transcribe_audio(
                base64.b64encode(b"audio bytes").decode("ascii"),
                filename="sample.wav",
                content_type="audio/wav",
                language="en",
                prompt="Names: Alex and Morgan.",
                temperature=0.2,
                timestamp_granularities=["segment", "word"],
                response_format="json",
                ctx=SimpleNamespace(request_id="mcp-stt"),
            ))
        finally:
            service_app.httpx.AsyncClient = original_client

        self.assertEqual(observed["url"], service_app.GATEWAY_URL + "/v1/audio/transcriptions")
        self.assertIn(("language", "en"), observed["data"])
        self.assertIn(("prompt", "Names: Alex and Morgan."), observed["data"])
        self.assertIn(("temperature", "0.2"), observed["data"])
        self.assertEqual(
            [value for key, value in observed["data"] if key == "timestamp_granularities[]"],
            ["segment", "word"],
        )
        self.assertEqual(observed["files"]["file"][1], b"audio bytes")
        self.assertEqual(result["text"], "Hello.")


if __name__ == "__main__":
    unittest.main()
