from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping
from uuid import UUID

from .context import TraceContext
from .events import InvocationEvent
from .invocation import BinaryReference, Invocation, InvocationInput, InvocationOperation, InvocationOptions, InvocationSource, ModelPolicy, Modality, Principal
from .tools import ToolDefinition


class SerializationError(ValueError):
    pass


def _to_json_value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise SerializationError("binary payloads must be stored separately and referenced")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _to_json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise SerializationError("JSON object keys must be strings")
        return {key: _to_json_value(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return [_to_json_value(item) for item in sorted(value, key=str)]
    if isinstance(value, (tuple, list)):
        return [_to_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise SerializationError(f"unsupported JSON value: {type(value).__name__}")


def _dumps(value: Any) -> str:
    try:
        return json.dumps(_to_json_value(value), separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        if isinstance(exc, SerializationError):
            raise
        raise SerializationError(str(exc)) from exc


def _datetime(value: str) -> datetime:
    if not isinstance(value, str):
        raise SerializationError("timestamp must be an ISO-8601 string")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SerializationError("timestamp must be a valid ISO-8601 string") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise SerializationError("timestamp must include a timezone")
    return result


def dumps_invocation(invocation: Invocation) -> str:
    if not isinstance(invocation, Invocation):
        raise TypeError("dumps_invocation requires an Invocation")
    return _dumps(invocation)


def loads_invocation(payload: str) -> Invocation:
    try:
        raw = json.loads(payload)
        trace = TraceContext(**raw["trace_context"])
        principal = Principal(**raw["principal"])
        policy = ModelPolicy(**raw["model_policy"])
        source_input = raw["input"]
        input_value = InvocationInput(
            text=source_input.get("text"),
            messages=tuple(source_input.get("messages", ())),
            attachments=tuple(BinaryReference(**item) for item in source_input.get("attachments", ())),
            data=source_input.get("data", {}),
        )
        options = InvocationOptions(**raw["options"])
        tools = tuple(ToolDefinition(**item) for item in raw.get("tools", ()))
        return Invocation(
            id=raw["id"],
            trace_context=trace,
            operation=InvocationOperation(raw["operation"]),
            modality={Modality(item) for item in raw["modality"]},
            session_id=raw.get("session_id"),
            parent_invocation_id=raw.get("parent_invocation_id"),
            source=InvocationSource(raw["source"]),
            principal=principal,
            requested_model=raw.get("requested_model"),
            model_policy=policy,
            input=input_value,
            tools=tools,
            options=options,
            metadata=raw.get("metadata", {}),
            created_at=_datetime(raw["created_at"]),
            deadline=None if raw.get("deadline") is None else _datetime(raw["deadline"]),
        )
    except SerializationError:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SerializationError(f"invalid invocation JSON: {exc}") from exc


def dumps_event(event: InvocationEvent) -> str:
    if not isinstance(event, InvocationEvent):
        raise TypeError("dumps_event requires an InvocationEvent")
    return _dumps(event)


def loads_event(payload: str) -> InvocationEvent:
    try:
        raw = json.loads(payload)
        raw["timestamp"] = _datetime(raw["timestamp"])
        return InvocationEvent(**raw)
    except SerializationError:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SerializationError(f"invalid invocation event JSON: {exc}") from exc
