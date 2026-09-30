from __future__ import annotations

import unittest

import numpy as np

from voice.audio.frame_adapter import (
    AudioPCMFrame,
    convert_input_audio,
    convert_output_audio,
    float_to_pcm16,
)


class AudioFrameAdapterTests(unittest.TestCase):
    def test_input_downmixes_stereo_and_resamples_48khz_to_mono_16khz(self):
        stereo = np.column_stack(
            (np.full(480, 1000, dtype=np.int16), np.full(480, 3000, dtype=np.int16))
        )

        result = convert_input_audio(stereo.tobytes(), sample_rate=48_000, channels=2)
        samples = np.frombuffer(result, dtype=np.int16)

        self.assertEqual(len(samples), 160)
        self.assertEqual(int(samples[80]), 2000)

    def test_input_accepts_supported_sample_rates_and_mono_or_stereo(self):
        for sample_rate in (16_000, 24_000, 32_000, 44_100, 48_000):
            frame_count = sample_rate // 100
            for channels in (1, 2):
                with self.subTest(sample_rate=sample_rate, channels=channels):
                    source = np.zeros((frame_count, channels), dtype=np.int16)
                    result = convert_input_audio(
                        source.tobytes(), sample_rate=sample_rate, channels=channels
                    )
                    self.assertEqual(len(result), 160 * 2)

    def test_input_rejects_empty_odd_byte_malformed_and_unsupported_frames(self):
        invalid_frames = (
            (b"", 16_000, 1),
            (b"\x00", 16_000, 1),
            (b"\x00\x00", 22_050, 1),
            (b"\x00\x00", 16_000, 0),
            (b"\x00\x00", 16_000, 3),
            (b"\x00\x00\x00\x00\x00\x00", 16_000, 2),
        )
        for data, sample_rate, channels in invalid_frames:
            with self.subTest(sample_rate=sample_rate, channels=channels, size=len(data)):
                with self.assertRaises(ValueError):
                    convert_input_audio(data, sample_rate=sample_rate, channels=channels)

        with self.assertRaises(ValueError):
            float_to_pcm16(np.zeros((2, 2), dtype=np.float32))

    def test_output_resamples_provider_pcm_to_48khz_mono_frame(self):
        provider_audio = np.full(240, 16_384, dtype=np.int16)

        result = convert_output_audio(
            provider_audio.tobytes(), sample_rate=24_000, channels=1
        )

        self.assertIsInstance(result, AudioPCMFrame)
        self.assertEqual(result.sample_rate, 48_000)
        self.assertEqual(result.channels, 1)
        self.assertEqual(len(result.data), 480 * 2)
        self.assertLess(abs(int(np.frombuffer(result.data, dtype=np.int16)[240]) - 16_384), 10)

    def test_float_to_pcm16_clips_and_rounds_boundaries(self):
        samples = np.array(
            [-1.1, -1.0, -1.5 / 32_768, -0.5 / 32_768, 0.5 / 32_768, 1.5 / 32_768, 1.0, 1.1],
            dtype=np.float64,
        )

        result = np.frombuffer(float_to_pcm16(samples), dtype=np.int16)

        np.testing.assert_array_equal(
            result, np.array([-32_768, -32_768, -2, 0, 0, 2, 32_767, 32_767], dtype=np.int16)
        )


if __name__ == "__main__":
    unittest.main()
