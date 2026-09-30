from __future__ import annotations

import asyncio
import unittest

from observability.propagation import extract_trace_context
from voice.metrics import VoiceMetrics
from voice.runtime import RealtimeRuntime
from voice.session import VoiceSessionRegistry
from voice.turn_detection import TurnEvent


class ImmediateProviders:
    def __init__(self):
        self.evaluations = []

    async def transcribe(self, wav_audio: bytes, *, traceparent: str | None = None) -> dict:
        return {"text": "Question.", "segments": [{"text": "Question."}]}

    async def chat_deltas(self, history, *, traceparent: str | None = None):
        yield "Answer."

    async def synthesize(self, text: str, *, traceparent: str | None = None):
        return b"\x00\x00" * 24, 24_000, 1

    async def report_turn(self, event):
        self.evaluations.append(dict(event))

    async def close(self):
        return None


class GatedSynthesisProviders(ImmediateProviders):
    def __init__(self):
        super().__init__()
        self.synthesis_started = asyncio.Event()
        self.release_synthesis = asyncio.Event()

    async def synthesize(self, text: str, *, traceparent: str | None = None):
        self.synthesis_started.set()
        await self.release_synthesis.wait()
        return b"\x00\x00" * 24, 24_000, 1


class RealtimeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_barge_in_invalidates_audio_queued_before_send_lock(self):
        session, _ = VoiceSessionRegistry().create(principal="client", traceparent=None)
        providers = GatedSynthesisProviders()
        events = []

        async def send_event(event):
            events.append(dict(event))

        runtime = RealtimeRuntime(
            session=session,
            provider=providers,
            metrics=VoiceMetrics(),
            send_event=send_event,
            parent_trace_context=extract_trace_context({}, session_id=session.id),
        )
        runtime.start_turn(b"\x00\x00" * 100)
        await asyncio.wait_for(providers.synthesis_started.wait(), timeout=1)
        await runtime._send_lock.acquire()
        providers.release_synthesis.set()

        async def pending_audio_send_exists():
            while runtime._pending_audio_send_task is None:
                await asyncio.sleep(0)

        await asyncio.wait_for(pending_audio_send_exists(), timeout=1)
        barge_in_task = asyncio.create_task(
            runtime.handle_turn_event(TurnEvent(kind="speech_started"))
        )
        await asyncio.sleep(0)
        self.assertNotIn(
            "input_audio_buffer.speech_started", [event["type"] for event in events]
        )
        runtime._send_lock.release()
        await asyncio.wait_for(barge_in_task, timeout=1)
        await runtime.close()

        event_types = [event["type"] for event in events]
        self.assertNotIn("response.audio.delta", event_types)
        self.assertLess(
            event_types.index("input_audio_buffer.speech_started"),
            event_types.index("response.cancelled"),
        )
        self.assertEqual(providers.evaluations[0]["audio_output_bytes"], 0)
        self.assertTrue(providers.evaluations[0]["truncation_recorded"])

    async def test_cancel_before_queued_audio_send_skips_stale_frame(self):
        session, _ = VoiceSessionRegistry().create(principal="client", traceparent=None)
        providers = GatedSynthesisProviders()
        events = []

        async def send_event(event):
            events.append(dict(event))

        runtime = RealtimeRuntime(
            session=session,
            provider=providers,
            metrics=VoiceMetrics(),
            send_event=send_event,
            parent_trace_context=extract_trace_context({}, session_id=session.id),
        )
        runtime.start_turn(b"\x00\x00" * 100)
        await asyncio.wait_for(providers.synthesis_started.wait(), timeout=1)
        await runtime._send_lock.acquire()
        providers.release_synthesis.set()

        async def pending_audio_send_exists():
            while runtime._pending_audio_send_task is None:
                await asyncio.sleep(0)

        await asyncio.wait_for(pending_audio_send_exists(), timeout=1)
        cancel_task = asyncio.create_task(runtime.cancel_response("client_cancelled"))
        await asyncio.sleep(0)
        runtime._send_lock.release()
        await asyncio.wait_for(cancel_task, timeout=1)
        await runtime.close()

        event_types = [event["type"] for event in events]
        self.assertNotIn("response.audio.delta", event_types)
        self.assertIn("response.cancelled", event_types)

    async def test_barge_in_marker_waits_for_inflight_audio_delta(self):
        session, _ = VoiceSessionRegistry().create(principal="client", traceparent=None)
        providers = ImmediateProviders()
        events = []
        audio_send_started = asyncio.Event()
        release_audio_send = asyncio.Event()

        async def send_event(event):
            if event["type"] == "response.audio.delta":
                audio_send_started.set()
                await release_audio_send.wait()
            events.append(dict(event))

        runtime = RealtimeRuntime(
            session=session,
            provider=providers,
            metrics=VoiceMetrics(),
            send_event=send_event,
            parent_trace_context=extract_trace_context({}, session_id=session.id),
        )
        runtime.start_turn(b"\x00\x00" * 100)
        await asyncio.wait_for(audio_send_started.wait(), timeout=1)

        barge_in_task = asyncio.create_task(
            runtime.handle_turn_event(TurnEvent(kind="speech_started"))
        )
        await asyncio.sleep(0)
        self.assertNotIn(
            "input_audio_buffer.speech_started", [event["type"] for event in events]
        )
        release_audio_send.set()
        await asyncio.wait_for(barge_in_task, timeout=1)
        await runtime.close()

        event_types = [event["type"] for event in events]
        self.assertLess(
            event_types.index("response.audio.delta"),
            event_types.index("input_audio_buffer.speech_started"),
        )
        self.assertLess(
            event_types.index("input_audio_buffer.speech_started"),
            event_types.index("response.cancelled"),
        )
        self.assertTrue(providers.evaluations[0]["truncation_recorded"])
        self.assertEqual(providers.evaluations[0]["audio_output_bytes"], 48)

    async def test_cancel_during_audio_send_accounts_for_delivered_frame(self):
        session, _ = VoiceSessionRegistry().create(principal="client", traceparent=None)
        providers = ImmediateProviders()
        events = []
        audio_send_started = asyncio.Event()
        release_audio_send = asyncio.Event()

        async def send_event(event):
            if event["type"] == "response.audio.delta":
                audio_send_started.set()
                await release_audio_send.wait()
            events.append(dict(event))

        runtime = RealtimeRuntime(
            session=session,
            provider=providers,
            metrics=VoiceMetrics(),
            send_event=send_event,
            parent_trace_context=extract_trace_context({}, session_id=session.id),
        )
        runtime.start_turn(b"\x00\x00" * 100)
        await asyncio.wait_for(audio_send_started.wait(), timeout=1)

        cancel_task = asyncio.create_task(runtime.cancel_response("barge_in"))
        await asyncio.sleep(0)
        release_audio_send.set()
        await asyncio.wait_for(cancel_task, timeout=1)
        await runtime.close()

        audio_event = next(event for event in events if event["type"] == "response.audio.delta")
        truncation = next(
            event for event in events if event["type"] == "conversation.item.truncated"
        )
        assistant_entries = [
            message for message in session.history if message["role"] == "assistant"
        ]
        self.assertEqual(len(audio_event["delta"]), 64)
        self.assertEqual(truncation["audio_end_ms"], 1)
        self.assertEqual([message["content"] for message in assistant_entries], ["Answer."])
        self.assertEqual(providers.evaluations[0]["audio_output_bytes"], 48)
        self.assertTrue(providers.evaluations[0]["truncation_recorded"])

    async def test_cancel_during_done_send_does_not_reopen_completed_response(self):
        session, _ = VoiceSessionRegistry().create(principal="client", traceparent=None)
        providers = ImmediateProviders()
        events = []
        done_send_started = asyncio.Event()
        release_done_send = asyncio.Event()

        async def send_event(event):
            if event["type"] == "response.done":
                done_send_started.set()
                await release_done_send.wait()
            events.append(dict(event))

        runtime = RealtimeRuntime(
            session=session,
            provider=providers,
            metrics=VoiceMetrics(),
            send_event=send_event,
            parent_trace_context=extract_trace_context({}, session_id=session.id),
        )
        runtime.start_turn(b"\x00\x00" * 100)
        await asyncio.wait_for(done_send_started.wait(), timeout=1)

        cancel_task = asyncio.create_task(runtime.cancel_response("barge_in"))
        await asyncio.sleep(0)
        release_done_send.set()
        await asyncio.wait_for(cancel_task, timeout=1)
        await runtime.close()

        assistant_entries = [
            message for message in session.history if message["role"] == "assistant"
        ]
        self.assertEqual([message["content"] for message in assistant_entries], ["Answer."])
        self.assertIn("response.done", [event["type"] for event in events])
        self.assertNotIn("response.cancelled", [event["type"] for event in events])
        self.assertNotIn("conversation.item.truncated", [event["type"] for event in events])
        self.assertEqual(
            [event["event_type"] for event in providers.evaluations],
            ["voice.turn.completed"],
        )


    async def test_webrtc_audio_sink_replaces_base64_event_and_records_transport_acceptance(self):
        session, _ = VoiceSessionRegistry().create(principal="client", traceparent=None)
        providers = ImmediateProviders()
        events = []
        audio_outputs = []

        async def send_event(event):
            events.append(dict(event))

        async def send_audio(output):
            audio_outputs.append(output)
            return True

        runtime = RealtimeRuntime(
            session=session,
            provider=providers,
            metrics=VoiceMetrics(),
            send_event=send_event,
            send_audio=send_audio,
            parent_trace_context=extract_trace_context({}, session_id=session.id),
        )
        runtime.start_turn(b"\x00\x00" * 100)
        await runtime.response_task
        await runtime.close()

        self.assertNotIn("response.audio.delta", [event["type"] for event in events])
        self.assertEqual(len(audio_outputs), 1)
        self.assertEqual(audio_outputs[0].pcm, b"\x00\x00" * 24)
        self.assertEqual(audio_outputs[0].sample_rate, 24_000)
        self.assertEqual(audio_outputs[0].channels, 1)
        self.assertEqual(audio_outputs[0].text, "Answer.")
        self.assertEqual(session.audio_output_bytes, 48)
        self.assertEqual(providers.evaluations[0]["audio_output_bytes"], 48)

    async def test_webrtc_audio_sink_rejects_stale_generation(self):
        session, _ = VoiceSessionRegistry().create(principal="client", traceparent=None)
        providers = GatedSynthesisProviders()
        events = []
        audio_outputs = []

        async def send_event(event):
            events.append(dict(event))

        async def send_audio(output):
            audio_outputs.append(output)
            return True

        runtime = RealtimeRuntime(
            session=session,
            provider=providers,
            metrics=VoiceMetrics(),
            send_event=send_event,
            send_audio=send_audio,
            parent_trace_context=extract_trace_context({}, session_id=session.id),
        )
        runtime.start_turn(b"\x00\x00" * 100)
        await asyncio.wait_for(providers.synthesis_started.wait(), timeout=1)
        await runtime._send_lock.acquire()
        providers.release_synthesis.set()

        async def pending_audio_send_exists():
            while runtime._pending_audio_send_task is None:
                await asyncio.sleep(0)

        await asyncio.wait_for(pending_audio_send_exists(), timeout=1)
        session.invalidate_generation()
        runtime._send_lock.release()
        await runtime.response_task
        await runtime.close()

        self.assertEqual(audio_outputs, [])
        self.assertNotIn("response.audio.delta", [event["type"] for event in events])
        self.assertEqual(session.audio_output_bytes, 0)

    async def test_webrtc_audio_sink_records_only_transport_acceptance(self):
        session, _ = VoiceSessionRegistry().create(principal="client", traceparent=None)
        providers = ImmediateProviders()
        events = []
        audio_outputs = []

        async def send_event(event):
            events.append(dict(event))

        async def send_audio(output):
            audio_outputs.append(output)
            return False

        runtime = RealtimeRuntime(
            session=session,
            provider=providers,
            metrics=VoiceMetrics(),
            send_event=send_event,
            send_audio=send_audio,
            parent_trace_context=extract_trace_context({}, session_id=session.id),
        )
        runtime.start_turn(b"\x00\x00" * 100)
        await runtime.response_task
        await runtime.close()

        self.assertEqual(len(audio_outputs), 1)
        self.assertNotIn("response.audio.delta", [event["type"] for event in events])
        self.assertEqual(session.audio_output_bytes, 0)
        self.assertEqual(providers.evaluations[0]["audio_output_bytes"], 0)

    async def test_websocket_default_emits_existing_base64_audio_delta(self):
        session, _ = VoiceSessionRegistry().create(principal="client", traceparent=None)
        providers = ImmediateProviders()
        events = []

        async def send_event(event):
            events.append(dict(event))

        runtime = RealtimeRuntime(
            session=session,
            provider=providers,
            metrics=VoiceMetrics(),
            send_event=send_event,
            parent_trace_context=extract_trace_context({}, session_id=session.id),
        )
        runtime.start_turn(b"\x00\x00" * 100)
        await runtime.response_task
        await runtime.close()

        audio = next(event for event in events if event["type"] == "response.audio.delta")
        self.assertEqual(audio["delta"], "A" * 64)
        self.assertEqual(audio["sample_rate"], 24_000)
        self.assertEqual(audio["channels"], 1)
        self.assertEqual(audio["text"], "Answer.")
        self.assertEqual(audio["generation_id"], 1)
        self.assertEqual(session.audio_output_bytes, 48)
if __name__ == "__main__":
    unittest.main()
