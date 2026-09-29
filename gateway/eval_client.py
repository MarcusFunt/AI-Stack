from __future__ import annotations

import logging
import os
import time
from uuid import uuid4

import httpx
from starlette.background import BackgroundTasks

from core.invocation import Invocation
from core.router import ModelRoute

_LOGGER = logging.getLogger(__name__)
EVAL_ROUTER_URL = os.getenv("EVAL_ROUTER_URL", "").rstrip("/")
EVAL_ROUTER_TIMEOUT_SECONDS = 1.0


def build_invocation_event(
    invocation: Invocation,
    model_route: ModelRoute | None,
    *,
    http_status: int,
    duration_ms: float,
    response_bytes: int | None,
) -> dict:
    success = 200 <= http_status < 300
    return {
        "event_id": str(uuid4()),
        "invocation_id": invocation.id,
        "trace_id": invocation.trace_context.trace_id,
        "event_type": "invocation.completed" if success else "invocation.failed",
        "source": invocation.source.value,
        "operation": invocation.operation.value,
        "model_id": model_route.model_id if model_route is not None else invocation.requested_model,
        "provider_id": model_route.provider_id if model_route is not None else None,
        "http_status": http_status,
        "duration_ms": max(float(duration_ms), 0.0),
        "response_bytes": response_bytes,
        "stream": invocation.options.stream,
    }


async def post_screen_event(event: dict) -> bool:
    if not EVAL_ROUTER_URL:
        return False
    api_key = os.getenv("AI_API_KEY", "").strip()
    if not api_key:
        return False
    try:
        async with httpx.AsyncClient(timeout=EVAL_ROUTER_TIMEOUT_SECONDS) as client:
            response = await client.post(
                EVAL_ROUTER_URL + "/v1/screenings",
                headers={"Authorization": f"Bearer {api_key}"},
                json=event,
            )
        if response.status_code >= 400:
            _LOGGER.warning(
                "evaluation service rejected screening event",
                extra={"http_status": response.status_code},
            )
            return False
        return True
    except Exception as exc:
        _LOGGER.warning("evaluation service screening submission failed", extra={"error_type": type(exc).__name__})
        return False


async def report_invocation_screen(
    invocation: Invocation,
    model_route: ModelRoute | None,
    *,
    http_status: int,
    started_at: float,
    response_bytes: int | None,
) -> bool:
    event = build_invocation_event(
        invocation,
        model_route,
        http_status=http_status,
        duration_ms=(time.perf_counter() - started_at) * 1000,
        response_bytes=response_bytes,
    )
    return await post_screen_event(event)


def attach_screening_task(
    response,
    invocation: Invocation | None,
    model_route: ModelRoute | None,
    *,
    started_at: float,
) -> None:
    if invocation is None:
        return
    existing = response.background
    tasks = BackgroundTasks()
    if existing is not None:
        tasks.add_task(existing)

    async def safe_report() -> None:
        try:
            raw_bytes = response.headers.get("content-length")
            response_bytes = int(raw_bytes) if raw_bytes is not None and raw_bytes.isdigit() else None
            await report_invocation_screen(
                invocation,
                model_route,
                http_status=response.status_code,
                started_at=started_at,
                response_bytes=response_bytes,
            )
        except Exception as exc:
            _LOGGER.warning("evaluation callback failed", extra={"error_type": type(exc).__name__})

    tasks.add_task(safe_report)
    response.background = tasks
