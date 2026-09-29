from __future__ import annotations

import json
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


class ResponsesRequestError(ValueError):
    pass


class OpenAIResponsesAdapter:
    """Translate the supported Responses API subset to and from chat payloads."""

    @staticmethod
    def _text_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, Mapping) and item.get("type") in {"input_text", "output_text", "text"} and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                else:
                    raise ResponsesRequestError("only text content is supported by this Responses API version")
            return "".join(parts)
        raise ResponsesRequestError("message content must be text")

    def _input_messages(self, value: Any) -> list[dict[str, Any]]:
        if isinstance(value, str):
            return [{"role": "user", "content": value}]
        if not isinstance(value, list):
            raise ResponsesRequestError("input must be a string or an array of text items")
        messages = []
        for item in value:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, Mapping):
                raise ResponsesRequestError("input array items must be objects or strings")
            item_type = item.get("type", "message")
            if item_type == "function_call_output":
                call_id, output = item.get("call_id"), item.get("output")
                if not isinstance(call_id, str) or not isinstance(output, str):
                    raise ResponsesRequestError("function_call_output requires string call_id and output")
                messages.append({"role": "tool", "tool_call_id": call_id, "content": output})
                continue
            if item_type == "function_call":
                call_id, name = item.get("call_id"), item.get("name")
                arguments = item.get("arguments", "{}")
                if not isinstance(call_id, str) or not isinstance(name, str) or not isinstance(arguments, str):
                    raise ResponsesRequestError("function_call requires string call_id, name, and arguments")
                messages.append({"role": "assistant", "content": None, "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }]})
                continue
            if item_type == "input_text":
                text = item.get("text")
                if not isinstance(text, str):
                    raise ResponsesRequestError("input_text requires a string text field")
                messages.append({"role": "user", "content": text})
                continue
            if item_type != "message":
                raise ResponsesRequestError(f"unsupported Responses input item type: {item_type!r}")
            role = item.get("role")
            if role not in {"user", "assistant", "system", "developer"}:
                raise ResponsesRequestError("message role must be user, assistant, system, or developer")
            content = self._text_content(item.get("content", ""))
            messages.append({"role": "system" if role == "developer" else role, "content": content})
        if not messages:
            raise ResponsesRequestError("input array must contain at least one item")
        return messages

    def to_chat_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ResponsesRequestError("request body must be an object")
        for field in ("previous_response_id", "conversation", "background"):
            if payload.get(field) not in (None, False):
                raise ResponsesRequestError(f"{field} is not supported; include conversation history in input")
        for field in ("include", "reasoning", "prompt", "service_tier", "truncation", "max_tool_calls"):
            if field in payload and payload[field] is not None:
                raise ResponsesRequestError(f"{field} is not supported by this Responses API version")
        store = payload.get("store", False)
        if not isinstance(store, bool):
            raise ResponsesRequestError("store must be a boolean")
        if store:
            raise ResponsesRequestError("stored Responses are not supported; send conversation history in input")
        if "stream" in payload and not isinstance(payload["stream"], bool):
            raise ResponsesRequestError("stream must be a boolean")
        if "metadata" in payload and not isinstance(payload["metadata"], Mapping):
            raise ResponsesRequestError("metadata must be an object")
        if "input" not in payload:
            raise ResponsesRequestError("input is required")
        model = payload.get("model", "local-fast")
        if not isinstance(model, str) or not model.strip():
            raise ResponsesRequestError("model must be a non-empty string")
        messages = self._input_messages(payload["input"])
        instructions = payload.get("instructions")
        if instructions is not None:
            if not isinstance(instructions, str):
                raise ResponsesRequestError("instructions must be a string")
            messages.insert(0, {"role": "system", "content": instructions})
        chat: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": bool(payload.get("stream", False)),
        }
        for source, target in (("temperature", "temperature"), ("top_p", "top_p"), ("max_output_tokens", "max_tokens")):
            if source in payload:
                value = payload[source]
                if isinstance(value, bool) or not isinstance(value, (float, int)):
                    raise ResponsesRequestError(f"{source} must be numeric")
                if source == "max_output_tokens":
                    if value <= 0 or not isinstance(value, int):
                        raise ResponsesRequestError("max_output_tokens must be a positive integer")
                elif not 0 <= value <= (1 if source == "top_p" else 2):
                    limit = 1 if source == "top_p" else 2
                    raise ResponsesRequestError(f"{source} must be between 0 and {limit}")
                chat[target] = value
        if isinstance(payload.get("metadata"), Mapping):
            chat["metadata"] = dict(payload["metadata"])
        if "tools" in payload:
            raw_tools = payload["tools"]
            if not isinstance(raw_tools, list):
                raise ResponsesRequestError("tools must be an array")
            tools = []
            for tool in raw_tools:
                if not isinstance(tool, Mapping) or tool.get("type") != "function" or not isinstance(tool.get("name"), str):
                    raise ResponsesRequestError("only function tools are supported")
                function = {"name": tool["name"]}
                for key in ("description", "parameters", "strict"):
                    if key in tool:
                        function[key] = tool[key]
                tools.append({"type": "function", "function": function})
            chat["tools"] = tools
        if "tool_choice" in payload:
            choice = payload["tool_choice"]
            if isinstance(choice, Mapping) and choice.get("type") == "function" and isinstance(choice.get("name"), str):
                chat["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
            elif isinstance(choice, str) and choice in {"auto", "none", "required"}:
                chat["tool_choice"] = choice
            else:
                raise ResponsesRequestError("tool_choice must be auto, none, required, or a function name")
        text = payload.get("text")
        if text is not None:
            if not isinstance(text, Mapping):
                raise ResponsesRequestError("text must be an object")
            output_format = text.get("format")
            if isinstance(output_format, Mapping) and output_format.get("type") == "json_object":
                chat["response_format"] = {"type": "json_object"}
            elif output_format not in (None, {"type": "text"}):
                raise ResponsesRequestError("only text and json_object output formats are supported")
        return chat

    def from_chat_response(
        self,
        chat_response: Mapping[str, Any],
        request_payload: Mapping[str, Any],
        *,
        response_id: str | None = None,
    ) -> dict[str, Any]:
        choices = chat_response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise ResponsesRequestError("backend returned an invalid chat completion")
        message = choices[0].get("message", {})
        if not isinstance(message, Mapping):
            raise ResponsesRequestError("backend returned an invalid assistant message")
        response_id = response_id or f"resp_{uuid4().hex}"
        created = datetime.now(timezone.utc).timestamp()
        model = str(chat_response.get("model") or request_payload.get("model", "local-fast"))
        output = []
        content = message.get("content")
        if isinstance(content, str):
            output.append({
                "id": f"msg_{uuid4().hex}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content, "annotations": []}],
            })
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if not isinstance(call, Mapping):
                    continue
                function = call.get("function", {})
                if not isinstance(function, Mapping) or not isinstance(function.get("name"), str):
                    continue
                output.append({
                    "id": f"fc_{uuid4().hex}",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": str(call.get("id") or f"call_{uuid4().hex}"),
                    "name": function["name"],
                    "arguments": function.get("arguments", "{}"),
                })
        if not output:
            output.append({
                "id": f"msg_{uuid4().hex}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "", "annotations": []}],
            })
        usage = chat_response.get("usage")
        responses_usage = None
        if isinstance(usage, Mapping):
            input_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
            output_tokens = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
            responses_usage = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": int(usage.get("total_tokens", input_tokens + output_tokens) or 0),
            }
        metadata = request_payload.get("metadata")
        finish_reason = choices[0].get("finish_reason") if isinstance(choices[0], Mapping) else None
        response_status = "incomplete" if finish_reason == "length" else "completed"
        response = {
            "id": response_id,
            "object": "response",
            "created_at": created,
            "status": response_status,
            "error": None,
            "incomplete_details": {"reason": "max_output_tokens"} if response_status == "incomplete" else None,
            "instructions": request_payload.get("instructions"),
            "model": model,
            "output": output,
            "parallel_tool_calls": bool(request_payload.get("parallel_tool_calls", True)),
            "previous_response_id": None,
            "temperature": request_payload.get("temperature"),
            "tool_choice": request_payload.get("tool_choice", "auto"),
            "tools": request_payload.get("tools", []),
            "top_p": request_payload.get("top_p"),
            "max_output_tokens": request_payload.get("max_output_tokens"),
            "metadata": dict(metadata) if isinstance(metadata, Mapping) else {},
            "usage": responses_usage,
            "store": False,
        }
        return response


def _event(name: str, payload: Mapping[str, Any]) -> bytes:
    content = dict(payload)
    content.setdefault("type", name)
    return f"event: {name}\ndata: {json.dumps(content, separators=(',', ':'))}\n\n".encode("utf-8")


async def map_chat_stream(
    source: AsyncIterable[bytes],
    *,
    adapter: OpenAIResponsesAdapter,
    request_payload: Mapping[str, Any],
    response_id: str,
) -> AsyncIterator[bytes]:
    model = request_payload.get("model", "local-fast")
    created = datetime.now(timezone.utc).timestamp()
    skeleton = {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": "in_progress",
        "model": model,
        "output": [],
        "metadata": request_payload.get("metadata", {}),
    }
    yield _event("response.created", {"response": skeleton})
    yield _event("response.in_progress", {"response": skeleton})

    message_id = f"msg_{uuid4().hex}"
    message_item = {"id": message_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}
    yield _event("response.output_item.added", {"output_index": 0, "item": message_item})
    content_part = {"type": "output_text", "text": "", "annotations": []}
    yield _event("response.content_part.added", {"item_id": message_id, "output_index": 0, "content_index": 0, "part": content_part})

    buffer = bytearray()
    text_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    usage = None
    finish_reason = None
    terminal = False
    try:
        async for chunk in source:
            buffer.extend(chunk)
            normalized = bytes(buffer).replace(b"\r\n", b"\n")
            buffer = bytearray(normalized)
            while b"\n\n" in buffer:
                raw_event, _, remaining = buffer.partition(b"\n\n")
                buffer = bytearray(remaining)
                data_lines = [line[5:].strip() for line in raw_event.split(b"\n") if line.startswith(b"data:")]
                if not data_lines:
                    continue
                data = b"\n".join(data_lines)
                if data == b"[DONE]":
                    terminal = True
                    break
                try:
                    chunk_data = json.loads(data)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(chunk_data, Mapping):
                    continue
                if isinstance(chunk_data.get("usage"), Mapping):
                    usage = dict(chunk_data["usage"])
                choices = chunk_data.get("choices")
                if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
                    continue
                choice = choices[0]
                finish_reason = choice.get("finish_reason") or finish_reason
                delta = choice.get("delta", {})
                if not isinstance(delta, Mapping):
                    continue
                text = delta.get("content")
                if isinstance(text, str) and text:
                    text_parts.append(text)
                    yield _event("response.output_text.delta", {
                        "item_id": message_id,
                        "output_index": 0,
                        "content_index": 0,
                        "delta": text,
                    })
                calls = delta.get("tool_calls")
                if isinstance(calls, list):
                    for call in calls:
                        if not isinstance(call, Mapping):
                            continue
                        index = call.get("index", 0)
                        if not isinstance(index, int):
                            continue
                        state = tool_calls.setdefault(index, {
                            "id": None,
                            "name": "",
                            "arguments": "",
                            "item_id": f"fc_{uuid4().hex}",
                            "added": False,
                            "emitted_arguments": 0,
                        })
                        state["id"] = call.get("id") or state["id"]
                        function = call.get("function", {})
                        argument_piece = ""
                        if isinstance(function, Mapping):
                            if isinstance(function.get("name"), str):
                                state["name"] += function["name"]
                            if isinstance(function.get("arguments"), str):
                                argument_piece = function["arguments"]
                                state["arguments"] += argument_piece
                        if not state["added"] and state["id"]:
                            state["added"] = True
                            yield _event("response.output_item.added", {
                                "output_index": index + 1,
                                "item": {
                                    "id": state["item_id"],
                                    "type": "function_call",
                                    "status": "in_progress",
                                    "call_id": state["id"] or f"call_{uuid4().hex}",
                                    "name": state["name"],
                                    "arguments": "",
                                },
                            })
                            if state["arguments"]:
                                state["emitted_arguments"] = len(state["arguments"])
                                yield _event("response.function_call_arguments.delta", {
                                    "item_id": state["item_id"],
                                    "output_index": index + 1,
                                    "delta": state["arguments"],
                                })
                        elif state["added"] and argument_piece:
                            state["emitted_arguments"] += len(argument_piece)
                            yield _event("response.function_call_arguments.delta", {
                                "item_id": state["item_id"],
                                "output_index": index + 1,
                                "delta": argument_piece,
                            })
            if terminal:
                break
    finally:
        closer = getattr(source, "aclose", None)
        if closer is not None:
            try:
                await closer()
            except Exception:
                pass
    if buffer and not terminal:
        # Process a final unterminated SSE event using the same event shape.
        data_lines = [line[5:].strip() for line in bytes(buffer).split(b"\n") if line.startswith(b"data:")]
        if data_lines:
            try:
                chunk_data = json.loads(b"\n".join(data_lines))
                choices = chunk_data.get("choices", [])
                if choices and isinstance(choices[0], Mapping):
                    delta = choices[0].get("delta", {})
                    text = delta.get("content") if isinstance(delta, Mapping) else None
                    if isinstance(text, str) and text:
                        text_parts.append(text)
                        yield _event("response.output_text.delta", {"item_id": message_id, "output_index": 0, "content_index": 0, "delta": text})
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                pass

    text = "".join(text_parts)
    yield _event("response.output_text.done", {"item_id": message_id, "output_index": 0, "content_index": 0, "text": text})
    final_message_item = {
        "id": message_id,
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    yield _event("response.content_part.done", {"item_id": message_id, "output_index": 0, "content_index": 0, "part": final_message_item["content"][0]})
    yield _event("response.output_item.done", {"output_index": 0, "item": final_message_item})

    chat_tool_calls = []
    for index, call in sorted(tool_calls.items()):
        call_id = call["id"] or f"call_{uuid4().hex}"
        if not call["added"]:
            call["added"] = True
            yield _event("response.output_item.added", {
                "output_index": index + 1,
                "item": {
                    "id": call["item_id"],
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": call_id,
                    "name": call["name"],
                    "arguments": "",
                },
            })
        if call["emitted_arguments"] < len(call["arguments"]):
            remaining_arguments = call["arguments"][call["emitted_arguments"]:]
            yield _event("response.function_call_arguments.delta", {
                "item_id": call["item_id"],
                "output_index": index + 1,
                "delta": remaining_arguments,
            })
        yield _event("response.function_call_arguments.done", {
            "item_id": call["item_id"],
            "output_index": index + 1,
            "arguments": call["arguments"],
        })
        yield _event("response.output_item.done", {
            "output_index": index + 1,
            "item": {
                "id": call["item_id"],
                "type": "function_call",
                "status": "completed",
                "call_id": call_id,
                "name": call["name"],
                "arguments": call["arguments"],
            },
        })
        chat_tool_calls.append({"id": call_id, "type": "function", "function": {"name": call["name"], "arguments": call["arguments"]}})
    chat_response: dict[str, Any] = {
        "id": response_id,
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": text, "tool_calls": chat_tool_calls}, "finish_reason": finish_reason}],
    }
    if usage is not None:
        chat_response["usage"] = usage
    final_response = adapter.from_chat_response(chat_response, request_payload, response_id=response_id)
    final_response["created_at"] = created
    if final_response["output"] and final_response["output"][0].get("type") == "message":
        final_response["output"][0]["id"] = message_id
    output_position = 1 if final_response["output"] and final_response["output"][0].get("type") == "message" else 0
    for _, call in sorted(tool_calls.items()):
        if output_position < len(final_response["output"]):
            final_response["output"][output_position]["id"] = call["item_id"]
        output_position += 1
    terminal_event = "response.incomplete" if final_response["status"] == "incomplete" else "response.completed"
    yield _event(terminal_event, {"response": final_response})
