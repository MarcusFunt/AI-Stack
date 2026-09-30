import io
import json
import tempfile
import unittest
import wave
from pathlib import Path

from stt.benchmark.datasets import manifest
from stt.benchmark.datasets import speech_recognition


def _audio():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 16000)
    return {"bytes": buffer.getvalue()}


class HuggingFaceAsrPreparationTests(unittest.TestCase):
    def test_all_suites_load_only_expected_test_configs_and_emit_v2(self):
        specifications = [
            (speech_recognition.CoRalConversationTestAdapter(), "CoRal-project/coral-v3", "conversational"),
            (speech_recognition.NstDanishTestAdapter(), "alexandrainst/nst-da", None),
            (speech_recognition.FleursDanishTestAdapter(), "google/fleurs", "da_dk"),
        ]
        for index, (adapter, repo_id, config) in enumerate(specifications):
            with self.subTest(dataset=adapter.spec.dataset), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                row = {
                    "id": f"sample-{index}",
                    "audio": _audio(),
                    "text": "En dansk sætning med æøå.",
                    "transcription": "normalized text",
                    "raw_transcription": "En dansk sætning med æøå.",
                    "speaker_id": 42,
                    "age": 54,
                    "sex": "Female",
                    "gender": "female",
                    "dialect": "Sønderjysk",
                }
                calls = []

                def loader(loaded_repo, *args, **kwargs):
                    calls.append((loaded_repo, args, kwargs))
                    return [row]

                output = root / "prepared" / adapter.spec.dataset
                path = adapter.prepare(
                    None,
                    output,
                    lock_path=root / "dataset-lock.json",
                    dataset_loader=loader,
                    revision_resolver=lambda _: "f" * 40,
                )
                prepared = manifest.load_manifest(path)
                self.assertEqual(len(prepared), 1)
                self.assertEqual(prepared[0]["dataset_class"], "HELD-OUT-IN-DOMAIN")
                self.assertEqual(prepared[0]["text"], "En dansk sætning med æøå.")
                self.assertEqual(prepared[0]["segments"], [])
                self.assertEqual(prepared[0]["metadata"]["source_revision"], "f" * 40)
                self.assertEqual(prepared[0]["metadata"]["source_license"], adapter.spec.license)
                self.assertEqual(prepared[0]["metadata"]["bootstrap_group"], "42")
                self.assertEqual(prepared[0]["metadata"]["bootstrap_group_type"], "speaker")
                if adapter.spec.dataset == "nst-da-test":
                    self.assertEqual(prepared[0]["metadata"]["test_split_status"], "utterance-held-out")
                    self.assertEqual(prepared[0]["metadata"]["speaker_split_relation"], "speaker-overlapping")
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0], repo_id)
                self.assertEqual(calls[0][1], ((config,) if config else ()))
                self.assertEqual(calls[0][2]["split"], "test")
                self.assertEqual(calls[0][2]["revision"], "f" * 40)

    def test_cooral_metadata_summary_marks_small_groups_insufficient(self):
        adapter = speech_recognition.CoRalConversationTestAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter.prepare(
                None,
                root / "prepared",
                lock_path=root / "dataset-lock.json",
                dataset_loader=lambda *args, **kwargs: [{
                    "id_recording": "record-1",
                    "audio": _audio(),
                    "text": "kort reference",
                    "age": 30,
                    "gender": "female",
                    "dialect": "Fynsk",
                }],
                revision_resolver=lambda _: "a" * 40,
            )
            summary = json.loads((root / "prepared" / "strata-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["minimum_reference_words"], 1000)
            self.assertEqual(summary["strata"]["dialect"]["Fynsk"]["status"], "insufficient_n")
            self.assertEqual(summary["strata"]["age_group"]["25-49"]["reference_words"], 2)

    def test_missing_speaker_id_uses_recording_as_bootstrap_group(self):
        adapter = speech_recognition.NstDanishTestAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = adapter.prepare(
                None,
                root / "prepared",
                lock_path=root / "dataset-lock.json",
                dataset_loader=lambda *args, **kwargs: [{
                    "id_recording": "independent-clip",
                    "audio": _audio(),
                    "text": "En test reference.",
                }],
                revision_resolver=lambda _: "c" * 40,
            )
            prepared = manifest.load_manifest(path)
        self.assertEqual(prepared[0]["metadata"]["bootstrap_group"], "independent-clip")
        self.assertEqual(prepared[0]["metadata"]["bootstrap_group_type"], "recording")

    def test_blank_text_is_rejected_before_manifest_is_written(self):
        adapter = speech_recognition.NstDanishTestAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "reference text is empty"):
                adapter.prepare(
                    None,
                    root / "prepared",
                    lock_path=root / "dataset-lock.json",
                    dataset_loader=lambda *args, **kwargs: [{"id": "bad", "audio": _audio(), "text": " "}],
                    revision_resolver=lambda _: "b" * 40,
                )


if __name__ == "__main__":
    unittest.main()
