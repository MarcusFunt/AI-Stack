from __future__ import annotations

import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp"))

from core.context import TraceContext
from core.invocation import Principal
from core.tool_broker import ToolBroker, ToolCall
from outbound import (
    MCPConfigError,
    MCPRemoteToolProvider,
    load_mcp_auth_environment,
    load_mcp_server_configs,
)


class FakeSession:
    def __init__(self):
        self.initialized = False
        self.calls = []

    async def initialize(self):
        self.initialized = True

    async def list_tools(self):
        return {
            "tools": [
                {
                    "name": "lookup",
                    "description": "Look up an item",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"key": {"type": "string"}},
                        "required": ["key"],
                        "additionalProperties": False,
                    },
                },
                {"name": "not_allowed", "inputSchema": {"type": "object"}},
                {
                    "name": "unchecked_schema",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"key": {"type": "string", "pattern": ".*"}},
                    },
                },
            ]
        }

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return type("CallResult", (), {"isError": False, "structuredContent": {"found": arguments["key"]}, "content": []})()


class MCPToolProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_configuration_reads_credential_by_environment_variable_name(self):
        configs = load_mcp_server_configs(
            '[{"id":"docs","url":"https://mcp.example/tools","allowed_tools":["lookup"],"auth_env":"DOCS_TOKEN"}]'
        )
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0].auth_env, "DOCS_TOKEN")
        self.assertNotIn("secret-that-must-not-enter-config", repr(configs[0]))

    def test_configuration_rejects_credentials_in_urls_and_non_loopback_http(self):
        with self.assertRaises(MCPConfigError):
            load_mcp_server_configs('[{"id":"docs","url":"https://user:secret@mcp.example","allowed_tools":["lookup"]}]')
        with self.assertRaises(MCPConfigError):
            load_mcp_server_configs('[{"id":"docs","url":"http://mcp.example/tools","allowed_tools":["lookup"]}]')

    def test_auth_environment_map_keeps_tokens_separate_from_server_configuration(self):
        credentials = load_mcp_auth_environment('{"DOCS_TOKEN":"secret-value"}')
        self.assertEqual(credentials, {"DOCS_TOKEN": "secret-value"})
        with self.assertRaises(MCPConfigError):
            load_mcp_auth_environment('{"bad-name":"secret-value"}')

    async def test_discovery_registers_only_exactly_allowlisted_tools(self):
        config = load_mcp_server_configs(
            '[{"id":"docs","url":"https://mcp.example/tools","allowed_tools":["lookup","unchecked_schema"]}]'
        )[0]
        session = FakeSession()

        @asynccontextmanager
        async def session_factory(url, headers, timeout):
            yield session

        provider = MCPRemoteToolProvider([config], session_factory=session_factory)
        definitions = await provider.discover()

        self.assertTrue(session.initialized)
        self.assertEqual([item.name for item in definitions], ["mcp.docs.lookup"])
        self.assertEqual(provider.server_status()["docs"]["state"], "ready")
        self.assertEqual(provider.server_status()["docs"]["tool_count"], 1)

    async def test_broker_call_passes_auth_and_trace_context_to_remote_server(self):
        config = load_mcp_server_configs(
            '[{"id":"docs","url":"https://mcp.example/tools","allowed_tools":["lookup"],"auth_env":"DOCS_TOKEN"}]'
        )[0]
        session = FakeSession()
        seen = {}

        @asynccontextmanager
        async def session_factory(url, headers, timeout):
            seen.update(url=url, headers=dict(headers), timeout=timeout)
            yield session

        provider = MCPRemoteToolProvider(
            [config], environ={"DOCS_TOKEN": "top-secret"}, session_factory=session_factory
        )
        broker = ToolBroker()
        broker.register_provider(config.provider_id, provider)
        for definition in await provider.discover():
            broker.register_tool(definition)
        trace = TraceContext(trace_id="a" * 32, span_id="b" * 16)
        call = ToolCall(
            id="call-1",
            invocation_id=str(uuid4()),
            tool_name="mcp.docs.lookup",
            arguments={"key": "item-7"},
            trace_context=trace,
        )

        result = await broker.execute(
            call,
            principal=Principal(id="mcp-client", kind="service", scopes={"mcp:docs:lookup"}),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.content, {"found": "item-7"})
        self.assertEqual(session.calls, [("lookup", {"key": "item-7"})])
        self.assertEqual(seen["headers"]["Authorization"], "Bearer top-secret")
        self.assertTrue(seen["headers"]["traceparent"].startswith(f"00-{trace.trace_id}-"))
        self.assertNotIn("top-secret", repr(provider.server_status()))

    async def test_unavailable_server_is_reported_without_raising_from_discovery(self):
        config = load_mcp_server_configs(
            '[{"id":"docs","url":"https://mcp.example/tools","allowed_tools":["lookup"]}]'
        )[0]

        @asynccontextmanager
        async def broken_factory(url, headers, timeout):
            raise OSError("server offline")
            yield  # pragma: no cover

        provider = MCPRemoteToolProvider([config], session_factory=broken_factory)
        definitions = await provider.discover()

        self.assertEqual(definitions, ())
        self.assertEqual(provider.server_status()["docs"]["state"], "unavailable")
        self.assertEqual(provider.server_status()["docs"]["tool_count"], 0)

    async def test_slow_server_is_bounded_and_reported_unavailable(self):
        config = load_mcp_server_configs(
            '[{"id":"docs","url":"https://mcp.example/tools","allowed_tools":["lookup"]}]'
        )[0]

        @asynccontextmanager
        async def slow_factory(url, headers, timeout):
            import asyncio
            await asyncio.sleep(2)
            yield FakeSession()

        provider = MCPRemoteToolProvider([config], session_factory=slow_factory, timeout_seconds=1)
        definitions = await provider.discover()

        self.assertEqual(definitions, ())
        self.assertEqual(provider.server_status()["docs"]["state"], "unavailable")


if __name__ == "__main__":
    unittest.main()
