import unittest
from pathlib import Path

from stt.benchmark.datasets import base


class DatasetAdapterContractTests(unittest.TestCase):
    def test_adapter_spec_and_prepare_contract(self):
        self.assertTrue(callable(getattr(base, "DatasetSpec", None)))
        self.assertTrue(callable(getattr(base, "DatasetAdapter", None)))

        class ExampleAdapter(base.DatasetAdapter):
            spec = base.DatasetSpec(
                dataset="example",
                dataset_class="HELD-OUT-IN-DOMAIN",
                source_url="https://example.test/dataset",
                license="CC0",
            )

            def prepare(self, source_path: Path, output_dir: Path, **options) -> Path:
                return output_dir

        adapter = ExampleAdapter()
        self.assertEqual(adapter.spec.dataset, "example")
        self.assertEqual(adapter.prepare(Path("source"), Path("output")), Path("output"))


if __name__ == "__main__":
    unittest.main()
