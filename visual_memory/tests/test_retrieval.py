import json

from PIL import Image

from visual_memory.index import VectorIndex
from visual_memory.retrieval import VisualMemoryEngine
from visual_memory.storage import VisualMemoryStore


class FakeEmbedder:
    dimension = 4
    model = "test-model"
    revision = "test-revision"

    def __init__(self):
        self.calls = []

    def embed_image(self, image, *, instruction=None, vision_token_budget=560):
        self.calls.append(("image", vision_token_budget))
        pixel = image.convert("RGB").getpixel((0, 0))
        values = [float(pixel[0]), float(pixel[1]), float(pixel[2]), 1.0]
        norm = sum(value * value for value in values) ** 0.5
        return [value / norm for value in values]

    def embed_text(self, inputs, *, instruction=None, dimensions=768):
        self.calls.append(("text", tuple(inputs)))
        # The test query deliberately shares the red image direction.
        return [[1.0, 0.0, 0.0, 0.0] for _ in inputs]


class FakeHnswIndex:
    def __init__(self, space, dim):
        self.space = space
        self.dim = dim
        self.items = {}
        self.max_elements = 0

    def init_index(self, max_elements, ef_construction, M):
        self.max_elements = max_elements

    def add_items(self, vectors, labels):
        for vector, label in zip(vectors, labels):
            self.items[int(label)] = [float(value) for value in vector]

    def save_index(self, path):
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(self.items, stream)

    def load_index(self, path, max_elements=None):
        with open(path, encoding="utf-8") as stream:
            self.items = {int(key): value for key, value in json.load(stream).items()}
        self.max_elements = max_elements or len(self.items)

    def get_current_count(self):
        return len(self.items)

    def resize_index(self, capacity):
        self.max_elements = capacity

    def knn_query(self, query, k):
        query = list(query[0])
        ranked = sorted(
            ((label, 1.0 - sum(a * b for a, b in zip(query, vector))) for label, vector in self.items.items()),
            key=lambda pair: pair[1],
        )[:k]
        return [[label for label, _ in ranked]], [[distance for _, distance in ranked]]


class FakeHnswLib:
    @staticmethod
    def Index(space, dim):
        return FakeHnswIndex(space, dim)


def image_bytes(color):
    from io import BytesIO
    from PIL import ImageDraw

    stream = BytesIO()
    image = Image.new("RGB", (96, 96), color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((48, 0, 95, 47), fill="white")
    draw.rectangle((0, 48, 47, 95), fill="black")
    draw.ellipse((32, 32, 64, 64), fill="yellow")
    image.save(stream, format="PNG")
    return stream.getvalue()


def test_hnsw_index_rebuilds_when_missing_corrupt_or_incompatible(tmp_path):
    store = VisualMemoryStore(
        tmp_path / "memory.sqlite3", tmp_path / "images", model="test-model", revision="test-revision", dimension=4
    )
    engine = VisualMemoryEngine(store, FakeEmbedder(), index_dir=tmp_path / "indexes", hnswlib_module=FakeHnswLib)
    first = engine.index_image(image_bytes("red"), namespace="website:demo", crop_mode="none")
    index = engine.vector_index
    assert index.rebuild_reason == "missing"
    assert first.embeddings_created == 1

    index_path = index.index_path
    metadata_path = index.metadata_path
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["descriptor"]["revision"] = "stale-revision"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    incompatible = VectorIndex(store, "test-model", "test-revision", dimension=4,
                               index_dir=tmp_path / "indexes", hnswlib_module=FakeHnswLib)
    assert incompatible.rebuild_reason == "incompatible"

    index_path.write_text("corrupt", encoding="utf-8")
    rebuilt = VectorIndex(store, "test-model", "test-revision", dimension=4,
                          index_dir=tmp_path / "indexes", hnswlib_module=FakeHnswLib)
    assert rebuilt.rebuild_reason == "corrupt"
    other = VectorIndex(store, "test-model", "new-revision", dimension=4,
                        index_dir=tmp_path / "indexes", hnswlib_module=FakeHnswLib)
    assert other.index_path != index_path
    assert other.rebuild_reason == "missing"
    store.close()


def test_text_to_image_and_image_to_image_search_respect_filters(tmp_path):
    store = VisualMemoryStore(
        tmp_path / "memory.sqlite3", tmp_path / "images", model="test-model", revision="test-revision", dimension=4
    )
    embedder = FakeEmbedder()
    engine = VisualMemoryEngine(store, embedder, index_dir=tmp_path / "indexes", hnswlib_module=FakeHnswLib)
    red = engine.index_image(image_bytes("red"), namespace="website:demo", source="website",
                             session_id="s1", tags=["header"], crop_mode="none")
    engine.index_image(image_bytes("blue"), namespace="game:demo", source="game", crop_mode="none")

    text_matches = engine.search_text("red screenshot", namespace="website:demo", top_k=4)
    image_matches = engine.search_image(image_bytes("red"), namespace="website:demo", top_k=4)

    assert text_matches["matches"][0]["id"] == red.frame_id
    assert image_matches["matches"][0]["id"] == red.frame_id
    assert all(match["metadata"]["namespace"] == "website:demo" for match in text_matches["matches"])
    assert "embedding" not in text_matches["matches"][0]
    assert embedder.calls[-1][0] == "image"
    store.close()


def test_indexing_generates_bounded_crop_vectors_and_exact_duplicates_skip_inference(tmp_path):
    store = VisualMemoryStore(
        tmp_path / "memory.sqlite3", tmp_path / "images", model="test-model", revision="test-revision", dimension=4
    )
    embedder = FakeEmbedder()
    engine = VisualMemoryEngine(store, embedder, index_dir=tmp_path / "indexes", hnswlib_module=FakeHnswLib)
    content = image_bytes("red")
    first = engine.index_image(content, namespace="references:design", crop_mode="basic")
    calls = len(embedder.calls)
    duplicate = engine.index_image(content, namespace="references:design", crop_mode="basic")

    assert first.embeddings_created >= 1
    assert duplicate.duplicate_level == "identical"
    assert duplicate.embeddings_created == 0
    assert len(embedder.calls) == calls
    assert store.counts()["frames"] == 2
    store.close()


def test_configured_perceptual_duplicate_reuses_vectors_and_keeps_another_observation(tmp_path):
    store = VisualMemoryStore(
        tmp_path / "memory.sqlite3", tmp_path / "images", model="test-model", revision="test-revision", dimension=4
    )
    engine = VisualMemoryEngine(
        store, FakeEmbedder(), index_dir=tmp_path / "indexes", hnswlib_module=FakeHnswLib,
        perceptual_duplicate_distance=64,
    )
    first = engine.index_image(image_bytes("red"), namespace="game:run", crop_mode="none")
    second = engine.index_image(image_bytes("blue"), namespace="game:run", crop_mode="none")

    assert second.frame_id == first.frame_id
    assert second.duplicate_level == "near_duplicate"
    assert second.embeddings_created == 0
    assert store.counts() == {"assets": 1, "frames": 2, "vectors": 1}
    store.close()


def test_exact_asset_is_reembedded_when_the_active_model_revision_changes(tmp_path):
    content = image_bytes("red")
    first_store = VisualMemoryStore(
        tmp_path / "memory.sqlite3", tmp_path / "images", model="test-model", revision="test-revision", dimension=4
    )
    first_engine = VisualMemoryEngine(
        first_store, FakeEmbedder(), index_dir=tmp_path / "indexes", hnswlib_module=FakeHnswLib
    )
    original = first_engine.index_image(content, namespace="website:demo", crop_mode="none")
    first_store.close()

    updated_store = VisualMemoryStore(
        tmp_path / "memory.sqlite3", tmp_path / "images", model="test-model-v2", revision="test-revision-v2", dimension=4
    )
    updated_embedder = FakeEmbedder()
    updated_embedder.model = "test-model-v2"
    updated_embedder.revision = "test-revision-v2"
    updated_engine = VisualMemoryEngine(
        updated_store, updated_embedder, index_dir=tmp_path / "indexes", hnswlib_module=FakeHnswLib
    )
    repeated = updated_engine.index_image(content, namespace="website:demo", crop_mode="none")

    assert repeated.frame_id == original.frame_id
    assert repeated.duplicate_level == "identical"
    assert repeated.embeddings_created == 1
    assert updated_store.counts() == {"assets": 1, "frames": 2, "vectors": 2}
    updated_store.close()


def test_optional_temporal_expansion_adds_neighboring_frames(tmp_path):
    store = VisualMemoryStore(
        tmp_path / "memory.sqlite3", tmp_path / "images", model="test-model", revision="test-revision", dimension=4
    )
    engine = VisualMemoryEngine(store, FakeEmbedder(), index_dir=tmp_path / "indexes", hnswlib_module=FakeHnswLib)
    engine.index_image(image_bytes("blue"), namespace="game:run", session_id="s1", sequence_id="match-1",
                       sequence_number=1, crop_mode="none")
    event = engine.index_image(image_bytes("red"), namespace="game:run", session_id="s1", sequence_id="match-1",
                               sequence_number=2, crop_mode="none")
    engine.index_image(image_bytes("green"), namespace="game:run", session_id="s1", sequence_id="match-1",
                       sequence_number=3, crop_mode="none")

    ordinary = engine.search_text("red", namespace="game:run", top_k=1)
    expanded = engine.search_text("red", namespace="game:run", top_k=1, expand_temporal=2)

    assert ordinary["matches"][0]["id"] == event.frame_id
    assert "temporal_context" not in ordinary["matches"][0]
    assert [frame["offset"] for frame in expanded["matches"][0]["temporal_context"]] == [-1, 1]
    store.close()


def test_indexing_records_nearest_neighbor_similarity_without_a_cutoff(tmp_path):
    store = VisualMemoryStore(
        tmp_path / "memory.sqlite3", tmp_path / "images", model="test-model", revision="test-revision", dimension=4
    )
    engine = VisualMemoryEngine(store, FakeEmbedder(), index_dir=tmp_path / "indexes", hnswlib_module=FakeHnswLib)
    first = engine.index_image(image_bytes("red"), namespace="website:demo", crop_mode="none")
    second = engine.index_image(image_bytes("blue"), namespace="website:demo", crop_mode="none")

    novelty = store.get_record(second.frame_id)["metadata"]["visual_memory"]
    assert novelty["nearest_neighbor"]["frame_id"] == first.frame_id
    assert 0.0 <= novelty["nearest_neighbor"]["similarity"] <= 1.0
    assert novelty["novelty_distance"] == 1.0 - novelty["nearest_neighbor"]["similarity"]
    store.close()


def test_text_chunks_share_the_image_index_and_exact_repeats_skip_embedding(tmp_path):
    store = VisualMemoryStore(
        tmp_path / "memory.sqlite3", tmp_path / "images", model="test-model", revision="test-revision", dimension=4
    )
    embedder = FakeEmbedder()
    engine = VisualMemoryEngine(store, embedder, index_dir=tmp_path / "indexes", hnswlib_module=FakeHnswLib)
    kwargs = {
        "namespace": "code:project-x",
        "source": "code",
        "metadata": {"source_path": "src/nav.tsx", "start_line": 12, "end_line": 20},
    }
    first = engine.index_text("navigation menu overlaps title", **kwargs)
    calls = len(embedder.calls)
    duplicate = engine.index_text("navigation menu overlaps title", **kwargs)

    assert first.embeddings_created == 1
    assert duplicate.duplicate_level == "identical"
    assert duplicate.embeddings_created == 0
    assert len(embedder.calls) == calls
    matches = engine.search_image(image_bytes("red"), namespace="code:project-x", top_k=2)
    assert matches["matches"][0]["id"] == first.frame_id
    assert matches["matches"][0]["text"] == "navigation menu overlaps title"
    assert matches["matches"][0]["metadata"]["source_path"] == "src/nav.tsx"
    store.close()


def test_search_can_filter_source_session_tags_time_and_crop_and_bound_embedding_output(tmp_path):
    store = VisualMemoryStore(
        tmp_path / "memory.sqlite3", tmp_path / "images", model="test-model", revision="test-revision", dimension=4
    )
    engine = VisualMemoryEngine(store, FakeEmbedder(), index_dir=tmp_path / "indexes", hnswlib_module=FakeHnswLib)
    engine.index_image(image_bytes("red"), namespace="game:run", source="game", session_id="s1",
                       timestamp="2026-10-07T10:00:00Z", tags=["boss"], crop_mode="basic")

    result = engine.search_text("red", namespace="game:run", source="game", session_id="s1",
                                start_time="2026-10-07T09:00:00Z", end_time="2026-10-07T11:00:00Z",
                                tags=["boss"], crop_type="full", top_k=1, include_embedding=True)
    assert len(result["matches"]) == 1
    assert len(result["matches"][0]["embedding"]) == 4
    assert result["matches"][0]["crop"] is None
