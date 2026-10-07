# Visual memory

AI-Stack uses EmbeddingGemma 2 to find useful visual and text evidence, then uses Qwen3-VL when a request needs detailed visual reasoning. EmbeddingGemma does not replace Qwen.

```text
screenshots, crops, and indexed text/code
                 │
                 ▼
       EmbeddingGemma 2 (CPU)
                 │
       SQLite vectors + HNSW
                 │
                 ▼
 compact context pack: up to 6 crops,
 4 references, and 8 text chunks
                 │
                 ▼
     Qwen3-VL through the gateway
```

The service is internal to the Compose network, has no host port, and has no Docker socket. The gateway authenticates public calls and resolves embedding requests through the existing model registry. The visual-memory worker is CPU-only by default so frequent retrieval does not evict Qwen or compete for the RTX 3060.

## Models and provenance

The primary embedding model is `google/embeddinggemma-2`, pinned in the repository to revision `914f7f89142e33e77833254d9c9b90c3cef7303b`. It uses the text and vision encoders, returns normalized 768-dimensional vectors, and loads with local files only. Runtime does not fetch model weights. Model provenance and availability are exposed by the authenticated status endpoint and dashboard.

Qwen3-VL receives the actual current image, selected crops, and reference images for detailed analysis. The pooled 768-dimensional EmbeddingGemma vector has discarded spatial detail and is not a sequence of Qwen tokens. AI-Stack does not project it into Qwen's token space. The current image remains part of the Qwen request.

## Install and start

From the repository root in Windows PowerShell:

```powershell
python -m pip install --user huggingface_hub
.\scripts\install-visual-embedding.ps1
.\scripts\ai.ps1 start visual-memory
.\scripts\ai.ps1 status
```

The installer uses the modern `hf download` command, pins the revision above, verifies the expected configuration files, and writes a small provenance record under `models/embedding/embeddinggemma-2/`. The model directory is a runtime mount and is not part of a Docker build context. The service can answer readiness and diagnostics without loading the model; first embedding use loads it locally.

For one authenticated live index-and-search check using a generated frame, run `.\scripts\smoke.ps1 -VisualMemoryOnly` after starting the service. This check reads `AI_API_KEY` from the current process environment and uses only the CPU visual-memory worker.

Use the gateway API key through the process environment for authenticated requests. Do not put it in a script, benchmark output, or source file.

## Gateway API

All public operations require the gateway bearer token.

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/embeddings/text` | Embed text or code. |
| `POST /v1/embeddings/image` | Embed one image; accepts token budgets `fast`/280, `balanced`/560, or `detail`/1120. |
| `POST /v1/visual-memory/index` | Store an image, its metadata, and full-frame/basic-crop vectors. |
| `POST /v1/visual-memory/index/text` | Store a bounded text/code record in a namespace. |
| `POST /v1/visual-memory/search/text` | Search indexed images and text/code with namespace and metadata filters. |
| `POST /v1/visual-memory/search/image` | Find similar images and crops. |
| `GET /v1/visual-memory/status` | Read model, database, index, and latency diagnostics. |
| `POST /v1/vision/analyze-detailed` | Retrieve bounded context, then ask Qwen3-VL to analyze the current image. |
| `POST /v1/vision/compare` | Compare the current image with an uploaded or stored reference using Qwen3-VL. |

`/v1/vision/analyze` keeps its existing request behavior. Detailed analysis reports evidence record IDs and embedding/Qwen provenance. The context pack is bounded so many near-identical frames do not consume the reference budget.

For callers that can reuse an earlier answer, detailed analysis accepts `skip_if_identical_frame=true`. When the uploaded bytes exactly match an indexed image in the same namespace, it returns `analysis_skipped` and avoids a Qwen call. This is opt-in because a new prompt can require a new answer. Similarity-based skipping is not enabled; novelty thresholds need representative local benchmark data first. The escalation and avoided-escalation counters distinguish these paths.

MCP exposes `index_visual_frame`, `search_visual_memory`, and `analyze_visual` through the gateway. It does not implement its own index or call the embedding worker directly.

## Project source retrieval

The host-side indexer requires a project root and namespace:

```powershell
.\scripts\index-visual-project.ps1 -ProjectRoot D:\projects\my-app -Namespace code:my-app
```

It uploads only allowlisted text extensions to the loopback gateway. It excludes `.env` and `.env.*`, detected secret values, binary and oversized files, model trees, generated output, and paths that escape the selected root. Indexed source chunks include a relative path, line bounds, and content hash. Treat the visual-memory database as project data and protect its host directory accordingly.

## Storage and retention

SQLite is authoritative for records, metadata, relationships, and durable float32 vectors. HNSW is a rebuildable search accelerator. Both live under `data/visual-memory/`; model files stay under `models/embedding/`.

Image indexing accepts `retention_policy=metadata-only`, `thumbnail`, or `full-image`; the default is `full-image`. Metadata-only keeps image metadata and vectors but no image bytes. Thumbnail stores a WebP rendition with its longest edge capped at 512 pixels, and full-image stores a lossless WebP rendition. Exact duplicates reuse the asset; a later request can upgrade its retained rendition, while lower-fidelity requests never downgrade it. The detailed and compare gateway routes accept the same field. Retrieval still returns metadata for records with no retained image, and contextual vision analysis omits their image attachments. Automatic expiration and a namespace cleanup API are not implemented. Avoid sending continuous full-resolution capture at video rates until capture decimation is configured; any database/image-store maintenance must be operator-managed while the service is stopped.

## Benchmarking

The default benchmark creates eight deterministic website and game screenshots locally and uses a fake embedder. It is suitable for verifying the harness and has no model download or GPU work:

```powershell
python -m visual_memory.benchmark --embedder fake --output $env:TEMP\visual-memory-fixtures.json
```

For a real EmbeddingGemma run, start the stack and pass the gateway API key through the process environment:

```powershell
python -m visual_memory.benchmark --embedder gateway --output data/benchmarks/visual-memory-embeddinggemma.json
```

The report compares text-to-image, image-to-code, and text-to-code retrieval using Recall@1/5/10 and MRR. Gateway mode exercises persistent index and search APIs and records p50/p95 end-to-end latency, image throughput, process RSS when available, and estimated plus observed database/HNSW growth for 280, 560, and 1120 vision tokens. Gateway mode stores fixtures and code in unique persistent benchmark namespaces; there is no namespace cleanup API yet. Generated fixtures are deliberately small and synthetic; results are a local baseline, not a representative quality claim. Real screenshots, known labels, and representative code chunks should be added to a private evaluation set before tuning thresholds or token budgets.

An optional [Qwen3-VL-Embedding-2B challenger](https://huggingface.co/Qwen/Qwen3-VL-Embedding-2B) can run on CPU from a local snapshot only. Its model card and Sentence Transformers docs cover text, image, and mixed-modality inputs. Install its benchmark-only dependency and provide an existing local directory and immutable 40-character commit SHA:

```powershell
python -m pip install -r visual_memory/benchmark/requirements.txt
python -m visual_memory.benchmark --embedder qwen `
  --qwen-model-path D:\models\Qwen3-VL-Embedding-2B `
  --qwen-revision <immutable-commit-sha> `
  --token-budgets 560
```

This path sets Hugging Face offline mode and uses CPU. The challenger is not a runtime service and cannot claim CUDA outside the supervisor.

The explicit `--include-vlm` option compares the legacy screenshot-only Qwen route with retrieval-assisted detailed analysis and writes raw answers plus expected labels to JSONL for human scoring. It first checks supervisor owner, running GPU services, and active leases, then refuses if any are present or the status is incomplete. The detailed run indexes synthetic fixtures in a unique persistent benchmark namespace. Remove that namespace through future retention tooling; no automatic LLM judge is used.

## Diagnostics and metrics

The dashboard Visual Memory card and `GET /v1/visual-memory/status` show model/revision, device, loaded state, model files, SQLite counts/writability, index compatibility/size, and p50/p95 timings. `/metrics` exposes low-cardinality counters and histograms without record IDs, code, vectors, or user-provided labels. Doctor output reports counts and readiness only.
