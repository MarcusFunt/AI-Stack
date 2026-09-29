from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .opik import OpikAdapter
from .schemas import (
    DeepEvaluationResult,
    DeepEvaluationSubmission,
    EscalationRecord,
    EvaluationScreenResult,
    EscalationClaimRequest,
    InvocationEvaluationEvent,
    ScreeningSubmission,
    VoiceTurnEvaluationEvent,
)
from .screening import evaluate_invocation
from .store import EvaluationStore
from .voice_eval import evaluate_voice_turn


def create_app(
    *,
    store: EvaluationStore | None = None,
    api_key: str | None = None,
    opik_adapter: OpikAdapter | None = None,
    latency_threshold_ms: float | None = None,
) -> FastAPI:
    configured_key = (os.getenv("AI_API_KEY", "") if api_key is None else api_key).strip()
    if not configured_key:
        raise RuntimeError("AI_API_KEY must be set")
    data_path = Path(os.getenv("EVAL_ROUTER_DB", "/data/eval-router.sqlite3"))
    active_store = store or EvaluationStore(data_path)
    active_opik = opik_adapter or OpikAdapter()
    threshold = (
        float(os.getenv("EVAL_LATENCY_THRESHOLD_MS", "5000"))
        if latency_threshold_ms is None
        else latency_threshold_ms
    )
    if threshold <= 0:
        raise ValueError("latency_threshold_ms must be positive")

    app = FastAPI(title="AI-Stack Eval Router", version="0.1.0")
    app.state.store = active_store
    app.state.opik_adapter = active_opik
    app.state.latency_threshold_ms = threshold

    @app.middleware("http")
    async def internal_auth(request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {configured_key}"
        if not secrets.compare_digest(supplied, expected):
            return JSONResponse(
                {"detail": "invalid evaluation service credential"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "eval-router"}

    @app.post("/v1/screenings", response_model=ScreeningSubmission, status_code=201)
    async def screen(event: InvocationEvaluationEvent) -> ScreeningSubmission:
        result, reasons = evaluate_invocation(event, latency_threshold_ms=threshold)
        result, escalation = active_store.save_screening(event.event_id, result, reasons)
        await active_opik.submit(result)
        return ScreeningSubmission(screening=result, escalation=escalation)

    @app.post("/v1/voice-evaluations", response_model=ScreeningSubmission, status_code=201)
    async def screen_voice_turn(event: VoiceTurnEvaluationEvent) -> ScreeningSubmission:
        result, reasons = evaluate_voice_turn(event, latency_threshold_ms=threshold)
        result, escalation = active_store.save_screening(event.event_id, result, reasons)
        await active_opik.submit(result)
        return ScreeningSubmission(screening=result, escalation=escalation)

    @app.get("/v1/screenings", response_model=list[EvaluationScreenResult])
    def list_screenings(limit: int = Query(default=100, ge=1, le=500)) -> list[EvaluationScreenResult]:
        return active_store.list_screenings(limit)

    @app.get("/v1/screenings/{screening_id}")
    def get_screening(screening_id: str):
        try:
            return active_store.get_screening(screening_id)
        except KeyError as exc:
            raise HTTPException(404, "screening not found") from exc

    @app.get("/v1/escalations", response_model=list[EscalationRecord])
    def list_escalations(limit: int = Query(default=100, ge=1, le=500)) -> list[EscalationRecord]:
        return active_store.list_escalations(limit)

    @app.post("/v1/escalations/claim")
    def claim_escalation(request: EscalationClaimRequest):
        return active_store.claim_next_escalation(request.worker_id)

    @app.post("/v1/escalations/{escalation_id}/complete")
    def complete_escalation(escalation_id: str, request: DeepEvaluationSubmission):
        try:
            return active_store.complete_escalation(escalation_id, request.worker_id, request.result)
        except KeyError as exc:
            raise HTTPException(404, "escalation not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    return app


app = create_app()
