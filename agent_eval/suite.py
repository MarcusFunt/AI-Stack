from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class SealedCase:
    id: str
    objective: str
    files: dict[str, str]
    public_tests: str | None = None
    hidden_tests: str | None = None
    task_type: str = "python"
    harness: str = "python-unit"
    max_iterations: int = 3
    oracle: str = "unittest"


CASES: tuple[SealedCase, ...] = (
    SealedCase(
        id="sum-with-negatives",
        objective="Fix add(a, b) so it returns the mathematical sum for all integers.",
        files={"calculator.py": "def add(a, b):\n    return a - b\n"},
        public_tests="""import unittest
from calculator import add
class Tests(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(add(2, 3), 5)
""",
        hidden_tests="""import unittest
from calculator import add
class Hidden(unittest.TestCase):
    def test_negative(self):
        self.assertEqual(add(-9, 4), -5)
    def test_zero(self):
        self.assertEqual(add(0, 0), 0)
""",
    ),
    SealedCase(
        id="clamp-semantics",
        objective="Fix clamp(value, low, high) so it clamps below low and above high and leaves in-range values unchanged.",
        files={"clamp_utils.py": "def clamp(value, low, high):\n    return min(low, max(high, value))\n"},
        public_tests="""import unittest
from clamp_utils import clamp
class Tests(unittest.TestCase):
    def test_above(self):
        self.assertEqual(clamp(20, 0, 10), 10)
""",
        hidden_tests="""import unittest
from clamp_utils import clamp
class Hidden(unittest.TestCase):
    def test_below(self):
        self.assertEqual(clamp(-3, 0, 10), 0)
    def test_inside(self):
        self.assertEqual(clamp(7, 0, 10), 7)
""",
    ),
    SealedCase(
        id="safe-division",
        objective="Fix safe_divide(a, b) so division works normally and division by zero returns 0.0.",
        files={"division.py": "def safe_divide(a, b):\n    return a / b\n"},
        public_tests="""import unittest
from division import safe_divide
class Tests(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(safe_divide(3, 0), 0.0)
""",
        hidden_tests="""import unittest
from division import safe_divide
class Hidden(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(safe_divide(9, 3), 3)
    def test_negative(self):
        self.assertEqual(safe_divide(-8, 2), -4)
""",
    ),
    SealedCase(
        id="stable-dedupe",
        objective="Fix dedupe(items) so it removes duplicates while preserving first-occurrence order.",
        files={"dedupe.py": "def dedupe(items):\n    return list(set(items))\n"},
        public_tests="""import unittest
from dedupe import dedupe
class Tests(unittest.TestCase):
    def test_order(self):
        self.assertEqual(dedupe([3, 1, 3, 2]), [3, 1, 2])
""",
        hidden_tests="""import unittest
from dedupe import dedupe
class Hidden(unittest.TestCase):
    def test_strings(self):
        self.assertEqual(dedupe(["b","a","b","c","a"]), ["b","a","c"])
""",
    ),
    SealedCase(
        id="whitespace-words",
        objective="Fix count_words(text) so arbitrary whitespace separates words and whitespace-only text has zero words.",
        files={"words.py": "def count_words(text):\n    return len(text.split(' '))\n"},
        public_tests="""import unittest
from words import count_words
class Tests(unittest.TestCase):
    def test_spaces(self):
        self.assertEqual(count_words("one   two"), 2)
""",
        hidden_tests="""import unittest
from words import count_words
class Hidden(unittest.TestCase):
    def test_tabs_newlines(self):
        self.assertEqual(count_words("one\\ttwo\\nthree"), 3)
    def test_blank(self):
        self.assertEqual(count_words("   "), 0)
""",
    ),
    SealedCase(
        id="create-retry-policy",
        objective="Create retry_policy.py implementing backoff_seconds(attempt, base=0.5, cap=8.0) as min(cap, base * 2**attempt), raising ValueError for negative attempts.",
        files={},
        public_tests="""import unittest
from retry_policy import backoff_seconds
class Tests(unittest.TestCase):
    def test_growth(self):
        self.assertEqual(backoff_seconds(3), 4.0)
""",
        hidden_tests="""import unittest
from retry_policy import backoff_seconds
class Hidden(unittest.TestCase):
    def test_cap(self):
        self.assertEqual(backoff_seconds(10), 8.0)
    def test_negative(self):
        with self.assertRaises(ValueError):
            backoff_seconds(-1)
""",
    ),
    SealedCase(
        id="create-and-repair",
        objective="Create helpers.py with GREETING_PREFIX='Hello, ' and repair greet(name) to use it without changing the name's case.",
        files={"greeting.py": "def greet(name):\n    return 'Hello, ' + name.upper()\n"},
        public_tests="""import unittest
from greeting import greet
class Tests(unittest.TestCase):
    def test_case(self):
        self.assertEqual(greet("Ada"), "Hello, Ada")
""",
        hidden_tests="""import unittest
from greeting import greet
from helpers import GREETING_PREFIX
class Hidden(unittest.TestCase):
    def test_prefix(self):
        self.assertEqual(GREETING_PREFIX, "Hello, ")
    def test_lowercase(self):
        self.assertEqual(greet("linus"), "Hello, linus")
""",
    ),
    SealedCase(
        id="syntax-and-behavior",
        objective="Repair greet(name). The file has a syntax error and greet must return 'Hello, ' followed by the original name.",
        files={"greeting.py": "def greet(name)\n    return 'Hello, ' + name.upper()\n"},
        public_tests="""import unittest
from greeting import greet
class Tests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(greet("Ada"), "Hello, Ada")
""",
        hidden_tests="""import unittest
from greeting import greet
class Hidden(unittest.TestCase):
    def test_preserves_case(self):
        self.assertEqual(greet("mIxEd"), "Hello, mIxEd")
""",
        task_type="python",
        harness="python-unit",
        max_iterations=4,
    ),
    SealedCase(
        id="mean-float",
        objective="Fix mean(values) so it returns an arithmetic mean as a float and raises ValueError for an empty input.",
        files={"stats_utils.py": "def mean(values):\n    if not values:\n        raise ValueError('empty')\n    return sum(values) // len(values)\n"},
        public_tests="""import unittest
from stats_utils import mean
class Tests(unittest.TestCase):
    def test_fraction(self):
        self.assertEqual(mean([1,2]), 1.5)
""",
        hidden_tests="""import unittest
from stats_utils import mean
class Hidden(unittest.TestCase):
    def test_negative(self):
        self.assertEqual(mean([-2,1]), -0.5)
    def test_empty(self):
        with self.assertRaises(ValueError):
            mean([])
""",
    ),
    SealedCase(
        id="first-or-none",
        objective="Fix first_or_none(items) so it returns the first element when present and None for an empty sequence.",
        files={"collections_utils.py": "def first_or_none(items):\n    return items[0]\n"},
        public_tests="""import unittest
from collections_utils import first_or_none
class Tests(unittest.TestCase):
    def test_empty(self):
        self.assertIsNone(first_or_none([]))
""",
        hidden_tests="""import unittest
from collections_utils import first_or_none
class Hidden(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(first_or_none([0,2]), 0)
    def test_tuple(self):
        self.assertEqual(first_or_none(("x","y")), "x")
""",
    ),
    SealedCase(
        id="bounded-history",
        objective=(
            "Fix append_bounded(items, value, limit) so it returns a new list "
            "with the newest at most limit values, never mutates items, and "
            "raises ValueError when limit is below 1."
        ),
        files={
            "history.py": (
                "def append_bounded(items, value, limit):\n"
                "    items.append(value)\n"
                "    return items\n"
            )
        },
        public_tests="""import unittest
from history import append_bounded
class Tests(unittest.TestCase):
    def test_trim(self):
        self.assertEqual(append_bounded([1,2,3], 4, 3), [2,3,4])
""",
        hidden_tests="""import unittest
from history import append_bounded
class Hidden(unittest.TestCase):
    def test_no_mutation(self):
        values = [1,2]
        self.assertEqual(append_bounded(values, 3, 5), [1,2,3])
        self.assertEqual(values, [1,2])
    def test_invalid_limit(self):
        with self.assertRaises(ValueError):
            append_bounded([], 1, 0)
""",
    ),
    SealedCase(
        id="parse-timeout",
        objective=(
            "Fix parse_timeout(value, default=5.0, cap=60.0). Accept numeric "
            "values and strings, use default for None or invalid input, and "
            "clamp valid values to the inclusive range 0..cap."
        ),
        files={
            "timeouts.py": (
                "def parse_timeout(value, default=5.0, cap=60.0):\n"
                "    return float(value)\n"
            )
        },
        public_tests="""import unittest
from timeouts import parse_timeout
class Tests(unittest.TestCase):
    def test_string_and_cap(self):
        self.assertEqual(parse_timeout("12.5"), 12.5)
        self.assertEqual(parse_timeout(90), 60.0)
""",
        hidden_tests="""import unittest
from timeouts import parse_timeout
class Hidden(unittest.TestCase):
    def test_default_paths(self):
        self.assertEqual(parse_timeout(None, default=3), 3)
        self.assertEqual(parse_timeout("bad", default=4), 4)
    def test_negative_and_custom_cap(self):
        self.assertEqual(parse_timeout(-1), 0.0)
        self.assertEqual(parse_timeout("9", cap=7), 7.0)
""",
    ),
)


def get_suite(name: str) -> list[dict[str, Any]]:
    if name != "agent-lab-selfmod-v1":
        raise KeyError(name)
    return [asdict(case) for case in CASES]


def suite_hash(name: str) -> str:
    payload = json.dumps(get_suite(name), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
