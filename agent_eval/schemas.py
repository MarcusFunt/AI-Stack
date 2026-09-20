from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EvaluationStatus(str, Enum):
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    CANCELLING = "cancelling"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ERROR = "error"


class EvaluationCreate(BaseModel):
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    candidate_commit: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    base_commit: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    suite: str = "agent-lab-selfmod-v1"
    model: str = "local-fast"
    token_budget: int = Field(default=50_000, ge=1_000, le=500_000)
    wall_time_seconds: int = Field(default=900, ge=30, le=7200)


class EvaluationRecord(BaseModel):
    id: str
    run_id: str
    status: EvaluationStatus
    candidate_commit: str
    base_commit: str
    suite: str
    suite_hash: str
    model: str
    token_budget: int
    wall_time_seconds: int
    created_at: datetime
    updated_at: datetime
    result: dict[str, Any] | None = None
    error: str | None = None
    attestation_path: str | None = None
    attestation_signature: str | None = None


class Attestation(BaseModel):
    schema_version: int = 1
    evaluation_id: str
    run_id: str
    candidate_commit: str
    base_commit: str
    evaluator_version: str
    evaluator_source_hash: str
    suite: str
    suite_hash: str
    model: str
    status: str
    created_at: str
    reference_hash: str | None = None
    reference_evaluation_id: str | None = None
    comparison: dict[str, Any] = Field(default_factory=dict)
    case_results: list[dict[str, Any]]
    critical_checks: dict[str, bool]
    model_usage: dict[str, Any]
    duration_s: float
