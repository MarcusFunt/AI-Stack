from __future__ import annotations

import asyncio
import importlib
import os
import struct
import sys
import unittest
import wave
from io import BytesIO
from unittest.mock import patch

from fastapi.testclient import TestClient


class FakeProviders:
    def __init__(self, **kwargs):
        self.transcription_count = 0
        self.chat_histories = []
        self.closed = False
        self.evaluation_events = []
        self.traceparents = []

    async def transcribe(self, wav_audio: bytes, *, traceparent: str | None = None) -> dict:
        self.traceparents.append(("stt", traceparent))
        self.transcription_count += 1
        with wave.open(BytesIO(wav_audio), "rb") as audio:
            assert audio.getframerate() == 16_000
            assert audio.getnchannels() == 1
            assert audio.getsampwidth() == 2
        text = f"Question {self.transcription_count}."
        return {"text": text, "segments": [{"text": text}]}

    async def chat_deltas(self, history, *, traceparent: str | None = None):
        self.traceparents.append(("llm", traceparent))
        self.chat_histories.append([dict(message) for message in history])
        if len(self.chat_histories) == 1:
            yield "First sentence. "
            await asyncio.sleep(2)
            yield "Second sentence."
        else:
            yield "Next answer."

    async def synthesize(self, text: str, *, traceparent: str | None = None):
        self.traceparents.append(("tts", traceparent))
        return b"\x00\x00" * 2_400, 24_000, 1

    async def close(self):
        self.closed = True

    async def report_turn(self, event):
        self.evaluation_events.append(dict(event))
        return True


class RealtimeVoiceAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = patch.dict(os.environ, {"AI_API_KEY": "voice-test-key"})
        cls.environment.start()
        cls.voice = importlib.import_module("voice.app")

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("voice.app", None)
        cls.environment.stop()

    def setUp(self):
        self.providers = []

        def make_providers(**kwargs):
            provider = FakeProviders(**kwargs)
            self.providers.append(provider)
            return provider

        app = self.voice.create_app(api_key="voice-test-key", provider_factory=make_providers)
        self.client = TestClient(app)

    @staticmethod
    def _pcm(value: int) -> bytes:
        return struct.pack("<320h", *([value] * 320))

    def _send_turn(self, websocket, value: int = 900):
        websocket.send_bytes(self._pcm(value))
        for _ in range(33):
            websocket.send_bytes(self._pcm(0))

    def test_session_auth_english_configuration_and_single_use_ticket(self):
        unauthorized = self.client.post("/sessions", json={})
        self.assertEqual(unauthorized.status_code, 401)

        unsupported = self.client.post(
            "/sessions",
            headers={"Authorization": "Bearer voice-test-key"},
            json={"language": "da"},
        )
        self.assertEqual(unsupported.status_code, 400)

        created = self.client.post(
            "/sessions",
            headers={"Authorization": "Bearer voice-test-key"},
            json={"language": "en"},
        )
        self.assertEqual(created.status_code, 200)
        session = created.json()
        self.assertEqual(session["language"], "en")
        self.assertEqual(session["input_sample_rate"], 16_000)
        with self.client.websocket_connect(
            f"/ws/{session['id']}",
            subprotocols=["ai-stack.voice.v1", f"ai-stack.ticket.{session['client_secret']['value']}"],
        ) as websocket:
            self.assertEqual(websocket.receive_json()["type"], "session.created")
            self.assertEqual(websocket.receive_json()["type"], "session.configured")
            websocket.send_json({"type": "session.close"})
            self.assertEqual(websocket.receive_json()["type"], "session.closed")

        with self.assertRaises(Exception):
            with self.client.websocket_connect(
                f"/ws/{session['id']}",
                subprotocols=["ai-stack.voice.v1", f"ai-stack.ticket.{session['client_secret']['value']}"],
            ):
                pass

    def test_streaming_turn_barge_in_and_presented_history(self):
        session = self.client.post(
            "/sessions",
            headers={"Authorization": "Bearer voice-test-key"},
            json={"language": "en"},
        ).json()
        with self.client.websocket_connect(
            f"/ws/{session['id']}",
            subprotocols=["ai-stack.voice.v1", f"ai-stack.ticket.{session['client_secret']['value']}"],
        ) as websocket:
            websocket.receive_json()
            websocket.receive_json()
            websocket.send_json({"type": "session.configure", "language": "en"})
            self.assertEqual(websocket.receive_json()["type"], "session.configured")
            self._send_turn(websocket)

            first_audio = None
            for _ in range(12):
                event = websocket.receive_json()
                if event["type"] == "response.audio.delta":
                    first_audio = event
                    break
            self.assertIsNotNone(first_audio)
            self.assertEqual(first_audio["sample_rate"], 24_000)
            self.assertTrue(first_audio["delta"])

            websocket.send_bytes(self._pcm(900))
            for _ in range(33):
                websocket.send_bytes(self._pcm(0))

            received = []
            for _ in range(30):
                event = websocket.receive_json()
                received.append(event)
                if event["type"] == "response.done":
                    break

            event_types = [event["type"] for event in received]
            self.assertIn("response.cancelled", event_types)
            self.assertIn("conversation.item.truncated", event_types)
            self.assertIn("conversation.item.input_audio_transcription.completed", event_types)
            self.assertEqual(event_types[-1], "response.done")

        provider = self.providers[0]
        self.assertEqual(len(provider.chat_histories), 2)
        assistant_messages = [
            message["content"]
            for message in provider.chat_histories[1]
            if message["role"] == "assistant"
        ]
        self.assertEqual(assistant_messages, ["First sentence."])
        self.assertTrue(provider.closed)
        self.assertEqual(
            [event["event_type"] for event in provider.evaluation_events],
            ["voice.turn.interrupted", "voice.turn.completed"],
        )
        self.assertTrue(provider.evaluation_events[0]["truncation_recorded"])
        self.assertNotIn("transcript", provider.evaluation_events[0])
        self.assertNotIn("audio", provider.evaluation_events[0])

    def test_voice_stage_spans_preserve_and_forward_the_inbound_trace_id(self):
        parent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        session = self.client.post(
            "/sessions",
            headers={"Authorization": "Bearer voice-test-key", "traceparent": parent},
            json={"language": "en"},
        ).json()
        with self.client.websocket_connect(
            f"/ws/{session['id']}",
            subprotocols=["ai-stack.voice.v1", f"ai-stack.ticket.{session['client_secret']['value']}"],
        ) as websocket:
            websocket.receive_json()
            websocket.receive_json()
            self._send_turn(websocket)
            for _ in range(20):
                if websocket.receive_json()["type"] == "response.done":
                    break

        traceparents = dict(self.providers[0].traceparents)
        self.assertTrue({"stt", "llm", "tts"} <= traceparents.keys())
        for traceparent in traceparents.values():
            self.assertTrue(traceparent.startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-"))
            self.assertNotEqual(traceparent.split("-")[2], "00f067aa0ba902b7")


if __name__ == "__main__":
    unittest.main()
