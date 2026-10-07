from __future__ import annotations

import json
import re
import uuid

from visual_memory.benchmark.fixtures import build_cases
from visual_memory.benchmark.runner import (
    DeterministicFixtureEmbedder,
    GatewayEmbedder,
    assert_supervisor_idle,
    run_gateway_retrieval_benchmark,
    run_retrieval_benchmark,
)


def test_generated_fixture_benchmark_has_reproducible_retrieval_metrics():
    cases = build_cases()
    result = run_retrieval_benchmark(
        DeterministicFixtureEmbedder(cases),
        cases,
        token_budgets=(280, 560, 1120),
    )

    assert result["fixture_count"] >= 6
    assert result["token_budgets"] == [280, 560, 1120]
    assert result["by_token_budget"]["560"]["retrieval"]["text_to_image"]["recall_at_1"] == 1.0
    assert result["by_token_budget"]["560"]["retrieval"]["image_to_code"]["recall_at_10"] == 1.0
    assert result["performance"]["image_fps"] > 0
    assert result["index_growth"]["estimated_bytes"] > 0


def test_benchmark_report_has_no_vectors_or_fixture_image_payloads():
    cases = build_cases()
    result = run_retrieval_benchmark(
        DeterministicFixtureEmbedder(cases),
        cases[:1],
        token_budgets=(560,),
    )

    serialized = repr(result)
    assert "embeddings" not in result
    assert "vectors" not in result
    assert "PNG" not in serialized
    assert "image_bytes" not in serialized


def test_gpu_benchmark_preflight_requires_idle_supervisor():
    assert_supervisor_idle({"gpu_owner": None, "running_gpu_services": [], "active_jobs": {}})

    try:
        assert_supervisor_idle({
            "gpu_owner": "llm",
            "running_gpu_services": ["llm"],
            "active_jobs": {"llm": 1},
        })
    except RuntimeError as error:
        assert "preflight refused" in str(error)
    else:
        raise AssertionError("expected a busy supervisor to block the VLM benchmark")


def test_gpu_benchmark_preflight_fails_closed_on_unknown_status_shape():
    try:
        assert_supervisor_idle({"gpu_owner": None})
    except RuntimeError as error:
        assert "missing required fields" in str(error)
    else:
        raise AssertionError("unknown supervisor state must not be considered idle")


class InMemoryGateway:
    _multipart = staticmethod(GatewayEmbedder._multipart)

    def __init__(self, cases):
        self.cases = cases
        self.embedder = DeterministicFixtureEmbedder(cases)
        self.by_code = {case.code: case for case in cases}
        self.records = []
        self.image_payloads = {case.label: self._png(case.image) for case in cases}
        self.request_counts = {}

    @staticmethod
    def _png(image):
        from io import BytesIO

        stream = BytesIO()
        image.save(stream, format="PNG")
        return stream.getvalue()

    @staticmethod
    def _parts(body, content_type):
        boundary = content_type.split("boundary=", 1)[1].encode()
        fields = {}
        image = None
        for part in body.split(b"--" + boundary)[1:-1]:
            part = part.lstrip(b"\r\n")
            if part.endswith(b"\r\n"):
                part = part[:-2]
            headers, payload = part.split(b"\r\n\r\n", 1)
            name = re.search(rb'name="([^"]+)"', headers).group(1).decode()
            if b"filename=" in headers:
                image = payload
            else:
                fields[name] = payload.decode()
        return fields, image

    def _rank(self, query, namespace, source):
        vectors = self.records
        vector = query
        ranked = []
        for item in vectors:
            if item["namespace"] != namespace or item["source"] != source:
                continue
            score = sum(left * right for left, right in zip(vector, item["vector"], strict=True))
            ranked.append({"id": item["id"], "score": score})
        return sorted(ranked, key=lambda item: item["score"], reverse=True)

    def request(self, path, body=None, content_type=None, method=None):
        self.request_counts[path] = self.request_counts.get(path, 0) + 1
        if path == "/v1/visual-memory/index":
            fields, image_bytes = self._parts(body, content_type)
            case = next(case for case in self.cases if self.image_payloads[case.label] == image_bytes)
            record_id = str(uuid.uuid4())
            self.records.append({"id": record_id, "label": case.label, "namespace": fields["namespace"],
                                 "source": fields["source"], "vector": self.embedder.embed_image(case.image)})
            return 200, json.dumps({"frame_id": record_id}).encode()
        if path == "/v1/visual-memory/search/image":
            fields, image_bytes = self._parts(body, content_type)
            case = next(case for case in self.cases if self.image_payloads[case.label] == image_bytes)
            vector = self.embedder.embed_image(case.image)
            matches = self._rank(vector, fields["namespace"], fields["source"])
            return 200, json.dumps({"matches": matches}).encode()
        raise AssertionError(f"unexpected request: {path}")

    def json(self, path, payload=None, method=None):
        self.request_counts[path] = self.request_counts.get(path, 0) + 1
        if path == "/v1/visual-memory/status":
            return {
                "loaded": True,
                "process_rss_bytes": 50_000_000,
                "database": {"size_bytes": len(self.records) * 2048},
                "index": {"size_bytes": len(self.records) * 4096},
            }
        if path == "/v1/visual-memory/index/text":
            case = self.by_code[payload["text"]]
            record_id = str(uuid.uuid4())
            self.records.append({"id": record_id, "label": case.label, "namespace": payload["namespace"],
                                 "source": payload["source"],
                                 "vector": self.embedder.embed_text([case.code])[0]})
            return {"frame_id": record_id}
        if path == "/v1/visual-memory/search/text":
            query = payload["query"]
            vector = self.embedder.embed_text([query])[0]
            return {"matches": self._rank(vector, payload["namespace"], payload["source"])}
        raise AssertionError(f"unexpected request: {path}")


def test_gateway_benchmark_measures_persistent_index_and_search_routes():
    cases = build_cases()
    client = InMemoryGateway(cases)
    report = run_gateway_retrieval_benchmark(client, cases[:2], token_budgets=(560,))

    assert report["retrieval"]["text_to_image"]["recall_at_1"] == 1.0
    assert report["retrieval"]["image_to_code"]["mrr"] == 1.0
    assert report["performance"]["image_search"]["requests"] == 2
    assert report["index_growth"]["database_bytes_delta"] == 4 * 2048
    assert report["runtime"]["process_rss_after_bytes"] == 50_000_000
    assert client.request_counts["/v1/visual-memory/index"] == 2
