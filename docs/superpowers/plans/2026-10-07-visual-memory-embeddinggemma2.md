# Multimodal Visual Memory + EmbeddingGemma 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add CPU-first multimodal retrieval memory that indexes screenshots, crops, and safe project text, then supplies bounded evidence and the actual current image to Qwen3-VL for detailed analysis.

**Architecture:** A new internal `visual-memory` FastAPI service loads only EmbeddingGemma 2 text and vision towers on CPU. It exposes embeddings and visual-memory operations, stores authoritative metadata and vectors in SQLite, rebuilds a persistent HNSW index, and creates a model-independent `VisualContextPack`. The gateway authenticates requests, uses the existing `InvocationRouter` and supervisor leases, and orchestrates bounded context into a new VLM endpoint while preserving `/v1/vision/analyze` unchanged.

**Tech Stack:** Python 3.12, FastAPI, Transformers `>=5.19.0,<6` `AutoProcessor`/`AutoModel`, PyTorch CPU, Pillow, SQLite, hnswlib, existing core invocation/router and supervisor APIs, MCP, React/Vite.

**Spec:** User attachment `C:\Users\marcu\.codex\attachments\9b03883a-4e1b-4cbf-b9e0-7f4067fcaea1\Pasted text.txt` (AI-Stack: Multimodal Visual Memory + EmbeddingGemma 2).

## Global Constraints

- Base branch work on commit `35a68fca1c4d950d40e8bb7249e8426bf7428867` in branch `feat/visual-memory-embeddinggemma2`.
- Gateway owns public authentication and routing; supervisor alone owns container/GPU lifecycle.
- `visual-memory` is CPU-resident by default, internal to network `ai`, has no host port, and has no Docker socket.
- Use the existing `Capability.EMBEDDING`, `InvocationOperation.EMBED`, and `InvocationRouter`; keep alias `vision` mapped to `local-vlm`.
- Pin `google/embeddinggemma-2` to immutable revision `914f7f89142e33e77833254d9c9b90c3cef7303b`; use `local_files_only=true` at runtime and do not load the audio tower.
- Require Transformers `>=5.19.0,<6`; EmbeddingGemma 2 landed in Transformers 5.19.0.
- Produce 768-dimensional L2-normalized vectors; record model revision, Transformers/Torch versions, loaded towers, dtype, device, and visual token budget.
- Model weights remain in `models/embedding/`, outside Docker build contexts; durable memory is under `data/visual-memory/`.
- Keep the old `POST /v1/vision/analyze` multipart contract and behavior intact; Qwen receives real image bytes, never decoded embeddings.
- Never allow the visual-memory service to crawl host paths; project indexing runs host-side with explicit root and extension allowlists and excludes `.env`/`.env.*`, secrets, binaries, models, and generated directories.
- Preserve read-only containers, dropped capabilities, `no-new-privileges`, loopback exposure, and Windows PowerShell 5.1 compatibility.
- Do not read `.env` or `.env.*`, change environment files, weaken authentication, or stop/restart an active GPU job.

## Review Focus

- Malformed, oversized, or extreme-dimension images must fail before decode/inference; pin with image-validation and upload-limit tests in Task 1.
- Revision, dimension, or normalization changes must never mix incompatible vectors; pin with index-compatibility/rebuild tests in Task 2.
- Repeated/near-duplicate frames must avoid redundant image/vector writes while preserving temporal references; pin with duplicate and neighbor tests in Task 3.
- Detailed analysis must include the current image and enforce crop/reference/text limits while legacy analysis stays unchanged; pin with gateway/VLM contract tests in Task 4.
- Project indexing must reject traversal, symlink escapes, `.env*`, large files, and non-allowlisted extensions; pin with security tests in Task 5.

---

### Task 1: CPU embedding service and canonical gateway routing (Phase 1)

**Files:**
- Create: `visual_memory/{__init__.py,app.py,model.py,schemas.py,embeddings.py,image_processing.py,Dockerfile,requirements.txt}`
- Modify: `config/models.json`, `compose.yaml`, `gateway/app.py`
- Create: `gateway/adapters/embedding.py`, `tests/visual_memory/test_embeddings.py`, `tests/gateway/test_embedding_routes.py`
- Modify: `tests/core/test_router.py`

**Interfaces:**
- `EmbeddingModel.embed_text(inputs, instruction=None, dimensions=768) -> list[list[float]]`
- `EmbeddingModel.embed_image(image, instruction=None, vision_token_budget=560) -> list[float]`
- Internal endpoints: `POST /v1/embeddings/text`, `POST /v1/embeddings/image`, and `GET /health`.
- Gateway resolves both embedding endpoints through an `Invocation(operation=InvocationOperation.EMBED, ...)`; provider `visual-memory` is obtained from `InvocationRouter` and forwarded through the existing supervisor lease path.

- [x] Write tests for 768-D normalization, text/image request validation, supported image token budgets (280/560/1120), lazy model loading and audio-tower exclusion using a fake model/processor.
- [x] Run `python -m pytest tests/visual_memory/test_embeddings.py -q` and confirm failures are for missing APIs/behavior.
- [x] Implement the minimal embedding service with CPU device selection, `local_files_only`, immutable model revision, image byte/pixel bounds, `AutoProcessor`/`AutoModel`, masked pooling and float32 L2 normalization.
- [x] Add a non-GPU Compose service on `ai` with no host port, read-only root, `/tmp` tmpfs, dropped capabilities, no-new-privileges and runtime-only mounts; register its CPU resources and `local-visual-embedding`/`visual-embedding` without changing `vision`.
- [x] Add the gateway embedding adapter/routes and test that they resolve via the canonical router and supervisor forwarding helpers.
- [x] Run visual-memory, core-router and gateway embedding tests; run `docker compose config` with a safe environment that does not inspect `.env`.
- [x] Commit Task 1 as `feat(embedding): add multimodal embedding worker`.

### Task 2: Durable visual-memory records, crops, namespaces and search (Phase 2)

**Files:**
- Create: `visual_memory/{crops.py,index.py,storage.py,retrieval.py}` and `visual_memory/tests/{test_crops.py,test_storage.py,test_retrieval.py}`
- Modify: `visual_memory/app.py`, `visual_memory/schemas.py`, `visual_memory/requirements.txt`, `.gitignore`
- Create: small generated, license-safe fixtures under `tests/fixtures/visual_memory/`

**Interfaces:**
- `generate_crops(image) -> list[Crop]` returns full image plus deterministic quadrants/strips/center crop with normalized boxes.
- `VisualMemoryStore` persists records and normalized float32 vectors in SQLite; `VectorIndex` loads or rebuilds HNSW from those vectors.
- Index/search endpoints: `POST /v1/visual-memory/index`, `/index/text`, `/search/text`, and `/search/image`.

- [x] Test crop order/coordinates and rejection of empty, low-variance, and near-identical crops.
- [x] Test record/vector persistence over reopen, namespace/session/source/time/tag/crop filters, HNSW rebuild after missing/corrupt/incompatible index, and incompatible model isolation.
- [x] Test exact and perceptual duplicate handling and bounded optional embedding output.
- [x] Implement schema migrations, SQLite transactions, image SHA-256/perceptual hashes, content-addressed compressed image persistence, vectors-as-SQLite-payload, and HNSW index metadata keyed by repo/revision/dimension/normalization.
- [x] Implement deterministic crop embedding, indexing, text/image search, and exact duplicate reuse; keep final near-duplicate thresholds configurable and uncalibrated until benchmarks.
- [x] Run `python -m pytest visual_memory/tests -q` and `python -m pytest tests/visual_memory -q`.
- [x] Commit Task 2 as `feat(memory): add persistent visual vector index`.

### Task 3: Temporal memory, selection and pure-data context packs (Phases 2–4)

**Files:**
- Create: `visual_memory/context_pack.py`, `visual_memory/tests/test_context_pack.py`
- Modify: `visual_memory/storage.py`, `visual_memory/retrieval.py`, `visual_memory/app.py`

**Interfaces:**
- `VisualContextPack` contains current frame metadata, ranked evidence, selected crops, structured context, and text/code chunks.
- Search can expand neighboring sequence frames; `build_context_pack(...)` deduplicates parents and selects bounded diverse evidence.

- [x] Test previous/next sequence links and optional ±2 frame expansion.
- [x] Test deterministic parent/crop deduplication, diversity ranking, image/text maxima, and that the current frame is retained.
- [x] Implement temporal sequence/session persistence, nearest-neighbor novelty metadata without guessed thresholds, and context-pack selection over top-20 candidates.
- [x] Run temporal, retrieval and context-pack tests.
- [x] Commit Task 3 as `feat(memory): add temporal visual context packs`.

### Task 4: Qwen context endpoint and retrieval-assisted gateway orchestration (Phases 3–4)

**Files:**
- Modify: `vlm/app.py`, `vlm/requirements.txt`, `gateway/app.py`, `gateway/adapters/openai_audio.py`
- Create: `gateway/visual_memory.py`, `tests/gateway/test_visual_analysis.py`, `tests/vlm/test_context_analysis.py`

**Interfaces:**
- Preserve `POST /v1/vision/analyze` unchanged; add internal `POST /v1/vision/analyze-context` accepting 1 current frame, at most 6 crops, at most 4 references and structured text.
- Add authenticated gateway `POST /v1/vision/analyze-detailed` and `POST /v1/vision/compare`; all worker calls use configured service routes and supervisor leases.

- [x] Test legacy vision request remains byte/field-compatible and detailed requests reject excess images, uploads, invalid profiles and invalid JSON context.
- [x] Test detailed orchestration returns concrete record IDs/scores/provenance and sends original current/reference image bytes to Qwen exactly once.
- [x] Implement multi-image VLM input and `VisualContextPack` rendering without changing the old route.
- [x] Implement exact/perceptual checks, text/image retrieval, context ranking and one Qwen call; accept/preserve website DOM/viewport/errors and game scene/frame/telemetry metadata.
- [x] Implement reference comparison by record ID or uploaded image, with Qwen receiving both original images.
- [x] Run gateway/VLM visual tests plus existing `tests/gateway/test_audio_vision_adapters.py` and `tests/gateway/test_chat_tracing.py`.
- [ ] Commit Task 4 as `feat(vision): add retrieval-assisted Qwen analysis`.

### Task 5: Safe project indexing, cross-modal retrieval and MCP tools (Phase 5)

**Files:**
- Create: `scripts/index_visual_project.py`, `scripts/index-visual-project.ps1`, `visual_memory/tests/test_code_indexer.py`
- Modify: `visual_memory/app.py`, `visual_memory/storage.py`, `mcp/app.py`, `mcp/requirements.txt`
- Create: `tests/mcp/test_visual_memory_tools.py`

**Interfaces:**
- Host indexer requires explicit project root and namespace, allowlisted text extensions, bounded file/chunk sizes and uploads only to authenticated loopback gateway.
- Add `index_visual_frame`, `search_visual_memory`, and `analyze_visual` MCP tools that call gateway APIs only.

- [ ] Test traversal/symlink escape, `.env`/`.env.*`, secret-pattern exclusions, binary/oversized file rejection, chunk hash/path/range, and gateway-only upload target.
- [ ] Test MCP argument validation and gateway forwarding without implementing retrieval inside MCP.
- [ ] Implement generic text/code record indexing and code search in the shared vector namespace.
- [ ] Implement gateway-backed MCP tools with image/base64 limits and trace propagation.
- [ ] Run indexer and MCP tests; run PowerShell parse validation for the wrapper.
- [ ] Commit Task 5 as `feat(memory): add safe project code retrieval`.

### Task 6: Metrics, diagnostics, model install, dashboard, benchmark and docs (Phase 6)

**Files:**
- Modify: `visual_memory/app.py`, `gateway/app.py`, `scripts/doctor.ps1`, `scripts/smoke.py`, `scripts/smoke.ps1`, dashboard status API/panel files, `README.md`, `ARCHITECTURE.md`
- Create: `scripts/install-visual-embedding.ps1`, `visual_memory/benchmark/`, `docs/visual-memory.md`, related tests/fixtures

**Interfaces:**
- Low-cardinality metrics cover embed requests/failures/latency, preprocessing, index/search, duplicate levels, Qwen escalation/avoidance, and context size.
- Benchmark command compares EmbeddingGemma 2 and Qwen3-VL-Embedding-2B on fixture-labelled retrieval and performance workloads without downloading models in CI.
- Doctor reports service/model/files/database/index readiness and counts without outputting raw vectors or code.

- [ ] Test metric labels omit IDs and private contents; test diagnostics avoid vector/code payloads; add deterministic fake-embedder smoke test.
- [ ] Implement all lightweight benchmark harness scenarios/metrics as opt-in local jobs; keep GPU/model benchmarking behind an explicit command and check supervisor state before any GPU operation.
- [ ] Add `hf download` installation pinned to the exact revision and validate models land only under `models/embedding/`.
- [ ] Add dashboard status for model, revision, device, loaded state, counts, index size and latency percentiles.
- [ ] Update docs with the EmbeddingGemma retrieval/Qwen detailed-analysis split, CPU-first reason, context behavior, setup, retention and why embeddings are not Qwen soft tokens.
- [ ] Run applicable service/core/gateway/MCP/dashboard tests and builds, Python syntax checks, `docker compose config`, and `git diff --check`. Do not invoke `scripts/doctor.ps1` or live `scripts/smoke.ps1` in a way that reads `.env`; use isolated test settings for these checks.
- [ ] Commit Task 6 as `test(memory): add visual retrieval benchmark and docs`.

---

## Spec coverage self-check

- Service/model/API/token budgets/crops: Tasks 1–2.
- SQLite/HNSW/namespaces/duplicate gate/temporal memory: Tasks 2–3.
- Qwen multiple images, detailed orchestration, website/game/compare: Task 4.
- Code indexing and MCP: Task 5.
- Metrics/dashboard/benchmark/installation/provenance/docs/doctor/smoke: Task 6.
- Authentication, supervisor leases, runtime mounts, CPU-only placement, legacy vision compatibility and `.env` safety are global constraints and regression tests in Tasks 1, 4, 5 and 6.
