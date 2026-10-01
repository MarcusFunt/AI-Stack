from __future__ import annotations

import asyncio
import importlib
import os
import unittest
import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import numpy as np
from fastapi.testclient import TestClient

from observability.propagation import extract_trace_context
from voice.metrics import VoiceMetrics
from voice.runtime import RealtimeRuntime
from voice.runtime import AudioOutput
from voice.session import VoiceSessionRegistry
from voice.turn_detection import TurnEvent
from voice.transports.small_webrtc import (
    PeerLifecycle,
    SmallWebRTCPeer,
    make_control_envelope,
    process_input_audio_frame,
    send_output_audio_frame,
)


class FakePeer:
    def __init__(self, *, on_closed, started=None, release=None, **_kwargs):
        self.on_closed = on_closed
        self.started = started
        self.release = release
        self.offer = None
        self.close_calls = 0
        self.close_reason = None

    async def accept_offer(self, offer):
        self.offer = dict(offer)
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        return {"sdp": "v=0\r\nt=0 0\r\n", "type": "answer", "pc_id": "private"}

    async def close(self, reason="closed"):
        if self.close_calls:
            return
        self.close_calls += 1
        self.close_reason = reason
        await self.on_closed(reason)


class GatedProviders:
    def __init__(self):
        self.synthesis_started = asyncio.Event()
        self.wait_for_release = asyncio.Event()
        self.close_calls = 0
        self.evaluations = []

    async def transcribe(self, _audio, *, traceparent=None):
        return {"text": "Question.", "segments": [{"text": "Question."}]}

    async def chat_deltas(self, _history, *, traceparent=None):
        yield "Answer."

    async def synthesize(self, _text, *, traceparent=None):
        self.synthesis_started.set()
        await self.wait_for_release.wait()
        return b"\x00\x00" * 24, 24_000, 1

    async def report_turn(self, event):
        self.evaluations.append(dict(event))

    async def close(self):
        self.close_calls += 1
        self.wait_for_release.set()


class CountingRuntime(RealtimeRuntime):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1
        await super().close()


def load_voice_app_module():
    with patch.dict(os.environ, {"AI_API_KEY": "voice-test-key"}):
        return importlib.import_module("voice.app")


def new_app(peer_factory, *, metrics=None, session_max_seconds=None):
    voice_app = load_voice_app_module()
    options = {}
    if session_max_seconds is not None:
        options["session_max_seconds"] = session_max_seconds
    return voice_app.create_app(
        api_key="voice-test-key",
        provider_factory=lambda **_kwargs: GatedProviders(),
        metrics=metrics or VoiceMetrics(),
        webrtc_factory=peer_factory,
        **options,
    )


def create_session(client):
    response = client.post(
        "/sessions",
        headers={"Authorization": "Bearer voice-test-key"},
        json={"language": "en"},
    )
    if response.status_code != 200:
        raise AssertionError(response.text)
    return response.json()


def offer_request(client, session, *, ticket=None, headers=None, sdp="v=0\r\nt=0 0\r\n"):
    request_headers = {
        "Authorization": "Bearer voice-test-key",
        "X-Voice-Session-Ticket": ticket or session["client_secret"]["value"],
    }
    if headers:
        request_headers.update(headers)
    return client.post(
        f"/sessions/{session['id']}/offer",
        headers=request_headers,
        json={"type": "offer", "sdp": sdp},
    )


class WebRTCOfferRouteTests(unittest.TestCase):
    def test_webrtc_peer_closes_and_releases_session_at_configured_maximum_lifetime(self):
        peers = []
        metrics = VoiceMetrics()
        app = new_app(
            lambda **kwargs: peers.append(FakePeer(**kwargs)) or peers[-1],
            metrics=metrics,
            session_max_seconds=0.02,
        )
        with TestClient(app) as client:
            session = create_session(client)
            response = offer_request(client, session)
            self.assertEqual(response.status_code, 200)
            deadline = time.monotonic() + 1
            while peers[0].close_calls == 0 and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertEqual(peers[0].close_calls, 1)
        self.assertEqual(peers[0].close_reason, "timeout")
        self.assertEqual(metrics.active_sessions, 0)
        self.assertNotIn(session["id"], app.state.webrtc_peers)

    def test_offer_requires_api_key_and_one_use_ticket(self):
        peers = []
        app = new_app(lambda **kwargs: peers.append(FakePeer(**kwargs)) or peers[-1])
        with TestClient(app) as client:
            session = create_session(client)
            path = f"/sessions/{session['id']}/offer"
            body = {"type": "offer", "sdp": "v=0\r\nt=0 0\r\n"}

            missing_auth = client.post(
                path,
                headers={"X-Voice-Session-Ticket": session["client_secret"]["value"]},
                json=body,
            )
            bad_ticket = client.post(
                path,
                headers={
                    "Authorization": "Bearer voice-test-key",
                    "X-Voice-Session-Ticket": "wrong-ticket",
                },
                json=body,
            )
            success = offer_request(client, session)
            replay = offer_request(client, session)

        self.assertEqual(missing_auth.status_code, 401)
        self.assertEqual(bad_ticket.status_code, 401)
        self.assertEqual(success.status_code, 200)
        self.assertEqual(replay.status_code, 401)
        self.assertEqual(len(peers), 1)
        self.assertEqual(success.json(), {"sdp": "v=0\r\nt=0 0\r\n", "type": "answer"})

    def test_replayed_and_concurrent_offers_create_at_most_one_peer(self):
        async def run_case():
            started = asyncio.Event()
            release = asyncio.Event()
            peers = []

            def factory(**kwargs):
                peer = FakePeer(started=started, release=release, **kwargs)
                peers.append(peer)
                return peer

            app = new_app(factory)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://voice") as client:
                session = await client.post(
                    "/sessions",
                    headers={"Authorization": "Bearer voice-test-key"},
                    json={"language": "en"},
                )
                session_data = session.json()
                first_task = asyncio.create_task(offer_request(client, session_data))
                await asyncio.wait_for(started.wait(), timeout=1)
                second = await offer_request(client, session_data)
                release.set()
                first = await asyncio.wait_for(first_task, timeout=1)

            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 401)
            self.assertEqual(len(peers), 1)

        asyncio.run(run_case())

    def test_offer_rejects_invalid_type_and_sdp_over_128_kib(self):
        peers = []
        app = new_app(lambda **kwargs: peers.append(FakePeer(**kwargs)) or peers[-1])
        with TestClient(app) as client:
            session = create_session(client)
            path = f"/sessions/{session['id']}/offer"
            headers = {
                "Authorization": "Bearer voice-test-key",
                "X-Voice-Session-Ticket": session["client_secret"]["value"],
            }
            invalid_type = client.post(
                path, headers=headers, json={"type": "answer", "sdp": "v=0"}
            )
            oversize = client.post(
                path,
                headers=headers,
                json={"type": "offer", "sdp": "x" * (128 * 1024)},
            )
            valid = client.post(
                path, headers=headers, json={"type": "offer", "sdp": "v=0\r\nt=0 0\r\n"}
            )

        self.assertEqual(invalid_type.status_code, 400)
        self.assertEqual(oversize.status_code, 413)
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(len(peers), 1)


class WebRTCPeerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_peer_setup_can_close_reentrantly_only_once(self):
        session, _ = VoiceSessionRegistry().create(principal="client", traceparent=None)
        closed_reasons = []
        disconnect_calls = 0

        async def on_closed(reason):
            closed_reasons.append(reason)

        peer = SmallWebRTCPeer(
            session=session,
            provider_factory=lambda **_kwargs: GatedProviders(),
            metrics=VoiceMetrics(),
            parent_trace_context=extract_trace_context({}, session_id=session.id),
            on_closed=on_closed,
        )

        class Connection:
            async def disconnect(self):
                nonlocal disconnect_calls
                disconnect_calls += 1
                await peer.close("client")

        peer.connection = Connection()
        await peer.close("failed")

        self.assertEqual(disconnect_calls, 1)
        self.assertEqual(closed_reasons, ["failed"])

    async def test_speech_start_flushes_already_queued_webrtc_audio(self):
        session, _ = VoiceSessionRegistry().create(principal="client", traceparent=None)
        providers = GatedProviders()
        flush_calls = []

        async def send_event(_event):
            return None

        async def clear_audio():
            flush_calls.append(session.generation_counter)

        runtime = RealtimeRuntime(
            session=session,
            provider=providers,
            metrics=VoiceMetrics(),
            send_event=send_event,
            parent_trace_context=extract_trace_context({}, session_id=session.id),
            clear_audio=clear_audio,
        )
        runtime.start_turn(b"\x00\x00" * 100)
        await asyncio.wait_for(providers.synthesis_started.wait(), timeout=1)

        await runtime.handle_turn_event(TurnEvent(kind="speech_started"))

        self.assertEqual(flush_calls, [session.generation_counter])
        self.assertEqual(session.generation_counter, 2)
        await runtime.close()

    async def test_completed_response_cancel_still_flushes_transport_queue(self):
        session, _ = VoiceSessionRegistry().create(principal="client", traceparent=None)
        flush_calls = 0

        async def send_event(_event):
            return None

        async def clear_audio():
            nonlocal flush_calls
            flush_calls += 1

        runtime = RealtimeRuntime(
            session=session,
            provider=GatedProviders(),
            metrics=VoiceMetrics(),
            send_event=send_event,
            parent_trace_context=extract_trace_context({}, session_id=session.id),
            clear_audio=clear_audio,
        )
        runtime.response_state = {"terminal_status": "completed"}

        await runtime.cancel_response("client_cancelled")

        self.assertEqual(flush_calls, 1)
        await runtime.close()

    async def test_peer_disconnect_cancels_active_generation_and_closes_resources_once(self):
        session, _ = VoiceSessionRegistry().create(principal="client", traceparent=None)
        providers = GatedProviders()
        events = []

        async def send_event(event):
            events.append(dict(event))

        runtime = CountingRuntime(
            session=session,
            provider=providers,
            metrics=VoiceMetrics(),
            send_event=send_event,
            parent_trace_context=extract_trace_context({}, session_id=session.id),
        )
        runtime.start_turn(b"\x00\x00" * 100)
        await asyncio.wait_for(providers.synthesis_started.wait(), timeout=1)
        peer_close_calls = 0

        async def close_peer():
            nonlocal peer_close_calls
            peer_close_calls += 1

        lifecycle = PeerLifecycle(
            runtime=runtime,
            provider=providers,
            close_peer=close_peer,
        )
        await asyncio.gather(lifecycle.close("client"), lifecycle.close("client"))

        self.assertEqual(runtime.close_calls, 1)
        self.assertEqual(providers.close_calls, 1)
        self.assertEqual(peer_close_calls, 1)
        self.assertIsNone(runtime.response_task)
        self.assertFalse(session.generation_is_current(1))
        self.assertEqual(providers.evaluations[0]["audio_output_bytes"], 0)

    async def test_audio_frames_feed_runtime_and_control_envelope_keeps_generation_identity(self):
        event = TurnEvent(kind="speech_started")

        class Detector:
            def __init__(self):
                self.audio = None

            def feed(self, audio):
                self.audio = audio
                return [event]

        class Runtime:
            def __init__(self):
                self.events = []

            async def handle_turn_event(self, received):
                self.events.append(received)

        source = np.column_stack(
            (np.full(480, 1000, dtype=np.int16), np.full(480, 3000, dtype=np.int16))
        )
        frame = SimpleNamespace(audio=source.tobytes(), sample_rate=48_000, num_channels=2)
        detector = Detector()
        runtime = Runtime()
        session, _ = VoiceSessionRegistry().create(principal="client", traceparent=None)
        metrics = VoiceMetrics()

        converted = await process_input_audio_frame(
            frame,
            detector=detector,
            runtime=runtime,
            session=session,
            metrics=metrics,
        )
        envelope = make_control_envelope({
            "type": "response.output_text.delta",
            "session_id": session.id,
            "turn_id": "turn-fixed",
            "response_id": "response-fixed",
            "generation_id": 17,
            "delta": "Hello.",
        })

        self.assertTrue(converted)
        self.assertEqual(len(detector.audio), 160 * 2)
        self.assertEqual(int(np.frombuffer(detector.audio, dtype=np.int16)[80]), 2000)
        self.assertEqual(runtime.events, [event])
        self.assertEqual(session.audio_input_bytes, 160 * 2)
        self.assertEqual(envelope["protocol"], "ai-stack.voice.v1")
        self.assertEqual(envelope["turn_id"], "turn-fixed")
        self.assertEqual(envelope["response_id"], "response-fixed")
        self.assertEqual(envelope["generation_id"], 17)
        self.assertEqual(envelope["delta"], "Hello.")
        with self.assertRaises(ValueError):
            make_control_envelope({"type": "response.audio.delta", "delta": "AAAA"})

    async def test_output_audio_is_converted_to_aligned_webrtc_frames(self):
        class Transport:
            def __init__(self):
                self.frames = []

            async def send_audio(self, frame):
                self.frames.append(frame)

        source = np.full(240, 1000, dtype=np.int16).tobytes()
        output = AudioOutput(
            pcm=source,
            sample_rate=24_000,
            channels=1,
            text="Hello.",
            response_id="response-fixed",
            turn_id="turn-fixed",
            generation_id=17,
        )
        metrics = VoiceMetrics()
        transport = Transport()

        accepted = await send_output_audio_frame(
            output,
            transport=transport,
            metrics=metrics,
            frame_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        )

        self.assertTrue(accepted)
        self.assertEqual(len(transport.frames), 1)
        self.assertEqual(transport.frames[0].sample_rate, 48_000)
        self.assertEqual(transport.frames[0].num_channels, 1)
        self.assertEqual(len(transport.frames[0].audio) % 960, 0)
        self.assertEqual(len(transport.frames[0].audio), 960)
        self.assertEqual(metrics.webrtc_audio_bytes["output"], 960)
        self.assertEqual(metrics.webrtc_conversion_counts["output"], 1)


if __name__ == "__main__":
    unittest.main()
