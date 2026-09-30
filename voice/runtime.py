from __future__ import annotations

import asyncio
import base64
import io
import time
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from observability.propagation import inject_trace_context
from observability.tracing import (
    current_trace_context,
    end_span_handle,
    start_span,
    start_span_handle,
)
from voice.metrics import VoiceMetrics
from voice.providers import RealtimeProvider
from voice.session import RealtimeSession, SentenceChunker, visible_assistant_text
from voice.turn_detection import TurnEvent

VOICE_SAMPLE_RATE = 16_000


@dataclass(frozen=True, slots=True)
class AudioOutput:
    pcm: bytes
    sample_rate: int
    channels: int
    text: str
    response_id: str
    turn_id: str
    generation_id: int


def _pcm_wav(audio: bytes, sample_rate: int = VOICE_SAMPLE_RATE) -> bytes:
    result = io.BytesIO()
    with wave.open(result, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(audio)
    return result.getvalue()


def _traceparent(context: Any) -> str:
    headers: dict[str, str] = {}
    inject_trace_context(headers, context)
    return headers["traceparent"]


class RealtimeRuntime:
    """Transport-independent voice turn orchestration and generation ownership."""

    def __init__(
        self,
        *,
        session: RealtimeSession,
        provider: RealtimeProvider,
        metrics: VoiceMetrics,
        send_event: Callable[[dict[str, Any]], Awaitable[None]],
        parent_trace_context: Any,
        send_audio: Callable[[AudioOutput], Awaitable[bool]] | None = None,
    ) -> None:
        self.session = session
        self.provider = provider
        self.metrics = metrics
        self.send_event = send_event
        self.parent_trace_context = parent_trace_context
        self.send_audio = send_audio
        self.response_task: asyncio.Task | None = None
        self.response_state: dict[str, Any] | None = None
        self.active_turn_stats: dict[str, Any] | None = None
        self.evaluation_tasks: set[asyncio.Task] = set()
        self._send_lock = asyncio.Lock()
        self._pending_audio_send_task: asyncio.Task | None = None

    async def _emit(
        self,
        event_type: str,
        *,
        guard_generation_id: int | None = None,
        guard_response_id: str | None = None,
        **data: Any,
    ) -> bool:
        event = {"type": event_type, "session_id": self.session.id, **data}
        async with self._send_lock:
            if guard_generation_id is not None and not self.session.generation_is_current(
                guard_generation_id, guard_response_id
            ):
                return False
            await self.send_event(event)
        return True

    def _trace_id(self) -> str:
        parts = (self.session.traceparent or "").split("-")
        if len(parts) == 4 and len(parts[1]) == 32 and any(ch != "0" for ch in parts[1]):
            return parts[1].lower()
        return uuid4().hex

    def start_turn(self, audio: bytes) -> None:
        response_id = "resp_" + uuid4().hex[:24]
        generation_id = self.session.begin_generation(response_id)
        started_at = time.perf_counter()
        self.active_turn_stats = {
            "id": str(uuid4()),
            "response_id": response_id,
            "generation_id": generation_id,
            "started_at": started_at,
            "audio_input_bytes": len(audio),
            "audio_output_bytes": 0,
            "first_transcript_at": None,
            "first_token_at": None,
            "first_audio_at": None,
            "evaluation_recorded": False,
        }
        self.response_task = asyncio.create_task(self._run_turn(audio, self.active_turn_stats))

    async def handle_turn_event(self, event: TurnEvent) -> None:
        if event.kind == "speech_started":
            task = self.response_task
            response = self.response_state
            turn = self.active_turn_stats
            captured_state = (task, response, turn) if task is not None else None
            generation_already_invalidated = False
            if task is not None and not task.done():
                if response is None or response.get("terminal_status") is None:
                    self.session.invalidate_generation()
                    generation_already_invalidated = True
            await self._emit("input_audio_buffer.speech_started", audio_start_ms=0)
            if task is not None and task is self.response_task and not task.done():
                await self.cancel_response(
                    "barge_in",
                    generation_already_invalidated=generation_already_invalidated,
                    captured_state=captured_state,
                )
            elif generation_already_invalidated and captured_state is not None:
                # The stale generation may finish its guarded send while the marker
                # waits for the transport lock. Preserve its interruption evidence.
                await self.cancel_response(
                    "barge_in",
                    generation_already_invalidated=True,
                    captured_state=captured_state,
                )
            self.session.state = "LISTENING"
        elif event.kind == "speech_stopped" and event.audio:
            self.session.state = "TURN_PENDING"
            await self._emit("input_audio_buffer.speech_stopped")
            if self.response_task is not None and not self.response_task.done():
                await self.cancel_response("superseded_turn")
            self.start_turn(event.audio)

    def _enqueue_evaluation(
        self,
        turn: dict[str, Any],
        event_type: str,
        *,
        interrupted: bool = False,
        truncation_recorded: bool = False,
        audio_integrity_ok: bool = True,
    ) -> None:
        if turn["evaluation_recorded"]:
            return
        turn["evaluation_recorded"] = True
        elapsed_ms = lambda key: (
            round((turn[key] - turn["started_at"]) * 1000, 2)
            if turn.get(key) is not None
            else None
        )
        timing_names = {
            "first_transcript_at": "time_to_first_transcript_ms",
            "first_token_at": "time_to_first_token_ms",
            "first_audio_at": "time_to_first_audio_ms",
        }
        for at_key, metric_name in timing_names.items():
            elapsed = elapsed_ms(at_key)
            if elapsed is not None:
                self.metrics.observe_latency(metric_name, elapsed)
        payload = {
            "event_id": str(uuid4()),
            "session_id": self.session.id,
            "turn_id": turn["id"],
            "trace_id": self._trace_id(),
            "event_type": event_type,
            "duration_ms": round((time.perf_counter() - turn["started_at"]) * 1000, 2),
            "time_to_first_transcript_ms": elapsed_ms("first_transcript_at"),
            "time_to_first_token_ms": elapsed_ms("first_token_at"),
            "time_to_first_audio_ms": elapsed_ms("first_audio_at"),
            "latency_baseline_ms": self.metrics.latency_baseline(),
            "audio_input_bytes": turn["audio_input_bytes"],
            "audio_output_bytes": turn["audio_output_bytes"],
            "interrupted": interrupted,
            "truncation_recorded": truncation_recorded,
            "audio_integrity_ok": audio_integrity_ok,
            "stt_provider": self.session.stt_provider,
            "llm_provider": self.session.llm_provider,
            "tts_provider": self.session.tts_provider,
        }

        async def deliver() -> None:
            reporter = getattr(self.provider, "report_turn", None)
            if callable(reporter):
                try:
                    await reporter(payload)
                except Exception:
                    return

        task = asyncio.create_task(deliver())
        self.evaluation_tasks.add(task)
        task.add_done_callback(self.evaluation_tasks.discard)

    async def _record_interruption(
        self,
        turn: dict[str, Any] | None,
        response: dict[str, Any] | None,
        reason: str,
    ) -> None:
        if response is not None and not response.get("interruption_recorded"):
            response["interruption_recorded"] = True
            response["interrupted"] = True
            response["cancel_reason"] = reason
            audio_ms = round(
                response["emitted_audio_bytes"]
                / max(1, response["output_sample_rate"] * response["output_channels"] * 2)
                * 1000
            )
            await self._emit(
                "response.cancelled",
                response_id=response["id"],
                turn_id=response["turn_id"],
                generation_id=response["generation_id"],
                reason=reason,
            )
            if response["emitted_text"].strip():
                await self._emit(
                    "conversation.item.truncated",
                    response_id=response["id"],
                    turn_id=response["turn_id"],
                    generation_id=response["generation_id"],
                    content_index=0,
                    audio_end_ms=audio_ms,
                )
                self.session.history.append({
                    "role": "assistant",
                    "content": visible_assistant_text(
                        response["generated_text"], response["emitted_text"].strip(), interrupted=True
                    ),
                })
            truncation_recorded = (
                not bool(response["emitted_audio_bytes"]) or bool(response["emitted_text"].strip())
            )
            self.metrics.interruptions += 1
        else:
            truncation_recorded = True
        if turn is not None:
            self._enqueue_evaluation(
                turn,
                "voice.turn.interrupted",
                interrupted=True,
                truncation_recorded=truncation_recorded,
                audio_integrity_ok=True,
            )

    async def cancel_response(
        self,
        reason: str,
        *,
        generation_already_invalidated: bool = False,
        captured_state: tuple[asyncio.Task, dict[str, Any] | None, dict[str, Any] | None] | None = None,
    ) -> None:
        if captured_state is None:
            task = self.response_task
            response = self.response_state
            turn = self.active_turn_stats
        else:
            task, response, turn = captured_state
        if response is not None and response.get("terminal_status") is not None:
            # Completion has been committed and its final event is in flight. Let that
            # event settle instead of turning a completed response into an interruption.
            if task is not None and not task.done():
                await asyncio.gather(task, return_exceptions=True)
            return
        if task is None or task.done():
            if generation_already_invalidated and captured_state is not None:
                await self._record_interruption(turn, response, reason)
            if self.response_task is task:
                self.response_task = None
                self.response_state = None
                self.active_turn_stats = None
            return
        if not generation_already_invalidated:
            self.session.invalidate_generation()
        if response is not None:
            response["cancel_reason"] = reason
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await self._record_interruption(turn, response, reason)
        if self.response_task is task:
            self.response_task = None
            self.response_state = None
            self.active_turn_stats = None

    async def _run_turn(self, audio: bytes, turn: dict[str, Any]) -> None:
        started_at = turn["started_at"]
        turn_span = start_span_handle(
            "voice.turn",
            parent=self.parent_trace_context,
            attributes={
                "openinference.span.kind": "CHAIN",
                "gen_ai.operation.name": "voice_turn",
                "ai_stack.session_id": self.session.id,
                "ai_stack.turn_id": turn["id"],
                "ai_stack.audio.input_bytes": len(audio),
            },
        )
        turn_trace = current_trace_context(self.parent_trace_context, turn_span)
        turn_error = None
        generation_id = turn["generation_id"]
        response_id = turn["response_id"]

        def is_current() -> bool:
            return self.session.generation_is_current(generation_id, response_id)

        try:
            self.session.state = "TRANSCRIBING"
            with start_span(
                "voice.stt.transcribe",
                parent=turn_trace,
                attributes={
                    "openinference.span.kind": "LLM",
                    "gen_ai.operation.name": "transcribe",
                    "gen_ai.provider.name": "gateway",
                    "ai_stack.session_id": self.session.id,
                    "ai_stack.turn_id": turn["id"],
                },
            ) as stt_span:
                stt_trace = current_trace_context(turn_trace, stt_span)
                transcript = await self.provider.transcribe(
                    _pcm_wav(audio), traceparent=_traceparent(stt_trace)
                )
                stt_span.set_attribute("ai_stack.audio.input_bytes", len(audio))
            if not is_current():
                return
            turn["first_transcript_at"] = time.perf_counter()
            full_text = transcript["text"].strip()
            transcript_id = str(uuid4())
            segments = transcript.get("segments") or []
            partials = [segment.get("text", "") for segment in segments if isinstance(segment, dict)]
            partials = [part for part in partials if isinstance(part, str) and part]
            if not partials and full_text:
                partials = [full_text]
            for part in partials:
                if not is_current():
                    return
                await self._emit(
                    "conversation.item.input_audio_transcription.delta",
                    item_id=transcript_id,
                    turn_id=turn["id"],
                    generation_id=generation_id,
                    guard_generation_id=generation_id,
                    guard_response_id=response_id,
                    delta=part,
                )
                if not is_current():
                    return
            if not is_current():
                return
            await self._emit(
                "conversation.item.input_audio_transcription.completed",
                item_id=transcript_id,
                turn_id=turn["id"],
                generation_id=generation_id,
                guard_generation_id=generation_id,
                guard_response_id=response_id,
                transcript=full_text,
                language="en",
            )
            if not is_current():
                return
            if not full_text:
                self.session.state = "LISTENING"
                self._enqueue_evaluation(turn, "voice.turn.empty")
                return

            self.session.current_user_turn += 1
            self.session.history.append({"role": "user", "content": full_text})
            self.session.current_assistant_turn += 1
            response = {
                "id": response_id,
                "turn_id": turn["id"],
                "generation_id": generation_id,
                "generated_text": "",
                "emitted_text": "",
                "emitted_audio_bytes": 0,
                "output_sample_rate": 24_000,
                "output_channels": 1,
                "interrupted": False,
                "terminal_status": None,
                "first_audio_at": None,
            }
            self.response_state = response
            created = await self._emit(
                "response.created",
                response_id=response_id,
                turn_id=turn["id"],
                generation_id=generation_id,
                guard_generation_id=generation_id,
                guard_response_id=response_id,
            )
            if not created:
                return
            self.session.state = "GENERATING"
            chunker = SentenceChunker()

            async def speak(chunk: str) -> None:
                with start_span(
                    "voice.tts.synthesize",
                    parent=turn_trace,
                    attributes={
                        "openinference.span.kind": "LLM",
                        "gen_ai.operation.name": "synthesize",
                        "gen_ai.provider.name": "gateway",
                        "ai_stack.session_id": self.session.id,
                        "ai_stack.turn_id": turn["id"],
                    },
                ) as tts_span:
                    tts_trace = current_trace_context(turn_trace, tts_span)
                    pcm, sample_rate, channels = await self.provider.synthesize(
                        chunk, traceparent=_traceparent(tts_trace)
                    )
                    tts_span.set_attribute("ai_stack.audio.output_bytes", len(pcm))
                if not is_current() or not pcm:
                    return

                if self.send_audio is None:

                    async def send_audio_delta() -> bool:
                        return await self._emit(
                            "response.audio.delta",
                            response_id=response_id,
                            turn_id=turn["id"],
                            generation_id=generation_id,
                            guard_generation_id=generation_id,
                            guard_response_id=response_id,
                            delta=base64.b64encode(pcm).decode("ascii"),
                            sample_rate=sample_rate,
                            channels=channels,
                            text=chunk,
                        )

                else:
                    audio_output = AudioOutput(
                        pcm=pcm,
                        sample_rate=sample_rate,
                        channels=channels,
                        text=chunk,
                        response_id=response_id,
                        turn_id=turn["id"],
                        generation_id=generation_id,
                    )

                    async def send_audio_delta() -> bool:
                        async with self._send_lock:
                            if not self.session.generation_is_current(
                                generation_id, response_id
                            ):
                                return False
                            return await self.send_audio(audio_output)

                send_task = asyncio.create_task(send_audio_delta())
                self._pending_audio_send_task = send_task

                def record_audio_delivery() -> None:
                    response["emitted_text"] += chunk + " "
                    response["emitted_audio_bytes"] += len(pcm)
                    response["output_sample_rate"] = sample_rate
                    response["output_channels"] = channels
                    turn["audio_output_bytes"] = response["emitted_audio_bytes"]
                    self.session.audio_output_bytes += len(pcm)
                    if response["first_audio_at"] is None:
                        first_audio_at = time.perf_counter()
                        response["first_audio_at"] = first_audio_at
                        turn["first_audio_at"] = first_audio_at
                        self.metrics.observe_first_audio(first_audio_at - started_at)

                try:
                    delivered = await asyncio.shield(send_task)
                except asyncio.CancelledError:
                    # If the transport accepted the frame before cancellation arrived,
                    # settle the send and account for it before recording truncation.
                    sent = await asyncio.gather(send_task, return_exceptions=True)
                    if sent and sent[0] is True:
                        record_audio_delivery()
                    if self._pending_audio_send_task is send_task:
                        self._pending_audio_send_task = None
                    raise
                else:
                    if delivered:
                        record_audio_delivery()
                    if self._pending_audio_send_task is send_task:
                        self._pending_audio_send_task = None

            with start_span(
                "voice.llm.generate",
                parent=turn_trace,
                attributes={
                    "openinference.span.kind": "LLM",
                    "gen_ai.operation.name": "chat",
                    "gen_ai.provider.name": "gateway",
                    "gen_ai.request.model": "local-fast",
                    "ai_stack.session_id": self.session.id,
                    "ai_stack.turn_id": turn["id"],
                },
            ) as llm_span:
                llm_trace = current_trace_context(turn_trace, llm_span)
                async for delta in self.provider.chat_deltas(
                    self.session.history, traceparent=_traceparent(llm_trace)
                ):
                    if not is_current():
                        return
                    if turn["first_token_at"] is None:
                        turn["first_token_at"] = time.perf_counter()
                        llm_span.set_attribute(
                            "gen_ai.server.time_to_first_token_ms",
                            (turn["first_token_at"] - started_at) * 1000,
                        )
                    response["generated_text"] += delta
                    delivered = await self._emit(
                        "response.output_text.delta",
                        response_id=response_id,
                        turn_id=turn["id"],
                        generation_id=generation_id,
                        guard_generation_id=generation_id,
                        guard_response_id=response_id,
                        delta=delta,
                    )
                    if not delivered:
                        return
                    for chunk in chunker.feed(delta):
                        self.session.state = "SPEAKING"
                        await speak(chunk)
            for chunk in chunker.feed("", final=True):
                if not is_current():
                    return
                self.session.state = "SPEAKING"
                await speak(chunk)

            if not is_current():
                return
            assistant_text = visible_assistant_text(
                response["generated_text"], response["emitted_text"].strip(), interrupted=False
            )
            if assistant_text:
                self.session.history.append({"role": "assistant", "content": assistant_text})
            response["terminal_status"] = "completed"
            await self._emit(
                "response.done",
                response_id=response_id,
                turn_id=turn["id"],
                generation_id=generation_id,
                status="completed",
                output_text=assistant_text,
            )
            self.session.finish_generation(generation_id)
            self.metrics.turns += 1
            self._enqueue_evaluation(
                turn,
                "voice.turn.completed",
                audio_integrity_ok=bool(turn["audio_output_bytes"]),
            )
        except asyncio.CancelledError as exc:
            turn_error = exc
            raise
        except Exception as exc:
            if not is_current():
                return
            turn_error = exc
            await self._emit("error", code="voice_turn_failed", message=str(exc)[:256])
            self._enqueue_evaluation(
                turn,
                "voice.turn.failed",
                audio_integrity_ok=False,
                truncation_recorded=True,
            )
        finally:
            turn_span.set_attribute("ai_stack.turn.status", "error" if turn_error else "completed")
            for key, stats_key in (
                ("ai_stack.turn.time_to_first_transcript_ms", "first_transcript_at"),
                ("ai_stack.turn.time_to_first_token_ms", "first_token_at"),
                ("ai_stack.turn.time_to_first_audio_ms", "first_audio_at"),
            ):
                at = turn.get(stats_key)
                if at is not None:
                    turn_span.set_attribute(key, (at - started_at) * 1000)
            turn_span.set_attribute("ai_stack.audio.output_bytes", turn["audio_output_bytes"])
            end_span_handle(turn_span, turn_error)
            if self.response_task is asyncio.current_task():
                if self.session.generation_is_current(generation_id, response_id):
                    self.session.finish_generation(generation_id)
                self.response_state = None
                self.active_turn_stats = None
                self.response_task = None
            self.session.state = "LISTENING"

    async def close(self) -> None:
        if self.response_task is not None and not self.response_task.done():
            await self.cancel_response("session_closed")
        if self.evaluation_tasks:
            await asyncio.wait(self.evaluation_tasks, timeout=2.5)
