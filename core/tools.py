from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Transport-neutral tool declaration; execution lives in the Tool Broker."""
    name: str
    description: str = ""
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    required_scopes: frozenset[str] = field(default_factory=frozenset)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("tool name must be a non-empty string")
        object.__setattr__(self, "input_schema", dict(self.input_schema))
        object.__setattr__(self, "required_scopes", frozenset(self.required_scopes))
        object.__setattr__(self, "metadata", dict(self.metadata))
