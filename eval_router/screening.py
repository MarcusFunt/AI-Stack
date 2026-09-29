from __future__ import annotations

from .schemas import EvaluationScreenResult, InvocationEvaluationEvent


def evaluate_invocation(
    event: InvocationEvaluationEvent,
    *,
    latency_threshold_ms: float = 5_000,
) -> tuple[EvaluationScreenResult, tuple[str, ...]]:
    """Run privacy-safe deterministic checks and select deeper-eval reasons."""
    failures: list[str] = []
    warnings: list[str] = []
    escalation_reasons: list[str] = []

    if event.http_status >= 500:
        failures.append("http_server_error")
        escalation_reasons.append("http_server_error")
    elif event.http_status >= 400:
        warnings.append("http_client_error")
    elif event.http_status >= 300:
        warnings.append("http_redirect")

    if event.response_bytes == 0 and 200 <= event.http_status < 300:
        failures.append("empty_response")
        escalation_reasons.append("empty_response")
    if event.http_status < 400 and (not event.model_id or not event.provider_id):
        failures.append("missing_model_provider_identity")
        escalation_reasons.append("missing_model_provider_identity")
    if event.duration_ms > latency_threshold_ms:
        warnings.append("abnormal_latency")
        escalation_reasons.append("abnormal_latency")

    if failures:
        status = "fail"
        score = 0.0
    elif warnings:
        status = "warn"
        score = 0.5
    else:
        status = "pass"
        score = 1.0

    checks = {
        "http_success": 200 <= event.http_status < 300,
        "response_nonempty": event.response_bytes is None or event.response_bytes > 0,
        "model_provider_recorded": bool(event.model_id and event.provider_id),
        "latency_within_threshold": event.duration_ms <= latency_threshold_ms,
        "trace_id_present": bool(event.trace_id),
    }
    explanation = ",".join(failures + warnings) or None
    result = EvaluationScreenResult(
        invocation_id=event.invocation_id,
        trace_id=event.trace_id,
        score=score,
        status=status,
        explanation=explanation,
        evidence={
            "http_status": event.http_status,
            "duration_ms": round(event.duration_ms, 2),
            "response_bytes": event.response_bytes,
            "stream": event.stream,
            "source": event.source,
            "operation": event.operation,
            "model_id": event.model_id,
            "provider_id": event.provider_id,
            "checks": checks,
        },
        created_at=event.created_at,
    )
    return result, tuple(dict.fromkeys(escalation_reasons))
