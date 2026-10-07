from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import numpy as np

from .embeddings import normalize_vector
from .storage import NORMALIZATION_METHOD, VisualMemoryStore


class _ExactIndex:
    """Small local fallback used when hnswlib is unavailable outside the container."""

    def __init__(self, space: str, dim: int):
        self.space = space
        self.dim = dim
        self.items: dict[int, list[float]] = {}
        self.capacity = 0

    def init_index(self, max_elements: int, ef_construction: int, M: int) -> None:
        self.capacity = max_elements

    def add_items(self, data, ids) -> None:
        for vector, label in zip(data, ids):
            self.items[int(label)] = [float(value) for value in vector]

    def resize_index(self, capacity: int) -> None:
        self.capacity = capacity

    def get_current_count(self) -> int:
        return len(self.items)

    def save_index(self, path: str) -> None:
        Path(path).write_text(json.dumps(self.items, sort_keys=True), encoding="utf-8")

    def load_index(self, path: str, max_elements: int | None = None) -> None:
        self.items = {int(label): values for label, values in json.loads(Path(path).read_text(encoding="utf-8")).items()}
        self.capacity = max_elements or len(self.items)

    def knn_query(self, data, k: int):
        query = [float(value) for value in data[0]]
        ranked = []
        for label, vector in self.items.items():
            distance = 1.0 - sum(left * right for left, right in zip(query, vector))
            ranked.append((label, distance))
        ranked.sort(key=lambda pair: pair[1])
        return (
            np.asarray([[label for label, _ in ranked[:k]]], dtype=np.int64),
            np.asarray([[distance for _, distance in ranked[:k]]], dtype=np.float32),
        )


class _ExactLibrary:
    Index = _ExactIndex


class VectorIndex:
    """HNSW files are disposable indexes; vectors remain authoritative in SQLite."""

    def __init__(
        self,
        store: VisualMemoryStore,
        model: str,
        revision: str,
        *,
        dimension: int,
        normalization: str = NORMALIZATION_METHOD,
        index_dir: str | Path | None = None,
        hnswlib_module=None,
    ) -> None:
        self.store = store
        self.model = model
        self.revision = revision
        self.dimension = int(dimension)
        self.normalization = normalization
        self.index_dir = Path(index_dir or (store.db_path.parent / "indexes"))
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.descriptor = {
            "model": self.model,
            "revision": self.revision,
            "dimension": self.dimension,
            "normalization": self.normalization,
            "metric": "cosine",
            "format_version": 1,
        }
        digest = hashlib.sha256(json.dumps(self.descriptor, sort_keys=True).encode("utf-8")).hexdigest()[:20]
        self.index_path = self.index_dir / f"visual-{digest}.hnsw"
        self.metadata_path = self.index_dir / f"visual-{digest}.json"
        if hnswlib_module is None:
            try:
                import hnswlib as hnswlib_module
                self.backend_name = "hnswlib"
            except ImportError:
                hnswlib_module = _ExactLibrary
                self.backend_name = "exact-fallback"
        else:
            self.backend_name = getattr(hnswlib_module, "__name__", "injected-hnswlib")
        self._hnswlib = hnswlib_module
        self._index = None
        self._labels: set[int] = set()
        self.rebuild_reason: str | None = None
        self._load_or_rebuild()

    @property
    def count(self) -> int:
        return len(self._labels)

    def _new_index(self, capacity: int):
        index = self._hnswlib.Index(space="cosine", dim=self.dimension)
        index.init_index(max_elements=max(1, capacity), ef_construction=120, M=16)
        return index

    def _current_vectors(self) -> list[dict[str, Any]]:
        return self.store.list_vectors(
            model=self.model,
            revision=self.revision,
            dimension=self.dimension,
            normalization=self.normalization,
        )

    @staticmethod
    def _label_digest(labels) -> str:
        packed = ",".join(str(label) for label in sorted(int(value) for value in labels))
        return hashlib.sha256(packed.encode("ascii")).hexdigest()

    def _load_or_rebuild(self) -> None:
        vectors = self._current_vectors()
        if not self.index_path.exists() or not self.metadata_path.exists():
            self.rebuild("missing", vectors=vectors)
            return
        try:
            metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            if metadata.get("descriptor") != self.descriptor:
                self.rebuild("incompatible", vectors=vectors)
                return
            if metadata.get("count") != len(vectors):
                self.rebuild("stale", vectors=vectors)
                return
            current_labels = [int(vector["id"]) for vector in vectors]
            if metadata.get("label_digest") != self._label_digest(current_labels):
                self.rebuild("stale", vectors=vectors)
                return
            index = self._new_index(max(1, len(vectors) + 16))
            index.load_index(str(self.index_path), max_elements=max(1, len(vectors) + 16))
            if int(index.get_current_count()) != len(vectors):
                self.rebuild("stale", vectors=vectors)
                return
            self._index = index
            self._labels = {int(vector["id"]) for vector in vectors}
        except Exception:
            self.rebuild("corrupt", vectors=vectors)

    def rebuild(self, reason: str = "manual", *, vectors: list[dict[str, Any]] | None = None) -> None:
        vectors = self._current_vectors() if vectors is None else vectors
        index = self._new_index(max(1, len(vectors) + 16))
        labels = [int(vector["id"]) for vector in vectors]
        if vectors:
            data = np.asarray([vector["vector"] for vector in vectors], dtype=np.float32)
            index.add_items(data, np.asarray(labels, dtype=np.int64))
        self._persist(index, labels)
        self._index = index
        self._labels = set(labels)
        self.rebuild_reason = reason

    def _persist(self, index, labels) -> None:
        temp_index = self.index_path.with_suffix(self.index_path.suffix + ".tmp")
        temp_meta = self.metadata_path.with_suffix(".json.tmp")
        index.save_index(str(temp_index))
        os.replace(temp_index, self.index_path)
        temp_meta.write_text(
            json.dumps({
                "descriptor": self.descriptor,
                "count": len(labels),
                "label_digest": self._label_digest(labels),
            }, sort_keys=True), encoding="utf-8"
        )
        os.replace(temp_meta, self.metadata_path)

    def add(self, vector_id: int, vector: list[float]) -> None:
        values = normalize_vector(vector, dimension=self.dimension)
        label = int(vector_id)
        if label in self._labels:
            return
        if self._index is None:
            self._index = self._new_index(1)
        capacity = (
            self._index.get_max_elements()
            if hasattr(self._index, "get_max_elements")
            else getattr(self._index, "capacity", 0)
        )
        if len(self._labels) >= max(1, capacity):
            # hnswlib's Python Index exposes resize_index; use its max capacity hint.
            current = max(1, len(self._labels))
            self._index.resize_index(max(current + 1, current * 2))
        self._index.add_items(np.asarray([values], dtype=np.float32), np.asarray([label], dtype=np.int64))
        self._labels.add(label)
        self._persist(self._index, self._labels)

    def search(self, vector: list[float], *, top_k: int) -> list[tuple[int, float]]:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        values = normalize_vector(vector, dimension=self.dimension)
        if not self._labels:
            return []
        labels, distances = self._index.knn_query(
            np.asarray([values], dtype=np.float32), k=min(top_k, len(self._labels))
        )
        return [(int(label), max(-1.0, min(1.0, 1.0 - float(distance))))
                for label, distance in zip(labels[0], distances[0])]
