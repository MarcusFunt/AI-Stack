import unittest

from stt.benchmark.metrics import diarization_error, normalize_text, word_error_stats


class MetricsTests(unittest.TestCase):
    def test_normalization_removes_punctuation_and_optional_fillers(self):
        self.assertEqual(normalize_text("Hej, VERDEN!"), "hej verden")
        self.assertEqual(normalize_text("Øh, hej.", remove_fillers=True), "hej")

    def test_word_error_rate(self):
        stats = word_error_stats("en to tre", "en fire tre")
        self.assertEqual(stats.errors, 1)
        self.assertEqual(stats.reference_units, 3)
        self.assertAlmostEqual(stats.rate, 1 / 3)

    def test_diarization_finds_permuted_speaker_mapping(self):
        reference = [
            {"start": 0.0, "end": 1.0, "speaker": "Marcus"},
            {"start": 1.0, "end": 2.0, "speaker": "A"},
            {"start": 2.0, "end": 3.0, "speaker": "B"},
        ]
        hypothesis = [
            {"start": 0.0, "end": 1.0, "speaker": "2"},
            {"start": 1.0, "end": 2.0, "speaker": "0"},
            {"start": 2.0, "end": 3.0, "speaker": "1"},
        ]
        result = diarization_error(reference, hypothesis, duration_s=3.0, collar_s=0.0)
        self.assertEqual(result["der"], 0.0)
        self.assertEqual(result["mapping"]["2"], "Marcus")


if __name__ == "__main__":
    unittest.main()
