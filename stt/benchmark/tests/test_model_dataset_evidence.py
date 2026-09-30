import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stt.benchmark.analysis.comparison import dataset_summary_rows, pairwise_comparison_rows
from stt.benchmark.analysis.evidence import MODEL_DATASET_EVIDENCE, model_dataset_evidence
from stt.benchmark.analysis.report import (
    _provenance_evidence,
    _recommend_for_suite,
    generate_report,
)
from stt.benchmark.datasets.provenance import canonical_json_sha256


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
        edda_evidence = {
            "model_id": "danish-foundation-models/edda-v0.1",
            "model_card": "https://huggingface.co/danish-foundation-models/edda-v0.1",
            "dataset": "fleurs-da-dk-test",
            "status": "declared_hash_excluded_test",
            "decisive": True,
            "source": "frozen benchmark evidence",
            "note": "This exact relationship was recorded at run time.",
            "source_url": "https://huggingface.co/danish-foundation-models/edda-v0.1",
            "model_revision": "b" * 40,
        }
        saga_evidence = {
            "model_id": "capacit-ai/saga-2-m",
            "model_card": "https://huggingface.co/capacit-ai/saga-2-m",
            "dataset": "fleurs-da-dk-test",
            "status": "checkpoint_selection_overlap",
            "decisive": False,
            "source": "frozen benchmark evidence",
            "note": "Checkpoint selection used part of the test split.",
            "source_url": "https://huggingface.co/capacit-ai/saga-2-m",
            "model_revision": "a" * 40,
        }
        lock_entry = {
            "dataset": "fleurs-da-dk-test", "revision": "c" * 40,
            "files": [{"path": "manifest.jsonl", "sha256": "d" * 64}],
        }
        run = {
            "schema_version": 3,
            "evaluation_protocol": {"protocol_id": "test"},
            "evaluation_protocol_sha256": "f" * 64,
            "dataset": {
                "name": "fleurs-da-dk-test", "class": "HELD-OUT-IN-DOMAIN",
                "dataset_lock_sha256": "e" * 64, "dataset_lock_entry": lock_entry,
                "dataset_lock_entry_sha256": canonical_json_sha256(lock_entry),
            },
            "manifest": "manifest.jsonl",
            "recordings": [{"id": "clip", "duration_s": 1.0, "speaker_count": 1,
                            "reference_text": "hej", "reference_speakers": [],
                            "source_recording": "clip", "metadata": {}}],
            "models": {
                "edda": {"status": "success", "content_wer": 0.12,
                         "content_reference_words": 10, "revision": "b" * 40,
                         "evidence": edda_evidence,
                         "per_recording": {"clip": {"content_errors": 1,
                             "content_reference_words": 10, "content_wer": 0.1,
                             "hypothesis": "hej"}}},
                "saga2": {"status": "success", "content_wer": 0.01,
                          "content_reference_words": 10, "revision": "a" * 40,
                          "evidence": saga_evidence,
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
        self.assertEqual(
            provenance["evidence_relationships"]["fleurs-da-dk-test"]["edda"],
            edda_evidence,
        )
        self.assertEqual(
            provenance["evidence_relationships"]["fleurs-da-dk-test"]["saga2"],
            saga_evidence,
        )
        self.assertEqual(provenance["datasets"][0]["dataset_lock_sha256"], "e" * 64)
        self.assertEqual(
            provenance["datasets"][0]["dataset_lock_entry_sha256"],
            canonical_json_sha256(lock_entry),
        )

    def test_schema_v3_analysis_uses_frozen_evidence_everywhere(self):
        evidence = {
            "model_id": "edda-model",
            "model_card": "https://example.test/edda",
            "dataset": "fleurs-da-dk-test",
            "status": "frozen_status",
            "decisive": True,
            "source": "frozen_source",
            "note": "frozen note",
            "source_url": "https://example.test/edda",
            "model_revision": "d" * 40,
            "future_audit_field": {"kept": True},
        }
        other_evidence = {
            "model_id": "saga-model",
            "model_card": "https://example.test/saga",
            "dataset": "fleurs-da-dk-test",
            "status": "also_frozen",
            "decisive": False,
            "source": "frozen_source",
            "note": "frozen non-decisive note",
            "source_url": "https://example.test/saga",
            "model_revision": "e" * 40,
        }
        models = {}
        for alias, frozen, errors in (("edda", evidence, 0), ("saga2", other_evidence, 1)):
            models[alias] = {
                "status": "success", "content_wer": errors / 10,
                "content_reference_words": 10, "revision": frozen["model_revision"],
                "evidence": frozen,
                "per_recording": {
                    "clip": {"content_errors": errors, "content_reference_words": 10,
                             "content_wer": errors / 10, "hypothesis": "hej"}
                },
            }
        run = {
            "schema_version": 3,
            "evaluation_protocol_sha256": "f" * 64,
            "dataset": {"name": "fleurs-da-dk-test", "class": "HELD-OUT-IN-DOMAIN"},
            "recordings": [{"id": "clip", "duration_s": 1, "reference_text": "hej",
                            "reference_speakers": [], "source_recording": "clip", "metadata": {}}],
            "models": models,
            "diarization": {},
        }
        mutated_table = {
            "edda": {"fleurs-da-dk-test": {"status": "changed", "decisive": False}},
            "saga2": {"fleurs-da-dk-test": {"status": "changed", "decisive": True}},
        }
        with patch.dict(MODEL_DATASET_EVIDENCE, mutated_table, clear=True):
            self.assertEqual(model_dataset_evidence(run, "edda"), evidence)
            with patch(
                "stt.benchmark.analysis.report.model_dataset_evidence",
                side_effect=AssertionError("provenance must copy frozen schema-v3 evidence"),
            ):
                self.assertEqual(_provenance_evidence(run, "edda"), evidence)
            summary = {row["model"]: row for row in dataset_summary_rows(run)}
            pairs = pairwise_comparison_rows(run, samples=5000, seed=5)
            recommendation = _recommend_for_suite(
                run, pairs, label="FLEURS evidence"
            )
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                input_path = root / "results.json"
                input_path.write_text(json.dumps(run), encoding="utf-8")
                outputs = generate_report([input_path], root / "report", bootstrap_samples=5000)
                provenance = json.loads(outputs["provenance"].read_text(encoding="utf-8"))
        self.assertEqual(summary["edda"]["evidence_status"], "frozen_status")
        self.assertEqual(summary["edda"]["evidence_decisive"], True)
        self.assertEqual(pairs[0]["evidence_status_a"], "frozen_status")
        self.assertIn("edda is the only evaluated model with decisive evidence", recommendation)
        self.assertIn("saga2", recommendation)
        self.assertEqual(
            provenance["evidence_relationships"]["fleurs-da-dk-test"],
            {"edda": evidence, "saga2": other_evidence},
        )

    def test_schema_v3_without_evidence_is_invalid_instead_of_reconstructed(self):
        run = {
            "schema_version": 3,
            "dataset": {"name": "fleurs-da-dk-test"},
            "models": {"edda": {"revision": "a" * 40}},
        }
        with self.assertRaisesRegex(ValueError, "schema-v3.*evidence"):
            model_dataset_evidence(run, "edda")


if __name__ == "__main__":
    unittest.main()
