from __future__ import annotations

import unittest

from voice.metrics import VoiceMetrics


class VoiceMetricsTests(unittest.TestCase):
    def test_latency_baseline_reports_deterministic_p50_and_p95(self) -> None:
        metrics = VoiceMetrics()
        metrics.observe_latency("time_to_first_audio_ms", 100)
        metrics.observe_latency("time_to_first_audio_ms", 200)
        metrics.observe_latency("time_to_first_audio_ms", 300)

        baseline = metrics.latency_baseline()

        self.assertEqual(baseline["time_to_first_audio_ms"]["sample_count"], 3)
        self.assertEqual(baseline["time_to_first_audio_ms"]["p50"], 200)
        self.assertEqual(baseline["time_to_first_audio_ms"]["p95"], 290)

    def test_first_audio_histogram_uses_bounded_counters(self) -> None:
        metrics = VoiceMetrics()
        for _ in range(2_000):
            metrics.observe_first_audio(0.2)
        metrics.observe_first_audio(0.7)
        metrics.observe_first_audio(12.0)

        rendered = metrics.render()

        self.assertIn('ai_stack_voice_end_to_first_audio_seconds_bucket{le="0.25"} 2000', rendered)
        self.assertIn('ai_stack_voice_end_to_first_audio_seconds_bucket{le="0.5"} 2000', rendered)
        self.assertIn('ai_stack_voice_end_to_first_audio_seconds_bucket{le="1"} 2001', rendered)
        self.assertIn('ai_stack_voice_end_to_first_audio_seconds_bucket{le="+Inf"} 2002', rendered)
        self.assertIn("ai_stack_voice_end_to_first_audio_seconds_count 2002", rendered)
        self.assertFalse(hasattr(metrics, "first_audio_seconds"))


if __name__ == "__main__":
    unittest.main()
