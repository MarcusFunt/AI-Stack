import json
import tempfile
import unittest
from pathlib import Path

from stt.benchmark.datasets import danpass
from stt.benchmark.datasets import prepare


class DanPassAccessStatusTests(unittest.TestCase):
    def test_pending_access_status_is_explicit_and_contains_next_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            status_path = danpass.write_pending_access_status(Path(tmp))
            payload = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["dataset"], "danpass-dialogue")
            self.assertEqual(payload["status"], "PENDING_ACCESS")
            self.assertEqual(payload["dataset_class"], "PRIMARY-INDEPENDENTISH")
            self.assertIn("ninag@hum.ku.dk", payload["access"]["contact"])
            self.assertIn("non-commercial", payload["access"]["terms"].lower())
            self.assertIn("dialogue stereo", payload["planned_views"][0])
            self.assertIn("oracle mono", payload["planned_views"][1])

    def test_pending_access_cli_writes_status_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = prepare.main([
                "--suite", "danpass-dialogue",
                "--output-dir", str(root / "prepared"),
            ])
            self.assertEqual(result, 0)
            self.assertTrue((root / "prepared" / "dataset-status.json").is_file())


if __name__ == "__main__":
    unittest.main()
