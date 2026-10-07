from io import BytesIO
import sqlite3

from PIL import Image

from visual_memory.storage import VectorPayload, VisualMemoryStore


def image_bytes(color="red"):
    from io import BytesIO

    stream = BytesIO()
    Image.new("RGB", (32, 24), color).save(stream, format="PNG")
    return stream.getvalue()


def test_image_record_and_vector_survive_store_reopen(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    image_root = tmp_path / "images"
    store = VisualMemoryStore(db_path, image_root, model="model-a", revision="rev-a", dimension=4)
    result = store.index_image(
        image_bytes=image_bytes(),
        namespace="website:demo",
        source="website",
        session_id="session-1",
        timestamp="2026-10-07T10:00:00Z",
        tags=["desktop", "checkout"],
        metadata={"url": "https://example.test/cart"},
        vectors=[VectorPayload("full", None, [1.0, 0.0, 0.0, 0.0], 560)],
    )
    store.close()

    reopened = VisualMemoryStore(db_path, image_root, model="model-a", revision="rev-a", dimension=4)
    record = reopened.get_record(result.frame_id)
    vector = reopened.get_vector(result.vector_ids[0])

    assert record["namespace"] == "website:demo"
    assert record["session_id"] == "session-1"
    assert record["tags"] == ["desktop", "checkout"]
    assert record["metadata"] == {"url": "https://example.test/cart"}
    assert record["image_sha256"]
    stored_image = Image.open(BytesIO(reopened.read_image(result.frame_id)))
    assert stored_image.size == (32, 24)
    assert vector["vector"] == [1.0, 0.0, 0.0, 0.0]
    assert vector["vision_token_budget"] == 560
    reopened.close()


def test_exact_duplicate_adds_temporal_reference_without_duplicate_asset_or_vector(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    image_root = tmp_path / "images"
    store = VisualMemoryStore(db_path, image_root, model="model-a", revision="rev-a", dimension=4)
    content = image_bytes()
    vector = VectorPayload("full", None, [1.0, 0.0, 0.0, 0.0], 560)
    original = store.index_image(image_bytes=content, namespace="game:run", session_id="s1", vectors=[vector])
    repeated = store.index_image(image_bytes=content, namespace="game:run", session_id="s1", vectors=[])

    assert repeated.frame_id == original.frame_id
    assert repeated.duplicate_level == "identical"
    assert repeated.embeddings_created == 0
    assert store.counts() == {"assets": 1, "frames": 2, "vectors": 1}
    assert store.get_observation_ids(original.frame_id) == [original.observation_id, repeated.observation_id]
    store.close()


def test_vectors_with_different_provenance_do_not_overwrite_each_other(tmp_path):
    store = VisualMemoryStore(
        tmp_path / "memory.sqlite3", tmp_path / "images", model="model-a", revision="rev-a", dimension=4
    )
    content = image_bytes()
    first = store.index_image(
        image_bytes=content,
        namespace="references:design",
        vectors=[VectorPayload("full", None, [1.0, 0.0, 0.0, 0.0], 560)],
    )
    second_store = VisualMemoryStore(
        tmp_path / "memory.sqlite3", tmp_path / "images", model="model-b", revision="rev-b", dimension=4
    )
    second = second_store.index_image(
        image_bytes=content,
        namespace="references:design",
        vectors=[VectorPayload("full", None, [0.0, 1.0, 0.0, 0.0], 560)],
    )

    assert second.frame_id == first.frame_id
    assert store.counts()["assets"] == 1
    assert store.counts()["vectors"] == 2
    assert store.list_vectors(model="model-a", revision="rev-a")[0]["vector"] == [1.0, 0.0, 0.0, 0.0]
    assert store.list_vectors(model="model-b", revision="rev-b")[0]["vector"] == [0.0, 1.0, 0.0, 0.0]
    store.close()
    second_store.close()


def test_query_filters_namespace_source_session_time_tags_and_crop(tmp_path):
    store = VisualMemoryStore(
        tmp_path / "memory.sqlite3", tmp_path / "images", model="model-a", revision="rev-a", dimension=4
    )
    image = image_bytes()
    store.index_image(
        image_bytes=image,
        namespace="game:run-a",
        source="game",
        session_id="session-a",
        timestamp="2026-10-07T10:00:00Z",
        tags=["boss", "desktop"],
        vectors=[VectorPayload("full", None, [1.0, 0.0, 0.0, 0.0], 560),
                 VectorPayload("center", {"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5}, [0.0, 1.0, 0.0, 0.0], 280)],
    )
    store.index_image(
        image_bytes=image_bytes("blue"),
        namespace="game:run-b",
        source="game",
        session_id="session-b",
        timestamp="2026-10-07T11:00:00Z",
        tags=["menu"],
        vectors=[VectorPayload("full", None, [1.0, 0.0, 0.0, 0.0], 560)],
    )

    rows = store.find_vectors(namespace="game:run-a", source="game", session_id="session-a",
                              start_time="2026-10-07T09:00:00Z", end_time="2026-10-07T10:30:00Z",
                              tags=["boss"], crop_type="center")
    assert len(rows) == 1
    assert rows[0]["crop_type"] == "center"
    assert rows[0]["namespace"] == "game:run-a"
    store.close()


def test_sequence_links_are_ordered_by_sequence_number_and_stay_within_a_session(tmp_path):
    store = VisualMemoryStore(
        tmp_path / "memory.sqlite3", tmp_path / "images", model="model-a", revision="rev-a", dimension=4
    )

    first = store.index_image(image_bytes=image_bytes("red"), namespace="game:run", session_id="session-a",
                              sequence_id="round-1", sequence_number=1)
    last = store.index_image(image_bytes=image_bytes("green"), namespace="game:run", session_id="session-a",
                             sequence_id="round-1", sequence_number=3)
    middle = store.index_image(image_bytes=image_bytes("blue"), namespace="game:run", session_id="session-a",
                               sequence_id="round-1", sequence_number=2)
    unrelated = store.index_image(image_bytes=image_bytes("black"), namespace="game:run", session_id="session-b",
                                  sequence_id="round-1", sequence_number=1)

    assert store.get_observation(first.observation_id)["next_observation_id"] == middle.observation_id
    assert store.get_observation(last.observation_id)["previous_observation_id"] == middle.observation_id
    assert [item["observation_id"] for item in store.temporal_window(middle.observation_id, radius=2)] == [
        first.observation_id, middle.observation_id, last.observation_id
    ]
    assert store.temporal_window(unrelated.observation_id, radius=2)[0]["offset"] == 0
    store.close()


def test_schema_migrates_existing_observations_table_to_sequence_fields(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """CREATE TABLE assets (id TEXT PRIMARY KEY, namespace TEXT NOT NULL, item_type TEXT NOT NULL,
           content_hash TEXT NOT NULL, perceptual_hash TEXT, image_path TEXT, text_content TEXT, width INTEGER,
           height INTEGER, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL);
           CREATE TABLE observations (id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, namespace TEXT NOT NULL,
           source TEXT, session_id TEXT, timestamp TEXT NOT NULL, tags_json TEXT NOT NULL,
           metadata_json TEXT NOT NULL, duplicate_level TEXT);
           PRAGMA user_version=1;"""
    )
    connection.close()

    store = VisualMemoryStore(db_path, tmp_path / "images", model="model-a", revision="rev-a", dimension=4)
    columns = {row["name"] for row in store._connection.execute("PRAGMA table_info(observations)").fetchall()}
    assert {"sequence_id", "sequence_number", "previous_observation_id", "next_observation_id"}.issubset(columns)
    assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 3
    store.close()
