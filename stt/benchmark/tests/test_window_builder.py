import unittest

from stt.benchmark.datasets import samtalebank


def _segments(duration_s=270.0):
    speakers = ("A", "B", "C")
    return [
        {"start": float(start), "end": float(start + 2), "speaker": speakers[(start // 3) % 3], "text": "ord"}
        for start in range(0, int(duration_s), 3)
        if start + 2 <= duration_s
    ]


class WindowBuilderTests(unittest.TestCase):
    def test_windows_keep_three_speakers_and_rebase_without_cutting_segments(self):
        self.assertTrue(callable(getattr(samtalebank, "build_windows", None)))
        windows = samtalebank.build_windows(_segments(), duration_s=270.0, seed=20260930)
        original_intervals = {(item["start"], item["end"], item["speaker"]) for item in _segments()}
        self.assertGreaterEqual(len(windows), 2)
        self.assertTrue(all(window["speaker_count"] == 3 for window in windows))
        for window in windows:
            self.assertGreaterEqual(window["end_s"] - window["start_s"], 60.0)
            self.assertLessEqual(window["end_s"] - window["start_s"], 120.0)
            for segment in window["segments"]:
                source_interval = (
                    segment["start"] + window["start_s"],
                    segment["end"] + window["start_s"],
                    segment["speaker"],
                )
                self.assertIn(source_interval, original_intervals)
                self.assertGreaterEqual(segment["start"], 0.0)
                self.assertLessEqual(segment["end"], window["end_s"] - window["start_s"])
        self.assertEqual(windows[0]["segments"][0]["start"], 0.0)
        self.assertEqual(windows[0]["segments"][0]["end"], 2.0)
        for left, right in zip(windows, windows[1:]):
            self.assertLessEqual(left["end_s"], right["start_s"])

    def test_overlap_ratio_and_per_speaker_time_are_computed(self):
        self.assertTrue(callable(getattr(samtalebank, "window_metrics", None)))
        segments = _segments(90.0)
        segments.append({"start": 15.0, "end": 20.0, "speaker": "B", "text": "overlap"})
        stats = samtalebank.window_metrics(segments, 0.0, 90.0)
        self.assertAlmostEqual(stats["overlap_s"], 4.0)
        self.assertAlmostEqual(stats["overlap_ratio"], 4.0 / 90.0)
        self.assertAlmostEqual(stats["speech_ratio"], 61.0 / 90.0)
        self.assertAlmostEqual(stats["speaker_time_s"]["B"], 25.0)

    def test_fixed_seed_produces_identical_non_overlapping_windows(self):
        self.assertTrue(callable(getattr(samtalebank, "build_windows", None)))
        first = samtalebank.build_windows(_segments(), duration_s=270.0, seed=42)
        second = samtalebank.build_windows(_segments(), duration_s=270.0, seed=42)
        self.assertEqual(first, second)
        self.assertEqual(len({window["id"] for window in first}), len(first))

    def test_ineligible_two_speaker_windows_are_not_returned(self):
        self.assertTrue(callable(getattr(samtalebank, "build_windows", None)))
        two_speakers = [segment for segment in _segments() if segment["speaker"] != "C"]
        windows = samtalebank.build_windows(two_speakers, duration_s=270.0)
        self.assertEqual(windows, [])


if __name__ == "__main__":
    unittest.main()
