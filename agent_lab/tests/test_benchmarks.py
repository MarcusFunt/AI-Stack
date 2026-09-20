import unittest

from agent_lab.benchmarks import BenchmarkCase, CASES, _validate_cases


class BenchmarkDefinitionTests(unittest.TestCase):
    def test_builtin_benchmark_definitions_are_valid(self):
        _validate_cases(list(CASES))

    def test_invalid_holdout_is_rejected_before_model_run(self):
        case = BenchmarkCase(
            id="bad-fixture",
            objective="irrelevant",
            files={"x.py": "x = 1\n"},
            public_tests="import unittest\n",
            holdout_tests='value = "unterminated\n',
        )
        with self.assertRaisesRegex(ValueError, "invalid holdout tests"):
            _validate_cases([case])


if __name__ == "__main__":
    unittest.main()
