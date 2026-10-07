from __future__ import annotations

from typing import Mapping

from core.invocation import Invocation, InvocationInput, InvocationOperation, InvocationSource, Modality, ModelPolicy


class EmbeddingAdapter:
    """Maps embedding request metadata into the canonical invocation contract."""

    def to_text(self, payload: Mapping, trace_context) -> Invocation:
        inputs = payload.get("inputs")
        if not isinstance(inputs, list):
            raise ValueError("inputs must be an array")
        model = payload.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ValueError("model must be a non-empty string")
        instruction = payload.get("instruction")
        return Invocation(
            trace_context=trace_context,
            operation=InvocationOperation.EMBED,
            modality={Modality.TEXT},
            source=InvocationSource.INTERNAL,
            requested_model=model,
            model_policy=ModelPolicy(capability="embedding"),
            input=InvocationInput(
                data={
                    "input_count": len(inputs),
                    "instruction_present": isinstance(instruction, str) and bool(instruction.strip()),
                }
            ),
        )

    def to_image(self, payload: Mapping, trace_context) -> Invocation:
        upload = payload.get("image")
        if upload is None:
            raise ValueError("image is required")
        model = payload.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ValueError("model must be a non-empty string")
        filename = getattr(upload, "filename", None)
        content_type = getattr(upload, "content_type", None)
        size = getattr(upload, "size", None)
        instruction = payload.get("instruction")
        return Invocation(
            trace_context=trace_context,
            operation=InvocationOperation.EMBED,
            modality={Modality.IMAGE},
            source=InvocationSource.INTERNAL,
            requested_model=model,
            model_policy=ModelPolicy(capability="embedding"),
            input=InvocationInput(
                data={
                    "image": {
                        "filename": filename if isinstance(filename, str) else None,
                        "mime_type": content_type if isinstance(content_type, str) else "application/octet-stream",
                        "size_bytes": size if isinstance(size, int) and size >= 0 else None,
                    },
                    "instruction_present": isinstance(instruction, str) and bool(instruction.strip()),
                    "vision_token_budget": str(payload.get("vision_token_budget", "")) or None,
                }
            ),
        )
