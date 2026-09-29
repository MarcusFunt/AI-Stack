from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Protocol

from .schemas import DeepEvaluationResult
from .store import EvaluationStore

_LOGGER = logging.getLogger(__name__)


def _consume_task_result(task: asyncio.Task) -> None:
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


class DeepEvaluator(Protocol):
    def __call__(self, escalation: dict[str, Any]) -> Any: ...


class DeepEvalWorker:
    """Claim durable escalation records and hand them to an injected evaluator."""

    def __init__(
        self,
        store: EvaluationStore,
        evaluator: DeepEvaluator,
        *,
        timeout_seconds: float = 30.0,
    ):
        if not callable(evaluator):
            raise TypeError("evaluator must be callable")
        if not 0.01 <= timeout_seconds <= 3600:
            raise ValueError("timeout_seconds must be between 0.01 and 3600")
        self.store = store
        self.evaluator = evaluator
        self.timeout_seconds = timeout_seconds

    async def run_once(self, worker_id: str) -> bool:
        escalation = self.store.claim_next_escalation(worker_id)
        if escalation is None:
            return False
        evaluation_task = None
        try:
            async def evaluate():
                if inspect.iscoroutinefunction(self.evaluator):
                    return await self.evaluator(escalation)
                result = await asyncio.to_thread(self.evaluator, escalation)
                if inspect.isawaitable(result):
                    return await result
                return result

            evaluation_task = asyncio.create_task(evaluate())
            done, _ = await asyncio.wait({evaluation_task}, timeout=self.timeout_seconds)
            if not done:
                evaluation_task.cancel()
                evaluation_task.add_done_callback(_consume_task_result)
                raise asyncio.TimeoutError
            result = evaluation_task.result()
            self.store.complete_escalation(escalation.id, worker_id, DeepEvaluationResult.model_validate(result))
        except asyncio.CancelledError:
            if evaluation_task is not None and not evaluation_task.done():
                evaluation_task.cancel()
                evaluation_task.add_done_callback(_consume_task_result)
            try:
                self.store.release_escalation(escalation.id, worker_id)
            except ValueError:
                pass
            raise
        except Exception as exc:
            _LOGGER.warning(
                "deep evaluation worker failed",
                extra={"escalation_id": escalation.id, "error_type": type(exc).__name__},
            )
            try:
                error_code = "timeout" if isinstance(exc, asyncio.TimeoutError) else type(exc).__name__
                self.store.fail_escalation(escalation.id, worker_id, error_code)
            except ValueError:
                # Another worker may have reclaimed a claim whose lease expired.
                pass
        return True
