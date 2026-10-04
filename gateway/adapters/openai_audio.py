from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.context import TraceContext
from core.invocation import (
    Invocation,
    InvocationInput,
    InvocationOperation,
    InvocationOptions,
    InvocationSource,
    ModelPolicy,
    Modality,
    Principal,
)


def _upload_metadata(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    filename = getattr(value, "filename", None)
    mime_type = getattr(value, "content_type", None)
    size = getattr(value, "size", None)
    metadata: dict[str, Any] = {}
    if isinstance(filename, str):
        metadata["filename"] = filename
    if isinstance(mime_type, str):
        metadata["mime_type"] = mime_type
    if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
        metadata["size_bytes"] = size
    return metadata


class OpenAIAudioAdapter:
    """Map existing audio and vision HTTP requests into canonical invocations."""

    def to_transcription(self, form: Mapping[str, Any], trace_context: TraceContext | None = None) -> Invocation:
        upload = form.get("file")
        metadata = {"upload": _upload_metadata(upload)}
        for field in ("language", "response_format"):
            value = form.get(field)
            if isinstance(value, str):
                metadata[field] = value
        granularities = getattr(form, "getlist", lambda _name: [])("timestamp_granularities[]")
        if granularities:
            metadata["timestamp_granularities[]"] = [
                value for value in granularities if isinstance(value, str)
            ]
        return Invocation(
            trace_context=trace_context or TraceContext(),
            operation=InvocationOperation.TRANSCRIBE,
            modality={Modality.AUDIO},
            source=InvocationSource.OPENAI_AUDIO,
            principal=Principal(id="gateway-client", kind="api_key", scopes=frozenset({"audio:transcribe"})),
            model_policy=ModelPolicy(capability="transcription"),
            input=InvocationInput(data=metadata),
        )

    def to_speech(self, payload: Mapping[str, Any], trace_context: TraceContext | None = None) -> Invocation:
        voice = payload.get("voice")
        speed = payload.get("speed")
        options_data = {}
        if isinstance(voice, str):
            options_data["voice"] = voice
        if isinstance(speed, (float, int)) and not isinstance(speed, bool):
            options_data["speed"] = float(speed)
        language = payload.get("language")
        if isinstance(language, str):
            options_data["language"] = language
        instruct = payload.get("instruct")
        if isinstance(instruct, str):
            options_data["has_expressive_instructions"] = bool(instruct.strip())
            options_data["expressive_instruction_length"] = len(instruct)
        return Invocation(
            trace_context=trace_context or TraceContext(),
            operation=InvocationOperation.SYNTHESIZE,
            modality={Modality.TEXT},
            source=InvocationSource.OPENAI_AUDIO,
            principal=Principal(id="gateway-client", kind="api_key", scopes=frozenset({"audio:synthesize"})),
            model_policy=ModelPolicy(capability="speech"),
            input=InvocationInput(text=payload.get("input") if isinstance(payload.get("input"), str) else None),
            options=InvocationOptions(
                response_format=payload.get("response_format") if isinstance(payload.get("response_format"), str) else None,
                data=options_data,
            ),
        )

    def to_vision(self, form: Mapping[str, Any], trace_context: TraceContext | None = None) -> Invocation:
        image = form.get("image") or form.get("file")
        data = {"image": _upload_metadata(image)}
        max_tokens = form.get("max_new_tokens")
        if isinstance(max_tokens, str) and max_tokens.isdigit():
            data["max_output_tokens"] = int(max_tokens)
        return Invocation(
            trace_context=trace_context or TraceContext(),
            operation=InvocationOperation.ANALYZE,
            modality={Modality.IMAGE, Modality.TEXT},
            source=InvocationSource.OPENAI_VISION,
            principal=Principal(id="gateway-client", kind="api_key", scopes=frozenset({"vision:analyze"})),
            model_policy=ModelPolicy(capability="vision"),
            input=InvocationInput(text=form.get("prompt") if isinstance(form.get("prompt"), str) else None, data=data),
        )
