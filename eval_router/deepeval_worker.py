from __future__ import annotations

import inspect
import logging
from typing import Any, Protocol

from .schemas import DeepEvaluationResult
from .store import EvaluationStore

_LOGGER = logging.getLogger(__name__)


class DeepEvaluator(Protocol):
    def __call__(self, escalation: dict[str, Any]) -> Any: ...


class DeepEvalWorker:
    """Claim durable escalation records and hand them to an injected evaluator."""

    def __init__(self, store: EvaluationStore, evaluator: DeepEvaluator):
        if not callable(evaluator):
            raise TypeError("evaluator must be callable")
        self.store = store
        self.evaluator = evaluator

    async def run_once(self, worker_id: str) -> bool:
        escalation = self.store.claim_next_escalation(worker_id)
        if escalation is None:
            return False
        try:
            result = self.evaluator(escalation)
            if inspect.isawaitable(result):
                result = await result
            self.store.complete_escalation(escalation.id, worker_id, DeepEvaluationResult.model_validate(result))
        except Exception as exc:
            _LOGGER.warning(
                "deep evaluation worker failed",
                extra={"escalation_id": escalation.id, "error_type": type(exc).__name__},
            )
            self.store.fail_escalation(escalation.id, worker_id, type(exc).__name__)
        return True
