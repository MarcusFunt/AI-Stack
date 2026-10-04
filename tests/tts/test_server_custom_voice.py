from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


HAS_QWEN_TTS = importlib.util.find_spec("qwen_tts") is not None
VOICE_IDS = [
    "Aiden",
    "Ryan",
    "Vivian",
    "Serena",
    "Uncle_Fu",
    "Dylan",
    "Eric",
    "Ono_Anna",
    "Sohee",
]


@unittest.skipUnless(HAS_QWEN_TTS, "run inside the Qwen TTS runtime image")
class CustomVoiceServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if "TTS_SERVER_PATH" in os.environ:
            server_path = Path(os.environ["TTS_SERVER_PATH"])
        else:
            server_path = Path(__file__).resolve().parents[2] / "tts" / "server.py"
        spec = importlib.util.spec_from_file_location("tts_server_under_test", server_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"could not load TTS server from {server_path}")
        cls.server = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.server
        spec.loader.exec_module(cls.server)

    def setUp(self):
        server = self.server
        server.model = None
        server.model_type = None
        server.voice_prompt = None
        server.model_error = None
        if hasattr(server, "default_voice"):
            server.default_voice = "qwen-default"
        if hasattr(server, "supported_speakers"):
            server.supported_speakers = []

    def make_custom_voice_model(self):
        model = SimpleNamespace(
            model=SimpleNamespace(
                tts_model_type="custom_voice",
                tts_model_size="1b7",
                tokenizer_type="qwen3_tts_tokenizer_12hz",
            )
        )
        model.get_supported_speakers = Mock(return_value=VOICE_IDS)
        model.create_voice_clone_prompt = Mock(
            side_effect=ValueError("CustomVoice does not support clone prompts")
        )
        model.generate_custom_voice = Mock(return_value=([b"waveform"], 24000))
        model.generate_voice_clone = Mock(return_value=([b"waveform"], 24000))
        return model

    def test_custom_voice_model_loads_without_creating_a_clone_prompt(self):
        server = self.server
        expected_model = self.make_custom_voice_model()
        with (
            patch.object(server.torch.cuda, "is_available", return_value=True),
            patch.object(server.torch.cuda, "get_device_name", return_value="test GPU"),
            patch.object(server.Qwen3TTSModel, "from_pretrained", return_value=expected_model),
        ):
            server.load_model()

        self.assertIs(server.model, expected_model)
        self.assertIsNone(server.voice_prompt)
        self.assertIsNone(server.model_error)
        self.assertEqual(set(server.supported_speakers), set(VOICE_IDS))
        self.assertEqual(server.voices(), {"voices": VOICE_IDS})
        expected_model.create_voice_clone_prompt.assert_not_called()

    def test_custom_voice_request_keeps_the_selected_speaker_and_instruction(self):
        instruction = "Speak with delighted excitement and a rising final inflection."
        request = self.server.SpeechRequest(
            input="We have wonderful news.",
            voice="Ryan",
            language="English",
            instruct=instruction,
            response_format="wav",
        )

        self.assertEqual(request.voice, "Ryan")
        self.assertEqual(getattr(request, "instruct", None), instruction)

    def test_custom_voice_synthesis_forwards_speaker_and_instruction(self):
        server = self.server
        model = self.make_custom_voice_model()
        server.model = model
        server.model_type = "custom_voice"
        server.voice_prompt = object()
        server.supported_speakers = VOICE_IDS
        request = server.SpeechRequest(
            input="We have wonderful news.",
            voice="Ryan",
            language="English",
            instruct="Speak with delighted excitement.",
            response_format="wav",
        )

        with patch.object(server, "encode_audio", return_value=(b"wav", "audio/wav")):
            audio = server.synthesize(request)

        self.assertEqual(audio, (b"wav", "audio/wav"))
        model.generate_custom_voice.assert_called_once_with(
            text="We have wonderful news.",
            speaker="Ryan",
            language="English",
            instruct="Speak with delighted excitement.",
        )
        model.generate_voice_clone.assert_not_called()
