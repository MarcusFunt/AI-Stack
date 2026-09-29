from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Canonical tool declaration; the broker owns execution and permissions."""
    name: str
    description: str = ""
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    required_scopes: frozenset[str] = field(default_factory=frozenset)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    provider: str = "internal"
    permissions: frozenset[str] = field(default_factory=frozenset)
    timeout_ms: int = 30_000

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("tool name must be a non-empty string")
        if not isinstance(self.description, str):
            raise ValueError("tool description must be a string")
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("tool provider must be a non-empty string")
        if isinstance(self.timeout_ms, bool) or not isinstance(self.timeout_ms, int) or self.timeout_ms <= 0:
            raise ValueError("tool timeout_ms must be a positive integer")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "provider", self.provider.strip())
        object.__setattr__(self, "input_schema", deepcopy(dict(self.input_schema)))
        object.__setattr__(self, "required_scopes", frozenset(self.required_scopes))
        object.__setattr__(self, "permissions", frozenset(self.permissions))
        object.__setattr__(self, "metadata", deepcopy(dict(self.metadata)))
        if any(not isinstance(scope, str) or not scope.strip() for scope in self.required_scopes | self.permissions):
            raise ValueError("tool permissions must be non-empty strings")

    @property
    def all_permissions(self) -> frozenset[str]:
        return self.required_scopes | self.permissions
