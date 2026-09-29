from __future__ import annotations

from typing import Any

from core.invocation import Invocation


def invocation_attributes(
    invocation: Invocation, *, provider: str | None = None, service: str | None = None
) -> dict[str, Any]:
    """Return low-cardinality invocation metadata without prompt or principal data."""
    attributes: dict[str, Any] = {
        "openinference.span.kind": "LLM",
        "gen_ai.operation.name": invocation.operation.value,
        "ai_stack.invocation_id": invocation.id,
        "ai_stack.transport": invocation.source.value,
    }
    if invocation.requested_model:
        attributes["gen_ai.request.model"] = invocation.requested_model
    if provider:
        attributes["gen_ai.provider.name"] = provider
    if service:
        attributes["ai_stack.service"] = service
    if invocation.session_id or invocation.trace_context.session_id:
        attributes["ai_stack.session_id"] = invocation.session_id or invocation.trace_context.session_id
    if invocation.model_policy.profile:
        attributes["ai_stack.profile"] = invocation.model_policy.profile
    if invocation.modality:
        attributes["ai_stack.modality"] = sorted(item.value for item in invocation.modality)
    return attributes
