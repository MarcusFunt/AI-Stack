import unittest

from stt.benchmark.analysis.strata import metadata_stratified_metrics


class MetadataStrataTests(unittest.TestCase):
    def test_generic_speaker_strata_report_wer_and_minimum_word_status(self):
        result = {
            "dataset": {"name": "fleurs-da-dk-test", "class": "HELD-OUT-IN-DOMAIN"},
            "recordings": [
                {"id": "a", "metadata": {"strata": {"dialect": "west", "gender": "female"}}},
                {"id": "b", "metadata": {"strata": {"dialect": "west", "gender": "male"}}},
            ],
            "models": {
                "edda": {"status": "success", "content_wer": 3 / 900, "per_recording": {
                    "a": {"content_errors": 2, "content_reference_words": 400},
                    "b": {"content_errors": 1, "content_reference_words": 500},
                }},
                "saga2": {"status": "failed", "per_recording": {}},
            },
        }
        rows = metadata_stratified_metrics(result)
        west = next(row for row in rows if row["dimension"] == "dialect")
        self.assertEqual(west["model"], "edda")
        self.assertEqual(west["reference_words"], 900)
        self.assertEqual(west["content_errors"], 3)
        self.assertEqual(west["content_wer"], 3 / 900)
        self.assertEqual(west["status"], "insufficient_n")
        self.assertNotIn("saga2", {row["model"] for row in rows})


if __name__ == "__main__":
    unittest.main()
