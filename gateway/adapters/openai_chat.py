from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.context import TraceContext
from core.invocation import (
    Invocation,
    InvocationInput,
    InvocationOperation,
    InvocationOptions,
    InvocationSource,
    ModelPolicy,
    Modality,
    Principal,
)
from core.tools import ToolDefinition


class OpenAIChatAdapter:
    """Translate a compatible chat request into the protocol-neutral core model."""

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return None
        return value

    @staticmethod
    def _number(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (float, int)):
            return None
        return float(value)

    def to_invocation(self, payload: Mapping[str, Any], trace_context: TraceContext | None = None) -> Invocation:
        if not isinstance(payload, Mapping):
            raise TypeError("chat payload must be a mapping")
        requested = str(payload.get("model", "local-fast")).strip()
        raw_messages = payload.get("messages", ())
        messages = tuple(dict(item) for item in raw_messages if isinstance(item, Mapping)) if isinstance(raw_messages, list) else ()
        tools = []
        raw_tools = payload.get("tools", ())
        if isinstance(raw_tools, list):
            for item in raw_tools:
                function = item.get("function") if isinstance(item, Mapping) else None
                if not isinstance(function, Mapping) or not isinstance(function.get("name"), str):
                    continue
                schema = function.get("parameters", {})
                tools.append(ToolDefinition(
                    name=function["name"],
                    description=function.get("description", "") if isinstance(function.get("description", ""), str) else "",
                    input_schema=schema if isinstance(schema, Mapping) else {},
                ))
        response_format = payload.get("response_format")
        response_format_name = response_format.get("type") if isinstance(response_format, Mapping) else None
        return Invocation(
            trace_context=trace_context or TraceContext(),
            operation=InvocationOperation.GENERATE,
            modality={Modality.TEXT},
            source=InvocationSource.OPENAI_CHAT,
            principal=Principal(id="gateway-client", kind="api_key", scopes=frozenset({"chat"})),
            requested_model=requested,
            model_policy=ModelPolicy(capability="chat"),
            input=InvocationInput(messages=messages),
            tools=tools,
            options=InvocationOptions(
                stream=bool(payload.get("stream", False)),
                temperature=self._number(payload.get("temperature")),
                top_p=self._number(payload.get("top_p")),
                max_output_tokens=self._positive_int(payload.get("max_completion_tokens", payload.get("max_tokens"))),
                response_format=response_format_name if isinstance(response_format_name, str) else None,
            ),
        )
