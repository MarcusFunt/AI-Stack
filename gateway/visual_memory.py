from __future__ import annotations

import json
import uuid
from pathlib import PurePath
from typing import Any, Awaitable, Callable


class VisualAnalysisError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def encode_multipart(
    fields: dict[str, Any],
    files: list[tuple[str, str, str, bytes]],
) -> tuple[bytes, str]:
    boundary = f"ai-stack-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        values = value if isinstance(value, list) else [value]
        for item in values:
            chunks.extend([
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
                str(item).encode("utf-8"),
                b"\r\n",
            ])
    for field_name, filename, content_type, data in files:
        safe_name = PurePath(filename.replace("\\", "/")).name.replace('"', "_")
        chunks.extend([
            f"--{boundary}\r\n".encode("ascii"),
            f'Content-Disposition: form-data; name="{field_name}"; filename="{safe_name}"\r\n'.encode("utf-8"),
            f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
            data,
            b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _json_response(response, label: str) -> dict[str, Any]:
    status = int(getattr(response, "status_code", 502))
    body = getattr(response, "body", b"") or b""
    if status >= 400:
        detail = body.decode("utf-8", errors="replace")[:1000] or f"{label} failed"
        raise VisualAnalysisError(status, detail)
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise VisualAnalysisError(502, f"{label} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise VisualAnalysisError(502, f"{label} returned an invalid response")
    return value


def _image_response(response, label: str) -> bytes:
    status = int(getattr(response, "status_code", 502))
    body = getattr(response, "body", b"") or b""
    if status >= 400:
        detail = body.decode("utf-8", errors="replace")[:1000] or f"{label} failed"
        raise VisualAnalysisError(status, detail)
    if not body:
        raise VisualAnalysisError(502, f"{label} returned an empty image")
    return body


class VisualAnalysisOrchestrator:
    """Build bounded retrieval evidence and make exactly one contextual VLM call."""

    def __init__(self, memory_call: Callable[..., Awaitable[Any]], vlm_call: Callable[..., Awaitable[Any]]):
        self.memory_call = memory_call
        self.vlm_call = vlm_call

    async def _memory_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response = await self.memory_call(path, body, "application/json")
        return _json_response(response, path)

    async def _read_asset(
        self, frame_id: str, crop_type: str | None = None, namespace: str | None = None
    ) -> bytes:
        request_data = {"frame_id": frame_id, "crop_type": crop_type}
        if namespace:
            request_data["namespace"] = namespace
        response = await self.memory_call(
            "/v1/visual-memory/assets/read",
            json.dumps(request_data, separators=(",", ":")).encode("utf-8"),
            "application/json",
        )
        return _image_response(response, "visual memory asset read")

    async def analyze(
        self,
        *,
        image_bytes: bytes,
        image_filename: str,
        image_content_type: str,
        namespace: str,
        source: str,
        session_id: str | None,
        sequence_id: str | None,
        sequence_number: int | None,
        timestamp: str | None,
        tags: list[str],
        analysis_profile: str,
        structured_context: dict[str, Any],
        prompt: str,
        max_new_tokens: int,
        code_namespace: str | None = None,
        reference_id: str | None = None,
        reference_namespace: str | None = None,
        reference_image: tuple[str, str, bytes] | None = None,
        reference_prompt: str | None = None,
    ) -> dict[str, Any]:
        index_fields = {
            "namespace": namespace,
            "source": source,
            "crop_mode": "basic",
            "vision_token_budget": "balanced",
            "metadata_json": json.dumps(structured_context, ensure_ascii=False, separators=(",", ":")),
        }
        if session_id:
            index_fields["session_id"] = session_id
        if sequence_id:
            index_fields["sequence_id"] = sequence_id
        if sequence_number is not None:
            index_fields["sequence_number"] = str(sequence_number)
        if timestamp:
            index_fields["timestamp"] = timestamp
        if tags:
            index_fields["tags_json"] = json.dumps(tags, ensure_ascii=False, separators=(",", ":"))
        index_body, index_type = encode_multipart(
            index_fields,
            [("image", image_filename, image_content_type, image_bytes)],
        )
        index_response = await self.memory_call("/v1/visual-memory/index", index_body, index_type)
        indexed = _json_response(index_response, "visual memory indexing")
        current_id = indexed.get("frame_id")
        if not isinstance(current_id, str) or not current_id:
            raise VisualAnalysisError(502, "visual memory did not return a frame id")

        crop_search_body, crop_search_type = encode_multipart(
            {"namespace": namespace, "parent_id": current_id, "include_crops": "true", "top_k": "50"},
            [("image", image_filename, image_content_type, image_bytes)],
        )
        crop_response = await self.memory_call(
            "/v1/visual-memory/search/image", crop_search_body, crop_search_type
        )
        crop_results = _json_response(crop_response, "current crop retrieval")
        crop_candidates = [
            {
                "crop_id": match.get("vector_id"),
                "parent_id": current_id,
                "score": match.get("score", 0.0),
                "crop": match.get("crop"),
            }
            for match in crop_results.get("matches", [])
            if match.get("id") == current_id and isinstance(match.get("crop"), dict)
        ][:6]

        visual_search_body, visual_search_type = encode_multipart(
            {
                "namespace": namespace,
                "include_crops": "true",
                "top_k": "50",
                "expand_temporal": "2",
            },
            [("image", image_filename, image_content_type, image_bytes)],
        )
        visual_response = await self.memory_call(
            "/v1/visual-memory/search/image", visual_search_body, visual_search_type
        )
        visual_results = _json_response(visual_response, "visual retrieval")
        retrieved = [
            match for match in visual_results.get("matches", [])
            if isinstance(match, dict) and match.get("id") != current_id
        ][:20]
        for match in retrieved:
            match.setdefault("record_id", match.get("id"))
            match["parent_id"] = match.get("id")
            match.setdefault("reason", "nearest historical visual state")

        if reference_id:
            supplied_reference = {
                "record_id": reference_id,
                "parent_id": reference_id,
                "id": reference_id,
                "score": 1.0,
                "reason": "caller-selected comparison reference",
            }
            retrieved.insert(0, supplied_reference)
        elif reference_image:
            retrieved.insert(0, {
                "record_id": "uploaded-reference",
                "parent_id": "uploaded-reference",
                "id": "uploaded-reference",
                "score": 1.0,
                "reason": "caller-uploaded comparison reference",
            })

        text_context: list[dict[str, Any]] = []
        query = (reference_prompt or prompt).strip()[:32_000]
        if code_namespace and query:
            text_results = await self._memory_json(
                "/v1/visual-memory/search/text",
                {
                    "query": query,
                    "namespace": code_namespace,
                    "source": "code",
                    "top_k": 8,
                    "include_crops": False,
                },
            )
            text_context = [
                {
                    "record_id": match.get("id"),
                    "chunk_hash": match.get("content_hash") or match.get("id"),
                    "score": match.get("score"),
                    "text": match.get("text", ""),
                    "metadata": match.get("metadata") if isinstance(match.get("metadata"), dict) else {},
                }
                for match in text_results.get("matches", [])
                if isinstance(match, dict) and isinstance(match.get("text"), str)
            ]

        temporal_context = []
        for match in retrieved:
            temporal_context.extend(match.get("temporal_context") or [])
        current_frame = {
            "id": current_id,
            "observation_id": indexed.get("observation_id"),
            "namespace": namespace,
            "source": source,
            "session_id": session_id,
            "timestamp": timestamp,
            "analysis_profile": analysis_profile,
        }
        pack = await self._memory_json(
            "/v1/visual-memory/context-pack",
            {
                "current_frame": current_frame,
                "retrieved": retrieved,
                "crops": crop_candidates,
                "structured_context": {**structured_context, "analysis_profile": analysis_profile},
                "text_context": text_context,
                "temporal_context": temporal_context,
                "max_reference_images": 4,
                "max_crops": 6,
                "max_text_items": 8,
                "max_text_chars": 12_000,
            },
        )

        crop_files: list[tuple[str, str, str, bytes]] = []
        for crop in pack.get("crops", []):
            crop_data = crop.get("crop") or {}
            crop_type = crop_data.get("type")
            parent_id = crop.get("parent_id")
            if isinstance(crop_type, str) and isinstance(parent_id, str):
                data = await self._read_asset(parent_id, crop_type, namespace)
                crop_files.append(("crop_images", f"{parent_id}-{crop_type}.webp", "image/webp", data))

        reference_files: list[tuple[str, str, str, bytes]] = []
        for item in pack.get("retrieved", []):
            item_id = item.get("record_id") or item.get("id")
            if not isinstance(item_id, str):
                continue
            if item_id == "uploaded-reference" and reference_image:
                filename, media_type, data = reference_image
            else:
                metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
                item_namespace = reference_namespace if item_id == reference_id and reference_namespace else metadata.get("namespace") or namespace
                data = await self._read_asset(item_id, namespace=item_namespace)
                filename, media_type = f"{item_id}.webp", "image/webp"
            reference_files.append(("reference_images", filename, media_type, data))

        context_json = json.dumps(pack, ensure_ascii=False, separators=(",", ":"))
        fields = {
            "analysis_profile": analysis_profile,
            "context_json": context_json,
            "prompt": prompt,
            "max_new_tokens": str(max_new_tokens),
        }
        body, content_type = encode_multipart(
            fields,
            [("current_image", image_filename, image_content_type, image_bytes), *crop_files, *reference_files],
        )
        vlm_response = await self.vlm_call("/v1/vision/analyze-context", body, content_type)
        analysis = _json_response(vlm_response, "contextual vision analysis")
        return {
            "analysis": analysis,
            "current_frame": pack["current_frame"],
            "context_pack": pack,
            "evidence": pack.get("retrieved", []),
            "crop_evidence": pack.get("crops", []),
            "text_evidence": pack.get("text_context", []),
            "duplicate": {
                "duplicate": bool(indexed.get("duplicate")),
                "level": indexed.get("duplicate_level"),
            },
            "model_provenance": {
                "embedding_model": indexed.get("model"),
                "embedding_revision": indexed.get("revision"),
                "analysis_model": analysis.get("model"),
            },
        }
