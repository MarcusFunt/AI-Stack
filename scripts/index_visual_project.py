from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8090"
MAX_CHUNK_CHARS = 32_000
DEFAULT_CHUNK_CHARS = 8_000
DEFAULT_MAX_FILE_BYTES = 1 * 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024

ALLOWED_EXTENSIONS = frozenset({
    ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".hpp", ".html", ".java",
    ".js", ".jsx", ".kt", ".md", ".mjs", ".php", ".ps1", ".psm1", ".py", ".rb",
    ".rs", ".sass", ".scala", ".scss", ".sh", ".sql", ".swift", ".toml", ".ts",
    ".tsx", ".txt", ".vue", ".xml", ".yaml", ".yml", ".json",
})
SKIP_DIRECTORIES = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", "dist",
    "build", "target", "models", "vendor", ".next", ".nuxt",
})
SKIP_FILENAMES = frozenset({
    "id_rsa", "id_ed25519", "credentials", "credentials.json", "service-account.json",
})
SECRET_PATTERNS = (
    re.compile(
        r"(?im)^\s*(?:export\s+)?[A-Z0-9_]*(?:API[_-]?KEY|ACCESS[_-]?TOKEN|AUTH[_-]?TOKEN|CLIENT[_-]?SECRET|PASSWORD|PRIVATE[_-]?KEY|SECRET|TOKEN)[A-Z0-9_]*\s*[:=]\s*['\"]?[^\s'\"]{8,}"
    ),
    re.compile(
        r"""(?i)["'][A-Z0-9_]*(?:API[_-]?KEY|ACCESS[_-]?TOKEN|AUTH[_-]?TOKEN|CLIENT[_-]?SECRET|PASSWORD|PRIVATE[_-]?KEY|SECRET|TOKEN)[A-Z0-9_]*["']\s*:\s*["'][^"']{8,}["']"""
    ),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b"),
)


@dataclass(frozen=True)
class ProjectChunk:
    text: str
    source_path: str
    language: str
    start_line: int
    end_line: int
    chunk_hash: str


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _inside_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_gateway_url(gateway_url: str) -> str:
    try:
        parsed = urlsplit(gateway_url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("gateway URL must be the authenticated loopback gateway") from exc
    hostname = (parsed.hostname or "").lower()
    loopback = hostname == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = False
    if (
        parsed.scheme != "http"
        or not loopback
        or port != 8090
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("gateway URL must be the authenticated loopback gateway on port 8090")
    return f"http://{hostname}:8090"


def _is_environment_name(name: str) -> bool:
    lowered = name.lower()
    return lowered == ".env" or lowered.startswith(".env.")


def _split_chunks(text: str, *, chunk_chars: int) -> list[tuple[str, int, int]]:
    chunks: list[tuple[str, int, int]] = []
    pieces: list[str] = []
    used = 0
    first_line: int | None = None
    last_line = 0

    def flush() -> None:
        nonlocal pieces, used, first_line, last_line
        if pieces and first_line is not None:
            chunks.append(("".join(pieces), first_line, last_line))
        pieces = []
        used = 0
        first_line = None

    for line_number, line in enumerate(text.splitlines(keepends=True), start=1):
        segments = [line[offset:offset + chunk_chars] for offset in range(0, len(line), chunk_chars)]
        for segment in segments:
            if pieces and used + len(segment) > chunk_chars:
                flush()
            if first_line is None:
                first_line = line_number
            pieces.append(segment)
            used += len(segment)
            last_line = line_number
    flush()
    return chunks


def scan_project(
    project_root: str | Path,
    *,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> tuple[list[ProjectChunk], dict[str, int]]:
    if not 1 <= chunk_chars <= MAX_CHUNK_CHARS:
        raise ValueError(f"chunk_chars must be between 1 and {MAX_CHUNK_CHARS}")
    if not 1 <= max_file_bytes <= MAX_FILE_BYTES:
        raise ValueError(f"max_file_bytes must be between 1 and {MAX_FILE_BYTES}")
    try:
        root = Path(project_root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("project root must be an existing directory") from exc
    if not root.is_dir():
        raise ValueError("project root must be an existing directory")

    report = {
        "files_seen": 0,
        "files_indexed": 0,
        "excluded_environment": 0,
        "excluded_secret": 0,
        "excluded_extension": 0,
        "excluded_binary": 0,
        "excluded_oversized": 0,
        "excluded_symlink": 0,
        "excluded_unreadable": 0,
        "excluded_empty": 0,
    }
    chunks: list[ProjectChunk] = []
    for directory, directory_names, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        kept_directories = []
        for name in directory_names:
            candidate = current / name
            if _is_environment_name(name):
                continue
            if name.lower() in SKIP_DIRECTORIES:
                continue
            if candidate.is_symlink():
                report["excluded_symlink"] += 1
                continue
            kept_directories.append(name)
        directory_names[:] = kept_directories

        for name in filenames:
            path = current / name
            report["files_seen"] += 1
            if _is_environment_name(name):
                report["excluded_environment"] += 1
                continue
            if name.lower() in SKIP_FILENAMES:
                report["excluded_secret"] += 1
                continue
            if path.suffix.lower() not in ALLOWED_EXTENSIONS:
                report["excluded_extension"] += 1
                continue
            if path.is_symlink():
                report["excluded_symlink"] += 1
                continue
            try:
                resolved = path.resolve(strict=True)
                if not _inside_root(resolved, root):
                    report["excluded_symlink"] += 1
                    continue
                if not resolved.is_file():
                    continue
                if resolved.stat().st_size > max_file_bytes:
                    report["excluded_oversized"] += 1
                    continue
                raw = resolved.read_bytes()
            except (OSError, RuntimeError):
                report["excluded_unreadable"] += 1
                continue
            if b"\x00" in raw:
                report["excluded_binary"] += 1
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                report["excluded_binary"] += 1
                continue
            if any(pattern.search(text) for pattern in SECRET_PATTERNS):
                report["excluded_secret"] += 1
                continue
            if not text.strip():
                report["excluded_empty"] += 1
                continue
            relative = resolved.relative_to(root).as_posix()
            for chunk_text, first_line, last_line in _split_chunks(text, chunk_chars=chunk_chars):
                if chunk_text.strip():
                    chunks.append(ProjectChunk(
                        text=chunk_text,
                        source_path=relative,
                        language=path.suffix.lower().lstrip("."),
                        start_line=first_line,
                        end_line=last_line,
                        chunk_hash=content_hash(chunk_text),
                    ))
            report["files_indexed"] += 1
    return chunks, report


def upload_chunks(
    chunks: list[ProjectChunk],
    *,
    namespace: str,
    gateway_url: str,
    api_key: str,
) -> int:
    if not isinstance(namespace, str) or not namespace.strip() or len(namespace) > 256:
        raise ValueError("namespace must be non-empty and at most 256 characters")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("AI_API_KEY must be set to authenticate with the gateway")
    gateway = validate_gateway_url(gateway_url)
    headers = {"Authorization": f"Bearer {api_key.strip()}"}
    indexed = 0
    with httpx.Client(timeout=60.0, trust_env=False) as client:
        for chunk in chunks:
            payload = {
                "text": chunk.text,
                "namespace": namespace.strip(),
                "source": "code",
                "source_path": chunk.source_path,
                "language": chunk.language,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "tags": ["project-code"],
                "metadata": {"chunk_hash": chunk.chunk_hash},
            }
            response = client.post(
                gateway + "/v1/visual-memory/index/text",
                headers=headers,
                json=payload,
            )
            if response.status_code < 200 or response.status_code >= 300:
                raise RuntimeError(f"gateway rejected a code chunk with HTTP {response.status_code}")
            indexed += 1
    return indexed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Index safe, bounded project text through the local AI gateway.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--gateway-url", default=DEFAULT_GATEWAY_URL)
    parser.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS)
    parser.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES)
    args = parser.parse_args(argv)

    api_key = os.environ.get("AI_API_KEY", "")
    if not api_key.strip():
        parser.error("AI_API_KEY must be set; credentials are not read from project files")
    try:
        validate_gateway_url(args.gateway_url)
        chunks, report = scan_project(
            args.project_root,
            chunk_chars=args.chunk_chars,
            max_file_bytes=args.max_file_bytes,
        )
        report["chunks_created"] = len(chunks)
        report["chunks_uploaded"] = upload_chunks(
            chunks,
            namespace=args.namespace,
            gateway_url=args.gateway_url,
            api_key=api_key,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
