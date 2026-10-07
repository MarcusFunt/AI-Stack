from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from visual_memory import app as app_module
from visual_memory.metrics import VisualMemoryMetrics
from visual_memory.storage import VectorPayload, VisualMemoryStore


def test_metrics_are_low_cardinality_and_exclude_private_values():
    metrics = VisualMemoryMetrics()
    metrics.increment("embedding_requests_total")
    metrics.increment("index_vectors_total", 3)
    metrics.observe("embedding_latency_seconds", 0.125)

    rendered = metrics.render_prometheus()
    snapshot = metrics.snapshot()

    assert "embedding_requests_total 1" in rendered
    assert "index_vectors_total 3" in rendered
    assert 'embedding_latency_seconds_bucket{le="0.25"} 1' in rendered
    assert snapshot["percentiles_seconds"]["embedding_latency_seconds"]["p50"] == 0.125
    assert "frame-private-id" not in rendered
    assert "private-source-code" not in rendered
    assert "embedding_requests_total{" not in rendered


def test_diagnostics_report_counts_and_provenance_without_code_or_vectors(tmp_path, monkeypatch):
    database = tmp_path / "memory.sqlite3"
    store = VisualMemoryStore(database, tmp_path / "images", model="google/embeddinggemma-2",
                              revision="test-revision", dimension=768)
    private_code = "PRIVATE_CODE_MARKER_DO_NOT_EXPOSE"
    store.index_text(
        text=private_code,
        namespace="code:private",
        source="code",
        metadata={"source_path": "secret/private.py"},
        vectors=[VectorPayload("full", None, [1.0] + [0.0] * 767)],
    )
    store.close()
    monkeypatch.setattr(app_module, "DB_PATH", database)
    monkeypatch.setattr(app_module, "INDEX_PATH", tmp_path / "indexes")
    monkeypatch.setattr(app_module.backend, "provenance", lambda: {
        "model": "google/embeddinggemma-2", "revision": "test-revision", "dimension": 768,
        "device": "cpu", "loaded": False, "model_files_available": True,
    })
    monkeypatch.setattr(app_module, "get_memory_engine", lambda: (_ for _ in ()).throw(AssertionError("must not load")))
    app_module.METRICS.reset()
    client = TestClient(app_module.app)

    response = client.get("/diagnostics")
    metrics_response = client.get("/metrics")
    payload = response.json()

    assert response.status_code == 200
    assert payload["model"] == "google/embeddinggemma-2"
    assert payload["loaded"] is False
    assert payload["database"]["available"] is True
    assert payload["database"]["records"] == 1
    assert payload["database"]["observations"] == 1
    assert payload["database"]["vectors"] == 1
    assert payload["index"]["compatible"] is False
    assert "embedding_latency_seconds" in metrics_response.text
    serialized = json.dumps(payload) + metrics_response.text
    assert private_code not in serialized
    assert "secret/private.py" not in serialized
    assert "[1.0" not in serialized
