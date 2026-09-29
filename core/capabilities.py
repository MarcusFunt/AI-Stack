from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class Capability(str, Enum):
    CHAT = "chat"
    REASONING = "reasoning"
    STREAMING_TEXT = "streaming_text"
    TRANSCRIPTION = "transcription"
    STREAMING_TRANSCRIPTION = "streaming_transcription"
    SPEECH = "speech"
    STREAMING_SPEECH = "streaming_speech"
    VISION = "vision"
    AUDIO_INPUT = "audio_input"
    TOOL_CALLING = "tool_calling"
    EMBEDDING = "embedding"
    IMAGE_GENERATION = "image_generation"
    VIDEO_GENERATION = "video_generation"


def capability_name(capability: Capability | str) -> str:
    return capability.value if isinstance(capability, Capability) else str(capability)


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    name: str
    constraints: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _ProviderRecord:
    capabilities: frozenset[str]
    constraints: Mapping[str, Mapping[str, Any]]


class CapabilityRegistry:
    """Small deterministic registry for provider capability declarations."""

    def __init__(self) -> None:
        self._providers: dict[str, _ProviderRecord] = {}

    def register(self, provider_id: str, capabilities: set[Capability | str] | frozenset[Capability | str], *, constraints: Mapping[Capability | str, Mapping[str, Any]] | None = None) -> None:
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("provider_id must be a non-empty string")
        if provider_id in self._providers:
            raise ValueError(f"provider is already registered: {provider_id}")
        names = frozenset(capability_name(item) for item in capabilities)
        normalized_constraints = {capability_name(key): dict(value) for key, value in (constraints or {}).items()}
        unknown = normalized_constraints.keys() - names
        if unknown:
            raise ValueError(f"constraints declared for unsupported capabilities: {sorted(unknown)}")
        self._providers[provider_id] = _ProviderRecord(names, normalized_constraints)

    def providers_for(self, capability: Capability | str) -> tuple[str, ...]:
        name = capability_name(capability)
        return tuple(provider_id for provider_id, record in self._providers.items() if name in record.capabilities)

    def constraints_for(self, provider_id: str, capability: Capability | str) -> dict[str, Any]:
        record = self._providers.get(provider_id)
        if record is None:
            raise KeyError(provider_id)
        name = capability_name(capability)
        if name not in record.capabilities:
            raise KeyError(f"{provider_id} does not declare {name}")
        return dict(record.constraints.get(name, {}))

    def capabilities_for(self, provider_id: str) -> frozenset[str]:
        record = self._providers.get(provider_id)
        if record is None:
            raise KeyError(provider_id)
        return record.capabilities
