from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

with patch.dict("os.environ", {"AI_API_KEY": "voice-eval-module-key"}):
    from eval_router.app import create_app
from eval_router.opik import OpikAdapter
from eval_router.schemas import VoiceTurnEvaluationEvent
from eval_router.store import EvaluationStore
from eval_router.voice_eval import evaluate_voice_turn


def voice_event(**changes):
    values = {
        "event_id": str(uuid4()),
        "session_id": str(uuid4()),
        "turn_id": str(uuid4()),
        "trace_id": "b" * 32,
        "event_type": "voice.turn.completed",
        "duration_ms": 1400,
        "time_to_first_transcript_ms": 460,
        "time_to_first_token_ms": 700,
        "time_to_first_audio_ms": 1100,
        "latency_baseline_ms": {
            "sample_count": 3,
            "time_to_first_transcript_ms": {"sample_count": 3, "p50": 460, "p95": 510},
            "time_to_first_token_ms": {"sample_count": 3, "p50": 700, "p95": 820},
            "time_to_first_audio_ms": {"sample_count": 3, "p50": 1100, "p95": 1300},
        },
        "audio_input_bytes": 48_000,
        "audio_output_bytes": 96_000,
        "interrupted": False,
        "truncation_recorded": False,
        "audio_integrity_ok": True,
        "stt_provider": "local-stt",
        "llm_provider": "local-fast",
        "tts_provider": "local-tts",
    }
    values.update(changes)
    return VoiceTurnEvaluationEvent(**values)


class VoiceEvaluationTests(unittest.TestCase):
    def test_successful_turn_is_screened_without_transcript_or_audio_content(self):
        event = voice_event()

        result, reasons = evaluate_voice_turn(event, latency_threshold_ms=5000)

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.metric, "voice_turn_health")
        self.assertEqual(reasons, ())
        self.assertTrue(result.evidence["checks"]["audio_integrity_ok"])
        self.assertEqual(result.evidence["latency_baseline_ms"]["time_to_first_audio_ms"]["p95"], 1300)
        self.assertNotIn("transcript", result.evidence)
        self.assertNotIn("audio", result.evidence)

    def test_missing_truncation_after_interruption_is_escalated(self):
        result, reasons = evaluate_voice_turn(
            voice_event(event_type="voice.turn.interrupted", interrupted=True, truncation_recorded=False),
            latency_threshold_ms=5000,
        )

        self.assertEqual(result.status, "fail")
        self.assertIn("barge_in_truncation_missing", reasons)

    def test_latency_warning_uses_measured_first_audio_time(self):
        result, reasons = evaluate_voice_turn(
            voice_event(time_to_first_audio_ms=6000),
            latency_threshold_ms=5000,
        )

        self.assertEqual(result.status, "warn")
        self.assertIn("voice_first_audio_latency", reasons)

    def test_authenticated_endpoint_persists_voice_screening_and_queues_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                store=EvaluationStore(Path(tmp) / "eval.sqlite3"),
                api_key="internal-voice-key",
                opik_adapter=OpikAdapter(enabled=False),
            )
            client = TestClient(app)
            failed = voice_event(
                event_type="voice.turn.interrupted",
                interrupted=True,
                truncation_recorded=False,
            )

            denied = client.post("/v1/voice-evaluations", json=failed.model_dump(mode="json"))
            accepted = client.post(
                "/v1/voice-evaluations",
                headers={"Authorization": "Bearer internal-voice-key"},
                json=failed.model_dump(mode="json"),
            )

            self.assertEqual(denied.status_code, 401)
            self.assertEqual(accepted.status_code, 201)
            self.assertEqual(accepted.json()["screening"]["evaluator"], "voice-deterministic")
            self.assertIn("barge_in_truncation_missing", accepted.json()["escalation"]["reasons"])
            self.assertEqual(len(app.state.store.list_screenings()), 1)

    def test_opik_receives_voice_metric_without_transcript_content(self):
        class FakeClient:
            def __init__(self):
                self.traces = []

            def trace(self, **kwargs):
                self.traces.append(kwargs)

        result, _ = evaluate_voice_turn(voice_event())
        fake = FakeClient()
        adapter = OpikAdapter(enabled=True, client_factory=lambda: fake)

        asyncio.run(adapter.submit(result))

        trace = fake.traces[0]
        self.assertEqual(trace["name"], "ai-stack.voice.screen")
        self.assertEqual(trace["feedback_scores"][0]["name"], "voice_turn_health")
        self.assertNotIn("transcript", trace["metadata"])
        self.assertNotIn("audio", trace["metadata"])


if __name__ == "__main__":
    unittest.main()
