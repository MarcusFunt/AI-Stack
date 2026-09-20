import json
import tempfile
import unittest
from pathlib import Path

from agent_lab.benchmark_history import (
    benchmark_root,
    compare_summaries,
    load_latest,
    load_reference,
    save_latest,
    save_reference,
)


def summary(statuses, model="local-fast"):
    results = [{"case": case, "status": status} for case, status in statuses.items()]
    passed = sum(status == "passed" for status in statuses.values())
    return {
        "suite": "agent-lab-core-v2",
        "model": model,
        "case_ids": list(statuses),
        "cases": len(statuses),
        "passed": passed,
        "pass_rate": passed / len(statuses),
        "duration_s": 10.0,
        "results": results,
    }


class BenchmarkHistoryTests(unittest.TestCase):
    def test_comparison_detects_regressions_and_improvements(self):
        previous = summary({"a": "passed", "b": "failed", "c": "passed"})
        current = summary({"a": "failed", "b": "passed", "c": "passed"})
        comparison = compare_summaries(previous, current)
        self.assertTrue(comparison["compatible"])
        self.assertEqual(comparison["regressions"], ["a"])
        self.assertEqual(comparison["improvements"], ["b"])
        self.assertFalse(comparison["no_regressions"])

    def test_subset_compares_against_covered_full_suite(self):
        comparison = compare_summaries(
            summary({"a": "passed", "b": "failed"}),
            summary({"a": "failed"}),
        )
        self.assertTrue(comparison["compatible"])
        self.assertEqual(comparison["scope"], "subset")
        self.assertEqual(comparison["regressions"], ["a"])
        self.assertEqual(comparison["pass_rate_delta"], -1.0)
        self.assertIsNone(comparison["duration_delta_s"])

    def test_previous_subset_cannot_cover_larger_current_suite(self):
        comparison = compare_summaries(
            summary({"a": "passed"}),
            summary({"a": "passed", "b": "passed"}),
        )
        self.assertFalse(comparison["compatible"])

    def test_model_references_and_latest_results_are_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fast = summary({"a": "passed", "b": "passed"})
            reasoning = summary(
                {"a": "passed", "b": "failed"},
                model="local-reasoning",
            )
            save_reference(root, fast)
            save_reference(root, reasoning)
            save_latest(root, fast)
            save_latest(root, reasoning)

            self.assertEqual(load_reference(root, "local-fast")["model"], "local-fast")
            self.assertEqual(
                load_reference(root, "local-reasoning")["model"],
                "local-reasoning",
            )
            self.assertEqual(load_latest(root, "local-fast")["passed"], 2)
            self.assertEqual(load_latest(root, "local-reasoning")["passed"], 1)

    def test_subset_latest_does_not_replace_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            full = summary({"a": "passed", "b": "passed"})
            subset = summary({"a": "passed"})
            save_reference(root, full)
            save_latest(root, subset)
            reference = load_reference(root, "local-fast")
            self.assertEqual(reference["case_ids"], full["case_ids"])
            self.assertEqual(reference["cases"], 2)


if __name__ == "__main__":
    unittest.main()
