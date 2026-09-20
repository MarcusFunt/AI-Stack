import tempfile
import unittest
from pathlib import Path

from agent_lab.model_quality import (
    CASES,
    ModelQualityCase,
    compare_summaries,
    grade_case,
    output_token_budget,
    save_summary,
)


class ModelQualityGraderTests(unittest.TestCase):
    def test_builtin_case_ids_are_unique(self):
        ids = [case.id for case in CASES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 6)

    def test_exact_grader_ignores_thinking_and_fence(self):
        case = ModelQualityCase("x", "", "", "exact", "42")
        passed, detail = grade_case(
            case,
            "<think>reasoning</think>\n```text\n42\n```",
        )
        self.assertTrue(passed, detail)

    def test_json_grader_compares_structure_not_formatting(self):
        case = ModelQualityCase(
            "x", "", "", "json", {"count": 2, "items": [1, 3]}
        )
        passed, detail = grade_case(
            case,
            'Here is the result: {"items":[1,3],"count":2}',
        )
        self.assertTrue(passed, detail)

    def test_reasoning_model_gets_room_for_internal_reasoning(self):
        case = ModelQualityCase("x", "", "", "exact", "ok", max_tokens=32)
        self.assertEqual(output_token_budget(case, "local-fast"), 32)
        self.assertEqual(output_token_budget(case, "local-reasoning"), 512)

    def test_patch_grader_rejects_test_edits(self):
        case = ModelQualityCase(
            "x",
            "",
            "",
            "patch",
            {"path": "calc.py", "old": "return a - b", "new": "return a + b"},
        )
        raw = (
            '{"summary":"cheat","edits":[{"op":"replace",'
            '"path":"test_calc.py","old":"x","new":"y"}]}'
        )
        passed, detail = grade_case(case, raw)
        self.assertFalse(passed, detail)

    def test_patch_grader_accepts_expected_edit(self):
        case = next(case for case in CASES if case.id == "patch-contract")
        raw = (
            '{"summary":"fix","edits":[{"op":"replace","path":"calc.py",'
            '"old":"return a - b","new":"return a + b"}]}'
        )
        passed, detail = grade_case(case, raw)
        self.assertTrue(passed, detail)

    def test_patch_grader_rejects_wrong_schema_even_if_semantics_match(self):
        case = next(case for case in CASES if case.id == "patch-contract")
        raw = (
            '{"summary":"fix","edits":[{"file":"calc.py",'
            '"old_code":"return a - b","new_code":"return a + b"}]}'
        )
        passed, detail = grade_case(case, raw)
        self.assertFalse(passed, detail)

    def test_patch_grader_rejects_extra_fields(self):
        case = next(case for case in CASES if case.id == "patch-contract")
        raw = (
            '{"summary":"fix","edits":[{"op":"replace","path":"calc.py",'
            '"old":"return a - b","new":"return a + b","note":"extra"}]}'
        )
        passed, detail = grade_case(case, raw)
        self.assertFalse(passed, detail)

    def test_comparison_detects_model_regression(self):
        previous = {
            "case_ids": ["a", "b"],
            "results": [
                {"case": "a", "status": "passed"},
                {"case": "b", "status": "failed"},
            ],
        }
        current = {
            "case_ids": ["a", "b"],
            "results": [
                {"case": "a", "status": "failed"},
                {"case": "b", "status": "passed"},
            ],
        }
        comparison = compare_summaries(previous, current)
        self.assertEqual(comparison["regressions"], ["a"])
        self.assertEqual(comparison["improvements"], ["b"])

    def test_green_full_suites_get_separate_model_references(self):
        with tempfile.TemporaryDirectory() as tmp:
            for model in ("local-fast", "local-reasoning"):
                summary = {
                    "model": model,
                    "case_ids": [case.id for case in CASES],
                    "failed": 0,
                    "results": [
                        {"case": case.id, "status": "passed"} for case in CASES
                    ],
                }
                path = save_summary(Path(tmp), summary)
                self.assertEqual(path.parent.name, model)
                self.assertTrue((path.parent / "reference.json").is_file())


if __name__ == "__main__":
    unittest.main()
