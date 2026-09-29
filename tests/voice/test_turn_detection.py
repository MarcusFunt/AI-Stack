from __future__ import annotations

import struct
import unittest

from voice.turn_detection import VoiceTurnDetector


def pcm(value: int, count: int) -> bytes:
    return struct.pack(f"<{count}h", *([value] * count))


class VoiceTurnDetectorTests(unittest.TestCase):
    def test_detects_speech_start_and_silence_delimited_end(self):
        detector = VoiceTurnDetector(
            sample_rate=1_000,
            speech_threshold=100,
            end_silence_seconds=0.04,
            max_turn_seconds=2,
        )

        self.assertEqual(detector.feed(pcm(500, 20))[0].kind, "speech_started")
        self.assertEqual(detector.feed(pcm(0, 20)), [])
        ended = detector.feed(pcm(0, 20))

        self.assertEqual([event.kind for event in ended], ["speech_stopped"])
        self.assertEqual(len(ended[0].audio), 120)
        self.assertFalse(detector.active)

    def test_commit_flushes_a_turn_and_rejects_malformed_pcm(self):
        detector = VoiceTurnDetector(sample_rate=1_000, speech_threshold=100, max_turn_seconds=1)
        detector.feed(pcm(500, 20))

        ended = detector.commit()

        self.assertEqual(ended[0].audio, pcm(500, 20))
        self.assertEqual(detector.commit(), [])
        with self.assertRaises(ValueError):
            detector.feed(b"\x00")


if __name__ == "__main__":
    unittest.main()
