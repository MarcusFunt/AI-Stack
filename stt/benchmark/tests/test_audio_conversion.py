import io
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from stt.benchmark.datasets.audio import write_audio_16k_mono


class AudioConversionTests(unittest.TestCase):
    def test_huggingface_audio_is_downmixed_and_resampled(self):
        source = io.BytesIO()
        with wave.open(source, "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            frames = [struct.pack("<hh", 9000, -1000)] * 8000
            handle.writeframes(b"".join(frames))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "converted.wav"
            duration = write_audio_16k_mono(
                {"bytes": source.getvalue()}, target, "stereo-8k"
            )
            with wave.open(str(target), "rb") as handle:
                self.assertEqual(handle.getnchannels(), 1)
                self.assertEqual(handle.getframerate(), 16000)
                self.assertEqual(handle.getnframes(), 16000)
        self.assertAlmostEqual(duration, 1.0)


if __name__ == "__main__":
    unittest.main()
