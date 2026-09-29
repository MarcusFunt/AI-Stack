from __future__ import annotations

import os
import re

_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


def hf_token() -> str | None:
    return os.getenv("HF_TOKEN") or None


def resolve_model_revision(repo_id: str, requested: str | None = None) -> str:
    """Resolve a model ref once and return the immutable Hub commit SHA."""
    if requested is not None:
        requested = requested.strip()
        if not requested:
            raise ValueError("model revision must not be empty")
        if _COMMIT_SHA.fullmatch(requested):
            return requested.lower()

    from huggingface_hub import HfApi

    info = HfApi(token=hf_token()).model_info(repo_id, revision=requested or None)
    revision = getattr(info, "sha", None)
    if not isinstance(revision, str) or not _COMMIT_SHA.fullmatch(revision):
        raise RuntimeError(f"could not resolve immutable revision for {repo_id}")
    return revision.lower()


def parse_revision_overrides(values: list[str]) -> dict[str, str]:
    revisions: dict[str, str] = {}
    for value in values:
        alias, separator, revision = value.partition("=")
        alias = alias.strip()
        revision = revision.strip()
        if not separator or not alias or not revision:
            raise ValueError(
                f"invalid revision override {value!r}; expected ALIAS=REVISION"
            )
        if alias in revisions:
            raise ValueError(f"duplicate revision override for {alias}")
        revisions[alias] = revision
    return revisions
