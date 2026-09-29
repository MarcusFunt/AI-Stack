from __future__ import annotations

import asyncio
import threading
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from fastapi.testclient import TestClient

with patch.dict("os.environ", {"AI_API_KEY": "module-test-key"}):
    from eval_router.app import create_app
from eval_router.deepeval_worker import DeepEvalWorker
from eval_router.opik import OpikAdapter
from eval_router.screening import evaluate_invocation
from eval_router.schemas import InvocationEvaluationEvent
from eval_router.store import EvaluationStore


def event(**changes):
    values = {
        "event_id": str(uuid4()),
        "invocation_id": str(uuid4()),
        "trace_id": "a" * 32,
        "event_type": "invocation.completed",
        "source": "openai_chat",
        "operation": "generate",
        "model_id": "local-fast",
        "provider_id": "llm",
        "http_status": 200,
        "duration_ms": 250,
        "response_bytes": 128,
        "stream": False,
    }
    values.update(changes)
    return InvocationEvaluationEvent(**values)


class ScreeningTests(unittest.TestCase):
    def test_successful_invocation_gets_a_pass_result_without_content(self):
        request = event()

        result, reasons = evaluate_invocation(request, latency_threshold_ms=1000)

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.score, 1.0)
        self.assertEqual(reasons, ())
        self.assertEqual(result.evidence["http_status"], 200)
        self.assertNotIn("prompt", result.evidence)
        self.assertNotIn("output", result.evidence)

    def test_server_error_and_slow_response_get_escalation_reasons(self):
        request = event(event_type="invocation.failed", http_status=502, duration_ms=5000)

        result, reasons = evaluate_invocation(request, latency_threshold_ms=1200)

        self.assertEqual(result.status, "fail")
        self.assertEqual(set(reasons), {"http_server_error", "abnormal_latency"})

    def test_client_error_is_screened_without_deep_escalation(self):
        result, reasons = evaluate_invocation(
            event(event_type="invocation.failed", http_status=400),
            latency_threshold_ms=1000,
        )

        self.assertEqual(result.status, "warn")
        self.assertEqual(reasons, ())


class PersistenceTests(unittest.TestCase):
    def test_screening_and_suspicious_escalation_are_persisted_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EvaluationStore(Path(tmp) / "eval.sqlite3")
            request = event(http_status=500, event_type="invocation.failed")
            result, reasons = evaluate_invocation(request, latency_threshold_ms=1000)

            saved, escalation = store.save_screening(request.event_id, result, reasons)
            repeated, repeated_escalation = store.save_screening(request.event_id, result, reasons)

            self.assertEqual(saved.id, repeated.id)
            self.assertIsNotNone(escalation)
            self.assertEqual(escalation.id, repeated_escalation.id)
            self.assertEqual(escalation.status, "queued")
            self.assertEqual(escalation.reasons, ["http_server_error"])

    def test_deep_evaluation_worker_claims_and_completes_a_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EvaluationStore(Path(tmp) / "eval.sqlite3")
            request = event(http_status=500, event_type="invocation.failed")
            result, reasons = evaluate_invocation(request, latency_threshold_ms=1000)
            _, escalation = store.save_screening(request.event_id, result, reasons)
            seen = []

            async def evaluator(record):
                seen.append(record.id)
                return {"evaluator": "test", "score": 0.8, "status": "pass", "explanation": "bounded test"}

            worker = DeepEvalWorker(store, evaluator)
            claimed = asyncio.run(worker.run_once("worker-1"))
            completed = store.get_escalation(escalation.id)

            self.assertTrue(claimed)
            self.assertEqual(seen, [escalation.id])
            self.assertEqual(completed.status, "completed")
            self.assertEqual(completed.result["score"], 0.8)

    def test_hung_evaluator_is_timed_out_and_claim_is_not_left_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EvaluationStore(Path(tmp) / "eval.sqlite3")
            request = event(http_status=500, event_type="invocation.failed")
            result, reasons = evaluate_invocation(request, latency_threshold_ms=1000)
            _, escalation = store.save_screening(request.event_id, result, reasons)

            async def evaluator(_record):
                await asyncio.sleep(0.1)
                return {"evaluator": "test", "score": 1.0, "status": "pass"}

            worker = DeepEvalWorker(store, evaluator, timeout_seconds=0.01)
            self.assertTrue(asyncio.run(worker.run_once("worker-timeout")))
            completed = store.get_escalation(escalation.id)
            self.assertEqual(completed.status, "error")
            self.assertEqual(completed.result["error_code"], "timeout")

    def test_synchronous_evaluator_runs_off_the_event_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EvaluationStore(Path(tmp) / "eval.sqlite3")
            request = event(http_status=500, event_type="invocation.failed")
            result, reasons = evaluate_invocation(request, latency_threshold_ms=1000)
            _, escalation = store.save_screening(request.event_id, result, reasons)
            callback_thread = []

            def evaluator(_record):
                callback_thread.append(threading.get_ident())
                return {"evaluator": "sync-test", "score": 1.0, "status": "pass"}

            worker = DeepEvalWorker(store, evaluator)
            main_thread = threading.get_ident()
            self.assertTrue(asyncio.run(worker.run_once("worker-sync")))

            self.assertNotEqual(callback_thread[0], main_thread)
            self.assertEqual(store.get_escalation(escalation.id).status, "completed")

    def test_expired_claim_is_returned_to_the_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EvaluationStore(Path(tmp) / "eval.sqlite3")
            request = event(http_status=500, event_type="invocation.failed")
            result, reasons = evaluate_invocation(request, latency_threshold_ms=1000)
            _, escalation = store.save_screening(request.event_id, result, reasons)
            store.claim_next_escalation("worker-gone")
            with store._connection() as connection:
                connection.execute(
                    "UPDATE escalations SET updated_at=? WHERE id=?",
                    ("2000-01-01T00:00:00+00:00", escalation.id),
                )

            claimed = store.claim_next_escalation("worker-recovery", claim_ttl_seconds=1)

            self.assertEqual(claimed.id, escalation.id)
            self.assertEqual(claimed.status, "claimed")
            self.assertEqual(claimed.claimed_by, "worker-recovery")


class OpikAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_sends_only_screen_metadata_and_is_disabled_by_default(self):
        class FakeClient:
            def __init__(self):
                self.traces = []
                self.flushes = []

            def trace(self, **kwargs):
                self.traces.append(kwargs)

            def flush(self, **kwargs):
                self.flushes.append(kwargs)
                return True

        request = event()
        result, _ = evaluate_invocation(request, latency_threshold_ms=1000)
        client = FakeClient()
        adapter = OpikAdapter(enabled=True, client_factory=lambda: client)

        await adapter.submit(result)

        self.assertEqual(len(client.traces), 1)
        logged = client.traces[0]
        self.assertEqual(logged["name"], "ai-stack.invocation.screen")
        self.assertEqual(logged["metadata"]["ai_stack.trace_id"], request.trace_id)
        self.assertEqual(logged["feedback_scores"][0]["value"], 1.0)
        self.assertNotIn("input", logged)
        self.assertNotIn("output", logged)
        self.assertEqual(client.flushes, [{"timeout": 1}])

    async def test_opik_errors_fail_open(self):
        request = event()
        result, _ = evaluate_invocation(request, latency_threshold_ms=1000)

        adapter = OpikAdapter(enabled=True, client_factory=lambda: (_ for _ in ()).throw(RuntimeError("offline")))
        self.assertFalse(await adapter.submit(result))


class ServiceTests(unittest.TestCase):
    def test_screening_endpoint_authenticates_and_persists_every_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                store=EvaluationStore(Path(tmp) / "service.sqlite3"),
                api_key="internal-test-key",
                opik_adapter=OpikAdapter(enabled=False),
            )
            request = event()
            client = TestClient(app)

            denied = client.post("/v1/screenings", json=request.model_dump(mode="json"))
            accepted = client.post(
                "/v1/screenings",
                headers={"Authorization": "Bearer internal-test-key"},
                json=request.model_dump(mode="json"),
            )

            self.assertEqual(denied.status_code, 401)
            self.assertEqual(accepted.status_code, 201)
            self.assertEqual(accepted.json()["screening"]["status"], "pass")
            self.assertIsNone(accepted.json()["escalation"])
            self.assertEqual(len(app.state.store.list_screenings()), 1)

    def test_suspicious_invocation_creates_a_queued_escalation(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                store=EvaluationStore(Path(tmp) / "service.sqlite3"),
                api_key="internal-test-key",
                opik_adapter=OpikAdapter(enabled=False),
            )
            request = event(event_type="invocation.failed", http_status=500)
            response = TestClient(app).post(
                "/v1/screenings",
                headers={"Authorization": "Bearer internal-test-key"},
                json=request.model_dump(mode="json"),
            )

            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json()["escalation"]["status"], "queued")
            self.assertIn("http_server_error", response.json()["escalation"]["reasons"])

    def test_worker_claim_and_completion_routes_preserve_queue_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                store=EvaluationStore(Path(tmp) / "service.sqlite3"),
                api_key="internal-test-key",
                opik_adapter=OpikAdapter(enabled=False),
            )
            client = TestClient(app)
            headers = {"Authorization": "Bearer internal-test-key"}
            request = event(event_type="invocation.failed", http_status=500)
            submitted = client.post("/v1/screenings", headers=headers, json=request.model_dump(mode="json"))
            escalation_id = submitted.json()["escalation"]["id"]

            claim = client.post("/v1/escalations/claim", headers=headers, json={"worker_id": "worker-1"})
            complete = client.post(
                f"/v1/escalations/{escalation_id}/complete",
                headers=headers,
                json={
                    "worker_id": "worker-1",
                    "result": {
                        "evaluator": "deepeval-hook",
                        "score": 0.75,
                        "status": "warn",
                        "explanation": "hook result",
                    },
                },
            )

            self.assertEqual(claim.status_code, 200)
            self.assertEqual(claim.json()["status"], "claimed")
            self.assertEqual(complete.status_code, 200)
            self.assertEqual(complete.json()["status"], "completed")

            denied_completion = client.post(
                f"/v1/escalations/{escalation_id}/complete",
                headers=headers,
                json={
                    "worker_id": "worker-2",
                    "result": {"evaluator": "deepeval-hook", "score": 0.5, "status": "warn"},
                },
            )
            self.assertEqual(denied_completion.status_code, 409)


if __name__ == "__main__":
    unittest.main()
