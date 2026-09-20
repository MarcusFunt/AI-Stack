from __future__ import annotations

import re
from pathlib import Path


_ALLOWED_SUFFIXES = {
    ".py", ".ps1", ".yaml", ".yml", ".json", ".toml",
    ".md", ".txt", ".ts", ".tsx", ".js", ".jsx",
}
_IGNORED_PARTS = {
    ".git", "node_modules", "dist", "__pycache__", "data",
    "models", "third_party", ".venv", ".agent_lab_holdout",
}
_TOKEN_RE = re.compile(r"[a-z0-9_]+", re.IGNORECASE)


def _normalize_scope(value: str) -> str:
    return value.replace("\\", "/").strip("/").lower()


def _matches_scope(rel: Path, prefixes: list[str]) -> bool:
    if not prefixes:
        return False
    path = rel.as_posix().lower()
    return any(
        path == prefix or path.startswith(prefix + "/")
        for raw in prefixes
        if (prefix := _normalize_scope(raw))
    )


def _eligible_files(
    workspace: Path,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[Path]:
    files: list[Path] = []
    for path in workspace.rglob("*"):
        try:
            is_file = path.is_file()
        except OSError:
            continue
        if not is_file:
            continue
        rel = path.relative_to(workspace)
        if any(part in _IGNORED_PARTS for part in rel.parts):
            continue
        if include and not _matches_scope(rel, include):
            continue
        if exclude and _matches_scope(rel, exclude):
            continue
        if path.suffix.lower() not in _ALLOWED_SUFFIXES:
            continue
        try:
            raw = path.read_bytes()
            raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        files.append(path)
    return files


def _objective_relevance(path: Path, workspace: Path, objective: str) -> int:
    rel = path.relative_to(workspace).as_posix().lower()
    name = path.name.lower()
    objective_lower = objective.lower()
    score = 0

    if rel in objective_lower:
        score += 10_000
    if name in objective_lower:
        score += 2_000

    objective_tokens = set(_TOKEN_RE.findall(objective_lower))
    path_tokens = set(_TOKEN_RE.findall(rel))
    score += 100 * len(objective_tokens & path_tokens)

    for part in path.relative_to(workspace).parts:
        part_lower = part.lower()
        if part_lower in objective_lower:
            score += 25
    return score


def collect_repository_context(
    workspace: Path,
    max_bytes: int = 60_000,
    *,
    objective: str | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> str:
    pieces: list[str] = []
    used = 0
    files = _eligible_files(workspace, include=include, exclude=exclude)
    if objective:
        files.sort(
            key=lambda path: (
                -_objective_relevance(path, workspace, objective),
                path.relative_to(workspace).as_posix(),
            )
        )
    else:
        files.sort()

    for path in files:
        rel = path.relative_to(workspace)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        block = f"\n===== {rel.as_posix()} =====\n{text}\n"
        encoded = block.encode("utf-8")
        if used + len(encoded) > max_bytes:
            remaining = max_bytes - used
            if remaining > 500:
                pieces.append(encoded[:remaining].decode("utf-8", errors="ignore"))
            break
        pieces.append(block)
        used += len(encoded)
    return "".join(pieces)
