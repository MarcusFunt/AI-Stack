from __future__ import annotations

from pathlib import Path


_ALLOWED_SUFFIXES = {
    ".py", ".ps1", ".yaml", ".yml", ".json", ".toml",
    ".md", ".txt", ".ts", ".tsx", ".js", ".jsx",
}
_IGNORED_PARTS = {
    ".git", "node_modules", "dist", "__pycache__", "data",
    "models", "third_party", ".venv", ".agent_lab_holdout",
}


def collect_repository_context(workspace: Path, max_bytes: int = 60_000) -> str:
    pieces: list[str] = []
    used = 0
    files = sorted(path for path in workspace.rglob("*") if path.is_file())
    for path in files:
        rel = path.relative_to(workspace)
        if any(part in _IGNORED_PARTS for part in rel.parts):
            continue
        if path.suffix.lower() not in _ALLOWED_SUFFIXES:
            continue
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
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
