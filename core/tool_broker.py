from __future__ import annotations

import asyncio
from copy import deepcopy
import inspect
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, runtime_checkable
from uuid import UUID, uuid4

from .cancellation import CancellationToken, InvocationCancelled
from .context import TraceContext
from .invocation import Principal
from .tools import ToolDefinition

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    invocation_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    trace_context: TraceContext = field(default_factory=TraceContext)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("tool call id must be a non-empty string")
        try:
            object.__setattr__(self, "invocation_id", str(UUID(str(self.invocation_id))))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("invocation_id must be a valid UUID") from exc
        if not isinstance(self.tool_name, str) or not self.tool_name.strip():
            raise ValueError("tool_name must be a non-empty string")
        if not isinstance(self.arguments, Mapping):
            raise ValueError("tool arguments must be an object")
        if not isinstance(self.trace_context, TraceContext):
            raise TypeError("trace_context must be a TraceContext")
        object.__setattr__(self, "arguments", deepcopy(dict(self.arguments)))


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    success: bool
    content: Any = None
    error: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id.strip():
            raise ValueError("call_id must be a non-empty string")
        if self.started_at.tzinfo is None or self.finished_at.tzinfo is None:
            raise ValueError("tool result timestamps must be timezone-aware")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        if self.success and self.error is not None:
            raise ValueError("successful tool results cannot contain an error")


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    principal: Principal
    trace_context: TraceContext
    cancellation: CancellationToken


class ToolAuthorizationError(PermissionError):
    pass


class ToolInputError(ValueError):
    pass


class ToolOutputTooLarge(ValueError):
    pass


class ToolNotFoundError(KeyError):
    pass


class ToolProviderError(RuntimeError):
    pass


@runtime_checkable
class ToolProvider(Protocol):
    async def execute(self, call: ToolCall, context: ToolExecutionContext) -> Any: ...


class ToolRegistry:
    """Deterministic registry for canonical tool definitions."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if not isinstance(definition, ToolDefinition):
            raise TypeError("definition must be a ToolDefinition")
        if definition.name in self._tools:
            raise ValueError(f"tool is already registered: {definition.name}")
        self._tools[definition.name] = definition

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(name) from exc

    def list(self, principal: Principal | None = None) -> tuple[ToolDefinition, ...]:
        tools = tuple(self._tools.values())
        if principal is None:
            return tools
        scopes = frozenset(principal.scopes)
        return tuple(tool for tool in tools if tool.all_permissions <= scopes)


class FunctionToolProvider:
    """Run explicitly registered in-process tool callables."""

    def __init__(self, functions: Mapping[str, Any] | None = None) -> None:
        self.functions = dict(functions or {})

    def register(self, name: str, function) -> None:
        if not isinstance(name, str) or not name.strip() or not callable(function):
            raise ValueError("internal tools require a name and callable")
        if name in self.functions:
            raise ValueError(f"internal tool is already registered: {name}")
        self.functions[name] = function

    async def execute(self, call: ToolCall, context: ToolExecutionContext) -> Any:
        function = self.functions.get(call.tool_name)
        if function is None:
            raise ToolProviderError("internal tool implementation is unavailable")
        if inspect.iscoroutinefunction(function):
            return await function(dict(call.arguments))
        result = await asyncio.to_thread(function, dict(call.arguments))
        if inspect.isawaitable(result):
            return await result
        return result

    async def cancel(self, call_id: str) -> None:
        return None


class MQTTToolProvider:
    """Publish to fixed, explicitly registered MQTT topics only."""

    def __init__(self, client, topics: Mapping[str, str], *, publish_timeout: float = 5.0) -> None:
        if client is None:
            raise ValueError("an MQTT client is required")
        if not isinstance(publish_timeout, (int, float)) or isinstance(publish_timeout, bool) or publish_timeout <= 0 or not math.isfinite(publish_timeout):
            raise ValueError("publish_timeout must be a positive finite number")
        normalized = {}
        for name, topic in topics.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(topic, str) or not topic.strip():
                raise ValueError("MQTT tools require non-empty names and topics")
            if "+" in topic or "#" in topic:
                raise ValueError("MQTT publish topics cannot contain wildcards")
            normalized[name] = topic
        if not normalized:
            raise ValueError("at least one MQTT tool topic must be registered")
        self.client = client
        self.topics = normalized
        self.publish_timeout = publish_timeout

    async def execute(self, call: ToolCall, context: ToolExecutionContext) -> Any:
        topic = self.topics.get(call.tool_name)
        if topic is None:
            raise ToolProviderError("MQTT tool topic is not registered")
        payload = call.arguments.get("payload", dict(call.arguments))
        if isinstance(payload, bytes):
            raise ToolInputError("binary MQTT tool payloads are not supported")
        serialized = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        info = self.client.publish(topic, serialized, qos=0, retain=False)
        if getattr(info, "rc", 0) != 0:
            raise ToolProviderError("MQTT publish was rejected")
        waiter = getattr(info, "wait_for_publish", None)
        if callable(waiter):
            await asyncio.wait_for(asyncio.to_thread(waiter, self.publish_timeout), timeout=self.publish_timeout + 0.25)
        published = getattr(info, "is_published", None)
        if callable(published) and not published():
            raise ToolProviderError("MQTT publish did not complete")
        return {"topic": topic, "message_id": getattr(info, "mid", None)}

    async def cancel(self, call_id: str) -> None:
        return None


def validate_tool_arguments(value: Any, schema: Mapping[str, Any], *, path: str = "$") -> None:
    if not isinstance(schema, Mapping):
        raise ToolInputError("tool input schema must be an object")
    expected = schema.get("type")
    valid_types = {
        "object": lambda item: isinstance(item, Mapping),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    if expected is not None and expected not in valid_types:
        raise ToolInputError(f"{path} uses an unsupported schema type")
    if expected in valid_types and not valid_types[expected](value):
        raise ToolInputError(f"{path} must be {expected}")
    if expected == "number" and not math.isfinite(value):
        raise ToolInputError(f"{path} must be finite")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolInputError(f"{path} is not an allowed value")
    if expected in (None, "object") and isinstance(value, Mapping):
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping) or not isinstance(required, list):
            raise ToolInputError(f"{path} schema is invalid")
        missing = [key for key in required if key not in value]
        if missing:
            raise ToolInputError(f"{path} is missing required fields")
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ToolInputError(f"{path} contains unsupported fields")
        for key, item in value.items():
            if key in properties:
                validate_tool_arguments(item, properties[key], path=f"{path}.{key}")
    elif expected == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            raise ToolInputError(f"{path} has an invalid number of items")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                validate_tool_arguments(item, item_schema, path=f"{path}[{index}]")
    elif expected == "string":
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
            raise ToolInputError(f"{path} has an invalid length")
    elif expected in {"integer", "number"}:
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolInputError(f"{path} is below its minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolInputError(f"{path} exceeds its maximum")


class ToolBroker:
    """Single permission, validation, timeout, trace and output-size boundary."""

    def __init__(self, *, max_output_bytes: int = 64 * 1024) -> None:
        if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be a positive integer")
        self.max_output_bytes = max_output_bytes
        self.registry = ToolRegistry()
        self._providers: dict[str, ToolProvider] = {}

    def register_provider(self, provider_id: str, provider: ToolProvider) -> None:
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("provider_id must be a non-empty string")
        provider_id = provider_id.strip()
        if provider_id in self._providers:
            raise ValueError(f"tool provider is already registered: {provider_id}")
        if not callable(getattr(provider, "execute", None)):
            raise TypeError("tool provider must implement execute")
        self._providers[provider_id] = provider

    def register_tool(self, definition: ToolDefinition) -> None:
        if not isinstance(definition, ToolDefinition):
            raise TypeError("definition must be a ToolDefinition")
        if definition.provider not in self._providers:
            raise ValueError(f"tool provider is not registered: {definition.provider}")
        self.registry.register(definition)

    def list_tools(self, principal: Principal | None = None) -> tuple[ToolDefinition, ...]:
        return self.registry.list(principal)

    async def _notify_cancel(self, provider: ToolProvider, call_id: str) -> None:
        cancel = getattr(provider, "cancel", None)
        if callable(cancel):
            try:
                result = cancel(call_id)
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, timeout=1.0)
            except Exception:
                pass

    async def _run_provider(
        self,
        provider: ToolProvider,
        call: ToolCall,
        context: ToolExecutionContext,
        timeout: float,
    ) -> Any:
        if context.cancellation is not None:
            context.cancellation.raise_if_cancelled()
        task = asyncio.create_task(provider.execute(call, context))
        cancel_task = None
        callback = None
        if context.cancellation is not None:
            event = asyncio.Event()
            loop = asyncio.get_running_loop()
            callback = lambda _reason: loop.call_soon_threadsafe(event.set)
            context.cancellation.add_callback(callback)
            cancel_task = asyncio.create_task(event.wait())
        try:
            waiters = {task}
            if cancel_task is not None:
                waiters.add(cancel_task)
            done, _ = await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                if cancel_task is not None:
                    cancel_task.cancel()
                return task.result()
            if cancel_task is not None and cancel_task in done:
                await self._notify_cancel(provider, call.id)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise InvocationCancelled(context.cancellation.reason or "tool call cancelled")
            await self._notify_cancel(provider, call.id)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise asyncio.TimeoutError
        except asyncio.CancelledError:
            await self._notify_cancel(provider, call.id)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            if cancel_task is not None:
                if not cancel_task.done():
                    cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)
            if callback is not None and context.cancellation is not None:
                context.cancellation.remove_callback(callback)

    async def execute(
        self,
        call: ToolCall,
        *,
        principal: Principal,
        cancellation: CancellationToken | None = None,
    ) -> ToolResult:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        if not isinstance(call, ToolCall):
            raise TypeError("call must be a ToolCall")
        definition = self.registry.get(call.tool_name)
        missing = definition.all_permissions - principal.scopes
        if missing:
            raise ToolAuthorizationError("principal is not authorized for this tool")
        validate_tool_arguments(call.arguments, definition.input_schema)
        provider = self._providers.get(definition.provider)
        if provider is None:
            raise ToolProviderError("tool provider is unavailable")
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        token = cancellation or CancellationToken()
        context = ToolExecutionContext(principal=principal, trace_context=call.trace_context, cancellation=token)
        try:
            try:
                from observability.tracing import current_trace_context, start_span
            except ImportError:
                start_span = None
                current_trace_context = None
            if start_span is None or current_trace_context is None:
                content = await self._run_provider(provider, call, context, definition.timeout_ms / 1000)
            else:
                with start_span(
                    f"tool.{definition.name}",
                    parent=call.trace_context,
                    attributes={
                        "openinference.span.kind": "TOOL",
                        "ai_stack.tool.name": definition.name,
                        "ai_stack.tool.provider": definition.provider,
                        "ai_stack.invocation_id": call.invocation_id,
                    },
                ) as span:
                    context = ToolExecutionContext(
                        principal=principal,
                        trace_context=current_trace_context(call.trace_context, span),
                        cancellation=token,
                    )
                    content = await self._run_provider(provider, call, context, definition.timeout_ms / 1000)
            encoded = json.dumps(content, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(encoded) > self.max_output_bytes:
                raise ToolOutputTooLarge
            finished_at = datetime.now(timezone.utc)
            _LOGGER.info("tool execution completed", extra={
                "tool_name": definition.name,
                "tool_provider": definition.provider,
                "invocation_id": call.invocation_id,
                "success": True,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            })
            return ToolResult(call_id=call.id, success=True, content=content, started_at=started_at, finished_at=finished_at)
        except InvocationCancelled:
            raise
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            error = "tool timed out"
        except ToolOutputTooLarge:
            error = "tool output exceeds size limit"
        except Exception as exc:
            _LOGGER.warning("tool execution failed", extra={
                "tool_name": definition.name,
                "tool_provider": definition.provider,
                "invocation_id": call.invocation_id,
                "error_type": type(exc).__name__,
                "success": False,
            })
            error = "tool execution failed"
        finished_at = datetime.now(timezone.utc)
        return ToolResult(call_id=call.id, success=False, error=error, started_at=started_at, finished_at=finished_at)
