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
            "models": {alias: {"status": "success", "content_wer": 0.0}},
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


if __name__ == "__main__":
    unittest.main()
