import json
import tempfile
import unittest
from pathlib import Path

from stt.benchmark.analysis.report import (
    _merge_runs,
    _recommend_for_suite,
    generate_report,
)
from stt.benchmark.runner import _run_reference_diarization


class ReviewFindingTests(unittest.TestCase):
    def test_single_speaker_only_suite_skips_diarizer_construction(self):
        class ForbiddenDiarizer:
            def __init__(self, **kwargs):
                raise AssertionError("single-speaker suite should not load Nemotron")

        result = _run_reference_diarization(
            [{"id": "clip", "audio_path": "clip.wav", "segments": []}],
            {"clip": 1.0},
            None,
            diarizer_factory=ForbiddenDiarizer,
        )
        self.assertIsNone(result["revision"])
        self.assertEqual(result["predicted_by_id"], {})
        self.assertEqual(result["der_by_id"], {})

    def test_mixed_suite_only_diarizes_recordings_with_reference_segments(self):
        called = []

        class FakeDiarizer:
            revision = "a" * 40

            def __init__(self, **kwargs):
                pass

            def diarize(self, path):
                called.append(path)
                return []

            def unload(self):
                pass

        records = [
            {"id": "clip", "audio_path": "clip.wav", "segments": []},
            {"id": "conversation", "audio_path": "conversation.wav",
             "segments": [{"start": 0.0, "end": 0.5, "speaker": "A"}]},
        ]
        result = _run_reference_diarization(
            records, {"clip": 1.0, "conversation": 1.0}, None,
            diarizer_factory=FakeDiarizer,
        )
        self.assertEqual(called, ["conversation.wav"])
        self.assertEqual(set(result["predicted_by_id"]), {"conversation"})
        self.assertEqual(result["revision"], "a" * 40)

    def test_sam3_decision_uses_group_interval_when_available(self):
        result = {
            "dataset": {"name": "fleurs-da-dk-test"},
            "models": {
                "edda": {"content_wer": 0.1, "speaker_attributed_wer": 0.2},
                "hviske": {"content_wer": 0.2, "speaker_attributed_wer": 0.2},
            }
        }
        pairs = [{
            "model_a": "edda", "model_b": "hviske",
            "ci95_low": -0.2, "ci95_high": -0.1,
            "group_ci95_low": -0.1, "group_ci95_high": 0.02,
        }]
        decision = _recommend_for_suite(result, pairs, label="Sam3")
        self.assertIn("no unique winner", decision)
        self.assertIn("recording-group", decision)

    def test_merge_rejects_same_ids_from_a_different_dataset_revision(self):
        result = {
            "evaluation_protocol": {"protocol_id": "test-v1"},
            "evaluation_protocol_sha256": "f" * 64,
            "dataset": {"name": "sample", "class": "HELD-OUT-IN-DOMAIN",
                        "source_revisions": ["a" * 40]},
            "recordings": [{
                "id": "clip-1", "duration_s": 1.0, "source_recording": "r1",
                "reference_text": "hello", "metadata": {"source_hash": "1" * 64},
            }],
            "models": {"edda": {"per_recording": {"clip-1": {}}}},
        }
        other = json.loads(json.dumps(result))
        other["dataset"]["source_revisions"] = ["b" * 40]
        other["models"] = {"hviske": {"per_recording": {"clip-1": {}}}}
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "a.json", Path(tmp) / "b.json"
            first.write_text(json.dumps(result), encoding="utf-8")
            second.write_text(json.dumps(other), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "provenance"):
                _merge_runs([first, second])

    def test_report_contains_declared_model_training_data_map(self):
        result = {
            "dataset": {"name": "samtalebank-sam3", "class": "PRIMARY-INDEPENDENTISH",
                        "source_revisions": []},
            "recordings": [],
            "models": {"edda": {"content_wer": 0.1, "per_recording": {}}},
            "diarization": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "results.json"
            input_path.write_text(json.dumps(result), encoding="utf-8")
            outputs = generate_report([input_path], root / "report")
            report = outputs["report"].read_text(encoding="utf-8")
            provenance = json.loads(outputs["provenance"].read_text(encoding="utf-8"))
        self.assertIn("Declared model training data overlap", report)
        self.assertIn("CoRal", report)
        self.assertIn("danish-foundation-models/edda-v0.1", report)
        self.assertIn("evidence_relationships", provenance)


if __name__ == "__main__":
    unittest.main()
