from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import Any

from .schemas import EvaluationScreenResult

_LOGGER = logging.getLogger(__name__)


class OpikAdapter:
    """Optionally send privacy-safe screening feedback to Opik, fail-open."""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        project_name: str | None = None,
        host: str | None = None,
        api_key: str | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.enabled = (os.getenv("OPIK_ENABLED", "0").strip().lower() in {"1", "true", "yes"}) if enabled is None else enabled
        self.project_name = project_name or os.getenv("OPIK_PROJECT", "ai-stack")
        self.host = host or os.getenv("OPIK_URL")
        self.api_key = api_key if api_key is not None else os.getenv("OPIK_API_KEY")
        self.client_factory = client_factory
        self._client_instance = None

    def _client(self):
        if self._client_instance is None:
            if self.client_factory is not None:
                self._client_instance = self.client_factory()
            else:
                from opik import Opik

                options = {"project_name": self.project_name, "_show_misconfiguration_message": False}
                if self.host:
                    options["host"] = self.host
                if self.api_key:
                    options["api_key"] = self.api_key
                self._client_instance = Opik(**options)
        return self._client_instance

    def _submit_sync(self, result: EvaluationScreenResult) -> None:
        client = self._client()
        client.trace(
            name="ai-stack.invocation.screen",
            project_name=self.project_name,
            metadata={
                "ai_stack.screening_id": result.id,
                "ai_stack.invocation_id": result.invocation_id,
                "ai_stack.trace_id": result.trace_id,
                "ai_stack.evaluator": result.evaluator,
                "ai_stack.metric": result.metric,
                "ai_stack.status": result.status,
                "ai_stack.evidence": result.evidence,
            },
            feedback_scores=[{
                "name": result.metric,
                "value": result.score,
                "category_name": result.status,
                "reason": result.explanation,
            }],
        )
        flush = getattr(client, "flush", None)
        if callable(flush):
            flush(timeout=1)

    async def submit(self, result: EvaluationScreenResult) -> bool:
        if not self.enabled:
            return True
        try:
            await asyncio.to_thread(self._submit_sync, result)
            return True
        except Exception as exc:
            _LOGGER.warning(
                "Opik screening export failed",
                extra={"error_type": type(exc).__name__, "screening_id": result.id},
            )
            return False
