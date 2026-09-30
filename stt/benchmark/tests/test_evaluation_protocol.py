import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from stt.benchmark.evaluation_protocol import evaluation_protocol
from stt.benchmark.datasets.provenance import canonical_json_sha256
from stt.benchmark.analysis.report import _merge_runs


class EvaluationProtocolTests(unittest.TestCase):
    def _result(self, protocol_hash, alias):
        return {
            "schema_version": 3,
            "evaluation_protocol": {"protocol_id": "danish-stt-v1"},
            "evaluation_protocol_sha256": protocol_hash,
            "dataset": {"name": "fleurs-da-dk-test", "manifest_sha256": "a" * 64},
            "recordings": [{"id": "clip", "reference_text": "hej", "metadata": {}}],
            "models": {alias: {
                "status": "success", "content_wer": 0.0,
                "evidence": {
                    "dataset": "fleurs-da-dk-test", "status": "declared_hash_excluded_test",
                    "decisive": True, "source": "fixture", "note": "fixture",
                    "model_revision": None,
                },
            }},
        }

    def test_canonical_json_hash_is_stable_and_known(self):
        expected = "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"
        self.assertEqual(canonical_json_sha256({"a": 1, "b": 2}), expected)
        self.assertEqual(canonical_json_sha256({"b": 2, "a": 1}), expected)

    def test_protocol_includes_metric_definitions(self):
        protocol = evaluation_protocol(collar_s=0.25, reference_semantics="segment")
        self.assertEqual(protocol["der_frame_ms"], 10)
        self.assertEqual(protocol["der_standard_collar_s"], 0.25)
        self.assertEqual(protocol["reference_semantics"], "segment")
        self.assertEqual(protocol["speaker_turn_merge_gap_s"], 0.35)

    def test_identical_protocol_results_can_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for alias in ("edda", "hviske"):
                path = Path(tmp) / f"{alias}.json"
                path.write_text(json.dumps(self._result("f" * 64, alias)), encoding="utf-8")
                paths.append(path)
            merged = _merge_runs(paths)
        self.assertEqual(set(merged[0]["models"]), {"edda", "hviske"})

    def test_protocol_mismatch_and_missing_hash_are_rejected(self):
        for second_hash, expected in (("e" * 64, "mismatch"), (None, "missing")):
            with self.subTest(second_hash=second_hash), tempfile.TemporaryDirectory() as tmp:
                first = Path(tmp) / "first.json"
                second = Path(tmp) / "second.json"
                first.write_text(json.dumps(self._result("f" * 64, "edda")), encoding="utf-8")
                payload = self._result(second_hash, "hviske")
                if second_hash is None:
                    payload.pop("evaluation_protocol_sha256")
                second.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "evaluation protocol"):
                    _merge_runs([first, second])

    def test_dataset_lock_entry_hash_controls_compatibility(self):
        from stt.benchmark.analysis.report import _merge_runs

        entry = {
            "dataset": "fleurs-da-dk-test", "revision": "a" * 40,
            "files": [{"path": "manifest.jsonl", "sha256": "b" * 64}],
        }
        unrelated_entry_a = {"dataset": "other", "revision": "1" * 40}
        unrelated_entry_b = {"dataset": "other", "revision": "2" * 40}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._result("f" * 64, "edda")
            second = self._result("f" * 64, "hviske")
            for payload, unrelated in (
                (first, unrelated_entry_a),
                (second, unrelated_entry_b),
            ):
                whole_lock = {"schema_version": 1, "datasets": {
                    "fleurs-da-dk-test": entry, "other": unrelated,
                }}
                payload["dataset"].update({
                    "dataset_lock_entry": entry,
                    "dataset_lock_sha256": hashlib.sha256(
                        json.dumps(whole_lock, sort_keys=True).encode("utf-8")
                    ).hexdigest(),
                    "dataset_lock_entry_sha256": canonical_json_sha256(entry),
                })
            paths = [root / "first.json", root / "second.json"]
            for path, payload in zip(paths, (first, second)):
                path.write_text(json.dumps(payload), encoding="utf-8")
            merged = _merge_runs(paths)
            self.assertEqual(set(merged[0]["models"]), {"edda", "hviske"})

            changed_entry = {
                **entry,
                "files": [{"path": "manifest.jsonl", "sha256": "c" * 64}],
            }
            second["dataset"]["dataset_lock_entry"] = changed_entry
            second["dataset"]["dataset_lock_entry_sha256"] = canonical_json_sha256(changed_entry)
            paths[1].write_text(json.dumps(second), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "incompatible dataset provenance"):
                _merge_runs(paths)

    def test_dataset_lock_entry_hash_is_validated_against_embedded_entry(self):
        entry = {
            "dataset": "fleurs-da-dk-test", "revision": "a" * 40,
            "files": [{"path": "manifest.jsonl", "sha256": "b" * 64}],
        }
        first = self._result("f" * 64, "edda")
        second = self._result("f" * 64, "hviske")
        for payload in (first, second):
            payload["dataset"].update({
                "dataset_lock_entry": entry,
                "dataset_lock_entry_sha256": canonical_json_sha256(entry),
            })
        second["dataset"]["dataset_lock_entry_sha256"] = "0" * 64

        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp) / "first.json", Path(tmp) / "second.json"]
            for path, payload in zip(paths, (first, second)):
                path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "dataset lock entry SHA-256"):
                _merge_runs(paths)

    def test_schema_v3_merge_rejects_missing_frozen_evidence(self):
        result = self._result("f" * 64, "edda")
        del result["models"]["edda"]["evidence"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.json"
            path.write_text(json.dumps(result), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "schema-v3.*evidence"):
                _merge_runs([path])


if __name__ == "__main__":
    unittest.main()
