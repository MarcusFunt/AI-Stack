import asyncio
import json
import unittest

from gateway.adapters.openai_responses import OpenAIResponsesAdapter, ResponsesRequestError, map_chat_stream


class OpenAIResponsesAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = OpenAIResponsesAdapter()

    def test_maps_text_instructions_tools_and_metadata_to_chat(self):
        chat = self.adapter.to_chat_request({
            "model": "fast",
            "instructions": "Be concise",
            "input": "Hello",
            "tools": [{"type": "function", "name": "lookup", "description": "Look up", "parameters": {"type": "object"}}],
            "metadata": {"ticket": "42"},
            "max_output_tokens": 64,
        })

        self.assertEqual(chat["model"], "fast")
        self.assertEqual(chat["messages"][0], {"role": "system", "content": "Be concise"})
        self.assertEqual(chat["messages"][1], {"role": "user", "content": "Hello"})
        self.assertEqual(chat["tools"][0]["function"]["name"], "lookup")
        self.assertEqual(chat["metadata"], {"ticket": "42"})
        self.assertEqual(chat["max_tokens"], 64)

    def test_maps_stateless_conversation_and_tool_results(self):
        chat = self.adapter.to_chat_request({
            "input": [
                {"type": "message", "role": "user", "content": "Find it"},
                {"type": "function_call_output", "call_id": "call_1", "output": "found"},
            ],
        })
        self.assertEqual(chat["messages"][0]["role"], "user")
        self.assertEqual(chat["messages"][1], {"role": "tool", "tool_call_id": "call_1", "content": "found"})

    def test_rejects_unsupported_state_and_non_text_inputs_explicitly(self):
        with self.assertRaises(ResponsesRequestError):
            self.adapter.to_chat_request({"input": "hi", "previous_response_id": "resp_old"})
        with self.assertRaises(ResponsesRequestError):
            self.adapter.to_chat_request({"input": [{"type": "input_image", "image_url": "data:image/png;base64,..."}]})
        with self.assertRaises(ResponsesRequestError):
            self.adapter.to_chat_request({"input": "hi", "tool_choice": {"type": "unsupported"}})
        with self.assertRaises(ResponsesRequestError):
            self.adapter.to_chat_request({"input": "hi", "reasoning": {"effort": "high"}})

    def test_maps_buffered_text_tools_usage_and_metadata(self):
        response = self.adapter.from_chat_response({
            "id": "chatcmpl_1",
            "model": "local-fast",
            "choices": [{"message": {
                "role": "assistant",
                "content": "hello",
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
            }}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }, {"metadata": {"ticket": "42"}})
        self.assertEqual(response["object"], "response")
        self.assertEqual(response["output"][0]["content"][0]["text"], "hello")
        self.assertEqual(response["output"][1]["type"], "function_call")
        self.assertEqual(response["usage"]["total_tokens"], 7)
        self.assertEqual(response["metadata"], {"ticket": "42"})

    def test_streaming_tool_call_emits_function_argument_events(self):
        def sse(value):
            return ("data: " + json.dumps(value) + "\n\n").encode()

        async def chunks():
            yield sse({"choices": [{"delta": {"tool_calls": [{
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{\"q\":"},
            }]}, "finish_reason": None}]})
            yield sse({"choices": [{"delta": {"tool_calls": [{
                "index": 0,
                "function": {"arguments": "\"x\"}"},
            }]}, "finish_reason": None}]})
            yield b"data: [DONE]\n\n"

        async def collect():
            return [chunk async for chunk in map_chat_stream(chunks(), adapter=self.adapter, request_payload={"model": "local-fast"}, response_id="resp_tool")]

        output = b"".join(asyncio.run(collect())).decode()
        self.assertIn("event: response.function_call_arguments.delta", output)
        self.assertIn("event: response.function_call_arguments.done", output)
        self.assertIn("lookup", output)
        self.assertIn('"arguments":"{\\"q\\":\\"x\\"}"', output)

    def test_stream_parser_handles_split_crlf_and_json_chunks(self):
        async def chunks():
            event = b'data: {"choices":[{"delta":{"content":"split"},"finish_reason":null}]}\r\n\r\n'
            yield event[:17]
            yield event[17:len(event) - 1]
            yield event[len(event) - 1:]
            yield b"data: [DONE]\r\n\r\n"

        async def collect():
            return [chunk async for chunk in map_chat_stream(chunks(), adapter=self.adapter, request_payload={"model": "local-fast"}, response_id="resp_split")]

        output = b"".join(asyncio.run(collect())).decode()
        self.assertIn('"delta":"split"', output)
        self.assertIn("event: response.completed", output)

    def test_translates_chat_sse_into_responses_text_events(self):
        async def chunks():
            yield b'data: {"choices":[{"delta":{"role":"assistant","content":"Hel"},"finish_reason":null}]}\n\n'
            yield b'data: {"choices":[{"delta":{"content":"lo"},"finish_reason":"stop"}]}\n\n'
            yield b"data: [DONE]\n\n"

        async def collect():
            return [chunk async for chunk in map_chat_stream(chunks(), adapter=self.adapter, request_payload={"model": "local-fast"}, response_id="resp_1")]

        output = b"".join(asyncio.run(collect())).decode()
        events = [line.removeprefix("event: ") for line in output.splitlines() if line.startswith("event: ")]
        payloads = [json.loads(line.removeprefix("data: ")) for line in output.splitlines() if line.startswith("data: ")]
        deltas = [item["delta"] for item in payloads if "delta" in item]
        added = next(item["item"] for item in payloads if item.get("type") == "response.output_item.added")
        completed = next(item["response"] for item in payloads if item.get("type") == "response.completed")

        self.assertIn("response.created", events)
        self.assertIn("response.output_text.delta", events)
        self.assertIn("response.completed", events)
        self.assertEqual("".join(deltas), "Hello")
        self.assertEqual(completed["output"][0]["id"], added["id"])


if __name__ == "__main__":
    unittest.main()
