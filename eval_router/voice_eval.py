from __future__ import annotations

from .schemas import EvaluationScreenResult, VoiceTurnEvaluationEvent


def evaluate_voice_turn(
    event: VoiceTurnEvaluationEvent,
    *,
    latency_threshold_ms: float = 5_000,
) -> tuple[EvaluationScreenResult, tuple[str, ...]]:
    """Screen objective voice delivery and timing without retaining user content."""
    failures: list[str] = []
    warnings: list[str] = []
    escalation_reasons: list[str] = []

    if event.event_type == "voice.turn.failed":
        failures.append("voice_turn_failed")
        escalation_reasons.append("voice_turn_failed")
    if event.event_type == "voice.turn.empty":
        warnings.append("empty_transcription")
    if event.audio_input_bytes <= 0:
        failures.append("missing_voice_input_audio")
        escalation_reasons.append("missing_voice_input_audio")
    if event.event_type == "voice.turn.completed" and event.audio_output_bytes <= 0:
        failures.append("missing_voice_output_audio")
        escalation_reasons.append("missing_voice_output_audio")
    if not event.audio_integrity_ok:
        failures.append("voice_audio_integrity_failure")
        escalation_reasons.append("voice_audio_integrity_failure")
    if event.interrupted and not event.truncation_recorded:
        failures.append("barge_in_truncation_missing")
        escalation_reasons.append("barge_in_truncation_missing")

    if event.duration_ms > latency_threshold_ms:
        warnings.append("voice_turn_latency")
        escalation_reasons.append("voice_turn_latency")
    if event.time_to_first_audio_ms is not None and event.time_to_first_audio_ms > latency_threshold_ms:
        warnings.append("voice_first_audio_latency")
        escalation_reasons.append("voice_first_audio_latency")

    if failures:
        status = "fail"
        score = 0.0
    elif warnings:
        status = "warn"
        score = 0.5
    else:
        status = "pass"
        score = 1.0

    result = EvaluationScreenResult(
        invocation_id=event.turn_id,
        trace_id=event.trace_id,
        evaluator="voice-deterministic",
        metric="voice_turn_health",
        score=score,
        status=status,
        explanation=",".join(failures + warnings) or None,
        evidence={
            "event_type": event.event_type,
            "duration_ms": round(event.duration_ms, 2),
            "time_to_first_transcript_ms": event.time_to_first_transcript_ms,
            "time_to_first_token_ms": event.time_to_first_token_ms,
            "time_to_first_audio_ms": event.time_to_first_audio_ms,
            "audio_input_bytes": event.audio_input_bytes,
            "audio_output_bytes": event.audio_output_bytes,
            "interrupted": event.interrupted,
            "truncation_recorded": event.truncation_recorded,
            "stt_provider": event.stt_provider,
            "llm_provider": event.llm_provider,
            "tts_provider": event.tts_provider,
            "checks": {
                "audio_integrity_ok": event.audio_integrity_ok,
                "has_input_audio": event.audio_input_bytes > 0,
                "has_completed_output_audio": (
                    event.event_type != "voice.turn.completed" or event.audio_output_bytes > 0
                ),
                "barge_in_truncated": not event.interrupted or event.truncation_recorded,
            },
        },
        created_at=event.created_at,
    )
    return result, tuple(dict.fromkeys(escalation_reasons))
