from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable

from .crops import Crop, generate_crops, perceptual_hash
from .embeddings import EMBEDDING_DIMENSION, normalize_vector
from .image_processing import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, decode_image
from .index import VectorIndex
from .model import MODEL_REPO, MODEL_REVISION
from .storage import IngestResult, NORMALIZATION_METHOD, VectorPayload, VisualMemoryStore


class VisualMemoryEngine:
    def __init__(
        self,
        store: VisualMemoryStore,
        embedder,
        *,
        index_dir: str | Path | None = None,
        hnswlib_module=None,
        perceptual_duplicate_distance: int | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.model = getattr(embedder, "model", MODEL_REPO)
        self.revision = getattr(embedder, "revision", MODEL_REVISION)
        self.dimension = int(getattr(embedder, "dimension", EMBEDDING_DIMENSION))
        self.perceptual_duplicate_distance = perceptual_duplicate_distance
        self.vector_index = VectorIndex(
            store,
            self.model,
            self.revision,
            dimension=self.dimension,
            normalization=store.normalization,
            index_dir=index_dir,
            hnswlib_module=hnswlib_module,
        )

    def index_image(
        self,
        image_bytes: bytes,
        *,
        namespace: str,
        source: str | None = None,
        session_id: str | None = None,
        timestamp: str | None = None,
        tags: Iterable[str] = (),
        metadata: dict[str, Any] | None = None,
        crop_mode: str = "basic",
        vision_token_budget: int = 560,
        instruction: str | None = None,
    ) -> IngestResult:
        if crop_mode not in {"none", "basic"}:
            raise ValueError("crop_mode must be none or basic")
        if vision_token_budget not in {280, 560, 1120}:
            raise ValueError("unsupported vision token budget")
        if len(image_bytes) > MAX_IMAGE_BYTES:
            raise ValueError("image byte limit exceeded")
        content_hash = hashlib.sha256(image_bytes).hexdigest()
        existing_id = self.store.find_exact_asset(namespace, content_hash)
        if crop_mode == "none" and existing_id and self.store.has_vectors(
            existing_id, self.model, self.revision, ["full"]
        ):
            return self.store.record_duplicate(
                existing_id, namespace=namespace, source=source, session_id=session_id, timestamp=timestamp,
                tags=tags, metadata=metadata, duplicate_level="identical",
            )

        image = decode_image(image_bytes, max_bytes=MAX_IMAGE_BYTES, max_pixels=MAX_IMAGE_PIXELS)
        image_hash = perceptual_hash(image)
        if existing_id:
            crops = (
                [Crop("full", image.copy(), {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}, image_hash)]
                if crop_mode == "none"
                else generate_crops(image)
            )
            existing_crops = self.store.existing_crop_types(existing_id, self.model, self.revision)
            crops = [crop for crop in crops if crop.crop_type not in existing_crops]
            if not crops:
                return self.store.record_duplicate(
                    existing_id, namespace=namespace, source=source, session_id=session_id, timestamp=timestamp,
                    tags=tags, metadata=metadata, duplicate_level="identical",
                )
        else:
            if self.perceptual_duplicate_distance is not None:
                near_id = self.store.find_perceptual_duplicate(
                    namespace, image_hash, self.perceptual_duplicate_distance
                )
                if near_id:
                    return self.store.record_duplicate(
                        near_id, namespace=namespace, source=source, session_id=session_id, timestamp=timestamp,
                        tags=tags, metadata=metadata, duplicate_level="near_duplicate",
                    )
            if crop_mode == "none":
                crops = [Crop("full", image.copy(), {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}, image_hash)]
            else:
                crops = generate_crops(image)
        if not crops:
            raise ValueError("image has no informative crops")

        payloads: list[VectorPayload] = []
        for crop in crops:
            budget = vision_token_budget if crop.crop_type == "full" else 280
            vector = self.embedder.embed_image(
                crop.image,
                instruction=instruction,
                vision_token_budget=budget,
            )
            payloads.append(
                VectorPayload(
                    crop_type=crop.crop_type,
                    bbox=crop.box if crop.crop_type != "full" else None,
                    vector=normalize_vector(vector, dimension=self.dimension),
                    vision_token_budget=budget,
                )
            )
        result = self.store.index_image(
            image_bytes=image_bytes,
            namespace=namespace,
            source=source,
            session_id=session_id,
            timestamp=timestamp,
            tags=tags,
            metadata=metadata,
            vectors=payloads,
            perceptual_hash_value=image_hash,
        )
        for vector_id in result.vector_ids:
            vector_record = self.store.get_vector(vector_id)
            if vector_record is not None:
                self.vector_index.add(vector_id, vector_record["vector"])
        return result

    def _search(
        self,
        vector: list[float],
        *,
        namespace: str | None = None,
        source: str | None = None,
        session_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        tags: Iterable[str] | None = None,
        crop_type: str | None = None,
        include_crops: bool = True,
        top_k: int = 8,
        include_embedding: bool = False,
    ) -> dict[str, Any]:
        if not 1 <= top_k <= 50:
            raise ValueError("top_k must be between 1 and 50")
        if include_embedding and top_k > 10:
            raise ValueError("include_embedding is limited to top_k of 10")
        if not include_crops and crop_type not in (None, "full"):
            raise ValueError("crop_type conflicts with include_crops=false")
        if not include_crops:
            crop_type = "full"
        query = normalize_vector(vector, dimension=self.dimension)
        filtered_rows = self.store.find_vectors(
            namespace=namespace,
            source=source,
            session_id=session_id,
            start_time=start_time,
            end_time=end_time,
            tags=tags,
            crop_type=crop_type,
            model=self.model,
            revision=self.revision,
            dimension=self.dimension,
            normalization=self.store.normalization,
        )
        rows_by_id = {row["id"]: row for row in filtered_rows}
        if not rows_by_id:
            ranked = []
        else:
            ranked = self.vector_index.search(query, top_k=self.vector_index.count)
        matches = []
        for vector_id, score in ranked:
            row = rows_by_id.get(vector_id)
            if row is None:
                continue
            match = {
                "id": row["asset_id"],
                "score": score,
                "parent_id": None,
                "crop": None if row["crop_type"] == "full" else {"type": row["crop_type"], "bbox": row["bbox"]},
                "metadata": {
                    "namespace": row["namespace"],
                    "source": row["source"],
                    "session_id": row["session_id"],
                    "timestamp": row["timestamp"],
                    "tags": row["tags"],
                    **row["metadata"],
                },
                "duplicate_level": row["duplicate_level"],
            }
            if include_embedding:
                match["embedding"] = row["vector"]
            matches.append(match)
            if len(matches) >= top_k:
                break
        return {
            "matches": matches,
            "model": self.model,
            "revision": self.revision,
            "dimension": self.dimension,
        }

    def search_text(self, query: str, **filters) -> dict[str, Any]:
        if not query or not query.strip() or len(query) > 32_000:
            raise ValueError("query must be non-empty and at most 32000 characters")
        vectors = self.embedder.embed_text([query])
        if len(vectors) != 1:
            raise ValueError("embedding backend returned the wrong number of vectors")
        return self._search(vectors[0], **filters)

    def search_image(self, image_bytes: bytes, **filters) -> dict[str, Any]:
        image = decode_image(image_bytes, max_bytes=MAX_IMAGE_BYTES, max_pixels=MAX_IMAGE_PIXELS)
        vector = self.embedder.embed_image(image, vision_token_budget=560)
        return self._search(vector, **filters)
