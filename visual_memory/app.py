import json
import os
import threading

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from .embeddings import EmbeddingModel, resolve_vision_token_budget
from .image_processing import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, decode_image
from .model import EmbeddingGemma2Backend, MODEL_REPO
from .retrieval import VisualMemoryEngine
from .schemas import TextEmbeddingsRequest, VisualTextSearchRequest
from .storage import VisualMemoryStore


app = FastAPI(title="AI-Stack visual memory")
backend = EmbeddingGemma2Backend()
embedding_model = EmbeddingModel(backend)
_memory_engine = None
_memory_engine_lock = threading.Lock()


def get_memory_engine() -> VisualMemoryEngine:
    global _memory_engine
    if _memory_engine is None:
        with _memory_engine_lock:
            if _memory_engine is None:
                db_path = os.getenv("VISUAL_MEMORY_DB_PATH", "/data/visual-memory.sqlite3")
                image_root = os.getenv("VISUAL_MEMORY_IMAGE_ROOT", "/data/images")
                store = VisualMemoryStore(
                    db_path,
                    image_root,
                    model=MODEL_REPO,
                    revision=backend.revision,
                    dimension=embedding_model.dimension,
                )
                _memory_engine = VisualMemoryEngine(
                    store,
                    embedding_model,
                    index_dir=os.getenv("VISUAL_MEMORY_INDEX_PATH", "/data/indexes"),
                    perceptual_duplicate_distance=_perceptual_duplicate_distance(),
                )
    return _memory_engine


def _perceptual_duplicate_distance() -> int | None:
    value = os.getenv("VISUAL_MEMORY_PHASH_DUPLICATE_DISTANCE", "").strip()
    if not value:
        return None
    try:
        threshold = int(value)
    except ValueError as exc:
        raise RuntimeError("VISUAL_MEMORY_PHASH_DUPLICATE_DISTANCE must be an integer from 0 to 64") from exc
    if not 0 <= threshold <= 64:
        raise RuntimeError("VISUAL_MEMORY_PHASH_DUPLICATE_DISTANCE must be an integer from 0 to 64")
    return threshold


def _json_object(value: str | None, field: str) -> dict:
    if value is None or not value.strip():
        return {}
    if len(value) > 64_000:
        raise HTTPException(400, f"{field} is too large")
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"{field} must be valid JSON") from exc
    if not isinstance(result, dict):
        raise HTTPException(400, f"{field} must be a JSON object")
    return result


def _json_tags(value: str | None) -> list[str]:
    if value is None or not value.strip():
        return []
    if len(value) > 16_000:
        raise HTTPException(400, "tags_json is too large")
    try:
        tags = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "tags_json must be valid JSON") from exc
    if not isinstance(tags, list) or len(tags) > 100 or any(not isinstance(tag, str) or len(tag) > 128 for tag in tags):
        raise HTTPException(400, "tags_json must be an array of at most 100 short strings")
    return tags


@app.get("/health")
def health():
    provenance = backend.provenance()
    return {
        "status": "ok",
        **provenance,
        "vision_token_budgets": {"fast": 280, "balanced": 560, "detail": 1120},
    }


@app.post("/v1/embeddings/text")
def embed_text(request: TextEmbeddingsRequest):
    if request.dimensions != 768:
        raise HTTPException(400, "dimensions must be 768")
    try:
        if any(not value.strip() or len(value) > 32_000 for value in request.inputs):
            raise ValueError("each input must be non-empty and at most 32000 characters")
        embeddings = embedding_model.embed_text(
            request.inputs,
            instruction=request.instruction,
            dimensions=request.dimensions,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    provenance = backend.provenance()
    return {
        "model": MODEL_REPO,
        "revision": provenance["revision"],
        "dimension": 768,
        "embeddings": embeddings,
        "metadata": provenance,
    }


@app.post("/v1/embeddings/image")
async def embed_image(
    image: UploadFile = File(...),
    instruction: str | None = Form(default=None, max_length=1000),
    vision_token_budget: str | None = Form(default=None),
):
    data = await image.read(MAX_IMAGE_BYTES + 1)
    try:
        decoded = decode_image(data, max_bytes=MAX_IMAGE_BYTES, max_pixels=MAX_IMAGE_PIXELS)
        budget_mode, token_budget = resolve_vision_token_budget(vision_token_budget)
        vector = embedding_model.embed_image(
            decoded,
            instruction=instruction,
            vision_token_budget=token_budget,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        await image.close()
    provenance = backend.provenance()
    return {
        "model": MODEL_REPO,
        "revision": provenance["revision"],
        "dimension": 768,
        "embeddings": [vector],
        "metadata": {
            **provenance,
            "vision_token_budget": {"mode": budget_mode, "tokens": token_budget},
        },
    }


@app.post("/v1/visual-memory/index")
async def index_visual_memory_image(
    image: UploadFile = File(...),
    namespace: str = Form(..., max_length=256),
    source: str | None = Form(default=None, max_length=128),
    session_id: str | None = Form(default=None, max_length=256),
    timestamp: str | None = Form(default=None, max_length=64),
    metadata_json: str | None = Form(default=None),
    tags_json: str | None = Form(default=None),
    crop_mode: str = Form(default="basic"),
    vision_token_budget: str | None = Form(default=None),
):
    data = await image.read(MAX_IMAGE_BYTES + 1)
    await image.close()
    try:
        metadata = _json_object(metadata_json, "metadata_json")
        tags = _json_tags(tags_json)
        _, token_budget = resolve_vision_token_budget(vision_token_budget)
        result = get_memory_engine().index_image(
            data,
            namespace=namespace,
            source=source,
            session_id=session_id,
            timestamp=timestamp,
            metadata=metadata,
            tags=tags,
            crop_mode=crop_mode,
            vision_token_budget=token_budget,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    engine = get_memory_engine()
    return {
        "frame_id": result.frame_id,
        "observation_id": result.observation_id,
        "embeddings_created": result.embeddings_created,
        "duplicate": result.duplicate_level is not None,
        "duplicate_level": result.duplicate_level,
        "image_sha256": result.image_sha256,
        "model": engine.model,
        "revision": engine.revision,
    }


@app.post("/v1/visual-memory/search/text")
def search_visual_memory_text(request: VisualTextSearchRequest):
    if request.include_embedding and request.top_k > 10:
        raise HTTPException(400, "include_embedding is limited to top_k of 10")
    try:
        return get_memory_engine().search_text(**request.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/v1/visual-memory/search/image")
async def search_visual_memory_image(
    image: UploadFile = File(...),
    namespace: str | None = Form(default=None, max_length=256),
    source: str | None = Form(default=None, max_length=128),
    session_id: str | None = Form(default=None, max_length=256),
    start_time: str | None = Form(default=None, max_length=64),
    end_time: str | None = Form(default=None, max_length=64),
    tags_json: str | None = Form(default=None),
    crop_type: str | None = Form(default=None, max_length=64),
    include_crops: bool = Form(default=True),
    top_k: int = Form(default=8, ge=1, le=50),
    include_embedding: bool = Form(default=False),
):
    data = await image.read(MAX_IMAGE_BYTES + 1)
    await image.close()
    try:
        return get_memory_engine().search_image(
            data,
            namespace=namespace,
            source=source,
            session_id=session_id,
            start_time=start_time,
            end_time=end_time,
            tags=_json_tags(tags_json),
            crop_type=crop_type,
            include_crops=include_crops,
            top_k=top_k,
            include_embedding=include_embedding,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
