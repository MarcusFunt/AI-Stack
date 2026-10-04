from __future__ import annotations

import io
import wave
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from stt import app as stt_service


def sample_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(b"\x00\x00" * 16_000)
    return output.getvalue()


class FakeWhisperModel:
    def __init__(self) -> None:
        self.kwargs = None

    def transcribe(self, _audio, **kwargs):
        self.kwargs = kwargs
        words = [
            SimpleNamespace(start=0.1, end=0.4, word=" Hello"),
            SimpleNamespace(start=0.4, end=0.8, word=" world."),
        ]
        segments = [SimpleNamespace(start=0.1, end=0.8, text=" Hello world.", words=words)]
        return iter(segments), SimpleNamespace(language="en", language_probability=0.99)


class SpeechToTextAPITests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(stt_service.app)
        self.model = FakeWhisperModel()
        self.get_model = patch.object(stt_service, "get_model", return_value=self.model)
        self.get_model.start()
        self.addCleanup(self.get_model.stop)

    def test_transcription_forwards_prompt_temperature_and_word_timestamps(self):
        response = self.client.post(
            "/v1/audio/transcriptions",
            files={"file": ("sample.wav", sample_wav(), "audio/wav")},
            data={
                "language": "en",
                "prompt": "Names: Alex and Morgan.",
                "temperature": "0.2",
                "timestamp_granularities[]": ["segment", "word"],
                "response_format": "verbose_json",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.model.kwargs["initial_prompt"], "Names: Alex and Morgan.")
        self.assertEqual(self.model.kwargs["temperature"], 0.2)
        self.assertTrue(self.model.kwargs["word_timestamps"])
        self.assertEqual(response.json()["segments"][0]["words"][1]["word"], " world.")

    def test_transcription_emits_text_srt_and_vtt_formats(self):
        expected = {
            "text": "Hello world.",
            "srt": "1\n00:00:00,100 --> 00:00:00,800\nHello world.\n",
            "vtt": "WEBVTT\n\n1\n00:00:00.100 --> 00:00:00.800\nHello world.\n",
        }
        for response_format, text in expected.items():
            with self.subTest(response_format=response_format):
                response = self.client.post(
                    "/v1/audio/transcriptions",
                    files={"file": ("sample.wav", sample_wav(), "audio/wav")},
                    data={"response_format": response_format},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text, text)


if __name__ == "__main__":
    unittest.main()
