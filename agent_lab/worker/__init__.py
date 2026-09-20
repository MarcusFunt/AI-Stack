from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .graph import AgentRunner as AgentRunner

__all__ = ["AgentRunner"]


def __getattr__(name: str) -> Any:
    if name == "AgentRunner":
        from .graph import AgentRunner
        return AgentRunner
    raise AttributeError(name)
