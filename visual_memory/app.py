from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from .embeddings import EmbeddingModel, resolve_vision_token_budget
from .image_processing import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, decode_image
from .model import EmbeddingGemma2Backend, MODEL_REPO
from .schemas import TextEmbeddingsRequest


app = FastAPI(title="AI-Stack visual memory")
backend = EmbeddingGemma2Backend()
embedding_model = EmbeddingModel(backend)


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
