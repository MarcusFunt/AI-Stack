from __future__ import annotations

from pathlib import Path

import pytest

from scripts import index_visual_project as indexer


def test_scan_excludes_env_secret_and_non_allowlisted_files(tmp_path):
    (tmp_path / "main.py").write_text("def render():\n    return 'safe'\n", encoding="utf-8")
    (tmp_path / ".env").write_text("AI_API_KEY=should-not-be-read\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("TOKEN=should-not-be-read\n", encoding="utf-8")
    (tmp_path / "credentials.py").write_text('API_KEY = "sk-abcdefghijklmnopqrstuvwxyz012345"\n', encoding="utf-8")
    (tmp_path / "photo.png").write_bytes(b"not a source file")

    chunks, report = indexer.scan_project(tmp_path)

    assert [chunk.source_path for chunk in chunks] == ["main.py"]
    assert report["excluded_environment"] == 2
    assert report["excluded_secret"] == 1
    assert report["excluded_extension"] == 1


def test_scan_excludes_json_formatted_credentials(tmp_path):
    (tmp_path / "safe.py").write_text("setting = True\n", encoding="utf-8")
    (tmp_path / "config.json").write_text(
        '{"client_secret":"json-secret-value-123","enabled":true}\n', encoding="utf-8"
    )
    (tmp_path / "auth.json").write_text('{"password": "a very secret password"}\n', encoding="utf-8")

    chunks, report = indexer.scan_project(tmp_path)

    assert [chunk.source_path for chunk in chunks] == ["safe.py"]
    assert "json-secret-value-123" not in "".join(chunk.text for chunk in chunks)
    assert report["excluded_secret"] == 2


def test_scan_skips_binary_oversized_and_symlink_escape(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("secretly outside the project root", encoding="utf-8")
    (root / "binary.py").write_bytes(b"print('no')\x00")
    (root / "large.py").write_bytes(b"x" * 100)
    (root / "small.py").write_text("safe = True\n", encoding="utf-8")
    escape = root / "escape.py"
    escape.write_text(outside.read_text(encoding="utf-8"), encoding="utf-8")
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path.name == "escape.py" or original_is_symlink(path))

    chunks, report = indexer.scan_project(root, max_file_bytes=50)

    assert [chunk.source_path for chunk in chunks] == ["small.py"]
    assert report["excluded_binary"] == 1
    assert report["excluded_oversized"] == 1
    assert report["excluded_symlink"] == 1
    assert all("outside" not in chunk.text for chunk in chunks)


def test_containment_check_rejects_parent_and_external_paths(tmp_path):
    root = tmp_path / "project"
    root.mkdir()

    assert indexer._inside_root((root / "src" / "file.py").resolve(), root.resolve())
    assert not indexer._inside_root((root / ".." / "outside.py").resolve(), root.resolve())


def test_chunks_include_relative_path_line_range_and_stable_hash(tmp_path):
    source = tmp_path / "src" / "sample.py"
    source.parent.mkdir()
    source.write_bytes(b"line one\nline two\nline three\n")

    chunks, _report = indexer.scan_project(tmp_path, chunk_chars=12)

    assert len(chunks) == 3
    assert chunks[0].source_path == "src/sample.py"
    assert (chunks[0].start_line, chunks[0].end_line) == (1, 1)
    assert chunks[0].chunk_hash == indexer.content_hash(chunks[0].text)
    assert [chunk.text for chunk in chunks] == ["line one\n", "line two\n", "line three\n"]


def test_upload_uses_only_authenticated_loopback_gateway(monkeypatch, tmp_path):
    source = tmp_path / "sample.py"
    source.write_text("value = 7\n", encoding="utf-8")
    chunks, _report = indexer.scan_project(tmp_path)
    observed = []

    class FakeResponse:
        status_code = 200

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, *, headers, json):
            observed.append((url, dict(headers), dict(json)))
            return FakeResponse()

    monkeypatch.setattr(indexer.httpx, "Client", lambda **_kwargs: FakeClient())
    indexer.upload_chunks(chunks, namespace="code:test", gateway_url=indexer.DEFAULT_GATEWAY_URL, api_key="test-key")

    assert len(observed) == 1
    url, headers, payload = observed[0]
    assert url == "http://127.0.0.1:8090/v1/visual-memory/index/text"
    assert headers["Authorization"] == "Bearer test-key"
    assert payload["namespace"] == "code:test"
    assert payload["source"] == "code"
    assert payload["source_path"] == "sample.py"
    assert payload["metadata"]["chunk_hash"] == indexer.content_hash(payload["text"])

    with pytest.raises(ValueError, match="loopback"):
        indexer.validate_gateway_url("http://example.com:8090")
    with pytest.raises(ValueError, match="loopback"):
        indexer.validate_gateway_url("http://127.0.0.1:8000")


def test_project_root_must_exist_and_chunk_limit_is_bounded(tmp_path):
    with pytest.raises(ValueError, match="directory"):
        indexer.scan_project(tmp_path / "missing")
    with pytest.raises(ValueError, match="chunk_chars"):
        indexer.scan_project(tmp_path, chunk_chars=0)
