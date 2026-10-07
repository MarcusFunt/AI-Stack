from __future__ import annotations

from pydantic import BaseModel, Field


class TextEmbeddingsRequest(BaseModel):
    inputs: list[str] = Field(min_length=1, max_length=64)
    instruction: str | None = Field(default=None, max_length=1000)
    dimensions: int = 768


class TextEmbeddingsResponse(BaseModel):
    model: str
    revision: str
    dimension: int
    embeddings: list[list[float]]


class VisualTextSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=32_000)
    namespace: str | None = Field(default=None, max_length=256)
    source: str | None = Field(default=None, max_length=128)
    session_id: str | None = Field(default=None, max_length=256)
    start_time: str | None = None
    end_time: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=100)
    crop_type: str | None = Field(default=None, max_length=64)
    include_crops: bool = True
    top_k: int = Field(default=8, ge=1, le=50)
    include_embedding: bool = False
