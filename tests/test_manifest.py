"""Tests for eval and bench run manifest recording (manifest.py)."""

import json
from types import SimpleNamespace

import httpx2

from mainframe_rag.config import Settings
from mainframe_rag.manifest import (
    get_collection_snapshot_id,
    get_qdrant_version,
    write_run_manifest,
)


def test_get_qdrant_version_unreachable_is_unknown(monkeypatch):
    """Unreachable Qdrant must record "unknown" — never the pinned server
    version, which would forge comparability between run manifests."""

    def boom(url, timeout=3.0):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(httpx2, "get", boom)
    assert get_qdrant_version("http://qdrant:6333") == "unknown"
    monkeypatch.setattr(
        httpx2, "get", lambda url, timeout=3.0: SimpleNamespace(status_code=503, json=dict)
    )
    assert get_qdrant_version("http://qdrant:6333") == "unknown"
    monkeypatch.setattr(
        httpx2,
        "get",
        lambda url, timeout=3.0: SimpleNamespace(status_code=200, json=lambda: {"version": "1.19.0"}),
    )
    assert get_qdrant_version("http://qdrant:6333") == "1.19.0"


def test_get_collection_snapshot_id_with_fake(monkeypatch):
    class FakeHttp:
        @staticmethod
        def get(url, timeout=3.0):
            if "snapshots" in url:
                return SimpleNamespace(status_code=200, json=lambda: {"result": [{"name": "snap-123"}]})
            return SimpleNamespace(status_code=404)

    monkeypatch.setattr(httpx2, "get", FakeHttp.get)
    snap = get_collection_snapshot_id("http://mock:6333", "test-col")
    assert snap == "snap-123"


def _auth_fake(key):
    """Fake Qdrant that answers 401 unless the api-key header matches."""
    seen = []

    def get(url, timeout=3.0, headers=None):
        seen.append(headers)
        if (headers or {}).get("api-key") != key:
            return SimpleNamespace(status_code=401, json=dict)
        if "snapshots" in url:
            return SimpleNamespace(status_code=200, json=lambda: {"result": []})
        if "/collections/" in url:
            return SimpleNamespace(
                status_code=200, json=lambda: {"result": {"points_count": 7}}
            )
        return SimpleNamespace(status_code=200, json=lambda: {"version": "1.19.0"})

    return get, seen


def test_manifest_helpers_send_api_key_to_authenticated_qdrant(monkeypatch):
    get, seen = _auth_fake("s3cret")
    monkeypatch.setattr(httpx2, "get", get)
    assert get_qdrant_version("http://q:6333", api_key="s3cret") == "1.19.0"
    assert get_collection_snapshot_id("http://q:6333", "col", api_key="s3cret") == "col:7"
    assert all(h == {"api-key": "s3cret"} for h in seen)
    # Wrong or missing key: fixed fallbacks, and the key is not echoed back.
    assert get_qdrant_version("http://q:6333", api_key="wrong") == "unknown"
    assert get_qdrant_version("http://q:6333") == "unknown"
    assert get_collection_snapshot_id("http://q:6333", "col") is None


def test_manifest_helpers_without_key_send_no_auth_header(monkeypatch):
    calls = []

    def get(url, timeout=3.0, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=200, json=lambda: {"version": "1.19.0"})

    monkeypatch.setattr(httpx2, "get", get)
    assert get_qdrant_version("http://q:6333") == "1.19.0"
    assert calls == [{}]


def test_write_run_manifest_uses_settings_key_and_never_records_it(tmp_path, monkeypatch):
    get, _ = _auth_fake("s3cret")
    monkeypatch.setattr(httpx2, "get", get)
    settings = Settings(_env_file=None, qdrant_api_key="s3cret")
    manifest = write_run_manifest("eval", settings, {}, runs_dir=tmp_path)
    assert manifest["qdrant_version"] == "1.19.0"
    assert manifest["collection_snapshot_id"] == f"{settings.qdrant_collection}:7"
    assert "s3cret" not in (tmp_path / "eval_runs.jsonl").read_text(encoding="utf-8")


def test_write_run_manifest_appends_valid_json(tmp_path, monkeypatch):
    """With Qdrant unreachable (all httpx2.get fail), the manifest must still
    be written, with qdrant_version="unknown" and no snapshot id."""

    def not_found(url, timeout=3.0):
        return SimpleNamespace(status_code=404, json=dict)

    monkeypatch.setattr(httpx2, "get", not_found)
    settings = Settings(_env_file=None)
    metrics = {"recall@1": 0.833, "recall@5": 1.0, "mrr": 0.896}

    manifest = write_run_manifest("eval", settings, metrics, runs_dir=tmp_path)
    assert manifest["run_type"] == "eval"
    assert manifest["metrics"] == metrics
    assert "settings_hash" in manifest
    assert "git_sha" in manifest
    assert "model_ids" in manifest
    assert manifest["qdrant_version"] == "unknown"
    assert manifest["collection_snapshot_id"] is None

    out_file = tmp_path / "eval_runs.jsonl"
    assert out_file.exists()
    lines = out_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["run_type"] == "eval"
    assert record["metrics"]["recall@1"] == 0.833

    # Issue #596: run parameters ride the manifest when supplied, else None.
    with_params = write_run_manifest("eval_answers", settings, {}, runs_dir=tmp_path,
                                     params={"temperature": 0.0})
    assert with_params["params"] == {"temperature": 0.0}
    assert manifest["params"] is None

