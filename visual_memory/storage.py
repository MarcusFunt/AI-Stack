from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

from .crops import hamming_distance, perceptual_hash
from .embeddings import EMBEDDING_DIMENSION, normalize_vector


NORMALIZATION_METHOD = "l2-float32-v1"
SCHEMA_VERSION = 3


@dataclass(frozen=True)
class VectorPayload:
    crop_type: str
    bbox: dict[str, float] | None
    vector: list[float]
    vision_token_budget: int | None = None


@dataclass(frozen=True)
class IngestResult:
    frame_id: str
    observation_id: str
    duplicate_level: str | None
    embeddings_created: int
    vector_ids: tuple[int, ...]
    image_sha256: str | None
    content_hash: str = ""


class VisualMemoryStore:
    """SQLite is the source of truth for frame metadata and embedding payloads."""

    def __init__(
        self,
        db_path: str | Path,
        image_root: str | Path,
        *,
        model: str = "google/embeddinggemma-2",
        revision: str,
        dimension: int = EMBEDDING_DIMENSION,
        normalization: str = NORMALIZATION_METHOD,
    ) -> None:
        self.db_path = Path(db_path)
        self.image_root = Path(image_root)
        self.model = model
        self.revision = revision
        self.dimension = int(dimension)
        self.normalization = normalization
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.image_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY,
                    namespace TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    perceptual_hash TEXT,
                    image_path TEXT,
                    text_content TEXT,
                    width INTEGER,
                    height INTEGER,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(namespace, item_type, content_hash)
                );
                CREATE TABLE IF NOT EXISTS observations (
                    id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
                    namespace TEXT NOT NULL,
                    source TEXT,
                    session_id TEXT,
                    timestamp TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    duplicate_level TEXT,
                    sequence_id TEXT,
                    sequence_number INTEGER,
                    previous_observation_id TEXT,
                    next_observation_id TEXT
                );
                CREATE INDEX IF NOT EXISTS observations_asset_time ON observations(asset_id, timestamp);
                CREATE INDEX IF NOT EXISTS observations_scope ON observations(namespace, source, session_id, timestamp);
                CREATE TABLE IF NOT EXISTS vectors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
                    crop_type TEXT NOT NULL,
                    bbox_json TEXT,
                    embedding_model TEXT NOT NULL,
                    embedding_revision TEXT NOT NULL,
                    embedding_dimension INTEGER NOT NULL,
                    normalization_method TEXT NOT NULL,
                    vector_payload BLOB NOT NULL,
                    vision_token_budget INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE(asset_id, crop_type, embedding_model, embedding_revision,
                           embedding_dimension, normalization_method)
                );
                CREATE INDEX IF NOT EXISTS vectors_provenance ON vectors(
                    embedding_model, embedding_revision, embedding_dimension, normalization_method
                );
                """
            )
            current_version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(f"visual-memory schema {current_version} is newer than supported {SCHEMA_VERSION}")
            observation_columns = {
                row["name"] for row in self._connection.execute("PRAGMA table_info(observations)").fetchall()
            }
            for name, sql_type in (
                ("sequence_id", "TEXT"),
                ("sequence_number", "INTEGER"),
                ("previous_observation_id", "TEXT"),
                ("next_observation_id", "TEXT"),
            ):
                if name not in observation_columns:
                    self._connection.execute(f"ALTER TABLE observations ADD COLUMN {name} {sql_type}")
            if current_version < 3:
                self._connection.execute("DROP INDEX IF EXISTS observations_sequence")
                self._connection.execute(
                    """CREATE UNIQUE INDEX observations_sequence ON observations(
                    namespace, session_id, sequence_id, sequence_number
                    ) WHERE sequence_id IS NOT NULL AND sequence_number IS NOT NULL"""
                )
            self._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def find_exact_asset(self, namespace: str, content_hash: str, *, item_type: str = "image") -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT id FROM assets WHERE namespace=? AND item_type=? AND content_hash=?",
                (namespace, item_type, content_hash),
            ).fetchone()
        return row["id"] if row else None

    def has_vectors(self, asset_id: str, model: str, revision: str, crop_types: Iterable[str]) -> bool:
        requested = set(crop_types)
        if not requested:
            return False
        return requested.issubset(self.existing_crop_types(asset_id, model, revision))

    def existing_crop_types(self, asset_id: str, model: str, revision: str) -> set[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT crop_type FROM vectors WHERE asset_id=? AND embedding_model=? AND embedding_revision=? "
                "AND embedding_dimension=? AND normalization_method=?",
                (asset_id, model, revision, self.dimension, self.normalization),
            ).fetchall()
        return {row["crop_type"] for row in rows}

    def find_perceptual_duplicate(self, namespace: str, value: str, max_distance: int) -> str | None:
        if max_distance < 0:
            raise ValueError("perceptual duplicate distance must be non-negative")
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, perceptual_hash FROM assets WHERE namespace=? AND item_type='image' AND perceptual_hash IS NOT NULL",
                (namespace,),
            ).fetchall()
        ranked = [
            (hamming_distance(value, row["perceptual_hash"]), row["id"])
            for row in rows
            if len(value) == len(row["perceptual_hash"])
        ]
        close = [entry for entry in ranked if entry[0] <= max_distance]
        return min(close)[1] if close else None

    def _insert_observation(
        self,
        observation_id: str,
        asset_id: str,
        namespace: str,
        source: str | None,
        session_id: str | None,
        timestamp: str | None,
        tags: Iterable[str],
        metadata: dict[str, Any] | None,
        duplicate_level: str | None,
        sequence_id: str | None,
        sequence_number: int | None,
    ) -> None:
        sequence_id = sequence_id or session_id
        if sequence_number is not None and sequence_id is None:
            raise ValueError("sequence_number requires sequence_id or session_id")
        previous = None
        following = None
        if sequence_id:
            if sequence_number is None:
                row = self._connection.execute(
                    "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS next_number FROM observations "
                    "WHERE namespace=? AND session_id IS ? AND sequence_id=?",
                    (namespace, session_id, sequence_id),
                ).fetchone()
                sequence_number = int(row["next_number"])
            previous = self._connection.execute(
                """SELECT id FROM observations WHERE namespace=? AND session_id IS ? AND sequence_id=? AND sequence_number<?
                ORDER BY sequence_number DESC LIMIT 1""",
                (namespace, session_id, sequence_id, sequence_number),
            ).fetchone()
            following = self._connection.execute(
                """SELECT id FROM observations WHERE namespace=? AND session_id IS ? AND sequence_id=? AND sequence_number>?
                ORDER BY sequence_number LIMIT 1""",
                (namespace, session_id, sequence_id, sequence_number),
            ).fetchone()
        previous_id = previous["id"] if previous else None
        following_id = following["id"] if following else None
        try:
            self._connection.execute(
                """INSERT INTO observations
                (id,asset_id,namespace,source,session_id,timestamp,tags_json,metadata_json,duplicate_level,
                 sequence_id,sequence_number,previous_observation_id,next_observation_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (observation_id, asset_id, namespace, source, session_id, timestamp or self._now(),
                 self._json(list(tags)), self._json(metadata), duplicate_level, sequence_id, sequence_number,
                 previous_id, following_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("sequence_number already exists in this sequence") from exc
        if previous_id:
            self._connection.execute(
                "UPDATE observations SET next_observation_id=? WHERE id=?", (observation_id, previous_id)
            )
        if following_id:
            self._connection.execute(
                "UPDATE observations SET previous_observation_id=? WHERE id=?", (observation_id, following_id)
            )

    def record_duplicate(
        self,
        asset_id: str,
        *,
        namespace: str,
        source: str | None = None,
        session_id: str | None = None,
        timestamp: str | None = None,
        tags: Iterable[str] = (),
        metadata: dict[str, Any] | None = None,
        sequence_id: str | None = None,
        sequence_number: int | None = None,
        duplicate_level: str,
    ) -> IngestResult:
        if duplicate_level not in {"identical", "near_duplicate", "embedding"}:
            raise ValueError("unsupported duplicate level")
        observation_id = str(uuid.uuid4())
        with self._lock:
            row = self._connection.execute(
                "SELECT content_hash, item_type FROM assets WHERE id=? AND namespace=?", (asset_id, namespace)
            ).fetchone()
        if row is None:
            raise ValueError("duplicate asset does not exist in the namespace")
        with self._lock, self._connection:
            self._insert_observation(
                observation_id, asset_id, namespace, source, session_id, timestamp, tags, metadata,
                duplicate_level, sequence_id, sequence_number,
            )
        return IngestResult(
            asset_id, observation_id, duplicate_level, 0, (),
            row["content_hash"] if row["item_type"] == "image" else None, row["content_hash"],
        )

    def index_image(
        self,
        *,
        image_bytes: bytes,
        namespace: str,
        source: str | None = None,
        session_id: str | None = None,
        timestamp: str | None = None,
        tags: Iterable[str] = (),
        metadata: dict[str, Any] | None = None,
        vectors: Iterable[VectorPayload] = (),
        perceptual_hash_value: str | None = None,
        sequence_id: str | None = None,
        sequence_number: int | None = None,
    ) -> IngestResult:
        if not namespace or len(namespace) > 256:
            raise ValueError("namespace must be non-empty and at most 256 characters")
        if not image_bytes:
            raise ValueError("image upload is empty")
        content_hash = hashlib.sha256(image_bytes).hexdigest()
        vectors = tuple(vectors)
        existing_id = self.find_exact_asset(namespace, content_hash)
        duplicate_level = "identical" if existing_id else None
        if existing_id:
            asset_id = existing_id
        else:
            try:
                with Image.open(BytesIO(image_bytes)) as opened:
                    image = opened.convert("RGB")
                    image.load()
            except (OSError, ValueError) as exc:
                raise ValueError("invalid image upload") from exc
            perceptual_hash_value = perceptual_hash_value or perceptual_hash(image)
            asset_id = str(uuid.uuid4())
            relative = Path(content_hash[:2]) / f"{content_hash}.webp"
            image_path = self.image_root / relative
            image_path.parent.mkdir(parents=True, exist_ok=True)
            if not image_path.exists():
                buffer = BytesIO()
                image.save(buffer, format="WEBP", lossless=True, method=4)
                temp_path = image_path.with_name(f"{content_hash}.{uuid.uuid4().hex}.webp.tmp")
                temp_path.write_bytes(buffer.getvalue())
                os.replace(temp_path, image_path)
            width, height = image.size

        observation_id = str(uuid.uuid4())
        inserted_vector_ids: list[int] = []
        new_vector_count = 0
        with self._lock, self._connection:
            if not existing_id:
                try:
                    self._connection.execute(
                        "INSERT INTO assets VALUES (?, ?, 'image', ?, ?, ?, NULL, ?, ?, ?, ?)",
                        (asset_id, namespace, content_hash, perceptual_hash_value, str(relative), width, height,
                         self._json(metadata), self._now()),
                    )
                except sqlite3.IntegrityError:
                    existing = self.find_exact_asset(namespace, content_hash)
                    if existing is None:
                        raise
                    asset_id = existing
                    duplicate_level = "identical"
            self._insert_observation(
                observation_id, asset_id, namespace, source, session_id, timestamp, tags, metadata,
                duplicate_level, sequence_id, sequence_number,
            )
            for payload in vectors:
                vector = normalize_vector(payload.vector, dimension=self.dimension)
                packed = struct.pack(f"<{self.dimension}f", *vector)
                try:
                    cursor = self._connection.execute(
                        """INSERT INTO vectors
                        (asset_id,crop_type,bbox_json,embedding_model,embedding_revision,embedding_dimension,
                         normalization_method,vector_payload,vision_token_budget,created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (asset_id, payload.crop_type, self._json(payload.bbox) if payload.bbox else None,
                         self.model, self.revision, self.dimension, self.normalization, packed,
                         payload.vision_token_budget, self._now()),
                    )
                    inserted_vector_ids.append(int(cursor.lastrowid))
                    new_vector_count += 1
                except sqlite3.IntegrityError:
                    row = self._connection.execute(
                        """SELECT id FROM vectors WHERE asset_id=? AND crop_type=? AND embedding_model=?
                        AND embedding_revision=? AND embedding_dimension=? AND normalization_method=?""",
                        (asset_id, payload.crop_type, self.model, self.revision, self.dimension, self.normalization),
                    ).fetchone()
                    inserted_vector_ids.append(int(row["id"]))
        return IngestResult(asset_id, observation_id, duplicate_level, new_vector_count,
                            tuple(dict.fromkeys(inserted_vector_ids)), content_hash, content_hash)

    def index_text(
        self,
        *,
        text: str,
        namespace: str,
        source: str | None = None,
        session_id: str | None = None,
        timestamp: str | None = None,
        tags: Iterable[str] = (),
        metadata: dict[str, Any] | None = None,
        vectors: Iterable[VectorPayload] = (),
        sequence_id: str | None = None,
        sequence_number: int | None = None,
    ) -> IngestResult:
        if not namespace or len(namespace) > 256:
            raise ValueError("namespace must be non-empty and at most 256 characters")
        if not isinstance(text, str) or not text.strip() or len(text) > 32_000:
            raise ValueError("text must be non-empty and at most 32000 characters")
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        existing_id = self.find_exact_asset(namespace, content_hash, item_type="text")
        asset_id = existing_id or str(uuid.uuid4())
        observation_id = str(uuid.uuid4())
        inserted_vector_ids: list[int] = []
        new_vector_count = 0
        vectors = tuple(vectors)
        with self._lock, self._connection:
            if not existing_id:
                self._connection.execute(
                    """INSERT INTO assets
                    (id,namespace,item_type,content_hash,perceptual_hash,image_path,text_content,width,height,
                     metadata_json,created_at) VALUES (?,?,'text',?,NULL,NULL,?,NULL,NULL,?,?)""",
                    (asset_id, namespace, content_hash, text, self._json(metadata), self._now()),
                )
            self._insert_observation(
                observation_id, asset_id, namespace, source, session_id, timestamp, tags, metadata,
                "identical" if existing_id else None, sequence_id, sequence_number,
            )
            for payload in vectors:
                vector = normalize_vector(payload.vector, dimension=self.dimension)
                packed = struct.pack(f"<{self.dimension}f", *vector)
                try:
                    cursor = self._connection.execute(
                        """INSERT INTO vectors
                        (asset_id,crop_type,bbox_json,embedding_model,embedding_revision,embedding_dimension,
                         normalization_method,vector_payload,vision_token_budget,created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (asset_id, payload.crop_type, self._json(payload.bbox) if payload.bbox else None,
                         self.model, self.revision, self.dimension, self.normalization, packed,
                         payload.vision_token_budget, self._now()),
                    )
                    inserted_vector_ids.append(int(cursor.lastrowid))
                    new_vector_count += 1
                except sqlite3.IntegrityError:
                    row = self._connection.execute(
                        """SELECT id FROM vectors WHERE asset_id=? AND crop_type=? AND embedding_model=?
                        AND embedding_revision=? AND embedding_dimension=? AND normalization_method=?""",
                        (asset_id, payload.crop_type, self.model, self.revision, self.dimension, self.normalization),
                    ).fetchone()
                    inserted_vector_ids.append(int(row["id"]))
        return IngestResult(asset_id, observation_id, "identical" if existing_id else None, new_vector_count,
                            tuple(dict.fromkeys(inserted_vector_ids)), None, content_hash)

    def get_record(self, frame_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            """SELECT a.*, o.id AS observation_id, o.source, o.session_id, o.timestamp, o.tags_json,
            o.metadata_json AS observation_metadata_json, o.duplicate_level, o.sequence_id, o.sequence_number,
            o.previous_observation_id, o.next_observation_id, pa.id AS previous_frame_id, na.id AS next_frame_id
            FROM assets a LEFT JOIN observations o ON o.asset_id=a.id
            LEFT JOIN observations po ON po.id=o.previous_observation_id
            LEFT JOIN assets pa ON pa.id=po.asset_id
            LEFT JOIN observations no ON no.id=o.next_observation_id
            LEFT JOIN assets na ON na.id=no.asset_id
            WHERE a.id=? ORDER BY o.timestamp DESC LIMIT 1""",
            (frame_id,),
        ).fetchone()
        if row is None:
            return None
        return self._record_dict(row)

    @staticmethod
    def _record_dict(row: sqlite3.Row) -> dict[str, Any]:
        asset_metadata = json.loads(row["metadata_json"])
        observation_metadata = json.loads(row["observation_metadata_json"] or "{}")
        return {
            "id": row["id"],
            "namespace": row["namespace"],
            "item_type": row["item_type"],
            "source": row["source"],
            "session_id": row["session_id"],
            "timestamp": row["timestamp"],
            "tags": json.loads(row["tags_json"] or "[]"),
            "metadata": {**asset_metadata, **observation_metadata},
            "content_hash": row["content_hash"],
            "image_sha256": row["content_hash"] if row["item_type"] == "image" else None,
            "text": (row["text_content"] or "")[:4000] if row["item_type"] == "text" and "text_content" in row.keys() else None,
            "perceptual_hash": row["perceptual_hash"],
            "width": row["width"],
            "height": row["height"],
            "parent_id": None,
            "duplicate_level": row["duplicate_level"],
            "observation_id": row["observation_id"],
            "sequence_id": row["sequence_id"] if "sequence_id" in row.keys() else None,
            "sequence_number": row["sequence_number"] if "sequence_number" in row.keys() else None,
            "previous_observation_id": row["previous_observation_id"] if "previous_observation_id" in row.keys() else None,
            "next_observation_id": row["next_observation_id"] if "next_observation_id" in row.keys() else None,
            "previous_frame_id": row["previous_frame_id"] if "previous_frame_id" in row.keys() else None,
            "next_frame_id": row["next_frame_id"] if "next_frame_id" in row.keys() else None,
        }

    def get_observation(self, observation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """SELECT o.*, a.id AS frame_id, pa.id AS previous_frame_id, na.id AS next_frame_id
                FROM observations o JOIN assets a ON a.id=o.asset_id
                LEFT JOIN observations po ON po.id=o.previous_observation_id
                LEFT JOIN assets pa ON pa.id=po.asset_id
                LEFT JOIN observations no ON no.id=o.next_observation_id
                LEFT JOIN assets na ON na.id=no.asset_id WHERE o.id=?""",
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "observation_id": row["id"],
            "frame_id": row["frame_id"],
            "namespace": row["namespace"],
            "source": row["source"],
            "session_id": row["session_id"],
            "timestamp": row["timestamp"],
            "tags": json.loads(row["tags_json"]),
            "metadata": json.loads(row["metadata_json"]),
            "duplicate_level": row["duplicate_level"],
            "sequence_id": row["sequence_id"],
            "sequence_number": row["sequence_number"],
            "previous_observation_id": row["previous_observation_id"],
            "next_observation_id": row["next_observation_id"],
            "previous_frame_id": row["previous_frame_id"],
            "next_frame_id": row["next_frame_id"],
        }

    def temporal_window(self, observation_id: str, *, radius: int = 2) -> list[dict[str, Any]]:
        if not 0 <= radius <= 2:
            raise ValueError("temporal radius must be between 0 and 2")
        target = self.get_observation(observation_id)
        if target is None:
            raise KeyError(observation_id)
        if target["sequence_id"] is None or target["sequence_number"] is None:
            return [{**target, "offset": 0}]
        with self._lock:
            rows = self._connection.execute(
                """SELECT o.*, a.id AS frame_id, pa.id AS previous_frame_id, na.id AS next_frame_id
                FROM observations o JOIN assets a ON a.id=o.asset_id
                LEFT JOIN observations po ON po.id=o.previous_observation_id
                LEFT JOIN assets pa ON pa.id=po.asset_id
                LEFT JOIN observations no ON no.id=o.next_observation_id
                LEFT JOIN assets na ON na.id=no.asset_id
                WHERE o.namespace=? AND o.session_id IS ? AND o.sequence_id=? AND o.sequence_number BETWEEN ? AND ?
                ORDER BY o.sequence_number""",
                (target["namespace"], target["session_id"], target["sequence_id"],
                 target["sequence_number"] - radius, target["sequence_number"] + radius),
            ).fetchall()
        return [
            {
                "observation_id": row["id"],
                "frame_id": row["frame_id"],
                "namespace": row["namespace"],
                "source": row["source"],
                "session_id": row["session_id"],
                "timestamp": row["timestamp"],
                "tags": json.loads(row["tags_json"]),
                "metadata": json.loads(row["metadata_json"]),
                "sequence_id": row["sequence_id"],
                "sequence_number": row["sequence_number"],
                "previous_observation_id": row["previous_observation_id"],
                "next_observation_id": row["next_observation_id"],
                "previous_frame_id": row["previous_frame_id"],
                "next_frame_id": row["next_frame_id"],
                "offset": int(row["sequence_number"]) - target["sequence_number"],
            }
            for row in rows
        ]

    def get_vector(self, vector_id: int) -> dict[str, Any] | None:
        row = self._connection.execute("SELECT * FROM vectors WHERE id=?", (vector_id,)).fetchone()
        if row is None:
            return None
        return self._vector_dict(row)

    def _vector_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        vector = list(struct.unpack(f"<{row['embedding_dimension']}f", row["vector_payload"]))
        return {
            "id": int(row["id"]),
            "asset_id": row["asset_id"],
            "crop_type": row["crop_type"],
            "bbox": json.loads(row["bbox_json"]) if row["bbox_json"] else None,
            "model": row["embedding_model"],
            "revision": row["embedding_revision"],
            "dimension": int(row["embedding_dimension"]),
            "normalization": row["normalization_method"],
            "vector": vector,
            "vision_token_budget": row["vision_token_budget"],
        }

    def read_image(self, frame_id: str) -> bytes:
        row = self._connection.execute("SELECT image_path FROM assets WHERE id=?", (frame_id,)).fetchone()
        if row is None or not row["image_path"]:
            raise KeyError(frame_id)
        return (self.image_root / row["image_path"]).read_bytes()

    def get_observation_ids(self, frame_id: str) -> list[str]:
        return [row["id"] for row in self._connection.execute(
            "SELECT id FROM observations WHERE asset_id=? ORDER BY timestamp, rowid", (frame_id,)
        ).fetchall()]

    def counts(self) -> dict[str, int]:
        return {
            "assets": self._connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0],
            "frames": self._connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
            "vectors": self._connection.execute("SELECT COUNT(*) FROM vectors").fetchone()[0],
        }

    def list_vectors(
        self, *, model: str | None = None, revision: str | None = None, dimension: int | None = None,
        normalization: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = []
        values: list[Any] = []
        for column, value in (("embedding_model", model), ("embedding_revision", revision),
                              ("embedding_dimension", dimension), ("normalization_method", normalization)):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(value)
        sql = "SELECT * FROM vectors" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY id"
        return [self._vector_dict(row) for row in self._connection.execute(sql, values).fetchall()]

    def find_vectors(
        self, *, namespace: str | None = None, source: str | None = None, session_id: str | None = None,
        parent_id: str | None = None,
        start_time: str | None = None, end_time: str | None = None, tags: Iterable[str] | None = None,
        crop_type: str | None = None, model: str | None = None, revision: str | None = None,
        dimension: int | None = None, normalization: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        for column, value in (("a.namespace", namespace), ("o.source", source), ("o.session_id", session_id),
                              ("a.id", parent_id),
                              ("v.crop_type", crop_type), ("v.embedding_model", model),
                              ("v.embedding_revision", revision), ("v.embedding_dimension", dimension),
                              ("v.normalization_method", normalization)):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        if start_time:
            clauses.append("o.timestamp>=?")
            params.append(start_time)
        if end_time:
            clauses.append("o.timestamp<=?")
            params.append(end_time)
        sql = """SELECT v.*, a.namespace, a.item_type, a.content_hash, a.perceptual_hash, a.text_content, a.width, a.height,
            a.metadata_json, o.id AS observation_id, o.source, o.session_id, o.timestamp, o.tags_json,
            o.metadata_json AS observation_metadata_json, o.duplicate_level, o.sequence_id, o.sequence_number,
            o.previous_observation_id, o.next_observation_id
            FROM vectors v JOIN assets a ON a.id=v.asset_id JOIN observations o ON o.asset_id=a.id"""
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY o.timestamp DESC, v.id"
        requested_tags = set(tags or ())
        records: dict[int, dict[str, Any]] = {}
        for row in self._connection.execute(sql, params).fetchall():
            record = self._record_dict(row)
            if requested_tags and not requested_tags.issubset(set(record["tags"])):
                continue
            vector = self._vector_dict(row)
            records.setdefault(vector["id"], {**vector, **record})
        return list(records.values())
