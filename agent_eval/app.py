from __future__ import annotations

from functools import lru_cache

from fastapi import FastAPI, HTTPException, Query, status

from . import __version__
from .controller import EvaluatorController
from .schemas import EvaluationCreate, EvaluationRecord


app = FastAPI(title="AI Stack Agent Evaluator", version=__version__)


@lru_cache(maxsize=1)
def controller() -> EvaluatorController:
    return EvaluatorController()


def translate(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


@app.get("/health")
def health() -> dict:
    return controller().health()


@app.post(
    "/evaluations",
    response_model=EvaluationRecord,
    status_code=status.HTTP_201_CREATED,
)
def create_evaluation(request: EvaluationCreate) -> EvaluationRecord:
    try:
        return controller().create(request)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise translate(exc) from exc


@app.get("/evaluations", response_model=list[EvaluationRecord])
def list_evaluations(
    limit: int = Query(default=100, ge=1, le=500),
) -> list[EvaluationRecord]:
    return controller().store.list(limit)


@app.get("/evaluations/{evaluation_id}", response_model=EvaluationRecord)
def get_evaluation(evaluation_id: str) -> EvaluationRecord:
    try:
        return controller().store.get(evaluation_id)
    except KeyError as exc:
        raise translate(exc) from exc


@app.post("/evaluations/{evaluation_id}/cancel", response_model=EvaluationRecord)
def cancel_evaluation(evaluation_id: str) -> EvaluationRecord:
    try:
        return controller().cancel(evaluation_id)
    except (KeyError, ValueError) as exc:
        raise translate(exc) from exc


@app.get("/runs/{run_id}/evaluations", response_model=list[EvaluationRecord])
def run_evaluations(
    run_id: str,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[EvaluationRecord]:
    return controller().store.by_run(run_id, limit)


@app.get("/runs/{run_id}/latest-passing")
def latest_passing(run_id: str) -> dict:
    try:
        return controller().latest_passing_for_run(run_id)
    except KeyError as exc:
        raise translate(exc) from exc


@app.post("/evaluations/{evaluation_id}/set-reference")
def set_reference(evaluation_id: str) -> dict:
    try:
        return controller().set_reference(evaluation_id)
    except (KeyError, ValueError) as exc:
        raise translate(exc) from exc


@app.get("/references/{suite}")
def reference(suite: str) -> dict:
    try:
        return controller().get_reference(suite)
    except KeyError as exc:
        raise translate(exc) from exc


@app.get("/attestations/{evaluation_id}")
def attestation(evaluation_id: str) -> dict:
    try:
        return controller().verify_attestation(evaluation_id)
    except KeyError as exc:
        raise translate(exc) from exc
