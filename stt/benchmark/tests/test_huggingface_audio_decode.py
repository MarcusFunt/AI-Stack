import sys
import types
import unittest
from unittest.mock import patch

from stt.benchmark.datasets.huggingface import load_dataset_split


class FakeAudioFeature:
    def __init__(self, *, decode):
        self.decode = decode


class FakeDataset:
    column_names = ["audio", "text"]

    def __init__(self):
        self.cast = None

    def cast_column(self, name, feature):
        self.cast = (name, feature)
        return self


class HuggingFaceAudioDecodeTests(unittest.TestCase):
    def test_public_audio_split_disables_torchcodec_decoding(self):
        dataset = FakeDataset()
        module = types.ModuleType("datasets")
        module.Audio = FakeAudioFeature
        module.load_dataset = lambda *args, **kwargs: dataset
        with patch.dict(sys.modules, {"datasets": module}):
            loaded = load_dataset_split(
                "google/fleurs", "da_dk", split="test", revision="a" * 40
            )
        self.assertIs(loaded, dataset)
        self.assertEqual(dataset.cast[0], "audio")
        self.assertFalse(dataset.cast[1].decode)


if __name__ == "__main__":
    unittest.main()
