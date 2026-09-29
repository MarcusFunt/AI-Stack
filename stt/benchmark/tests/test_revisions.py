import unittest
from unittest.mock import patch

from stt.benchmark.revisions import parse_revision_overrides, resolve_model_revision


class RevisionTests(unittest.TestCase):
    def test_full_commit_sha_is_accepted_without_hub_lookup(self):
        sha = "a" * 40
        with patch("huggingface_hub.HfApi.model_info") as model_info:
            self.assertEqual(resolve_model_revision("example/model", sha), sha)
            model_info.assert_not_called()

    def test_revision_overrides_are_parsed_by_alias(self):
        self.assertEqual(
            parse_revision_overrides(["edda=abc", "nemotron=def"]),
            {"edda": "abc", "nemotron": "def"},
        )

    def test_duplicate_revision_alias_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_revision_overrides(["edda=abc", "edda=def"])


if __name__ == "__main__":
    unittest.main()
