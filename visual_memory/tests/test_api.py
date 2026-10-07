from dataclasses import dataclass
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from visual_memory import app as app_module


@dataclass
class FakeResult:
    frame_id: str = "frame-1"
    observation_id: str = "observation-1"
    duplicate_level: str | None = None
    embeddings_created: int = 10
    image_sha256: str = "abc123"
    content_hash: str = "abc123"
    retention_policy: str | None = "full-image"


class FakeEngine:
    model = "google/embeddinggemma-2"
    revision = "pinned-revision"

    def index_image(self, image_bytes, **kwargs):
        return FakeResult(
            duplicate_level="identical" if kwargs.get("session_id") == "repeat" else None,
            retention_policy=kwargs.get("retention_policy", "full-image"),
        )

    def index_text(self, text, **kwargs):
        return FakeResult(duplicate_level="identical" if kwargs.get("metadata", {}).get("source_path") == "repeat" else None)

    def search_text(self, query, **kwargs):
        return {"matches": [{"id": "frame-1", "score": 0.9, "metadata": {"namespace": kwargs.get("namespace")}}],
                "model": self.model, "revision": self.revision, "dimension": 768}

    def search_image(self, image_bytes, **kwargs):
        return self.search_text("", **kwargs)


def png_bytes():
    stream = BytesIO()
    image = Image.new("RGB", (32, 24), "red")
    image.putpixel((0, 0), (0, 0, 0))
    image.save(stream, format="PNG")
    return stream.getvalue()


def test_image_index_endpoint_records_namespace_provenance_and_duplicate_level(monkeypatch):
    monkeypatch.setattr(app_module, "get_memory_engine", lambda: FakeEngine(), raising=False)
    app_module.METRICS.reset()
    client = TestClient(app_module.app)

    first = client.post(
        "/v1/visual-memory/index",
        data={"namespace": "website:demo", "source": "website", "metadata_json": '{"url":"https://example.test"}'},
        files={"image": ("screen.png", png_bytes(), "image/png")},
    )
    duplicate = client.post(
        "/v1/visual-memory/index",
        data={"namespace": "website:demo", "session_id": "repeat"},
        files={"image": ("screen.png", png_bytes(), "image/png")},
    )

    assert first.status_code == 200
    assert first.json()["frame_id"] == "frame-1"
    assert first.json()["embeddings_created"] == 10
    assert first.json()["model"] == "google/embeddinggemma-2"
    assert first.json()["retention_policy"] == "full-image"
    assert duplicate.json()["duplicate"] is True
    assert app_module.METRICS.counter("index_records_total") == 2
    assert app_module.METRICS.counter("index_vectors_total") == 20
    assert app_module.METRICS.counter("duplicate_exact_total") == 1


def test_image_index_api_accepts_explicit_metadata_only_retention(monkeypatch):
    monkeypatch.setattr(app_module, "get_memory_engine", lambda: FakeEngine(), raising=False)
    client = TestClient(app_module.app)
    response = client.post(
        "/v1/visual-memory/index",
        data={"namespace": "website:ephemeral", "retention_policy": "metadata-only", "crop_mode": "none"},
        files={"image": ("frame.png", png_bytes(), "image/png")},
    )

    assert response.status_code == 200
    assert response.json()["retention_policy"] == "metadata-only"


def test_text_and_image_search_endpoints_do_not_include_embeddings_by_default(monkeypatch):
    monkeypatch.setattr(app_module, "get_memory_engine", lambda: FakeEngine(), raising=False)
    app_module.METRICS.reset()
    client = TestClient(app_module.app)

    text_result = client.post(
        "/v1/visual-memory/search/text",
        json={"query": "checkout button", "namespace": "website:demo", "top_k": 5},
    )
    image_result = client.post(
        "/v1/visual-memory/search/image",
        data={"namespace": "website:demo", "top_k": "5"},
        files={"image": ("query.png", png_bytes(), "image/png")},
    )

    assert text_result.status_code == 200
    assert image_result.status_code == 200
    assert "embedding" not in text_result.json()["matches"][0]
    assert text_result.json()["matches"][0]["metadata"]["namespace"] == "website:demo"
    assert app_module.METRICS.counter("embedding_requests_total") == 2
    assert app_module.METRICS.percentiles("visual_search_latency_seconds")["p95"] is not None


def test_search_api_rejects_unbounded_raw_embedding_results(monkeypatch):
    monkeypatch.setattr(app_module, "get_memory_engine", lambda: FakeEngine(), raising=False)
    client = TestClient(app_module.app)

    result = client.post(
        "/v1/visual-memory/search/text",
        json={"query": "button", "top_k": 11, "include_embedding": True},
    )
    assert result.status_code == 400


def test_text_index_endpoint_accepts_bounded_project_chunks(monkeypatch):
    monkeypatch.setattr(app_module, "get_memory_engine", lambda: FakeEngine(), raising=False)
    client = TestClient(app_module.app)

    result = client.post(
        "/v1/visual-memory/index/text",
        json={
            "text": "navigation menu overlaps title",
            "namespace": "code:project-x",
            "source": "code",
            "source_path": "src/nav.tsx",
            "language": "typescript",
            "start_line": 12,
            "end_line": 20,
        },
    )

    assert result.status_code == 200
    assert result.json()["frame_id"] == "frame-1"
    assert result.json()["model"] == "google/embeddinggemma-2"
