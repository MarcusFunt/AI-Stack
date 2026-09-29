import unittest
from types import SimpleNamespace

from core.context import TraceContext
from core.invocation import InvocationOperation, InvocationSource, Modality
from gateway.adapters.openai_audio import OpenAIAudioAdapter


class OpenAIAudioAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = OpenAIAudioAdapter()
        self.trace = TraceContext(request_id="audio-1")

    def test_transcription_maps_upload_metadata_without_copying_audio_bytes(self):
        upload = SimpleNamespace(filename="sample.wav", content_type="audio/wav", size=1234)
        invocation = self.adapter.to_transcription({"file": upload, "language": "en", "response_format": "text"}, self.trace)

        self.assertEqual(invocation.operation, InvocationOperation.TRANSCRIBE)
        self.assertEqual(invocation.modality, {Modality.AUDIO})
        self.assertEqual(invocation.source, InvocationSource.OPENAI_AUDIO)
        self.assertEqual(invocation.trace_context.request_id, "audio-1")
        self.assertEqual(invocation.input.data["upload"]["filename"], "sample.wav")
        self.assertEqual(invocation.input.data["upload"]["size_bytes"], 1234)
        self.assertNotIn("audio bytes", repr(invocation))

    def test_speech_maps_text_and_response_options(self):
        invocation = self.adapter.to_speech({"input": "hello", "voice": "alloy", "speed": 1.25, "response_format": "wav"}, self.trace)

        self.assertEqual(invocation.operation, InvocationOperation.SYNTHESIZE)
        self.assertEqual(invocation.modality, {Modality.TEXT})
        self.assertEqual(invocation.input.text, "hello")
        self.assertEqual(invocation.options.response_format, "wav")
        self.assertEqual(invocation.options.data["voice"], "alloy")
        self.assertEqual(invocation.options.data["speed"], 1.25)

    def test_vision_maps_prompt_and_image_metadata(self):
        upload = SimpleNamespace(filename="image.png", content_type="image/png", size=88)
        invocation = self.adapter.to_vision({"image": upload, "prompt": "describe", "max_new_tokens": "12"}, self.trace)

        self.assertEqual(invocation.operation, InvocationOperation.ANALYZE)
        self.assertEqual(invocation.source, InvocationSource.OPENAI_VISION)
        self.assertEqual(invocation.modality, {Modality.IMAGE, Modality.TEXT})
        self.assertEqual(invocation.input.text, "describe")
        self.assertEqual(invocation.input.data["image"]["mime_type"], "image/png")


if __name__ == "__main__":
    unittest.main()
