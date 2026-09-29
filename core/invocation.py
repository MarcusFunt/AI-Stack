from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping
from uuid import UUID, uuid4

from .context import TraceContext
from .tools import ToolDefinition


class InvocationOperation(str, Enum):
    GENERATE = "generate"
    TRANSCRIBE = "transcribe"
    SYNTHESIZE = "synthesize"
    ANALYZE = "analyze"
    TOOL = "tool"
    AGENT = "agent"
    EMBED = "embed"
    IMAGE_GENERATE = "image_generate"
    VIDEO_GENERATE = "video_generate"


class Modality(str, Enum):
    TEXT = "text"
    AUDIO = "audio"
    IMAGE = "image"
    VIDEO = "video"
    STRUCTURED = "structured"


class InvocationSource(str, Enum):
    OPENAI_CHAT = "openai_chat"
    OPENAI_RESPONSES = "openai_responses"
    OPENAI_AUDIO = "openai_audio"
    OPENAI_VISION = "openai_vision"
    OPENAI_REALTIME = "openai_realtime"
    MCP = "mcp"
    MQTT = "mqtt"
    INTERNAL = "internal"
    AGENT_LAB = "agent_lab"
    DASHBOARD = "dashboard"
    SCHEDULED = "scheduled"


def _uuid_string(value: str | UUID, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{label} must be a valid UUID") from exc


@dataclass(frozen=True, slots=True)
class Principal:
    id: str
    kind: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("principal id must be a non-empty string")
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("principal kind must be a non-empty string")
        object.__setattr__(self, "scopes", frozenset(self.scopes))
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True, slots=True)
class ModelPolicy:
    exact_model: str | None = None
    capability: str | None = None
    profile: str | None = None
    prefer_local: bool = True
    max_latency_ms: int | None = None
    allow_fallback: bool = True

    def __post_init__(self) -> None:
        for name in ("exact_model", "capability", "profile"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string or None")
        if self.max_latency_ms is not None and self.max_latency_ms <= 0:
            raise ValueError("max_latency_ms must be positive")


@dataclass(frozen=True, slots=True)
class BinaryReference:
    """A pointer to separately stored binary data, never the data itself."""
    uri: str
    mime_type: str
    size_bytes: int | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.uri.strip() or not self.mime_type.strip():
            raise ValueError("binary references require a URI and MIME type")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")
        if self.sha256 is not None and (len(self.sha256) != 64 or any(c not in "0123456789abcdefABCDEF" for c in self.sha256)):
            raise ValueError("sha256 must contain 64 hexadecimal characters")


@dataclass(frozen=True, slots=True)
class InvocationInput:
    text: str | None = None
    messages: tuple[Mapping[str, Any], ...] = ()
    attachments: tuple[BinaryReference, ...] = ()
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(dict(item) for item in self.messages))
        object.__setattr__(self, "attachments", tuple(self.attachments))
        object.__setattr__(self, "data", dict(self.data))


@dataclass(frozen=True, slots=True)
class InvocationOptions:
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    response_format: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("temperature", "top_p"):
            value = getattr(self, name)
            if value is not None and not 0 <= value <= 2:
                raise ValueError(f"{name} must be between 0 and 2")
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        object.__setattr__(self, "data", dict(self.data))


@dataclass(frozen=True, slots=True)
class Invocation:
    id: str = field(default_factory=lambda: str(uuid4()))
    trace_context: TraceContext = field(default_factory=TraceContext)
    operation: InvocationOperation = InvocationOperation.GENERATE
    modality: set[Modality] = field(default_factory=lambda: {Modality.TEXT})
    session_id: str | None = None
    parent_invocation_id: str | None = None
    source: InvocationSource = InvocationSource.INTERNAL
    principal: Principal = field(default_factory=lambda: Principal(id="internal", kind="service"))
    requested_model: str | None = None
    model_policy: ModelPolicy = field(default_factory=ModelPolicy)
    input: InvocationInput = field(default_factory=InvocationInput)
    tools: list[ToolDefinition] = field(default_factory=list)
    options: InvocationOptions = field(default_factory=InvocationOptions)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    deadline: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _uuid_string(self.id, "invocation id"))
        if self.parent_invocation_id is not None:
            object.__setattr__(self, "parent_invocation_id", _uuid_string(self.parent_invocation_id, "parent_invocation_id"))
        if self.session_id is not None and not isinstance(self.session_id, str):
            raise ValueError("session_id must be a string or None")
        if not isinstance(self.operation, InvocationOperation):
            object.__setattr__(self, "operation", InvocationOperation(self.operation))
        if not isinstance(self.source, InvocationSource):
            object.__setattr__(self, "source", InvocationSource(self.source))
        object.__setattr__(self, "modality", {item if isinstance(item, Modality) else Modality(item) for item in self.modality})
        if self.requested_model is not None and not self.requested_model.strip():
            raise ValueError("requested_model must be non-empty or None")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if self.deadline is not None and (self.deadline.tzinfo is None or self.deadline.utcoffset() is None):
            raise ValueError("deadline must be timezone-aware")
        object.__setattr__(self, "tools", list(self.tools))
        object.__setattr__(self, "metadata", dict(self.metadata))
