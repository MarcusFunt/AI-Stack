from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Iterable, Mapping

from .capabilities import capability_name
from .invocation import Invocation, InvocationOperation
from .providers import Provider


class InvocationRoutingError(ValueError):
    """Base error for a request that cannot be sent to a configured provider."""


class ModelNotFoundError(InvocationRoutingError):
    pass


class UnsupportedCapabilityError(InvocationRoutingError):
    pass


class ProviderUnavailableError(InvocationRoutingError):
    pass


@dataclass(frozen=True, slots=True)
class ModelRoute:
    requested_model: str | None
    model_id: str
    provider_id: str
    capabilities: frozenset[str]


_OPERATION_CAPABILITIES = {
    InvocationOperation.GENERATE: "chat",
    InvocationOperation.TRANSCRIBE: "transcription",
    InvocationOperation.SYNTHESIZE: "speech",
    InvocationOperation.ANALYZE: "vision",
    InvocationOperation.EMBED: "embedding",
    InvocationOperation.IMAGE_GENERATE: "image_generation",
    InvocationOperation.VIDEO_GENERATE: "video_generation",
}


class InvocationRouter:
    """Deterministic model/capability resolver with optional provider dispatch."""

    def __init__(self, models: Iterable[Mapping], *, aliases: Mapping[str, str] | None = None) -> None:
        self._models: dict[str, dict] = {}
        for model in models:
            model_id = model.get("id")
            provider_id = model.get("service")
            if not isinstance(model_id, str) or not model_id.strip():
                raise ValueError("each model requires a non-empty id")
            if not isinstance(provider_id, str) or not provider_id.strip():
                raise ValueError(f"model {model_id!r} requires a provider service")
            key = model_id.strip().lower()
            if key in self._models:
                raise ValueError(f"duplicate model id: {model_id}")
            capabilities = model.get("capabilities", ())
            if not isinstance(capabilities, (list, tuple, set, frozenset)):
                raise ValueError(f"model {model_id!r} capabilities must be a sequence")
            self._models[key] = {
                "id": model_id.strip(),
                "service": provider_id.strip(),
                "capabilities": frozenset(capability_name(item).lower() for item in capabilities),
            }
        self._aliases = {str(key).strip().lower(): str(value).strip().lower() for key, value in (aliases or {}).items()}
        self._providers: dict[str, Provider] = {}

    def register_provider(self, provider_id: str, provider: Provider) -> None:
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("provider_id must be a non-empty string")
        key = provider_id.strip()
        if key in self._providers:
            raise ValueError(f"provider is already registered: {key}")
        self._providers[key] = provider

    def _canonical_model_id(self, requested: str) -> str:
        key = requested.strip().lower()
        visited: set[str] = set()
        while key in self._aliases:
            if key in visited:
                raise ValueError(f"model alias cycle at {key!r}")
            visited.add(key)
            key = self._aliases[key]
        return key

    def resolve(self, invocation: Invocation) -> ModelRoute:
        if not isinstance(invocation, Invocation):
            raise TypeError("resolve requires an Invocation")
        requested = invocation.model_policy.exact_model or invocation.requested_model
        capability = invocation.model_policy.capability or _OPERATION_CAPABILITIES.get(invocation.operation)
        if requested is None:
            name = capability_name(capability).lower() if capability else None
            model = next((item for item in self._models.values() if name is None or name in item["capabilities"]), None)
            if model is None:
                raise ModelNotFoundError(f"no configured model supports {name or 'the requested operation'}")
        else:
            canonical = self._canonical_model_id(requested)
            model = self._models.get(canonical)
            if model is None:
                raise ModelNotFoundError(f"unknown model: {requested!r}")
        required = capability_name(capability).lower() if capability else None
        if required and required not in model["capabilities"]:
            raise UnsupportedCapabilityError(f"model {requested or model['id']!r} does not support {required}")
        return ModelRoute(
            requested_model=requested,
            model_id=model["id"],
            provider_id=model["service"],
            capabilities=model["capabilities"],
        )

    async def invoke(self, invocation: Invocation) -> AsyncIterator:
        route = self.resolve(invocation)
        provider = self._providers.get(route.provider_id)
        if provider is None:
            raise ProviderUnavailableError(f"provider {route.provider_id!r} is not registered")
        async for event in provider.invoke(invocation):
            yield event

    async def cancel(self, invocation: Invocation) -> None:
        route = self.resolve(invocation)
        provider = self._providers.get(route.provider_id)
        if provider is None:
            raise ProviderUnavailableError(f"provider {route.provider_id!r} is not registered")
        await provider.cancel(invocation.id)
