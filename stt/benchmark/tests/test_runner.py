import json
import tempfile
import unittest
from pathlib import Path

from stt.benchmark.runner import load_manifest


class ManifestTests(unittest.TestCase):
    def test_manifest_resolves_audio_and_builds_reference_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sample.wav").touch()
            manifest = root / "manifest.jsonl"
            manifest.write_text(
                json.dumps(
                    {
                        "id": "sample",
                        "audio": "sample.wav",
                        "segments": [
                            {
                                "start": 0.0,
                                "end": 1.0,
                                "speaker": "A",
                                "text": "Hej",
                            },
                            {
                                "start": 1.0,
                                "end": 2.0,
                                "speaker": "B",
                                "text": "med dig",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            record = load_manifest(manifest)[0]
            self.assertEqual(record["reference_text"], "Hej med dig")
            self.assertEqual(record["speaker_count"], 3)
            self.assertTrue(Path(record["audio_path"]).is_absolute())

    def test_manifest_rejects_duplicate_recording_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.wav").touch()
            (root / "b.wav").touch()
            manifest = root / "manifest.jsonl"
            rows = [
                {"id": "duplicate", "audio": "a.wav", "text": "a"},
                {"id": "duplicate", "audio": "b.wav", "text": "b"},
            ]
            manifest.write_text(
                "\n".join(json.dumps(row) for row in rows),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate recording id"):
                load_manifest(manifest)

    def test_manifest_rejects_segment_without_speaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sample.wav").touch()
            manifest = root / "manifest.jsonl"
            manifest.write_text(
                json.dumps(
                    {
                        "audio": "sample.wav",
                        "segments": [{"start": 0.0, "end": 1.0, "text": "Hej"}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "speaker"):
                load_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
