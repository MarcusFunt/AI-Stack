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
