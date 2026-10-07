from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class VisualContextPack:
    current_frame: dict[str, Any]
    retrieved: list[dict[str, Any]] = field(default_factory=list)
    crops: list[dict[str, Any]] = field(default_factory=list)
    structured_context: dict[str, Any] = field(default_factory=dict)
    text_context: list[dict[str, Any]] = field(default_factory=list)
    temporal_context: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _pure_data(value: Any, field_name: str) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain JSON-compatible data") from exc


def _ranked_unique_references(current_frame: dict, retrieved: Iterable[dict], limit: int) -> list[dict]:
    current_id = current_frame.get("id") or current_frame.get("frame_id")
    candidates: dict[str, tuple[int, dict]] = {}
    for position, item in enumerate(retrieved):
        if not isinstance(item, dict):
            raise ValueError("retrieved evidence must contain objects")
        pure = _pure_data(item, "retrieved evidence")
        record_id = pure.get("record_id") or pure.get("id")
        parent_id = pure.get("parent_id") or record_id
        if record_id is None or parent_id is None or parent_id == current_id:
            continue
        previous = candidates.get(str(parent_id))
        score = float(pure.get("score", pure.get("similarity", 0.0)) or 0.0)
        if previous is None or score > float(previous[1].get("score", previous[1].get("similarity", 0.0)) or 0.0):
            pure.setdefault("record_id", record_id)
            pure.setdefault("parent_id", parent_id)
            candidates[str(parent_id)] = (position, pure)

    ranked = sorted(
        candidates.values(),
        key=lambda entry: (-float(entry[1].get("score", entry[1].get("similarity", 0.0)) or 0.0), entry[0]),
    )
    selected: list[dict] = []
    selected_parents: set[str] = set()
    selected_diversity: set[str] = set()
    deferred = []
    for position, item in ranked:
        parent_id = str(item["parent_id"])
        metadata = item.get("metadata") or {}
        diversity = item.get("diversity_key") or metadata.get("namespace") or metadata.get("source") or parent_id
        diversity = str(diversity)
        if diversity not in selected_diversity and len(selected) < limit:
            selected.append(item)
            selected_parents.add(parent_id)
            selected_diversity.add(diversity)
        else:
            deferred.append((position, item))
    for _, item in deferred:
        if len(selected) >= limit:
            break
        parent_id = str(item["parent_id"])
        if parent_id not in selected_parents:
            selected.append(item)
            selected_parents.add(parent_id)
    return selected


def _dedupe_crops(crops: Iterable[dict], limit: int) -> list[dict]:
    selected: list[dict] = []
    seen: set[str] = set()
    for item in crops:
        if not isinstance(item, dict):
            raise ValueError("crops must contain objects")
        pure = _pure_data(item, "crops")
        key = pure.get("crop_id") or json.dumps(
            [pure.get("parent_id"), pure.get("crop"), pure.get("bbox")], sort_keys=True, separators=(",", ":")
        )
        if str(key) in seen:
            continue
        seen.add(str(key))
        selected.append(pure)
        if len(selected) >= limit:
            break
    return selected


def _bounded_text(items: Iterable[dict], max_items: int, max_chars: int) -> list[dict]:
    selected: list[dict] = []
    seen: set[str] = set()
    remaining = max_chars
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("text_context must contain objects")
        pure = _pure_data(item, "text_context")
        identity = pure.get("chunk_hash") or json.dumps(
            [pure.get("source_path"), pure.get("start_line"), pure.get("end_line"),
             pure.get("text", pure.get("content", pure.get("snippet", "")))],
            sort_keys=True,
            separators=(",", ":"),
        )
        if str(identity) in seen:
            continue
        seen.add(str(identity))
        text_key = next((key for key in ("text", "content", "snippet") if isinstance(pure.get(key), str)), None)
        if text_key:
            if remaining <= 0:
                break
            pure[text_key] = pure[text_key][:remaining]
            remaining -= len(pure[text_key])
        selected.append(pure)
        if len(selected) >= max_items:
            break
    return selected


def build_context_pack(
    current_frame: dict[str, Any],
    *,
    retrieved: Iterable[dict[str, Any]] = (),
    crops: Iterable[dict[str, Any]] = (),
    structured_context: dict[str, Any] | None = None,
    text_context: Iterable[dict[str, Any]] = (),
    temporal_context: Iterable[dict[str, Any]] = (),
    max_reference_images: int = 4,
    max_crops: int = 6,
    max_text_items: int = 8,
    max_text_chars: int = 12_000,
) -> VisualContextPack:
    if not isinstance(current_frame, dict) or not current_frame:
        raise ValueError("current_frame must be a non-empty object")
    if not 0 <= max_reference_images <= 4:
        raise ValueError("max_reference_images must be between 0 and the maximum of 4")
    if not 0 <= max_crops <= 6:
        raise ValueError("max_crops must be between 0 and the maximum of 6")
    if not 0 <= max_text_items <= 32 or not 0 <= max_text_chars <= 32_000:
        raise ValueError("text context limits are outside the supported maxima")
    pure_current = _pure_data(current_frame, "current_frame")
    pure_structured = _pure_data(structured_context or {}, "structured_context")
    if len(json.dumps(pure_structured, ensure_ascii=False)) > 32_000:
        raise ValueError("structured_context exceeds 32000 characters")
    pure_temporal = _pure_data(list(temporal_context)[:10], "temporal_context")
    if not isinstance(pure_temporal, list) or any(not isinstance(item, dict) for item in pure_temporal):
        raise ValueError("temporal_context must contain objects")
    return VisualContextPack(
        current_frame=pure_current,
        retrieved=_ranked_unique_references(pure_current, list(retrieved)[:20], max_reference_images),
        crops=_dedupe_crops(crops, max_crops),
        structured_context=pure_structured,
        text_context=_bounded_text(text_context, max_text_items, max_text_chars),
        temporal_context=pure_temporal,
    )
