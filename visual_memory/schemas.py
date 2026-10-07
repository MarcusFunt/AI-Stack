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
    expand_temporal: int = Field(default=0, ge=0, le=2)


class VisualTextIndexRequest(BaseModel):
    text: str = Field(min_length=1, max_length=32_000)
    namespace: str = Field(min_length=1, max_length=256)
    source: str | None = Field(default=None, max_length=128)
    source_path: str | None = Field(default=None, max_length=4096)
    language: str | None = Field(default=None, max_length=64)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    session_id: str | None = Field(default=None, max_length=256)
    sequence_id: str | None = Field(default=None, max_length=256)
    sequence_number: int | None = Field(default=None, ge=0)
    timestamp: str | None = Field(default=None, max_length=64)
    tags: list[str] = Field(default_factory=list, max_length=100)
    metadata: dict = Field(default_factory=dict)
