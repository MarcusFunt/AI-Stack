"""Immutable source-file and dataset-lock provenance helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable


def canonical_json_sha256(value: object) -> str:
    """Hash JSON data using a stable UTF-8 representation."""
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def preparation_code_provenance(repository_root: Path | None = None) -> dict:
    """Fingerprint tracked and untracked code used to prepare benchmark datasets."""
    root = Path(repository_root or Path(__file__).resolve().parents[3]).resolve()
    scopes = ["stt/benchmark/datasets", "scripts/prepare-stt-benchmark.ps1"]

    def git_bytes(*arguments: str) -> bytes:
        return subprocess.run(
            ["git", *arguments], cwd=root, check=True, capture_output=True
        ).stdout

    try:
        git_sha = git_bytes("rev-parse", "HEAD").decode("ascii").strip()
        tracked = [
            item.decode("utf-8")
            for item in git_bytes("ls-files", "-z", "--", *scopes).split(b"\0")
            if item
        ]
        tracked = [
            item for item in tracked
            if (item.endswith(".py") or item == "scripts/prepare-stt-benchmark.ps1")
            and Path(item).name.lower() != ".env"
            and not Path(item).name.lower().startswith(".env.")
        ]
        diff = git_bytes("diff", "--binary", "HEAD", "--", *tracked) if tracked else b""
        untracked_listing = git_bytes(
            "ls-files", "--others", "--exclude-standard", "-z", "--", *scopes
        )
        untracked_paths = [
            item.decode("utf-8") for item in untracked_listing.split(b"\0") if item
        ]
        untracked_files = []
        for relative in sorted(untracked_paths):
            name = Path(relative).name.lower()
            if not (relative.endswith(".py") or relative == "scripts/prepare-stt-benchmark.ps1"):
                continue
            if name == ".env" or name.startswith(".env."):
                continue
            path = root / Path(relative)
            if path.is_file():
                untracked_files.append({"path": relative, "sha256": file_sha256(path)})
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):
        git_sha = "working-tree"
        diff = b""
        untracked_files = []

    dirty = bool(diff or untracked_files)
    fingerprint = None
    if dirty:
        fingerprint = canonical_json_sha256({
            "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
            "untracked_files": untracked_files,
        })
    return {
        "git_sha": git_sha if len(git_sha) == 40 else "working-tree",
        "dirty": dirty,
        "working_tree_diff_sha256": fingerprint,
    }


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
    preparation_code: dict | None = None,
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
        "preparation_code": dict(preparation_code or {
            "git_sha": preparation_code_git_sha,
            "dirty": False,
            "working_tree_diff_sha256": None,
        }),
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
        "preparation_code": required["preparation_code"],
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
