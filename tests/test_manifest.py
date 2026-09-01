"""Tests for eval and bench run manifest recording (manifest.py)."""

import json
from types import SimpleNamespace

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


def test_get_qdrant_version_fallback():
    assert get_qdrant_version("http://invalid-host:9999") == "1.19.0"


def test_get_collection_snapshot_id_with_fake(monkeypatch):
    import httpx2

    class FakeHttp:
        @staticmethod
        def get(url, timeout=3.0):
            if "snapshots" in url:
                return SimpleNamespace(status_code=200, json=lambda: {"result": [{"name": "snap-123"}]})
            return SimpleNamespace(status_code=404)

    monkeypatch.setattr(httpx2, "get", FakeHttp.get)
    snap = get_collection_snapshot_id("http://mock:6333", "test-col")
    assert snap == "snap-123"


def test_write_run_manifest_appends_valid_json(tmp_path):
    settings = Settings(_env_file=None)
    metrics = {"recall@1": 0.833, "recall@5": 1.0, "mrr": 0.896}

    manifest = write_run_manifest("eval", settings, metrics, runs_dir=tmp_path)
    assert manifest["run_type"] == "eval"
    assert manifest["metrics"] == metrics
    assert "settings_hash" in manifest
    assert "git_sha" in manifest
    assert "model_ids" in manifest
    assert "qdrant_version" in manifest

    out_file = tmp_path / "eval_runs.jsonl"
    assert out_file.exists()
    lines = out_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["run_type"] == "eval"
    assert record["metrics"]["recall@1"] == 0.833
