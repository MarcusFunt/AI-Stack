"""Typed event envelopes for the next realtime voice protocol revision.

The current WebSocket remains on ``ai-stack.voice.v1``. These v2 models define
the versioned contract that transports and clients can adopt independently.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


RealtimeEventType = Literal[
    "session.created",
    "session.updated",
    "session.reconnecting",
    "session.closed",
    "input_audio.started",
    "input_audio.level",
    "input_audio.ended",
    "user.speech_started",
    "user.speech_stopped",
    "user.transcript.delta",
    "user.transcript.final",
    "turn.created",
    "turn.committed",
    "response.created",
    "response.text.delta",
    "response.audio.delta",
    "response.interrupted",
    "response.completed",
    "tool.call.started",
    "tool.call.completed",
    "tool.call.failed",
    "task.accepted",
    "task.progress",
    "task.completed",
    "task.failed",
    "task.cancelled",
    "conversation.summary.updated",
    "error",
]

RealtimeCommandType = Literal[
    "session.update",
    "input_audio.enable",
    "input_audio.disable",
    "input_audio.commit",
    "response.request",
    "response.cancel",
    "task.status",
    "task.modify",
    "task.cancel",
    "conversation.message.create",
    "conversation.context.add",
    "voice.profile.set",
    "session.close",
]

_RESPONSE_EVENTS = {
    "response.created",
    "response.text.delta",
    "response.audio.delta",
    "response.interrupted",
    "response.completed",
}


class RealtimeServerEvent(BaseModel):
    """A v2 server event with explicit identity for potentially stale output."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["v2"] = "v2"
    type: RealtimeEventType
    session_id: str = Field(min_length=1)
    turn_id: str | None = Field(default=None, min_length=1)
    response_id: str | None = Field(default=None, min_length=1)
    generation_id: int | None = Field(default=None, ge=1)
    task_id: str | None = Field(default=None, min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_response_identity(self) -> RealtimeServerEvent:
        if self.type in _RESPONSE_EVENTS and (
            self.turn_id is None or self.response_id is None or self.generation_id is None
        ):
            raise ValueError("response events require turn_id, response_id, and generation_id")
        return self


class RealtimeClientCommand(BaseModel):
    """A v2 client command envelope; cancellation names the generation it targets."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["v2"] = "v2"
    type: RealtimeCommandType
    session_id: str = Field(min_length=1)
    turn_id: str | None = Field(default=None, min_length=1)
    response_id: str | None = Field(default=None, min_length=1)
    generation_id: int | None = Field(default=None, ge=1)
    task_id: str | None = Field(default=None, min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_cancel_identity(self) -> RealtimeClientCommand:
        if self.type == "response.cancel" and (
            self.response_id is None or self.generation_id is None
        ):
            raise ValueError("response.cancel requires response_id and generation_id")
        if self.type.startswith("task.") and self.task_id is None:
            raise ValueError("task commands require task_id")
        return self
