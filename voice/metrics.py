from __future__ import annotations

from collections import deque
from math import floor


class VoiceMetrics:
    """Minimal Prometheus exposition with fixed, bounded metric dimensions."""

    def __init__(self) -> None:
        self.turns = 0
        self.interruptions = 0
        self.active_sessions = 0
        self.first_audio_count = 0
        self.first_audio_sum = 0.0
        self.first_audio_buckets = {bound: 0 for bound in (0.25, 0.5, 1.0, 2.0, 5.0, 10.0)}
        self._latency_samples = {
            name: deque(maxlen=512)
            for name in (
                "time_to_first_transcript_ms",
                "time_to_first_token_ms",
                "time_to_first_audio_ms",
            )
        }
        self.webrtc_offer_counts = {outcome: 0 for outcome in ("started", "succeeded", "failed")}
        self.webrtc_offer_failures = {
            failure_class: 0
            for failure_class in ("unauthorized", "invalid_offer", "ticket_rejected", "peer_error", "other")
        }
        self.webrtc_peers_active = 0
        self.webrtc_disconnect_counts = {
            reason: 0 for reason in ("client", "failed", "closed", "timeout", "other")
        }
        self.webrtc_audio_frames = {direction: 0 for direction in ("input", "output", "other")}
        self.webrtc_audio_bytes = {direction: 0 for direction in ("input", "output", "other")}
        self.webrtc_conversion_seconds = {direction: 0.0 for direction in ("input", "output", "other")}
        self.webrtc_conversion_counts = {direction: 0 for direction in ("input", "output", "other")}
        self.webrtc_conversion_buckets = {
            direction: {bound: 0 for bound in (0.001, 0.005, 0.01, 0.025, 0.05, 0.1)}
            for direction in ("input", "output", "other")
        }
        self.webrtc_conversion_failures = {
            failure_class: 0
            for failure_class in ("invalid_frame", "unsupported_format", "internal", "other")
        }
        self.webrtc_dropped_frames = {direction: 0 for direction in ("input", "output", "other")}

    def record_webrtc_offer(self, outcome: str) -> None:
        if outcome not in self.webrtc_offer_counts:
            outcome = "failed"
        self.webrtc_offer_counts[outcome] += 1

    def record_webrtc_offer_failure(self, failure_class: str) -> None:
        failure_class = (
            failure_class if failure_class in self.webrtc_offer_failures else "other"
        )
        self.webrtc_offer_failures[failure_class] += 1

    def record_webrtc_peer_started(self) -> None:
        self.webrtc_peers_active += 1

    def record_webrtc_peer_disconnected(self, reason: str) -> None:
        reason = reason if reason in self.webrtc_disconnect_counts else "other"
        self.webrtc_peers_active = max(0, self.webrtc_peers_active - 1)
        self.webrtc_disconnect_counts[reason] += 1

    @staticmethod
    def _webrtc_direction(direction: str) -> str:
        return direction if direction in {"input", "output"} else "other"

    def record_webrtc_audio(self, direction: str, byte_count: int) -> None:
        direction = self._webrtc_direction(direction)
        self.webrtc_audio_frames[direction] += 1
        self.webrtc_audio_bytes[direction] += max(0, int(byte_count))

    def observe_webrtc_conversion(self, direction: str, seconds: float) -> None:
        direction = self._webrtc_direction(direction)
        value = max(0.0, float(seconds))
        self.webrtc_conversion_seconds[direction] += value
        self.webrtc_conversion_counts[direction] += 1
        for bound in self.webrtc_conversion_buckets[direction]:
            if value <= bound:
                self.webrtc_conversion_buckets[direction][bound] += 1

    def record_webrtc_conversion_failure(self, failure_class: str) -> None:
        failure_class = (
            failure_class
            if failure_class in self.webrtc_conversion_failures
            else "other"
        )
        self.webrtc_conversion_failures[failure_class] += 1

    def record_webrtc_frame_drop(self, direction: str) -> None:
        direction = self._webrtc_direction(direction)
        self.webrtc_dropped_frames[direction] += 1

    def observe_latency(self, name: str, milliseconds: float) -> None:
        if name not in self._latency_samples:
            raise ValueError(f"unsupported voice latency metric: {name}")
        value = float(milliseconds)
        if value < 0:
            raise ValueError("latency must not be negative")
        self._latency_samples[name].append(value)

    @staticmethod
    def _percentile(values: list[float], quantile: float) -> float:
        ordered = sorted(values)
        if len(ordered) == 1:
            return round(ordered[0], 2)
        position = (len(ordered) - 1) * quantile
        lower = floor(position)
        upper = min(lower + 1, len(ordered) - 1)
        value = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
        return round(value, 2)

    def latency_baseline(self) -> dict:
        summaries = {}
        for name, samples in self._latency_samples.items():
            values = list(samples)
            if values:
                summaries[name] = {
                    "sample_count": len(values),
                    "p50": self._percentile(values, 0.50),
                    "p95": self._percentile(values, 0.95),
                }
        sample_count = max(
            (len(samples) for samples in self._latency_samples.values()), default=0
        )
        return {"sample_count": sample_count, **summaries}

    def observe_first_audio(self, seconds: float) -> None:
        value = max(float(seconds), 0.0)
        self.first_audio_count += 1
        self.first_audio_sum += value
        for bound in self.first_audio_buckets:
            if value <= bound:
                self.first_audio_buckets[bound] += 1

    def render(self) -> str:
        lines = [
            "# HELP ai_stack_voice_turns_total Completed voice turns.",
            "# TYPE ai_stack_voice_turns_total counter",
            f"ai_stack_voice_turns_total {self.turns}",
            "# HELP ai_stack_voice_interruptions_total Voice responses interrupted by barge-in or cancellation.",
            "# TYPE ai_stack_voice_interruptions_total counter",
            f"ai_stack_voice_interruptions_total {self.interruptions}",
            "# HELP ai_stack_voice_active_sessions Current realtime sessions.",
            "# TYPE ai_stack_voice_active_sessions gauge",
            f"ai_stack_voice_active_sessions {self.active_sessions}",
            "# HELP ai_stack_voice_end_to_first_audio_seconds End of user speech to first assistant audio.",
            "# TYPE ai_stack_voice_end_to_first_audio_seconds histogram",
        ]
        for bound, count in self.first_audio_buckets.items():
            lines.append(f'ai_stack_voice_end_to_first_audio_seconds_bucket{{le="{bound:g}"}} {count}')
        lines.extend([
            f'ai_stack_voice_end_to_first_audio_seconds_bucket{{le="+Inf"}} {self.first_audio_count}',
            f"ai_stack_voice_end_to_first_audio_seconds_sum {self.first_audio_sum:.6f}",
            f"ai_stack_voice_end_to_first_audio_seconds_count {self.first_audio_count}",
        ])
        for outcome, count in self.webrtc_offer_counts.items():
            lines.append(f'ai_stack_voice_webrtc_offers_total{{outcome="{outcome}"}} {count}')
        for failure_class, count in self.webrtc_offer_failures.items():
            lines.append(
                f'ai_stack_voice_webrtc_offer_failures_total{{class="{failure_class}"}} {count}'
            )
        lines.extend([
            "# TYPE ai_stack_voice_webrtc_peers_active gauge",
            f"ai_stack_voice_webrtc_peers_active {self.webrtc_peers_active}",
        ])
        for reason, count in self.webrtc_disconnect_counts.items():
            lines.append(
                f'ai_stack_voice_webrtc_peer_disconnects_total{{reason="{reason}"}} {count}'
            )
        for direction in self.webrtc_audio_frames:
            lines.append(
                f'ai_stack_voice_webrtc_audio_frames_total{{direction="{direction}"}} '
                f"{self.webrtc_audio_frames[direction]}"
            )
            lines.append(
                f'ai_stack_voice_webrtc_audio_bytes_total{{direction="{direction}"}} '
                f"{self.webrtc_audio_bytes[direction]}"
            )
            lines.append(
                f'ai_stack_voice_webrtc_audio_conversion_seconds_sum{{direction="{direction}"}} '
                f"{self.webrtc_conversion_seconds[direction]:.6f}"
            )
            lines.append(
                f'ai_stack_voice_webrtc_audio_conversion_seconds_count{{direction="{direction}"}} '
                f"{self.webrtc_conversion_counts[direction]}"
            )
            for bound, count in self.webrtc_conversion_buckets[direction].items():
                lines.append(
                    f'ai_stack_voice_webrtc_audio_conversion_seconds_bucket'
                    f'{{direction="{direction}",le="{bound:g}"}} {count}'
                )
            lines.append(
                f'ai_stack_voice_webrtc_audio_conversion_seconds_bucket'
                f'{{direction="{direction}",le="+Inf"}} '
                f"{self.webrtc_conversion_counts[direction]}"
            )
            lines.append(
                f'ai_stack_voice_webrtc_frames_dropped_total{{direction="{direction}"}} '
                f"{self.webrtc_dropped_frames[direction]}"
            )
        for failure_class, count in self.webrtc_conversion_failures.items():
            lines.append(
                f'ai_stack_voice_webrtc_audio_conversion_failures_total'
                f'{{class="{failure_class}"}} {count}'
            )
        lines.append("")
        return "\n".join(lines)


METRICS = VoiceMetrics()
