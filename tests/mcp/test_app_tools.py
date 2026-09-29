from __future__ import annotations

import os
import sys
import asyncio
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


if __name__ == "__main__":
    unittest.main()
