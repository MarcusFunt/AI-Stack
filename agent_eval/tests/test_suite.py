import ast
import unittest

from agent_eval.suite import CASES, get_suite, suite_hash


class SuiteTests(unittest.TestCase):
    def test_sealed_suite_has_hidden_oracles(self):
        suite = get_suite("agent-lab-selfmod-v1")
        self.assertGreaterEqual(len(suite), 10)
        self.assertTrue(all(case.get("hidden_tests") for case in suite))

    def test_suite_hash_is_stable_and_nonempty(self):
        first = suite_hash("agent-lab-selfmod-v1")
        second = suite_hash("agent-lab-selfmod-v1")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertEqual(len(CASES), len(get_suite("agent-lab-selfmod-v1")))

    def test_case_ids_are_unique_and_tests_compile(self):
        suite = get_suite("agent-lab-selfmod-v1")
        ids = [case["id"] for case in suite]
        self.assertEqual(len(ids), len(set(ids)))
        for case in suite:
            with self.subTest(case=case["id"]):
                if case.get("public_tests"):
                    ast.parse(case["public_tests"])
                ast.parse(case["hidden_tests"])

    def test_hidden_oracle_is_distinct_from_public_tests(self):
        for case in get_suite("agent-lab-selfmod-v1"):
            with self.subTest(case=case["id"]):
                self.assertNotEqual(
                    (case.get("public_tests") or "").strip(),
                    case["hidden_tests"].strip(),
                )
                self.assertNotIn(".agent_lab_holdout", case["objective"])


if __name__ == "__main__":
    unittest.main()
