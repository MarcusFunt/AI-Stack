from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RunStatus(str, Enum):
    PREPARING = "preparing"
    READY = "ready"
    RUNNING = "running"
    CANCELLING = "cancelling"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ERROR = "error"


class Budget(BaseModel):
    max_iterations: int = Field(default=8, ge=1, le=50)
    wall_time_minutes: int = Field(default=60, ge=1, le=1440)
    model_tokens: int = Field(default=100_000, ge=1)


class TaskSpec(BaseModel):
    repository: str = Field(default="ai-stack", pattern=r"^[a-zA-Z0-9_.-]+$")
    base_ref: str = "HEAD"
    task_type: str = Field(default="general", min_length=1, max_length=64)
    objective: str = Field(min_length=1, max_length=20_000)
    allowed_harnesses: list[str] = Field(default_factory=list)
    required_harnesses: list[str] = Field(default_factory=list)
    require_failing_baseline: bool = True
    allow_test_edits: bool = False
    budget: Budget = Field(default_factory=Budget)


class RunCreate(BaseModel):
    task: TaskSpec
    auto_start: bool = False


class HarnessCheck(BaseModel):
    id: str
    status: str
    duration_s: float = 0.0
    message: str = ""


class HarnessResult(BaseModel):
    harness_id: str
    harness_version: str
    status: str
    checks: list[HarnessCheck] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)


class RunRecord(BaseModel):
    id: str
    status: RunStatus
    task: TaskSpec
    created_at: datetime
    updated_at: datetime
    workspace: str | None = None
    base_commit: str | None = None
    selected_harness: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class EventRecord(BaseModel):
    id: int
    run_id: str
    ts: datetime
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ReviewCheck(BaseModel):
    id: str
    status: str
    message: str = ""


class PromotionReview(BaseModel):
    run_id: str
    eligible_for_manual_promotion: bool
    candidate_commit: str | None = None
    base_commit: str | None = None
    current_source_commit: str | None = None
    files_changed: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    checks: list[ReviewCheck] = Field(default_factory=list)
