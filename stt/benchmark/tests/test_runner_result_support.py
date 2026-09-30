import unittest
from pathlib import Path
import json
import tempfile

from stt.benchmark.runner import _aggregate_der, _dataset_info, _is_oom_error
from stt.benchmark.datasets.provenance import canonical_json_sha256


class RunnerResultSupportTests(unittest.TestCase):
    def test_dataset_info_embeds_exact_lock_entry_and_content_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            suite_dir = root / "data" / "stt-benchmark" / "prepared" / "fleurs"
            suite_dir.mkdir(parents=True)
            manifest = suite_dir / "manifest.jsonl"
            manifest.write_text("{}\n", encoding="utf-8")
            entry = {"dataset": "fleurs-da-dk-test", "revision": "a" * 40,
                     "files": [{"path": "manifest.jsonl", "sha256": "b" * 64}]}
            lock = root / "data" / "stt-benchmark" / "dataset-lock.json"
            lock.write_text(json.dumps({"schema_version": 1, "datasets": {
                "fleurs-da-dk-test": entry,
            }}), encoding="utf-8")
            expected_lock_hash = __import__("hashlib").sha256(lock.read_bytes()).hexdigest()
            info = _dataset_info([{
                "dataset": "fleurs-da-dk-test", "dataset_class": "HELD-OUT-IN-DOMAIN",
                "metadata": {"source_revision": "a" * 40},
            }], manifest)
        self.assertEqual(info["dataset_lock_sha256"], expected_lock_hash)
        self.assertEqual(info["dataset_lock_entry"], entry)
        self.assertEqual(info["dataset_lock_entry_sha256"], canonical_json_sha256(entry))

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
