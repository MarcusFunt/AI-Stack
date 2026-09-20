from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any


_BLOCKED_NAMES = {".env", ".env.local", ".env.production"}
_BLOCKED_PARTS = {".git", ".agent_lab_holdout"}
_ALLOWED_CREATE_SUFFIXES = {
    ".py", ".ps1", ".yaml", ".yml", ".json", ".toml",
    ".md", ".txt", ".ts", ".tsx", ".js", ".jsx",
}
_MAX_FILE_BYTES = 100_000
_MAX_TOTAL_BYTES = 250_000


class PatchError(RuntimeError):
    pass


def _normalize_scope(value: str) -> str:
    return value.replace("\\", "/").strip("/").lower()


def _matches_scope(path: str, prefixes: list[str] | None) -> bool:
    if not prefixes:
        return False
    normalized = path.replace("\\", "/").strip("/").lower()
    return any(
        normalized == prefix or normalized.startswith(prefix + "/")
        for raw in prefixes
        if (prefix := _normalize_scope(raw))
    )


def _resolve_candidate(
    root: Path,
    rel: str,
    allow_test_edits: bool,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> Path:
    normalized = rel.replace("\\", "/").strip("/")
    if not normalized:
        raise PatchError("missing edit path")
    candidate = (root / normalized).resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise PatchError(f"path escapes workspace: {rel}") from exc

    relative_posix = relative.as_posix()
    if include and not _matches_scope(relative_posix, include):
        raise PatchError(f"path is outside allowed edit scope: {rel}")
    if exclude and _matches_scope(relative_posix, exclude):
        raise PatchError(f"path is inside blocked edit scope: {rel}")
    if any(part in _BLOCKED_PARTS for part in relative.parts):
        raise PatchError(f"blocked path: {rel}")
    if candidate.name in _BLOCKED_NAMES or candidate.name.startswith(".env"):
        raise PatchError(f"blocked path: {rel}")
    if not allow_test_edits:
        lower_parts = [part.lower() for part in relative.parts]
        name = candidate.name.lower()
        if "tests" in lower_parts or name.startswith("test_") or name.endswith("_test.py"):
            raise PatchError(f"test files are protected: {rel}")
    return candidate


def _validate_text_size(path: str, content: str) -> None:
    size = len(content.encode("utf-8"))
    if size > _MAX_FILE_BYTES:
        raise PatchError(f"resulting file is too large: {path} ({size} bytes)")


def apply_exact_edits(
    workspace: Path,
    edits: list[dict[str, Any]],
    *,
    allow_test_edits: bool = False,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[str]:
    if len(edits) > 5:
        raise PatchError("model proposed more than 5 file operations")
    if not edits:
        return []

    root = workspace.resolve()
    planned: dict[Path, str] = {}
    original: dict[Path, str | None] = {}
    changed: list[str] = []
    total_bytes = 0

    for edit in edits:
        if not isinstance(edit, dict):
            raise PatchError("malformed edit")
        op = str(edit.get("op", "replace")).lower()
        rel = str(edit.get("path", ""))
        candidate = _resolve_candidate(
            root,
            rel,
            allow_test_edits,
            include=include,
            exclude=exclude,
        )
        if candidate in planned:
            raise PatchError(f"multiple operations target the same file: {rel}")

        if op == "replace":
            old = edit.get("old")
            new = edit.get("new")
            if not isinstance(old, str) or not isinstance(new, str) or not old:
                raise PatchError(f"malformed replace operation: {rel}")
            if not candidate.is_file():
                raise PatchError(f"file does not exist: {rel}")
            try:
                current = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise PatchError(f"cannot read UTF-8 text file {rel}: {exc}") from exc
            count = current.count(old)
            if count != 1:
                raise PatchError(
                    f"old text must occur exactly once in {rel}; found {count}"
                )
            content = current.replace(old, new, 1)
            original[candidate] = current
        elif op == "create":
            content = edit.get("content")
            if not isinstance(content, str):
                raise PatchError(f"malformed create operation: {rel}")
            if candidate.exists():
                raise PatchError(f"cannot create existing path: {rel}")
            if candidate.suffix.lower() not in _ALLOWED_CREATE_SUFFIXES:
                raise PatchError(f"unsupported new-file type: {rel}")
            original[candidate] = None
        else:
            raise PatchError(f"unsupported patch operation: {op}")

        _validate_text_size(rel, content)
        total_bytes += len(content.encode("utf-8"))
        if total_bytes > _MAX_TOTAL_BYTES:
            raise PatchError("patch output exceeds total size limit")
        planned[candidate] = content
        changed.append(candidate.relative_to(root).as_posix())

    applied: list[Path] = []
    try:
        for candidate, content in planned.items():
            candidate.parent.mkdir(parents=True, exist_ok=True)
            temp = candidate.with_name(
                f".{candidate.name}.agentlab-{uuid.uuid4().hex}.tmp"
            )
            temp.write_text(content, encoding="utf-8")
            temp.replace(candidate)
            applied.append(candidate)
    except Exception as exc:
        for candidate in reversed(applied):
            before = original[candidate]
            try:
                if before is None:
                    candidate.unlink(missing_ok=True)
                else:
                    candidate.write_text(before, encoding="utf-8")
            except OSError:
                pass
        raise PatchError(f"failed while committing patch: {exc}") from exc

    return changed
