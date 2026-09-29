from __future__ import annotations

from enum import Enum
from typing import AsyncIterator, Protocol, runtime_checkable

from .capabilities import Capability
from .events import InvocationEvent
from .invocation import Invocation


class ProviderHealthState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class ProviderHealth:
    __slots__ = ("status", "details")

    def __init__(self, status: ProviderHealthState | str, details: dict | None = None) -> None:
        self.status = status if isinstance(status, ProviderHealthState) else ProviderHealthState(status)
        self.details = dict(details or {})

    @property
    def ready(self) -> bool:
        return self.status is ProviderHealthState.HEALTHY


@runtime_checkable
class Provider(Protocol):
    async def health(self) -> ProviderHealth: ...

    async def capabilities(self) -> frozenset[Capability]: ...

    def invoke(self, invocation: Invocation) -> AsyncIterator[InvocationEvent]: ...

    async def cancel(self, invocation_id: str) -> None: ...
