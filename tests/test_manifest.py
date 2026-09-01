"""Tests for eval and bench run manifest recording (manifest.py)."""

import json
from types import SimpleNamespace

import httpx2

from mainframe_rag.config import Settings
from mainframe_rag.manifest import (
    compute_settings_hash,
    get_collection_snapshot_id,
    get_git_sha,
    get_qdrant_version,
    write_run_manifest,
)


def test_get_git_sha():
    sha = get_git_sha()
    assert isinstance(sha, str)
    assert len(sha) == 40 or sha == "unknown"


def test_compute_settings_hash_stable():
    s1 = Settings(_env_file=None)
    s2 = Settings(_env_file=None)
    assert compute_settings_hash(s1) == compute_settings_hash(s2)
    assert len(compute_settings_hash(s1)) == 64


def test_get_qdrant_version_unreachable_is_unknown(monkeypatch):
    """Unreachable Qdrant must record "unknown" — never the pinned server
    version, which would forge comparability between run manifests."""

    def boom(url, timeout=3.0):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(httpx2, "get", boom)
    assert get_qdrant_version("http://qdrant:6333") == "unknown"


def test_get_qdrant_version_non_200_is_unknown(monkeypatch):
    monkeypatch.setattr(
        httpx2, "get", lambda url, timeout=3.0: SimpleNamespace(status_code=503, json=dict)
    )
    assert get_qdrant_version("http://qdrant:6333") == "unknown"


def test_get_qdrant_version_served(monkeypatch):
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
