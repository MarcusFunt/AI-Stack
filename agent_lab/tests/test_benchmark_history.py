import json
import tempfile
import unittest
from pathlib import Path

from agent_lab.benchmark_history import (
    benchmark_root,
    compare_summaries,
    load_reference,
    save_reference,
)


def summary(statuses):
    results = [{"case": case, "status": status} for case, status in statuses.items()]
    passed = sum(status == "passed" for status in statuses.values())
    return {
        "suite": "agent-lab-core-v2",
        "model": "local-fast",
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

    def test_different_case_sets_are_not_compared(self):
        comparison = compare_summaries(
            summary({"a": "passed"}),
            summary({"a": "passed", "b": "passed"}),
        )
        self.assertFalse(comparison["compatible"])

    def test_subset_latest_does_not_replace_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            full = summary({"a": "passed", "b": "passed"})
            subset = summary({"a": "passed"})
            save_reference(root, full)
            bench_root = benchmark_root(root)
            (bench_root / "latest.json").write_text(
                json.dumps(subset), encoding="utf-8"
            )
            reference = load_reference(root)
            self.assertEqual(reference["case_ids"], full["case_ids"])
            self.assertEqual(reference["cases"], 2)


if __name__ == "__main__":
    unittest.main()
