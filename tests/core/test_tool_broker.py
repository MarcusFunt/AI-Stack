import asyncio
import unittest
from types import SimpleNamespace
from uuid import uuid4

from core.context import TraceContext
from core.cancellation import CancellationToken, InvocationCancelled
from core.invocation import Principal
from core.tools import ToolDefinition
from core.tool_broker import (
    FunctionToolProvider,
    MQTTToolProvider,
    ToolAuthorizationError,
    ToolBroker,
    ToolCall,
    ToolInputError,
    ToolOutputTooLarge,
)


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    def call(self, name="echo", arguments=None):
        return ToolCall(id="call_1", invocation_id=str(uuid4()), tool_name=name, arguments=arguments or {}, trace_context=TraceContext())

    async def test_registry_executes_internal_tool_after_permission_and_schema_checks(self):
        provider = FunctionToolProvider({"echo": lambda arguments: {"echo": arguments["text"]}})
        broker = ToolBroker()
        broker.register_provider("internal", provider)
        broker.register_tool(ToolDefinition(
            name="echo",
            provider="internal",
            description="Return the supplied text",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False},
            permissions={"tools:echo"},
            timeout_ms=1000,
        ))

        with self.assertRaises(ToolAuthorizationError):
            await broker.execute(self.call(arguments={"text": "hello"}), principal=Principal(id="u1", kind="user"))
        with self.assertRaises(ToolInputError):
            await broker.execute(self.call(arguments={"text": 4}), principal=Principal(id="u1", kind="user", scopes={"tools:echo"}))

        result = await broker.execute(self.call(arguments={"text": "hello"}), principal=Principal(id="u1", kind="user", scopes={"tools:echo"}))
        self.assertTrue(result.success)
        self.assertEqual(result.content, {"echo": "hello"})
        self.assertEqual(result.call_id, "call_1")

    async def test_tool_definitions_and_calls_copy_nested_json_state(self):
        schema = {"type": "object", "properties": {"nested": {"type": "object", "properties": {"x": {"type": "string"}}}}}
        definition = ToolDefinition(name="echo", input_schema=schema)
        schema["properties"]["nested"]["properties"]["x"]["type"] = "number"
        self.assertEqual(definition.input_schema["properties"]["nested"]["properties"]["x"]["type"], "string")

        arguments = {"nested": {"x": "original"}}
        call = self.call(arguments=arguments)
        arguments["nested"]["x"] = "changed"
        self.assertEqual(call.arguments["nested"]["x"], "original")
        with self.assertRaises(ToolInputError):
            from core.tool_broker import validate_tool_arguments
            validate_tool_arguments({}, {"required": ["must_exist"]})

    async def test_timeout_cancels_provider_and_returns_bounded_error(self):
        class SlowProvider:
            def __init__(self):
                self.cancelled = []

            async def execute(self, call, context):
                await asyncio.sleep(10)

            async def cancel(self, call_id):
                self.cancelled.append(call_id)

        provider = SlowProvider()
        broker = ToolBroker()
        broker.register_provider("internal", provider)
        broker.register_tool(ToolDefinition(name="slow", provider="internal", timeout_ms=10))
        result = await broker.execute(self.call("slow"), principal=Principal(id="u", kind="user"))

        self.assertFalse(result.success)
        self.assertEqual(result.error, "tool timed out")
        self.assertEqual(provider.cancelled, ["call_1"])

    async def test_output_size_limit_prevents_large_tool_results(self):
        broker = ToolBroker(max_output_bytes=10)
        broker.register_provider("internal", FunctionToolProvider({"large": lambda arguments: "x" * 100}))
        broker.register_tool(ToolDefinition(name="large", provider="internal"))
        result = await broker.execute(self.call("large"), principal=Principal(id="u", kind="user"))

        self.assertFalse(result.success)
        self.assertEqual(result.error, "tool output exceeds size limit")

    async def test_cancellation_token_stops_active_tool(self):
        class SlowProvider:
            async def execute(self, call, context):
                await asyncio.sleep(10)

            async def cancel(self, call_id):
                return None

        broker = ToolBroker()
        broker.register_provider("internal", SlowProvider())
        broker.register_tool(ToolDefinition(name="slow", provider="internal", timeout_ms=5000))
        token = CancellationToken()
        task = asyncio.create_task(broker.execute(self.call("slow"), principal=Principal(id="u", kind="user"), cancellation=token))
        await asyncio.sleep(0.01)
        token.cancel("caller disconnected")
        with self.assertRaises(InvocationCancelled):
            await task

    async def test_provider_import_error_is_not_retried(self):
        calls = []

        async def broken(arguments):
            calls.append(1)
            raise ImportError("tool dependency missing")

        broker = ToolBroker()
        broker.register_provider("internal", FunctionToolProvider({"broken": broken}))
        broker.register_tool(ToolDefinition(name="broken", provider="internal"))
        result = await broker.execute(self.call("broken"), principal=Principal(id="u", kind="user"))

        self.assertFalse(result.success)
        self.assertEqual(result.error, "tool execution failed")
        self.assertEqual(len(calls), 1)

    async def test_mqtt_provider_publishes_only_registered_topics(self):
        class MQTTClient:
            def __init__(self):
                self.calls = []

            def publish(self, topic, payload, qos, retain):
                self.calls.append((topic, payload, qos, retain))
                return SimpleNamespace(rc=0, mid=7, wait_for_publish=lambda timeout=None: None)

        client = MQTTClient()
        broker = ToolBroker()
        broker.register_provider("mqtt", MQTTToolProvider(client, {"home.light.set": "home/light/set"}))
        broker.register_tool(ToolDefinition(name="home.light.set", provider="mqtt", input_schema={"type": "object"}, permissions={"home:write"}))
        result = await broker.execute(
            self.call("home.light.set", {"payload": {"state": "on"}}),
            principal=Principal(id="ha", kind="service", scopes={"home:write"}),
        )
        self.assertTrue(result.success)
        self.assertEqual(client.calls[0][0], "home/light/set")
        self.assertIn('"state":"on"', client.calls[0][1])
        self.assertFalse(client.calls[0][3])

        broker.register_tool(ToolDefinition(name="unmapped", provider="mqtt"))
        rejected = await broker.execute(self.call("unmapped"), principal=Principal(id="ha", kind="service"))
        self.assertFalse(rejected.success)
        self.assertNotIn("unmapped", [item[0] for item in client.calls])


if __name__ == "__main__":
    unittest.main()
