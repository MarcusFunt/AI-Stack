import unittest

from stt.benchmark.analysis.bootstrap import paired_bootstrap_wer


class PairedBootstrapTests(unittest.TestCase):
    def test_bootstrap_is_deterministic_and_uses_paired_units(self):
        units = [
            {"id": "a", "source_recording": "rec1", "model_a": {"errors": 0, "reference_units": 10}, "model_b": {"errors": 5, "reference_units": 10}},
            {"id": "b", "source_recording": "rec1", "model_a": {"errors": 0, "reference_units": 10}, "model_b": {"errors": 5, "reference_units": 10}},
            {"id": "c", "source_recording": "rec2", "model_a": {"errors": 0, "reference_units": 10}, "model_b": {"errors": 5, "reference_units": 10}},
        ]
        result = paired_bootstrap_wer(units, "model_a", "model_b", samples=5000, seed=123)
        repeated = paired_bootstrap_wer(units, "model_a", "model_b", samples=5000, seed=123)
        self.assertEqual(result, repeated)
        self.assertEqual(result["unit_count"], 3)
        self.assertAlmostEqual(result["delta_wer"], -0.5)
        self.assertEqual(result["ci95"], [-0.5, -0.5])
        self.assertFalse(result["ci_includes_zero"])
        self.assertEqual(result["group_bootstrap"]["group_count"], 2)

    def test_bootstrap_rejects_unpaired_or_empty_reference_units(self):
        units = [{"id": "a", "model_a": {"errors": 0, "reference_units": 0}, "model_b": {"errors": 0, "reference_units": 1}}]
        with self.assertRaisesRegex(ValueError, "positive total reference units"):
            paired_bootstrap_wer(units, "model_a", "model_b", samples=5000)


if __name__ == "__main__":
    unittest.main()
