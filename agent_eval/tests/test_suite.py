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


if __name__ == "__main__":
    unittest.main()
