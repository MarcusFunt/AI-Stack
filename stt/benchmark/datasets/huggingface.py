"""Small helpers for pinned, offline-testable Hugging Face dataset access."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


def validate_commit_sha(revision: str) -> str:
    revision = str(revision).strip()
    if not _COMMIT_SHA.fullmatch(revision):
        raise ValueError("dataset revision must be a full 40-character Hugging Face commit SHA")
    return revision.lower()


def resolve_dataset_revision(repo_id: str, revision: str = "main") -> str:
    """Resolve a branch/tag to the immutable commit SHA returned by the Hub."""
    try:
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise RuntimeError(
            "Hugging Face dataset preparation needs huggingface-hub; install "
            "stt/requirements-benchmark.txt."
        ) from exc
    try:
        info = HfApi().dataset_info(repo_id, revision=revision)
    except Exception as exc:
        raise RuntimeError(
            f"Could not resolve Hugging Face dataset {repo_id!r} at revision {revision!r}. "
            "Check connectivity, HF_TOKEN for gated access, and accepted dataset terms."
        ) from exc
    sha = validate_commit_sha(getattr(info, "sha", ""))
    return sha


def load_dataset_split(
    repo_id: str,
    config: str | None = None,
    *,
    split: str,
    revision: str,
    cache_dir: Path | None = None,
) -> Any:
    """Load a dataset split only at the supplied immutable revision."""
    revision = validate_commit_sha(revision)
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise RuntimeError(
            "Hugging Face dataset preparation needs the 'datasets' package; install "
            "stt/requirements-benchmark.txt."
        ) from exc
    try:
        arguments = dict(
            split=split,
            revision=revision,
            cache_dir=str(cache_dir) if cache_dir else None,
            token=True,
        )
        if config:
            return load_dataset(repo_id, config, **arguments)
        return load_dataset(repo_id, **arguments)
    except Exception as exc:
        raise RuntimeError(
            f"Could not load {repo_id!r} split {split!r} at pinned revision {revision}. "
            "Check connectivity, HF_TOKEN for gated access, and accepted dataset terms."
        ) from exc
