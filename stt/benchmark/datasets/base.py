"""Contracts shared by the public benchmark dataset adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DATASET_CLASSES = frozenset(
    {
        "PRIMARY-INDEPENDENTISH",
        "HELD-OUT-IN-DOMAIN",
        "CONTROLLED-SYNTHETIC",
    }
)


@dataclass(frozen=True)
class DatasetSpec:
    dataset: str
    dataset_class: str
    source_url: str
    license: str
    doi: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("dataset", "source_url", "license"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} must not be empty")
        if self.dataset_class not in DATASET_CLASSES:
            raise ValueError(f"unsupported dataset class: {self.dataset_class}")


class DatasetAdapter(ABC):
    """Interface for adapters that prepare a local canonical manifest."""

    spec: DatasetSpec

    @abstractmethod
    def prepare(
        self, source_path: Path, output_dir: Path, **options: Any
    ) -> Path:
        """Prepare the dataset and return its manifest path."""
