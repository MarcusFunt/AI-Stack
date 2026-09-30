import json
import tempfile
import unittest
import wave
from pathlib import Path

from stt.benchmark.runner import load_manifest


class PublicManifestRunnerTests(unittest.TestCase):
    def test_runner_rejects_v2_reference_times_outside_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with wave.open(str(root / "clip.wav"), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(b"\x00\x00" * 16000)
            row = {
                "schema_version": 2,
                "id": "invalid-window",
                "dataset": "samtalebank-sam3",
                "dataset_class": "PRIMARY-INDEPENDENTISH",
                "audio": "clip.wav",
                "speaker_count": 3,
                "text": "Hej",
                "segments": [{"start": 0.0, "end": 2.0, "speaker": "A", "text": "Hej"}],
                "metadata": {
                    "source_hash": "a" * 64,
                    "transcript_hash": "b" * 64,
                    "source_url": "https://talkbank.org/samtale/access/Sam3.html",
                    "source_license": "CC-BY-NC-SA-3.0",
                },
            }
            path = root / "manifest.jsonl"
            path.write_text(json.dumps(row), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "outside audio duration"):
                load_manifest(path)


if __name__ == "__main__":
    unittest.main()
