import tempfile
import subprocess
import unittest
from pathlib import Path

from stt.benchmark.datasets import provenance


class DatasetProvenanceTests(unittest.TestCase):
    def test_preparation_code_fingerprint_is_deterministic_and_includes_untracked_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "stt" / "benchmark" / "datasets" / "prep.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Benchmark Test"], cwd=root, check=True)
            subprocess.run(["git", "add", "stt/benchmark/datasets/prep.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)
            first = provenance.preparation_code_provenance(root)
            self.assertFalse(first["dirty"])

            untracked = root / "stt" / "benchmark" / "datasets" / "extra.py"
            untracked.write_text("VALUE = 2\n", encoding="utf-8")
            dirty_a = provenance.preparation_code_provenance(root)
            dirty_b = provenance.preparation_code_provenance(root)
            self.assertTrue(dirty_a["dirty"])
            self.assertEqual(dirty_a["working_tree_diff_sha256"], dirty_b["working_tree_diff_sha256"])
            untracked.write_text("VALUE = 3\n", encoding="utf-8")
            changed = provenance.preparation_code_provenance(root)
            self.assertNotEqual(dirty_a["working_tree_diff_sha256"], changed["working_tree_diff_sha256"])

    def test_lock_rejects_absolute_or_parent_traversal_file_paths(self):
        for path_value in (r"C:\raw\sample.cha", "../raw/sample.cha"):
            entry = {
                "dataset": "samtalebank-sam3",
                "revision": "a" * 40,
                "files": [{"path": path_value, "sha256": "b" * 64}],
            }
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(ValueError, "relative path"):
                    provenance.update_lock(Path(tmp) / "dataset-lock.json", entry)

    def test_lock_records_file_hash_and_rejects_revision_or_file_drift(self):
        for name in ("file_sha256", "build_lock_entry", "update_lock", "load_lock"):
            self.assertTrue(callable(getattr(provenance, name, None)), f"missing provenance API: {name}")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "transcript.cha"
            source.write_text("*A:\thej\n", encoding="utf-8")
            expected_hash = provenance.file_sha256(source)
            self.assertEqual(len(expected_hash), 64)
            entry = provenance.build_lock_entry(
                dataset="samtalebank-sam3",
                dataset_class="PRIMARY-INDEPENDENTISH",
                source_url="https://talkbank.org/samtale/access/Sam3.html",
                license="CC-BY-NC-SA-3.0",
                revision="a" * 40,
                files=[source],
                root=root,
                preparation_code_git_sha="b" * 40,
                reference_transform="talkbank-ca-v1",
            )
            self.assertEqual(entry["files"][0]["sha256"], expected_hash)
            lock_path = root / "dataset-lock.json"
            provenance.update_lock(lock_path, entry)
            self.assertEqual(provenance.load_lock(lock_path)["datasets"]["samtalebank-sam3"]["revision"], "a" * 40)

            changed_revision = {**entry, "revision": "c" * 40}
            with self.assertRaisesRegex(ValueError, "revision changed"):
                provenance.update_lock(lock_path, changed_revision)

            source.write_text("*A:\tændret\n", encoding="utf-8")
            changed_files = provenance.build_lock_entry(
                dataset="samtalebank-sam3",
                dataset_class="PRIMARY-INDEPENDENTISH",
                source_url="https://talkbank.org/samtale/access/Sam3.html",
                license="CC-BY-NC-SA-3.0",
                revision="a" * 40,
                files=[source],
                root=root,
                preparation_code_git_sha="b" * 40,
                reference_transform="talkbank-ca-v1",
            )
            with self.assertRaisesRegex(ValueError, "file hashes changed"):
                provenance.update_lock(lock_path, changed_files)


if __name__ == "__main__":
    unittest.main()
