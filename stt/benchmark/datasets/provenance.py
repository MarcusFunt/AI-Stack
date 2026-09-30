"""Immutable source-file and dataset-lock provenance helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable


def file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_lock_entry(
    *,
    dataset: str,
    dataset_class: str,
    source_url: str,
    license: str,
    revision: str,
    files: Iterable[Path],
    root: Path,
    preparation_code_git_sha: str,
    reference_transform: str | None = None,
    doi: str | None = None,
    downloaded_at: str | None = None,
) -> dict:
    required = {
        "dataset": dataset,
        "class": dataset_class,
        "source_url": source_url,
        "license": license,
        "revision": revision,
        "preparation_code_git_sha": preparation_code_git_sha,
    }
    for name, value in required.items():
        if not str(value).strip():
            raise ValueError(f"{name} must not be empty")

    base = Path(root).resolve()
    file_records = []
    for candidate in files:
        path = Path(candidate).resolve()
        try:
            relative = path.relative_to(base).as_posix()
        except ValueError as exc:
            raise ValueError(f"source file is outside lock root: {path}") from exc
        if not path.is_file():
            raise FileNotFoundError(f"source file not found: {path}")
        file_records.append({"path": relative, "sha256": file_sha256(path)})
    file_records.sort(key=lambda item: item["path"])
    if not file_records:
        raise ValueError("at least one source file is required for a dataset lock")

    entry = {
        "dataset": dataset,
        "class": dataset_class,
        "source_url": source_url,
        "license": license,
        "downloaded_at": downloaded_at or datetime.now(timezone.utc).isoformat(),
        "revision": revision,
        "files": file_records,
        "preparation_code_git_sha": preparation_code_git_sha,
    }
    if doi:
        entry["doi"] = doi
    if reference_transform:
        entry["reference_transform"] = reference_transform
    return entry


def load_lock(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {"schema_version": 1, "datasets": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError(f"unsupported dataset lock schema: {path}")
    datasets = payload.get("datasets")
    if not isinstance(datasets, dict):
        raise ValueError(f"dataset lock must contain a datasets object: {path}")
    return payload


def update_lock(path: Path, entry: dict) -> dict:
    path = Path(path)
    dataset = str(entry.get("dataset", "")).strip()
    revision = str(entry.get("revision", "")).strip()
    files = entry.get("files")
    if not dataset or not revision or not isinstance(files, list):
        raise ValueError("lock entry requires dataset, revision, and file hashes")
    for item in files:
        value = str(item.get("path", "")) if isinstance(item, dict) else ""
        normalized = value.replace("\\", "/")
        if (
            not isinstance(item, dict)
            or not value
            or Path(value).is_absolute()
            or PurePosixPath(normalized).is_absolute()
            or re.match(r"^[A-Za-z]:/", normalized)
            or ".." in PurePosixPath(normalized).parts
            or not re.fullmatch(r"[0-9a-fA-F]{64}", str(item.get("sha256", "")))
        ):
            raise ValueError("every locked file requires a relative path and SHA-256")

    payload = load_lock(path)
    previous = payload["datasets"].get(dataset)
    if previous is not None:
        if previous.get("revision") != revision:
            raise ValueError(
                f"dataset {dataset} revision changed from {previous.get('revision')} to {revision}"
            )
        old_files = sorted(previous.get("files", []), key=lambda item: item.get("path", ""))
        new_files = sorted(files, key=lambda item: item.get("path", ""))
        if old_files != new_files:
            raise ValueError(f"dataset {dataset} file hashes changed for the locked revision")

    payload["datasets"][dataset] = dict(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", dir=path.parent,
        prefix=path.name + ".", suffix=".tmp", delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return payload
