from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class InvocationEvaluationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    invocation_id: str
    trace_id: str
    event_type: Literal["invocation.completed", "invocation.failed"]
    source: str = Field(min_length=1, max_length=80)
    operation: str = Field(min_length=1, max_length=80)
    model_id: str | None = Field(default=None, max_length=160)
    provider_id: str | None = Field(default=None, max_length=80)
    http_status: int = Field(ge=100, le=599)
    duration_ms: float = Field(ge=0, allow_inf_nan=False)
    response_bytes: int | None = Field(default=None, ge=0)
    stream: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("event_id", "invocation_id")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        try:
            return str(UUID(value))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("event and invocation ids must be UUIDs") from exc

    @field_validator("trace_id")
    @classmethod
    def valid_trace_id(cls, value: str) -> str:
        if len(value) != 32 or any(character not in "0123456789abcdefABCDEF" for character in value):
            raise ValueError("trace_id must be a 32-character hexadecimal identifier")
        normalized = value.lower()
        if normalized == "0" * 32:
            raise ValueError("trace_id must not be all zeroes")
        return normalized


class VoiceTurnEvaluationEvent(BaseModel):
    """Privacy-safe completion metadata for the realtime voice path."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    turn_id: str
    trace_id: str
    event_type: Literal[
        "voice.turn.completed",
        "voice.turn.interrupted",
        "voice.turn.failed",
        "voice.turn.empty",
    ]
    duration_ms: float = Field(ge=0, allow_inf_nan=False)
    time_to_first_transcript_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    time_to_first_token_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    time_to_first_audio_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    latency_baseline_ms: VoiceLatencyBaseline | None = None
    audio_input_bytes: int = Field(ge=0)
    audio_output_bytes: int = Field(ge=0)
    interrupted: bool = False
    truncation_recorded: bool = False
    audio_integrity_ok: bool = True
    stt_provider: str = Field(min_length=1, max_length=80)
    llm_provider: str = Field(min_length=1, max_length=80)
    tts_provider: str = Field(min_length=1, max_length=80)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("event_id", "session_id", "turn_id")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        try:
            return str(UUID(value))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("event, session, and turn ids must be UUIDs") from exc

    @field_validator("trace_id")
    @classmethod
    def valid_trace_id(cls, value: str) -> str:
        if len(value) != 32 or any(character not in "0123456789abcdefABCDEF" for character in value):
            raise ValueError("trace_id must be a 32-character hexadecimal identifier")
        normalized = value.lower()
        if normalized == "0" * 32:
            raise ValueError("trace_id must not be all zeroes")
        return normalized


class LatencyPercentileSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_count: int = Field(ge=1)
    p50: float = Field(ge=0, allow_inf_nan=False)
    p95: float = Field(ge=0, allow_inf_nan=False)


class VoiceLatencyBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_count: int = Field(ge=0)
    time_to_first_transcript_ms: LatencyPercentileSummary | None = None
    time_to_first_token_ms: LatencyPercentileSummary | None = None
    time_to_first_audio_ms: LatencyPercentileSummary | None = None


class EvaluationScreenResult(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    invocation_id: str
    trace_id: str
    evaluator: str = "deterministic"
    metric: str = "invocation_health"
    score: float = Field(ge=0, le=1, allow_inf_nan=False)
    status: Literal["pass", "warn", "fail", "error"]
    explanation: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EscalationRecord(BaseModel):
    id: str
    screening_id: str
    invocation_id: str
    trace_id: str
    reasons: list[str]
    status: Literal["queued", "claimed", "completed", "error"]
    created_at: datetime
    claimed_by: str | None = None
    result: dict[str, Any] | None = None


class ScreeningSubmission(BaseModel):
    screening: EvaluationScreenResult
    escalation: EscalationRecord | None = None


class EscalationClaimRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class DeepEvaluationResult(BaseModel):
    evaluator: str = Field(min_length=1, max_length=80)
    score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    status: Literal["pass", "warn", "fail", "error"]
    explanation: str | None = Field(default=None, max_length=2048)


class DeepEvaluationSubmission(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    result: DeepEvaluationResult

