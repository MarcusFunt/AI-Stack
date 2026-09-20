from __future__ import annotations

from pathlib import Path


_BLOCKED_NAMES = {".env", ".env.local", ".env.production"}


class PatchError(RuntimeError):
    pass


def apply_exact_edits(workspace: Path, edits: list[dict[str, str]]) -> list[str]:
    if len(edits) > 5:
        raise PatchError("model proposed more than 5 edits")
    changed: list[str] = []
    root = workspace.resolve()
    for edit in edits:
        rel = str(edit.get("path", "")).replace("\\", "/")
        old = edit.get("old")
        new = edit.get("new")
        if not rel or not isinstance(old, str) or not isinstance(new, str) or not old:
            raise PatchError("malformed edit")
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise PatchError(f"path escapes workspace: {rel}") from exc
        if ".git" in candidate.parts or candidate.name in _BLOCKED_NAMES:
            raise PatchError(f"blocked path: {rel}")
        if not candidate.is_file():
            raise PatchError(f"file does not exist: {rel}")
        text = candidate.read_text(encoding="utf-8")
        count = text.count(old)
        if count != 1:
            raise PatchError(f"old text must occur exactly once in {rel}; found {count}")
        candidate.write_text(text.replace(old, new, 1), encoding="utf-8")
        changed.append(rel)
    return changed
