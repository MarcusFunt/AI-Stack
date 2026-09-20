import json
import tempfile
import unittest
from pathlib import Path

from agent_lab.improvement import (
    ValidatorSpec,
    evaluate_feature_candidate,
    load_feature_catalog,
)


class FakeSandbox:
    def __init__(self, validator_results, unit_result=None):
        self.validator_results = validator_results
        self.unit_result = unit_result or {
            "returncode": 0,
            "output": "Ran 5 tests in 0.1s\nOK\n",
            "duration_s": 0.1,
        }

    def python_validator(self, run_id, code, timeout_s=120):
        return self.validator_results[(run_id, code)]

    def python_unit(self, run_id, timeout_s, suite="public"):
        return self.unit_result


class ImprovementGateTests(unittest.TestCase):
    def setUp(self):
        self.public = ValidatorSpec("public", "PUBLIC")
        self.hidden = ValidatorSpec("hidden", "HIDDEN")
        self.base = "a" * 32
        self.candidate = "b" * 32

    def _results(self):
        return {
            (self.base, "PUBLIC"): {"returncode": 1, "output": "missing"},
            (self.base, "HIDDEN"): {"returncode": 1, "output": "missing"},
            (self.candidate, "PUBLIC"): {"returncode": 0, "output": "ok"},
            (self.candidate, "HIDDEN"): {"returncode": 0, "output": "ok"},
        }

    def test_all_gates_must_pass(self):
        result = evaluate_feature_candidate(
            FakeSandbox(self._results()),
            base_run_id=self.base,
            candidate_run_id=self.candidate,
            public_validator=self.public,
            hidden_validator=self.hidden,
        )
        self.assertTrue(result.eligible)
        self.assertTrue(all(c.status == "pass" for c in result.checks))
        self.assertEqual(
            [c.id for c in result.checks],
            [
                "baseline-public-fails",
                "baseline-hidden-fails",
                "candidate-public-passes",
                "candidate-hidden-passes",
                "candidate-full-regression",
            ],
        )

    def test_validator_that_already_passes_baseline_is_rejected(self):
        rows = self._results()
        rows[(self.base, "PUBLIC")] = {"returncode": 0, "output": "already green"}
        result = evaluate_feature_candidate(
            FakeSandbox(rows),
            base_run_id=self.base,
            candidate_run_id=self.candidate,
            public_validator=self.public,
            hidden_validator=self.hidden,
        )
        self.assertFalse(result.eligible)
        check = next(c for c in result.checks if c.id == "baseline-public-fails")
        self.assertEqual(check.status, "block")

    def test_candidate_regression_blocks_feature(self):
        unit = {
            "returncode": 1,
            "output": "FAILED (failures=1)",
            "duration_s": 0.2,
        }
        result = evaluate_feature_candidate(
            FakeSandbox(self._results(), unit_result=unit),
            base_run_id=self.base,
            candidate_run_id=self.candidate,
            public_validator=self.public,
            hidden_validator=self.hidden,
        )
        self.assertFalse(result.eligible)
        check = next(c for c in result.checks if c.id == "candidate-full-regression")
        self.assertEqual(check.status, "block")

    def test_timeout_is_not_accepted_as_baseline_failure(self):
        rows = self._results()
        rows[(self.base, "HIDDEN")] = {"returncode": 124, "output": ""}
        result = evaluate_feature_candidate(
            FakeSandbox(rows),
            base_run_id=self.base,
            candidate_run_id=self.candidate,
            public_validator=self.public,
            hidden_validator=self.hidden,
        )
        self.assertFalse(result.eligible)
        check = next(c for c in result.checks if c.id == "baseline-hidden-fails")
        self.assertEqual(check.status, "block")

    def test_validator_infrastructure_error_blocks_without_raising(self):
        class BrokenSandbox(FakeSandbox):
            def python_validator(self, run_id, code, timeout_s=120):
                if code == "HIDDEN":
                    raise RuntimeError("sandbox unavailable")
                return super().python_validator(run_id, code, timeout_s)

        result = evaluate_feature_candidate(
            BrokenSandbox(self._results()),
            base_run_id=self.base,
            candidate_run_id=self.candidate,
            public_validator=self.public,
            hidden_validator=self.hidden,
        )
        self.assertFalse(result.eligible)
        check = next(c for c in result.checks if c.id == "baseline-hidden-fails")
        self.assertEqual(check.status, "block")
        self.assertIn("infrastructure error", check.message)

    def test_catalog_requires_independent_hidden_validator(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text(json.dumps({
                "features": [{
                    "slug": "feature-one",
                    "objective": "Add a feature",
                    "public_validator": "assert False",
                    "hidden_validator": "assert False",
                }]
            }), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_feature_catalog(path)

    def test_catalog_loads_context_scope_and_stability(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text(json.dumps({
                "features": [{
                    "slug": "feature-one",
                    "objective": "Add a feature",
                    "public_validator": "assert False",
                    "hidden_validator": "assert 1 == 2",
                    "context_include": ["agent_lab/worker"],
                    "context_exclude": ["agent_lab/tests"],
                    "edit_include": ["agent_lab/worker/model.py"],
                    "sealed_repeats": 3,
                }]
            }), encoding="utf-8")
            feature = load_feature_catalog(path)[0]
            self.assertEqual(feature.context_include, ("agent_lab/worker",))
            self.assertEqual(feature.edit_include, ("agent_lab/worker/model.py",))
            self.assertEqual(feature.sealed_repeats, 3)


if __name__ == "__main__":
    unittest.main()
