from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Protocol, runtime_checkable


EMBEDDING_DIMENSION = 768
VISION_TOKEN_BUDGETS = {
    "fast": 280,
    "balanced": 560,
    "detail": 1120,
}
_BUDGET_NAMES = {value: key for key, value in VISION_TOKEN_BUDGETS.items()}


@runtime_checkable
class MultimodalEmbedder(Protocol):
    """Stable text, image, and image-plus-text embedding contract."""

    dimension: int

    def embed_text(self, inputs: Sequence[str], instruction: str | None = None) -> list[list[float]]: ...

    def embed_image(
        self,
        image,
        instruction: str | None = None,
        vision_token_budget: int | str | None = 560,
    ) -> list[float]: ...

    def embed_multimodal(
        self,
        image,
        text: str,
        instruction: str | None = None,
        vision_token_budget: int | str | None = 560,
    ) -> list[float]: ...


def resolve_vision_token_budget(value: int | str | None) -> tuple[str, int]:
    if value is None:
        return "balanced", VISION_TOKEN_BUDGETS["balanced"]
    if isinstance(value, str):
        if value in VISION_TOKEN_BUDGETS:
            return value, VISION_TOKEN_BUDGETS[value]
        try:
            value = int(value)
        except ValueError as exc:
            raise ValueError("unsupported vision token budget") from exc
    mode = _BUDGET_NAMES.get(value)
    if mode is None:
        raise ValueError("unsupported vision token budget")
    return mode, value


def normalize_vector(values: Iterable[float], *, dimension: int = EMBEDDING_DIMENSION) -> list[float]:
    vector = [float(value) for value in values]
    if len(vector) != dimension:
        raise ValueError(f"embedding dimension must be {dimension}")
    if any(not math.isfinite(value) for value in vector):
        raise ValueError("embedding values must be finite")
    norm = math.sqrt(math.fsum(value * value for value in vector))
    if norm <= 1e-12:
        raise ValueError("embedding vector has zero norm")
    return [value / norm for value in vector]


class EmbeddingModel:
    """Normalizes raw model outputs and exposes a stable visual embedding API."""

    def __init__(self, backend) -> None:
        self.backend = backend
        self.dimension = EMBEDDING_DIMENSION

    def embed_text(
        self,
        inputs: Sequence[str],
        instruction: str | None = None,
        dimensions: int = EMBEDDING_DIMENSION,
    ) -> list[list[float]]:
        if dimensions != EMBEDDING_DIMENSION:
            raise ValueError(f"dimensions must be {EMBEDDING_DIMENSION}")
        if not inputs or len(inputs) > 64:
            raise ValueError("inputs must contain between 1 and 64 texts")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 32_000 for value in inputs):
            raise ValueError("each input must be non-empty and at most 32000 characters")
        vectors = self.backend.embed_text(inputs, instruction=instruction)
        if len(vectors) != len(inputs):
            raise ValueError("embedding backend returned the wrong number of vectors")
        return [normalize_vector(vector) for vector in vectors]

    def embed_image(
        self,
        image,
        instruction: str | None = None,
        vision_token_budget: int | str | None = 560,
    ) -> list[float]:
        _, token_budget = resolve_vision_token_budget(vision_token_budget)
        return normalize_vector(
            self.backend.embed_image(
                image,
                instruction=instruction,
                vision_token_budget=token_budget,
            )
        )

    def embed_multimodal(
        self,
        image,
        text: str,
        instruction: str | None = None,
        vision_token_budget: int | str | None = 560,
    ) -> list[float]:
        if not isinstance(text, str) or not text.strip() or len(text) > 32_000:
            raise ValueError("multimodal text must be non-empty and at most 32000 characters")
        _, token_budget = resolve_vision_token_budget(vision_token_budget)
        backend_instruction = " ".join(part.strip() for part in (instruction, text) if part and part.strip())
        embedder = getattr(self.backend, "embed_multimodal", None)
        if callable(embedder):
            vector = embedder(
                image,
                text,
                instruction=instruction,
                vision_token_budget=token_budget,
            )
        else:
            vector = self.backend.embed_image(
                image,
                instruction=backend_instruction,
                vision_token_budget=token_budget,
            )
        return normalize_vector(vector)
