import hashlib
import hmac
import tempfile
import unittest
from pathlib import Path

from agent_eval.controller import EvaluatorController, canonical_json
from agent_eval.db import EvaluationStore
from agent_eval.schemas import EvaluationStatus


class AttestationTests(unittest.TestCase):
    def test_signature_detects_tampering(self):
        controller = EvaluatorController.__new__(EvaluatorController)
        controller.signing_key = b"x" * 32
        payload = {
            "candidate_commit": "a" * 40,
            "status": "passed",
            "critical_checks": {"network": True},
        }
        signature = controller._sign_attestation(payload)
        expected = hmac.new(
            controller.signing_key,
            canonical_json(payload),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(signature, expected)
        tampered = dict(payload)
        tampered["status"] = "failed"
        self.assertNotEqual(signature, controller._sign_attestation(tampered))

    def test_reference_comparison_detects_regression(self):
        controller = EvaluatorController.__new__(EvaluatorController)
        reference = {
            "case_results": [
                {"case": "a", "status": "passed"},
                {"case": "b", "status": "failed"},
            ]
        }
        current = [
            {"case": "a", "status": "failed"},
            {"case": "b", "status": "passed"},
        ]
        comparison = controller._compare_to_reference(reference, current)
        self.assertTrue(comparison["compatible"])
        self.assertEqual(comparison["regressions"], ["a"])
        self.assertEqual(comparison["improvements"], ["b"])
        self.assertFalse(comparison["meets_reference"])

    def test_reference_comparison_allows_equal_baseline(self):
        controller = EvaluatorController.__new__(EvaluatorController)
        reference = {
            "case_results": [
                {"case": "a", "status": "passed"},
                {"case": "b", "status": "failed"},
            ]
        }
        current = [
            {"case": "a", "status": "passed"},
            {"case": "b", "status": "failed"},
        ]
        comparison = controller._compare_to_reference(reference, current)
        self.assertTrue(comparison["meets_reference"])
        self.assertEqual(comparison["regressions"], [])

    def test_store_recovers_inflight_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EvaluationStore(Path(tmp) / "eval.sqlite3")
            record = store.create(
                "1" * 32,
                "2" * 32,
                "a" * 40,
                "b" * 40,
                "agent-lab-selfmod-v1",
                "c" * 64,
                "local-fast",
                10000,
                600,
            )
            store.update(record.id, status=EvaluationStatus.RUNNING)
            store.recover_inflight()
            recovered = store.get(record.id)
            self.assertEqual(recovered.status, EvaluationStatus.ERROR)
            self.assertIn("restarted", recovered.error)


if __name__ == "__main__":
    unittest.main()
