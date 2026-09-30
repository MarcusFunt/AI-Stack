import csv
import json
import tempfile
import unittest
from pathlib import Path

from stt.benchmark.analysis.evidence import model_dataset_evidence
from stt.benchmark.analysis.report import _recommend_for_suite, generate_report


class ModelDatasetEvidenceTests(unittest.TestCase):
    def test_saga_fleurs_is_visible_but_non_decisive_and_revision_linked(self):
        run = {
            "dataset": {"name": "fleurs-da-dk-test"},
            "models": {"saga2": {"revision": "a" * 40, "content_wer": 0.01}},
        }
        evidence = model_dataset_evidence(run, "saga2")
        self.assertEqual(evidence["status"], "checkpoint_selection_overlap")
        self.assertFalse(evidence["decisive"])
        self.assertEqual(evidence["model_revision"], "a" * 40)
        self.assertTrue(evidence["source_url"].startswith("https://"))

    def test_non_decisive_and_failed_candidates_cannot_win(self):
        run = {
            "dataset": {"name": "fleurs-da-dk-test"},
            "models": {
                "edda": {"status": "success", "content_wer": 0.12, "revision": "b" * 40},
                "saga2": {"status": "success", "content_wer": 0.01, "revision": "a" * 40},
                "hviske": {"status": "failed", "content_wer": None},
            },
        }
        pair_rows = [{
            "model_a": "edda", "model_b": "saga2",
            "ci95_low": 0.05, "ci95_high": 0.15,
        }]
        recommendation = _recommend_for_suite(run, pair_rows, label="FLEURS")
        self.assertIn("edda is the only evaluated model", recommendation)
        self.assertIn("saga2", recommendation)
        self.assertNotIn("favoring saga2", recommendation)

    def test_sa_wer_is_descriptive_when_content_interval_includes_zero(self):
        run = {
            "dataset": {"name": "fleurs-da-dk-test"},
            "models": {
                "edda": {"status": "success", "content_wer": 0.12,
                         "speaker_attributed_wer": 0.15, "revision": "b" * 40},
                "hviske": {"status": "success", "content_wer": 0.13,
                           "speaker_attributed_wer": 0.10, "revision": "c" * 40},
            },
        }
        pair_rows = [{
            "model_a": "edda", "model_b": "hviske",
            "ci95_low": -0.02, "ci95_high": 0.01,
        }]
        recommendation = _recommend_for_suite(run, pair_rows, label="FLEURS")
        self.assertIn("no unique winner", recommendation)
        self.assertIn("Speaker-attributed WER is", recommendation)
        self.assertIn("and is descriptive only", recommendation)
        self.assertNotIn("favors", recommendation)

    def test_report_retains_saga_score_and_evidence_in_csv_and_provenance(self):
        run = {
            "schema_version": 3,
            "evaluation_protocol": {"protocol_id": "test"},
            "evaluation_protocol_sha256": "f" * 64,
            "dataset": {"name": "fleurs-da-dk-test", "class": "HELD-OUT-IN-DOMAIN"},
            "manifest": "manifest.jsonl",
            "recordings": [{"id": "clip", "duration_s": 1.0, "speaker_count": 1,
                            "reference_text": "hej", "reference_speakers": [],
                            "source_recording": "clip", "metadata": {}}],
            "models": {
                "edda": {"status": "success", "content_wer": 0.12,
                         "content_reference_words": 10, "revision": "b" * 40,
                         "per_recording": {"clip": {"content_errors": 1,
                             "content_reference_words": 10, "content_wer": 0.1,
                             "hypothesis": "hej"}}},
                "saga2": {"status": "success", "content_wer": 0.01,
                          "content_reference_words": 10, "revision": "a" * 40,
                          "per_recording": {"clip": {"content_errors": 0,
                              "content_reference_words": 10, "content_wer": 0.0,
                              "hypothesis": "hej"}}},
            },
            "diarization": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "results.json"
            input_path.write_text(json.dumps(run), encoding="utf-8")
            outputs = generate_report([input_path], root / "report")
            with outputs["dataset_summary"].open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            provenance = json.loads(outputs["provenance"].read_text(encoding="utf-8"))
        saga = next(row for row in rows if row["model"] == "saga2")
        self.assertEqual(float(saga["content_wer"]), 0.01)
        self.assertEqual(saga["evidence_status"], "checkpoint_selection_overlap")
        self.assertEqual(saga["evidence_decisive"].lower(), "false")
        self.assertEqual(
            provenance["evidence_relationships"]["fleurs-da-dk-test"]["saga2"]["model_revision"],
            "a" * 40,
        )


if __name__ == "__main__":
    unittest.main()
