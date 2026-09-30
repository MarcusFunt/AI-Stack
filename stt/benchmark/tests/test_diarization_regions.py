import unittest

from stt.benchmark.metrics import diarization_error_regions


class DiarizationRegionTests(unittest.TestCase):
    def test_overlap_and_non_overlap_der_use_reference_overlap_regions(self):
        reference = [
            {"start": 0.0, "end": 2.0, "speaker": "A"},
            {"start": 1.0, "end": 3.0, "speaker": "B"},
        ]
        hypothesis = [
            {"start": 0.0, "end": 2.0, "speaker": "x"},
            {"start": 1.5, "end": 3.0, "speaker": "y"},
        ]
        result = diarization_error_regions(reference, hypothesis, duration_s=3.0, collar_s=0.0)
        self.assertIn("overlap_der", result)
        self.assertIn("non_overlap_der", result)
        self.assertEqual(result["overlap_reference_speaker_time"], 2.0)
        self.assertGreater(result["overlap_der"], 0.0)


if __name__ == "__main__":
    unittest.main()
