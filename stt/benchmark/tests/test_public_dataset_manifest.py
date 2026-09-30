import json
import tempfile
import unittest
import wave
from pathlib import Path

from stt.benchmark.datasets import manifest


def _write_wav(path: Path, duration_s: float = 2.0) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * int(16000 * duration_s))


def _row() -> dict:
    return {
        "schema_version": 2,
        "id": "sample-1",
        "dataset": "samtalebank-sam3",
        "dataset_class": "PRIMARY-INDEPENDENTISH",
        "source_recording": "recording-a",
        "audio": "clip.wav",
        "speaker_count": 3,
        "text": "Hej med jer.",
        "segments": [
            {"start": 0.1, "end": 0.7, "speaker": "A", "text": "Hej"},
            {"start": 0.8, "end": 1.2, "speaker": "B", "text": "med"},
            {"start": 1.3, "end": 1.8, "speaker": "C", "text": "jer"},
        ],
        "metadata": {
            "source_license": "CC-BY-NC-SA-3.0",
            "source_url": "https://talkbank.org/samtale/access/Sam3.html",
            "source_hash": "a" * 64,
            "transcript_hash": "b" * 64,
            "reference_transform": "talkbank-ca-v1",
        },
    }


class PublicManifestTests(unittest.TestCase):
    def test_v2_manifest_round_trips_metadata_and_validates_audio(self):
        self.assertTrue(callable(getattr(manifest, "write_manifest", None)))
        self.assertTrue(callable(getattr(manifest, "load_manifest", None)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_wav(root / "clip.wav")
            path = root / "manifest.jsonl"
            manifest.write_manifest(path, [_row()])
            loaded = manifest.load_manifest(path)
            self.assertEqual(loaded[0]["dataset_class"], "PRIMARY-INDEPENDENTISH")
            self.assertEqual(loaded[0]["metadata"]["source_hash"], "a" * 64)
            self.assertEqual(loaded[0]["segments"][1]["speaker"], "B")

    def test_validation_reports_recordings_segments_and_reference_words(self):
        self.assertTrue(callable(getattr(manifest, "validate_manifest", None)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_wav(root / "clip.wav")
            report = manifest.validate_manifest([_row()], audio_root=root)
            self.assertEqual(report["recording_count"], 1)
            self.assertEqual(report["segment_count"], 3)
            self.assertEqual(report["reference_word_count"], 3)

    def test_validation_rejects_segments_outside_audio_duration(self):
        self.assertTrue(callable(getattr(manifest, "validate_manifest", None)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_wav(root / "clip.wav")
            row = _row()
            row["segments"][0]["end"] = 3.0
            with self.assertRaisesRegex(ValueError, "outside audio duration"):
                manifest.validate_manifest([row], audio_root=root)

    def test_validation_requires_public_source_and_transcript_hashes(self):
        self.assertTrue(callable(getattr(manifest, "validate_manifest", None)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_wav(root / "clip.wav")
            row = _row()
            row["metadata"].pop("transcript_hash")
            with self.assertRaisesRegex(ValueError, "transcript_hash"):
                manifest.validate_manifest([row], audio_root=root)

    def test_schema_v1_private_manifest_remains_loadable(self):
        self.assertTrue(callable(getattr(manifest, "load_manifest", None)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_wav(root / "clip.wav")
            path = root / "manifest.jsonl"
            path.write_text(
                json.dumps({"schema_version": 1, "id": "private", "audio": "clip.wav", "text": "Hej"}),
                encoding="utf-8",
            )
            loaded = manifest.load_manifest(path)
            self.assertEqual(loaded[0]["id"], "private")
            self.assertEqual(loaded[0]["schema_version"], 1)

    def test_duplicate_manifest_ids_are_rejected(self):
        self.assertTrue(callable(getattr(manifest, "validate_manifest", None)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_wav(root / "clip.wav")
            first = _row()
            second = {**_row(), "audio": "clip.wav"}
            with self.assertRaisesRegex(ValueError, "duplicate manifest id"):
                manifest.validate_manifest([first, second], audio_root=root)


if __name__ == "__main__":
    unittest.main()
