import unittest
from pathlib import Path

from stt.benchmark.runner import _aggregate_der, _dataset_info, _is_oom_error


class RunnerResultSupportTests(unittest.TestCase):
    def test_dataset_info_collects_schema_v2_provenance(self):
        records = [{
            "dataset": "samtalebank-sam3",
            "dataset_class": "PRIMARY-INDEPENDENTISH",
            "metadata": {
                "source_url": "https://talkbank.org/samtale/access/Sam3.html",
                "source_license": "CC-BY-NC-SA-3.0",
                "source_revision": "a" * 40,
                "reference_transform": "talkbank-ca-v1",
            },
        }]
        payload = _dataset_info(records, Path("data/stt-benchmark/prepared/sam3/manifest.jsonl"))
        self.assertEqual(payload["name"], "samtalebank-sam3")
        self.assertEqual(payload["class"], "PRIMARY-INDEPENDENTISH")
        self.assertEqual(payload["source_revisions"], ["a" * 40])

    def test_global_and_macro_der_are_both_retained(self):
        values = {
            "a": {"der": 0.5, "miss": 0.5, "false_alarm": 0.0, "confusion": 0.0, "reference_speaker_time": 1.0},
            "b": {"der": 0.0, "miss": 0.0, "false_alarm": 0.0, "confusion": 0.0, "reference_speaker_time": 3.0},
        }
        result = _aggregate_der(values)
        self.assertEqual(result["global"], 0.125)
        self.assertEqual(result["macro"], 0.25)

    def test_oom_errors_are_classified_without_gpu_import(self):
        self.assertTrue(_is_oom_error(RuntimeError("CUDA out of memory")))
        self.assertFalse(_is_oom_error(ValueError("bad transcript")))


if __name__ == "__main__":
    unittest.main()
