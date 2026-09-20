from __future__ import annotations

from functools import lru_cache

from fastapi import FastAPI, HTTPException, Query, status

from . import __version__
from .controller import AgentLabController
from .schemas import EventRecord, RunCreate, RunRecord


app = FastAPI(title="AI Stack Agent Lab", version=__version__)


@lru_cache(maxsize=1)
def controller() -> AgentLabController:
    return AgentLabController()


def _not_found_or_bad_request(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="run not found")
    return HTTPException(status_code=409, detail=str(exc))


@app.get("/health")
def health() -> dict:
    return controller().health()


@app.get("/harnesses")
def harnesses() -> list[dict]:
    return controller().list_harnesses()


@app.post("/runs", response_model=RunRecord, status_code=status.HTTP_201_CREATED)
def create_run(request: RunCreate) -> RunRecord:
    try:
        return controller().create_run(request.task, request.auto_start)
    except (ValueError, RuntimeError) as exc:
        raise _not_found_or_bad_request(exc) from exc


@app.get("/runs", response_model=list[RunRecord])
def list_runs(limit: int = Query(default=100, ge=1, le=500)) -> list[RunRecord]:
    return controller().store.list_runs(limit)


@app.get("/runs/{run_id}", response_model=RunRecord)
def get_run(run_id: str) -> RunRecord:
    try:
        return controller().store.get_run(run_id)
    except KeyError as exc:
        raise _not_found_or_bad_request(exc) from exc


@app.get("/runs/{run_id}/events", response_model=list[EventRecord])
def get_events(run_id: str) -> list[EventRecord]:
    try:
        controller().store.get_run(run_id)
        return controller().store.list_events(run_id)
    except KeyError as exc:
        raise _not_found_or_bad_request(exc) from exc


@app.post("/runs/{run_id}/execute", response_model=RunRecord, status_code=202)
def execute_run(run_id: str) -> RunRecord:
    try:
        return controller().start_run(run_id)
    except (KeyError, ValueError) as exc:
        raise _not_found_or_bad_request(exc) from exc


@app.post("/runs/{run_id}/cancel", response_model=RunRecord)
def cancel_run(run_id: str) -> RunRecord:
    try:
        return controller().cancel_run(run_id)
    except (KeyError, ValueError) as exc:
        raise _not_found_or_bad_request(exc) from exc


@app.post("/runs/{run_id}/cleanup", response_model=RunRecord)
def cleanup_run(run_id: str) -> RunRecord:
    try:
        return controller().cleanup_workspace(run_id)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise _not_found_or_bad_request(exc) from exc
