from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from voice.audio.frame_adapter import convert_input_audio, convert_output_audio
from voice.runtime import AudioOutput, RealtimeRuntime
from voice.session import RealtimeSession
from voice.turn_detection import VoiceTurnDetector

VOICE_SUBPROTOCOL = "ai-stack.voice.v1"
INPUT_SAMPLE_RATE = 16_000
WEBRTC_SAMPLE_RATE = 48_000
WEBRTC_FRAME_BYTES = WEBRTC_SAMPLE_RATE * 2 // 100  # Pipecat requires 10 ms chunks.
MAX_INPUT_FRAME_BYTES = 128 * 1024


def make_control_envelope(event: dict[str, Any]) -> dict[str, Any]:
    """Add the voice protocol marker without allowing audio onto the data channel."""
    if event.get("type") == "response.audio.delta":
        raise ValueError("audio must be sent as WebRTC media")
    return {"protocol": VOICE_SUBPROTOCOL, **event}


async def process_input_audio_frame(
    frame: Any,
    *,
    detector: VoiceTurnDetector,
    runtime: RealtimeRuntime,
    session: RealtimeSession,
    metrics: Any,
) -> bool:
    """Convert one Pipecat frame into the existing turn detector's PCM format."""
    audio = getattr(frame, "audio", None)
    byte_count = len(audio) if isinstance(audio, (bytes, bytearray, memoryview)) else 0
    metrics.record_webrtc_audio("input", byte_count)
    started_at = time.perf_counter()
    try:
        if byte_count <= 0 or byte_count > MAX_INPUT_FRAME_BYTES:
            raise ValueError("input frame size is invalid")
        converted = convert_input_audio(
            bytes(audio),
            sample_rate=int(frame.sample_rate),
            channels=int(frame.num_channels),
        )
    except (AttributeError, TypeError, ValueError):
        metrics.record_webrtc_conversion_failure("invalid_frame")
        metrics.record_webrtc_frame_drop("input")
        return False
    finally:
        metrics.observe_webrtc_conversion("input", time.perf_counter() - started_at)

    session.audio_input_bytes += len(converted)
    try:
        events = detector.feed(converted)
    except OverflowError:
        metrics.record_webrtc_frame_drop("input")
        return False
    for event in events:
        await runtime.handle_turn_event(event)
    return True


async def send_output_audio_frame(
    audio_output: AudioOutput,
    *,
    transport: Any,
    metrics: Any,
    frame_factory: Callable[..., Any] | None = None,
) -> bool:
    """Convert provider PCM and enqueue it as Pipecat's aligned 48 kHz frames."""
    if frame_factory is None:
        from pipecat.frames.frames import OutputAudioRawFrame

        frame_factory = OutputAudioRawFrame

    conversion_started_at = time.perf_counter()
    try:
        converted = convert_output_audio(
            audio_output.pcm,
            sample_rate=audio_output.sample_rate,
            channels=audio_output.channels,
        )
        payload = converted.data
        remainder = len(payload) % WEBRTC_FRAME_BYTES
        if remainder:
            payload += bytes(WEBRTC_FRAME_BYTES - remainder)
    except (TypeError, ValueError):
        metrics.record_webrtc_conversion_failure("invalid_frame")
        metrics.record_webrtc_frame_drop("output")
        return False
    except Exception:
        metrics.record_webrtc_conversion_failure("internal")
        metrics.record_webrtc_frame_drop("output")
        return False
    finally:
        metrics.observe_webrtc_conversion(
            "output", time.perf_counter() - conversion_started_at
        )

    try:
        await transport.send_audio(frame_factory(
            audio=payload,
            sample_rate=converted.sample_rate,
            num_channels=converted.channels,
        ))
    except Exception:
        metrics.record_webrtc_frame_drop("output")
        return False
    metrics.record_webrtc_audio("output", len(payload))
    return True


class PeerLifecycle:
    """Close a runtime, its provider, and transport resources exactly once."""

    def __init__(
        self,
        *,
        runtime: Any,
        provider: Any,
        close_peer: Callable[[], Awaitable[None]],
        on_closed: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.runtime = runtime
        self.provider = provider
        self.close_peer = close_peer
        self.on_closed = on_closed
        self._closing = False

    async def close(self, reason: str = "closed") -> None:
        if self._closing:
            return
        self._closing = True
        try:
            if self.runtime is not None:
                await self.runtime.close()
        finally:
            try:
                if self.provider is not None:
                    await self.provider.close()
            finally:
                try:
                    await self.close_peer()
                finally:
                    if self.on_closed is not None:
                        await self.on_closed(reason)


class SmallWebRTCPeer:
    """Pipecat SmallWebRTC transport adapter for the canonical voice runtime."""

    def __init__(
        self,
        *,
        session: RealtimeSession,
        provider_factory: Callable[..., Any],
        metrics: Any,
        parent_trace_context: Any,
        on_closed: Callable[[str], Awaitable[None]],
    ) -> None:
        self.session = session
        self.provider_factory = provider_factory
        self.metrics = metrics
        self.parent_trace_context = parent_trace_context
        self.on_closed = on_closed
        self.provider: Any = None
        self.runtime: RealtimeRuntime | None = None
        self.connection: Any = None
        self.transport: Any = None
        self.runner: Any = None
        self.worker: Any = None
        self.worker_task: asyncio.Task | None = None
        self.detector: VoiceTurnDetector | None = None
        self.lifecycle: PeerLifecycle | None = None
        self._partial_close_started = False

    async def accept_offer(self, offer: dict[str, str]) -> dict[str, str]:
        # Pipecat's WebRTC extras are optional for WebSocket-only installations.
        from pipecat.frames.frames import InputAudioRawFrame, InterruptionFrame
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.worker import PipelineParams, PipelineWorker
        from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
        from pipecat.transports.base_transport import TransportParams
        from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection
        from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
        from pipecat.workers.runner import WorkerRunner

        ice_servers = []
        turn = self.session.turn_credentials
        if turn is not None:
            ice_servers.append(IceServer(
                urls="turn:coturn:3478?transport=tcp",
                username=turn.username,
                credential=turn.credential,
            ))
        self.connection = SmallWebRTCConnection(ice_servers=ice_servers)

        @self.connection.event_handler("closed")
        async def on_connection_closed(*_args: Any) -> None:
            await self.close("client")

        await self.connection.initialize(sdp=offer["sdp"], type=offer["type"])
        self.transport = SmallWebRTCTransport(
            self.connection,
            TransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=WEBRTC_SAMPLE_RATE,
                audio_out_sample_rate=WEBRTC_SAMPLE_RATE,
                audio_in_channels=1,
                audio_out_channels=1,
            ),
        )
        self.detector = VoiceTurnDetector(
            sample_rate=INPUT_SAMPLE_RATE,
            speech_threshold=float(os.getenv("VOICE_VAD_SPEECH_THRESHOLD", "420")),
            end_silence_seconds=float(os.getenv("VOICE_VAD_END_SILENCE_SECONDS", "0.65")),
            max_turn_seconds=float(os.getenv("VOICE_MAX_TURN_SECONDS", "90")),
        )
        self.provider = self.provider_factory(
            traceparent=self.session.traceparent,
            session_id=self.session.id,
        )

        async def send_event(event: dict[str, Any]) -> None:
            self.connection.send_app_message(make_control_envelope(event))

        async def clear_audio() -> None:
            await self.transport.output().queue_frame(
                InterruptionFrame(), FrameDirection.DOWNSTREAM
            )

        async def send_audio(audio_output: AudioOutput) -> bool:
            return await send_output_audio_frame(
                audio_output,
                transport=self.transport,
                metrics=self.metrics,
            )

        self.runtime = RealtimeRuntime(
            session=self.session,
            provider=self.provider,
            metrics=self.metrics,
            send_event=send_event,
            parent_trace_context=self.parent_trace_context,
            send_audio=send_audio,
            clear_audio=clear_audio,
        )
        self._ensure_lifecycle()

        class RuntimeAudioFrames(FrameProcessor):
            async def process_frame(processor_self, frame: Any, direction: Any) -> None:
                await super(RuntimeAudioFrames, processor_self).process_frame(frame, direction)
                if isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
                    await process_input_audio_frame(
                        frame,
                        detector=self.detector,
                        runtime=self.runtime,
                        session=self.session,
                        metrics=self.metrics,
                    )
                await processor_self.push_frame(frame, direction)

        frame_bridge = RuntimeAudioFrames()

        @self.transport.event_handler("on_app_message")
        async def on_app_message(_transport: Any, message: Any, _sender: str) -> None:
            if not isinstance(message, dict) or self.runtime is None or self.detector is None:
                return
            if message.get("protocol") not in (None, VOICE_SUBPROTOCOL):
                return
            event_type = message.get("type")
            if event_type == "input_audio_buffer.commit":
                for event in self.detector.commit():
                    await self.runtime.handle_turn_event(event)
            elif event_type == "response.cancel":
                await self.runtime.cancel_response("client_cancelled")
            elif event_type == "session.close":
                await self.close("client")

        @self.transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(*_args: Any) -> None:
            await self.close("client")

        pipeline = Pipeline([self.transport.input(), frame_bridge, self.transport.output()])
        worker = PipelineWorker(
            pipeline,
            params=PipelineParams(
                audio_in_sample_rate=WEBRTC_SAMPLE_RATE,
                audio_out_sample_rate=WEBRTC_SAMPLE_RATE,
                enable_metrics=True,
                enable_heartbeats=True,
            ),
            idle_timeout_secs=None,
            enable_rtvi=False,
            conversation_id=self.session.conversation_id,
        )
        self.worker = worker
        self.runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        await self.runner.add_workers(worker)
        runner_ready = asyncio.Event()

        @self.runner.event_handler("on_ready")
        async def on_runner_ready(*_args: Any) -> None:
            runner_ready.set()

        self.worker_task = asyncio.create_task(self.runner.run())
        self.worker_task.add_done_callback(self._runner_finished)
        await asyncio.wait_for(runner_ready.wait(), timeout=15)
        answer = self.connection.get_answer()
        if not isinstance(answer, dict):
            raise ValueError("Pipecat did not produce an SDP answer")
        return answer

    def _runner_finished(self, task: asyncio.Task) -> None:
        if self.lifecycle is not None and self.lifecycle._closing:
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            error = None
        asyncio.create_task(self.close("failed" if error is not None else "closed"))

    async def _close_transport(self) -> None:
        try:
            if self.runner is not None:
                await self.runner.cancel(reason="voice peer closed")
            if self.worker_task is not None and self.worker_task is not asyncio.current_task():
                await asyncio.gather(self.worker_task, return_exceptions=True)
        finally:
            if self.connection is not None:
                await self.connection.disconnect()

    async def close(self, reason: str = "closed") -> None:
        if self.lifecycle is None:
            if self._partial_close_started:
                return
            self._partial_close_started = True
            # Offer setup can fail before provider/runtime initialization.
            try:
                await self._close_transport()
            finally:
                await self.on_closed(reason)
            return
        await self.lifecycle.close(reason)

    def _ensure_lifecycle(self) -> PeerLifecycle:
        if self.lifecycle is None:
            self.lifecycle = PeerLifecycle(
                runtime=self.runtime,
                provider=self.provider,
                close_peer=self._close_transport,
                on_closed=self.on_closed,
            )
        return self.lifecycle
