from __future__ import annotations

class VoiceMetrics:
    """Minimal Prometheus exposition with fixed, bounded metric dimensions."""

    def __init__(self) -> None:
        self.turns = 0
        self.interruptions = 0
        self.active_sessions = 0
        self.first_audio_count = 0
        self.first_audio_sum = 0.0
        self.first_audio_buckets = {bound: 0 for bound in (0.25, 0.5, 1.0, 2.0, 5.0, 10.0)}

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
            "",
        ])
        return "\n".join(lines)


METRICS = VoiceMetrics()
