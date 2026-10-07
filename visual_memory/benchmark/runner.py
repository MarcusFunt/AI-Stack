from __future__ import annotations

import json
import math
import os
import statistics
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Protocol, Sequence

from PIL import Image

from .fixtures import BenchmarkCase


class BenchmarkEmbedder(Protocol):
    dimension: int

    def embed_text(self, inputs: Sequence[str], instruction: str | None = None) -> list[list[float]]: ...

    def embed_image(
        self,
        image: Image.Image,
        instruction: str | None = None,
        vision_token_budget: int = 560,
    ) -> list[float]: ...

    def embed_multimodal(
        self, image: Image.Image, text: str, instruction: str | None = None, vision_token_budget: int = 560
    ) -> list[float]: ...


class DeterministicFixtureEmbedder:
    """Stable fixture-only embedder for CI; it does not model real retrieval quality."""

    dimension = 8

    def __init__(self, cases: Sequence[BenchmarkCase]) -> None:
        self._labels = {case.label: position for position, case in enumerate(cases)}
        self._text_labels = {
            text: case.label
            for case in cases
            for text in (case.query, case.code)
        }

    def _vector(self, label: str) -> list[float]:
        vector = [0.0] * self.dimension
        vector[self._labels[label] % self.dimension] = 1.0
        return vector

    def _label_for_text(self, value: str) -> str:
        if value in self._text_labels:
            return self._text_labels[value]
        for label in self._labels:
            if label in value or any(token in value.lower() for token in label.split("_")):
                return label
        # The exact query strings are case descriptions; their order is stable in build_cases.
        for label in self._labels:
            if label.replace("_", " ") in value.lower():
                return label
        raise ValueError("fixture text is not mapped to a benchmark label")

    def embed_text(self, inputs: Sequence[str], instruction: str | None = None) -> list[list[float]]:
        return [self._vector(self._label_for_text(value)) for value in inputs]

    def embed_image(self, image: Image.Image, instruction: str | None = None, vision_token_budget: int = 560) -> list[float]:
        return self._vector(str(image.info["benchmark_label"]))

    def embed_multimodal(self, image, text, instruction=None, vision_token_budget=560) -> list[float]:
        return self.embed_image(image, instruction=instruction, vision_token_budget=vision_token_budget)


class GatewayEmbedder:
    """Calls authenticated gateway embedding endpoints using only the process API key."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 180.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.dimension = 768

    def request(self, path: str, body: bytes | None = None, content_type: str | None = None, method: str | None = None):
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method=method or ("POST" if body is not None else "GET"),
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            raise RuntimeError(f"gateway request {path} returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"gateway request {path} failed: {exc.reason}") from exc

    def json(self, path: str, payload: dict | None = None, method: str | None = None) -> dict:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
        _, response = self.request(path, body, "application/json" if body is not None else None, method)
        value = json.loads(response)
        if not isinstance(value, dict):
            raise RuntimeError(f"gateway request {path} returned a non-object response")
        return value

    def embed_text(self, inputs: Sequence[str], instruction: str | None = None) -> list[list[float]]:
        payload = {"inputs": list(inputs), "dimensions": self.dimension}
        if instruction:
            payload["instruction"] = instruction
        return self.json("/v1/embeddings/text", payload)["embeddings"]

    @staticmethod
    def _multipart(fields: dict[str, str], image: Image.Image) -> tuple[bytes, str]:
        import io

        stream = io.BytesIO()
        image.save(stream, format="PNG")
        boundary = "ai-stack-benchmark-" + uuid.uuid4().hex
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.extend((
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
                value.encode("utf-8"), b"\r\n",
            ))
        chunks.extend((
            f"--{boundary}\r\n".encode("ascii"),
            b'Content-Disposition: form-data; name="image"; filename="fixture.png"\r\n',
            b"Content-Type: image/png\r\n\r\n", stream.getvalue(), b"\r\n",
            f"--{boundary}--\r\n".encode("ascii"),
        ))
        return b"".join(chunks), f"multipart/form-data; boundary={boundary}"

    def embed_image(self, image: Image.Image, instruction: str | None = None, vision_token_budget: int = 560) -> list[float]:
        fields = {"vision_token_budget": str(vision_token_budget)}
        if instruction:
            fields["instruction"] = instruction
        body, content_type = self._multipart(fields, image)
        _, raw = self.request("/v1/embeddings/image", body, content_type)
        return json.loads(raw)["embeddings"][0]

    def embed_multimodal(self, image, text, instruction=None, vision_token_budget=560) -> list[float]:
        combined = " ".join(part.strip() for part in (instruction, text) if part and part.strip())
        return self.embed_image(image, instruction=combined, vision_token_budget=vision_token_budget)


class Qwen3VLEmbeddingChallenger:
    """Optional CPU-only, local-files-only challenger with an explicit immutable revision."""

    def __init__(self, model_path: str | Path, revision: str) -> None:
        import re

        path = Path(model_path).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("Qwen challenger model path must be an existing local directory")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
            raise ValueError("Qwen challenger revision must be an immutable 40-character commit SHA")
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError("install visual_memory/benchmark/requirements.txt for the Qwen challenger") from exc
        self._model = SentenceTransformer(
            str(path),
            device="cpu",
            revision=revision,
            local_files_only=True,
        )
        self.revision = revision.lower()
        self.dimension = int(self._model.get_sentence_embedding_dimension())
        self.model_path = str(path)

    def _encode(self, values, instruction=None):
        options = {"convert_to_numpy": True, "normalize_embeddings": True, "show_progress_bar": False}
        if instruction:
            options["prompt"] = instruction
        vectors = self._model.encode(values, **options)
        if len(vectors.shape) == 1:
            return vectors.tolist()
        return vectors.tolist()

    def embed_text(self, inputs, instruction=None):
        return self._encode(list(inputs), instruction)

    def embed_image(self, image, instruction=None, vision_token_budget=560):
        return self._encode([{"image": image}], instruction)[0]

    def embed_multimodal(self, image, text, instruction=None, vision_token_budget=560):
        return self._encode([{"image": image, "text": text}], instruction)[0]


def assert_supervisor_idle(status: dict) -> None:
    if not isinstance(status, dict):
        raise RuntimeError("GPU preflight failed: supervisor returned an invalid status")
    if "gpu_owner" not in status or "running_gpu_services" not in status or "active_jobs" not in status:
        raise RuntimeError("GPU preflight failed: supervisor status is missing required fields")
    running = status.get("running_gpu_services")
    jobs = status.get("active_jobs")
    if not isinstance(running, list) or not isinstance(jobs, dict):
        raise RuntimeError("GPU preflight failed: supervisor status fields have invalid types")
    active = {str(name): count for name, count in jobs.items() if isinstance(count, (int, float)) and count > 0}
    if status.get("gpu_owner") or running or active:
        raise RuntimeError(
            "GPU preflight refused: supervisor reports a GPU owner, running GPU service, or active job; "
            "wait for the current lease to finish before running the VLM benchmark"
        )


def percentile(values: Sequence[float], percentage: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentage
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - index) + ordered[upper] * (index - lower)


def _unit(vector: Sequence[float]) -> list[float]:
    values = [float(value) for value in vector]
    if not values or any(not math.isfinite(value) for value in values):
        raise ValueError("benchmark embedder returned an invalid vector")
    norm = math.sqrt(math.fsum(value * value for value in values))
    if norm <= 1e-12:
        raise ValueError("benchmark embedder returned a zero vector")
    return [value / norm for value in values]


def _rank(query: Sequence[float], documents: Sequence[tuple[str, Sequence[float]]]) -> list[str]:
    return [
        label for label, _ in sorted(
            documents,
            key=lambda item: math.fsum(a * b for a, b in zip(query, item[1], strict=True)),
            reverse=True,
        )
    ]


def _retrieval_metrics(ranked_ids: Sequence[Sequence[str]], expected: Sequence[str]) -> dict:
    count = len(expected)
    values = {k: 0 for k in (1, 5, 10)}
    reciprocal_ranks = []
    for ranking, expected_id in zip(ranked_ids, expected, strict=True):
        rank = next((index for index, item in enumerate(ranking, start=1) if item == expected_id), None)
        for k in values:
            values[k] += int(rank is not None and rank <= k)
        reciprocal_ranks.append(1.0 / rank if rank else 0.0)
    return {
        **{f"recall_at_{k}": round(hit_count / count, 4) if count else 0.0 for k, hit_count in values.items()},
        "mrr": round(statistics.fmean(reciprocal_ranks), 4) if reciprocal_ranks else 0.0,
        "queries": count,
    }


def run_retrieval_benchmark(
    embedder: BenchmarkEmbedder,
    cases: Sequence[BenchmarkCase],
    *,
    token_budgets: Sequence[int] = (280, 560, 1120),
) -> dict:
    if not cases:
        raise ValueError("benchmark requires at least one fixture case")
    if not token_budgets or any(value not in {280, 560, 1120} for value in token_budgets):
        raise ValueError("token budgets must be selected from 280, 560, and 1120")

    query_texts = [case.query for case in cases]
    code_texts = [case.code for case in cases]
    text_latencies = []
    text_vectors = []
    for query in query_texts:
        started = time.perf_counter()
        text_vectors.append(_unit(embedder.embed_text([query], instruction="Retrieve a relevant visual record.")[0]))
        text_latencies.append(time.perf_counter() - started)
    code_latencies = []
    code_vectors = []
    for code in code_texts:
        started = time.perf_counter()
        code_vectors.append(_unit(embedder.embed_text([code], instruction="Represent source code for retrieval.")[0]))
        code_latencies.append(time.perf_counter() - started)
    dimension = len(text_vectors[0])
    if dimension != int(embedder.dimension) or any(len(vector) != dimension for vector in text_vectors + code_vectors):
        raise ValueError("benchmark embedder returned inconsistent dimensions")

    image_runs: dict[str, dict] = {}
    for budget in token_budgets:
        image_vectors = []
        image_latencies = []
        for case in cases:
            started = time.perf_counter()
            vector = embedder.embed_image(case.image, vision_token_budget=budget)
            image_latencies.append(time.perf_counter() - started)
            image_vectors.append(_unit(vector))
        if any(len(vector) != dimension for vector in image_vectors):
            raise ValueError("benchmark embedder returned inconsistent dimensions")

        image_docs = [(f"image:{case.label}", vector) for case, vector in zip(cases, image_vectors, strict=True)]
        code_docs = [(f"code:{case.label}", vector) for case, vector in zip(cases, code_vectors, strict=True)]
        text_rankings = [_rank(query, image_docs) for query in text_vectors]
        image_code_rankings = [_rank(query, code_docs) for query in image_vectors]
        text_code_rankings = [_rank(query, code_docs) for query in text_vectors]
        record_count = len(image_docs) + len(code_docs)
        metadata_estimate = 512
        image_runs[str(budget)] = {
            "retrieval": {
                "text_to_image": _retrieval_metrics(text_rankings, [f"image:{case.label}" for case in cases]),
                "image_to_code": _retrieval_metrics(image_code_rankings, [f"code:{case.label}" for case in cases]),
                "text_to_code": _retrieval_metrics(text_code_rankings, [f"code:{case.label}" for case in cases]),
            },
            "performance": {
                "image_p50_seconds": round(percentile(image_latencies, 0.50), 6),
                "image_p95_seconds": round(percentile(image_latencies, 0.95), 6),
                "image_fps": round(len(image_latencies) / max(sum(image_latencies), 1e-12), 3),
                "text_p50_seconds": round(percentile(text_latencies, 0.50), 6),
                "text_p95_seconds": round(percentile(text_latencies, 0.95), 6),
                "code_p50_seconds": round(percentile(code_latencies, 0.50), 6),
                "code_p95_seconds": round(percentile(code_latencies, 0.95), 6),
            },
            "index_growth": {
                "estimated_bytes": record_count * (dimension * 4 + metadata_estimate),
                "estimated_bytes_per_record": dimension * 4 + metadata_estimate,
                "estimated_records": record_count,
            },
        }

    return {
        "schema_version": 1,
        "embedder": type(embedder).__name__,
        "dimension": dimension,
        "fixture_count": len(cases),
        "token_budgets": list(token_budgets),
        "by_token_budget": image_runs,
        "retrieval": image_runs[str(560 if 560 in token_budgets else token_budgets[0])]["retrieval"],
        "performance": image_runs[str(560 if 560 in token_budgets else token_budgets[0])]["performance"],
        "index_growth": image_runs[str(560 if 560 in token_budgets else token_budgets[0])]["index_growth"],
        "notes": [
            "Generated fixture results validate the harness; they are not a claim about real-world retrieval quality.",
            "Index growth is an estimate using float32 vectors plus a fixed metadata allowance, not a measured HNSW file size.",
        ],
    }


def _route_metrics(latencies: Sequence[float], *, total_records: int | None = None) -> dict:
    result = {
        "p50_seconds": round(percentile(latencies, 0.50), 6),
        "p95_seconds": round(percentile(latencies, 0.95), 6),
        "requests": len(latencies),
    }
    if total_records is not None:
        result["records_per_second"] = round(total_records / max(sum(latencies), 1e-12), 3)
    return result


def run_gateway_retrieval_benchmark(
    client: GatewayEmbedder,
    cases: Sequence[BenchmarkCase],
    *,
    token_budgets: Sequence[int] = (280, 560, 1120),
) -> dict:
    """Exercise the authenticated index and search APIs, including persistent HNSW retrieval."""
    if not cases:
        raise ValueError("benchmark requires at least one fixture case")
    if not token_budgets or any(value not in {280, 560, 1120} for value in token_budgets):
        raise ValueError("token budgets must be selected from 280, 560, and 1120")
    before = client.json("/v1/visual-memory/status")
    by_budget: dict[str, dict] = {}
    namespaces: list[str] = []
    all_image_latencies: list[float] = []
    for budget in token_budgets:
        namespace = f"benchmark:retrieval-{uuid.uuid4().hex[:10]}-{budget}"
        namespaces.append(namespace)
        image_ids: dict[str, str] = {}
        code_ids: dict[str, str] = {}
        image_index_latencies: list[float] = []
        text_index_latencies: list[float] = []
        text_search_latencies: list[float] = []
        image_search_latencies: list[float] = []
        text_to_image_rankings: list[list[str]] = []
        text_to_code_rankings: list[list[str]] = []
        image_to_code_rankings: list[list[str]] = []

        for case in cases:
            image_body, image_type = client._multipart({
                "namespace": namespace,
                "source": case.profile,
                "crop_mode": "none",
                "vision_token_budget": str(budget),
            }, case.image)
            started = time.perf_counter()
            _, raw = client.request("/v1/visual-memory/index", image_body, image_type)
            image_index_latencies.append(time.perf_counter() - started)
            image_id = json.loads(raw).get("frame_id")
            if not isinstance(image_id, str) or not image_id:
                raise RuntimeError("visual-memory image index response did not contain frame_id")
            image_ids[image_id] = case.label

            text_payload = {
                "text": case.code,
                "namespace": namespace,
                "source": "code",
                "metadata": {"source_path": f"benchmark/{case.label}.py", "fixture_label": case.label},
            }
            started = time.perf_counter()
            text_result = client.json("/v1/visual-memory/index/text", text_payload)
            text_index_latencies.append(time.perf_counter() - started)
            code_id = text_result.get("frame_id")
            if not isinstance(code_id, str) or not code_id:
                raise RuntimeError("visual-memory text index response did not contain frame_id")
            code_ids[code_id] = case.label
        all_image_latencies.extend(image_index_latencies)

        for case in cases:
            image_query = {"query": case.query, "namespace": namespace, "source": case.profile,
                           "top_k": 50, "include_crops": False}
            started = time.perf_counter()
            text_image_result = client.json("/v1/visual-memory/search/text", image_query)
            text_search_latencies.append(time.perf_counter() - started)
            text_to_image_rankings.append([
                image_ids[match["id"]] for match in text_image_result.get("matches", [])
                if match.get("id") in image_ids
            ])

            code_query = {"query": case.query, "namespace": namespace, "source": "code",
                          "top_k": 50, "include_crops": False}
            started = time.perf_counter()
            text_code_result = client.json("/v1/visual-memory/search/text", code_query)
            text_search_latencies.append(time.perf_counter() - started)
            text_to_code_rankings.append([
                code_ids[match["id"]] for match in text_code_result.get("matches", [])
                if match.get("id") in code_ids
            ])

            image_body, image_type = client._multipart({
                "namespace": namespace, "source": "code", "top_k": "50", "include_crops": "false",
            }, case.image)
            started = time.perf_counter()
            _, raw = client.request("/v1/visual-memory/search/image", image_body, image_type)
            image_search_latencies.append(time.perf_counter() - started)
            image_result = json.loads(raw)
            image_to_code_rankings.append([
                code_ids[match["id"]] for match in image_result.get("matches", [])
                if match.get("id") in code_ids
            ])

        by_budget[str(budget)] = {
            "namespace": namespace,
            "retrieval": {
                "text_to_image": _retrieval_metrics(text_to_image_rankings, [case.label for case in cases]),
                "image_to_code": _retrieval_metrics(image_to_code_rankings, [case.label for case in cases]),
                "text_to_code": _retrieval_metrics(text_to_code_rankings, [case.label for case in cases]),
            },
            "performance": {
                "image_index": _route_metrics(image_index_latencies, total_records=len(cases)),
                "text_index": _route_metrics(text_index_latencies, total_records=len(cases)),
                "text_search": _route_metrics(text_search_latencies),
                "image_search": _route_metrics(image_search_latencies),
                "first_image_index_seconds": round(image_index_latencies[0], 6),
                "image_fps": round(len(image_index_latencies) / max(sum(image_index_latencies), 1e-12), 3),
            },
            "index_growth": {
                "estimated_bytes": 2 * len(cases) * (768 * 4 + 512),
                "estimated_bytes_per_record": 768 * 4 + 512,
                "estimated_records": 2 * len(cases),
            },
        }
    after = client.json("/v1/visual-memory/status")
    selected = by_budget[str(560 if 560 in token_budgets else token_budgets[0])]
    before_database = before.get("database", {})
    after_database = after.get("database", {})
    before_index = before.get("index", {})
    after_index = after.get("index", {})
    return {
        "schema_version": 1,
        "embedder": "EmbeddingGemma 2 through authenticated gateway and persistent visual-memory index",
        "dimension": 768,
        "fixture_count": len(cases),
        "token_budgets": list(token_budgets),
        "namespaces": namespaces,
        "by_token_budget": by_budget,
        "retrieval": selected["retrieval"],
        "performance": selected["performance"],
        "index_growth": {
            **selected["index_growth"],
            "database_bytes_delta": int(after_database.get("size_bytes", 0)) - int(before_database.get("size_bytes", 0)),
            "hnsw_bytes_delta": int(after_index.get("size_bytes", 0)) - int(before_index.get("size_bytes", 0)),
        },
        "runtime": {
            "initially_loaded": bool(before.get("loaded")),
            "process_rss_before_bytes": before.get("process_rss_bytes"),
            "process_rss_after_bytes": after.get("process_rss_bytes"),
            "gpu_vram_bytes": None,
        },
        "notes": [
            "The first image index latency includes model loading when the worker was initially unloaded.",
            "Database and HNSW byte deltas cover the worker's shared store during this run; concurrent indexing can affect them.",
            "Fixture results are synthetic and are not a claim about real-world retrieval quality.",
        ],
    }


def _multipart_request(fields: dict[str, str], files: list[tuple[str, str, Image.Image]]) -> tuple[bytes, str]:
    import io

    boundary = "ai-stack-vlm-benchmark-" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend((f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(), value.encode(), b"\r\n"))
    for name, filename, image in files:
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        chunks.extend((
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: image/png\r\n\r\n", stream.getvalue(), b"\r\n",
        ))
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def run_qwen_vlm_comparison(client: GatewayEmbedder, cases: Sequence[BenchmarkCase], output_path: str | Path) -> dict:
    """Explicitly compare screenshot-only Qwen with retrieval-assisted analysis for human scoring."""
    status = client.json("/v1/system/status")
    assert_supervisor_idle(status)
    namespace = f"benchmark:vlm-{uuid.uuid4().hex[:12]}"
    for case in cases:
        body, content_type = client._multipart({
            "namespace": namespace,
            "source": case.profile,
            "crop_mode": "basic",
            "vision_token_budget": "balanced",
        }, case.image)
        client.request("/v1/visual-memory/index", body, content_type)

    rows = []
    for case in cases:
        prompt = f"Identify the visible state and whether this expected change is present: {case.expected_change}"
        baseline_body, baseline_type = _multipart_request(
            {"prompt": prompt}, [("image", f"{case.label}.png", case.image)]
        )
        _, baseline_raw = client.request("/v1/vision/analyze", baseline_body, baseline_type)
        detailed_body, detailed_type = _multipart_request(
            {"prompt": prompt, "analysis_profile": case.profile, "namespace": namespace},
            [("image", f"{case.label}.png", case.image)],
        )
        _, detailed_raw = client.request("/v1/vision/analyze-detailed", detailed_body, detailed_type)
        rows.append({
            "fixture_label": case.label,
            "profile": case.profile,
            "expected_change": case.expected_change,
            "qwen_only": json.loads(baseline_raw),
            "retrieval_assisted": json.loads(detailed_raw),
            "human_score": None,
        })
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    return {
        "rows": len(rows),
        "output": str(destination),
        "namespace": namespace,
        "scoring": "Raw outputs and expected labels are saved for human scoring; no LLM judge is used.",
    }
