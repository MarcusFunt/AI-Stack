import io
import json
import tempfile
import unittest
import wave
from pathlib import Path

from stt.benchmark.datasets import DatasetAdapter, manifest
from stt.benchmark.datasets import synthetic


def _wav_bytes(duration_s=4.0):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x01\x00" * int(16000 * duration_s))
    return buffer.getvalue()


def _row(sample_id, num_speakers=3):
    speakers = ("SPK1", "SPK2", "SPK3")
    return {
        "id": sample_id,
        "audio": {"bytes": _wav_bytes()},
        "num_speakers": num_speakers,
        "duration": 4.0,
        "segments": json.dumps([
            {"start": index * 0.8, "end": index * 0.8 + 0.6,
             "speaker": speaker, "text": f"dansk ord {index}"}
            for index, speaker in enumerate(speakers[:num_speakers])
        ]),
        "sources": json.dumps(["coral", "nst_da", "fleurs"]),
    }


class SyntheticDatasetPreparationTests(unittest.TestCase):
    def test_adapter_implements_shared_contract_and_synthetic_class(self):
        adapter = synthetic.DiarizationK3Adapter()
        self.assertIsInstance(adapter, DatasetAdapter)
        self.assertEqual(adapter.spec.dataset, "diarization-k3")
        self.assertEqual(adapter.spec.dataset_class, "CONTROLLED-SYNTHETIC")

    def test_pinned_loader_filters_k3_converts_rows_and_records_provenance(self):
        rows = [_row("sample_00001"), _row("sample_00002"), _row("sample_00003", 2)]
        loader_calls = []

        def loader(repo_id, *, split, revision, cache_dir=None):
            loader_calls.append((repo_id, split, revision))
            return rows

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "prepared" / "diarization-k3"
            lock_path = root / "dataset-lock.json"
            manifest_path = synthetic.prepare_diarization_k3(
                output,
                lock_path=lock_path,
                dataset_loader=loader,
                revision_resolver=lambda repo_id: "a" * 40,
            )

            recordings = manifest.load_manifest(manifest_path)
            self.assertEqual([row["id"] for row in recordings], ["sample_00001", "sample_00002"])
            self.assertEqual(loader_calls, [
                ("syvai/danish-diarization-bench", "test", "a" * 40)
            ])
            self.assertEqual(recordings[0]["dataset_class"], "CONTROLLED-SYNTHETIC")
            self.assertEqual(recordings[0]["speaker_count"], 3)
            self.assertEqual(recordings[0]["metadata"]["source_revision"], "a" * 40)
            self.assertEqual(recordings[0]["metadata"]["reference_transform"], "hf-segments-v1")
            self.assertEqual(recordings[0]["metadata"]["source_dataset_families"], ["coral", "fleurs", "nst_da"])
            self.assertEqual(len(recordings[0]["segments"]), 3)
            self.assertEqual(recordings[0]["text"], "dansk ord 0 dansk ord 1 dansk ord 2")

            validation = json.loads((output / "dataset-validation.json").read_text(encoding="utf-8"))
            self.assertEqual(validation["source_rows"], 3)
            self.assertEqual(validation["prepared_k3_rows"], 2)
            self.assertEqual(validation["filtered_non_k3_rows"], 1)
            self.assertIn("diarization-k3", json.loads(lock_path.read_text(encoding="utf-8"))["datasets"])

    def test_existing_lock_revision_is_reused_without_resolving_latest(self):
        rows = [_row("sample_00001")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "prepared"
            lock_path = root / "dataset-lock.json"
            synthetic.prepare_diarization_k3(
                output,
                lock_path=lock_path,
                dataset_loader=lambda *args, **kwargs: rows,
                revision_resolver=lambda repo_id: "b" * 40,
            )
            calls = []
            synthetic.prepare_diarization_k3(
                output,
                lock_path=lock_path,
                dataset_loader=lambda *args, **kwargs: rows,
                revision_resolver=lambda repo_id: calls.append(repo_id),
            )
            self.assertEqual(calls, [])

    def test_explicit_revision_cannot_silently_replace_existing_lock(self):
        rows = [_row("sample_00001")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "prepared"
            lock_path = root / "dataset-lock.json"
            synthetic.prepare_diarization_k3(
                output,
                lock_path=lock_path,
                dataset_loader=lambda *args, **kwargs: rows,
                revision_resolver=lambda repo_id: "d" * 40,
            )
            with self.assertRaisesRegex(ValueError, "use a new lock file"):
                synthetic.prepare_diarization_k3(
                    output,
                    lock_path=lock_path,
                    revision="e" * 40,
                    dataset_loader=lambda *args, **kwargs: rows,
                )

    def test_k3_row_with_inconsistent_speaker_labels_fails_loudly(self):
        malformed = _row("sample_bad", 2)
        malformed["num_speakers"] = 3
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "three distinct speakers"):
                synthetic.prepare_diarization_k3(
                    root / "prepared",
                    lock_path=root / "dataset-lock.json",
                    dataset_loader=lambda *args, **kwargs: [malformed],
                    revision_resolver=lambda repo_id: "c" * 40,
                )


if __name__ == "__main__":
    unittest.main()
