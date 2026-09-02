"""Unit tests for scripts/loadtest.py (pure helpers, VRAM detection, baseline export)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from scripts.loadtest import (
    export_to_baseline,
    parse_server_timing,
    query_vram_mb,
    run_load,
)


def test_parse_server_timing():
    assert parse_server_timing(None) == {}
    assert parse_server_timing("") == {}
    assert parse_server_timing("   ") == {}

    header = "embed;dur=12.5, qdrant;dur=34, llm;dur=56.2, ttft;dur=45.0"
    timings = parse_server_timing(header)
    assert timings == {
        "embed_ms": 12.5,
        "qdrant_ms": 34.0,
        "llm_ms": 56.2,
        "ttft_ms": 45.0,
    }

    # Quoted duration values and whitespace
    header_quoted = 'embed;dur="10.2",   qdrant;dur="20.5"  , invalid;foo=bar'
    timings_q = parse_server_timing(header_quoted)
    assert timings_q == {"embed_ms": 10.2, "qdrant_ms": 20.5}


def test_query_vram_mb_success(monkeypatch):
    mock_run = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=["nvidia-smi"],
            returncode=0,
            stdout="7567, 8151\n",
            stderr="",
        )
    )
    monkeypatch.setattr(subprocess, "run", mock_run)
    res = query_vram_mb()
    assert res == {"used_mb": 7567.0, "total_mb": 8151.0}


def test_query_vram_mb_unavailable(monkeypatch):
    def mock_fail(*a, **k):
        raise FileNotFoundError("nvidia-smi not found")

    monkeypatch.setattr(subprocess, "run", mock_fail)
    assert query_vram_mb() is None

    mock_err = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=["nvidia-smi"],
            returncode=1,
            stdout="",
            stderr="NVIDIA-SMI has failed",
        )
    )
    monkeypatch.setattr(subprocess, "run", mock_err)
    assert query_vram_mb() is None


def test_export_to_baseline_new_file(tmp_path: Path):
    baseline_file = tmp_path / "baseline.json"
    result = {
        "latency_ms": {"p50": 15.0, "p95": 30.0},
        "stages": {
            "embed_ms": {"p50": 5.0, "p95": 10.0},
            "qdrant_ms": {"p50": 8.0, "p95": 15.0},
        },
        "vram": {"used_mb": 2048.0, "total_mb": 8192.0},
    }
    export_to_baseline(baseline_file, "search", result)
    assert baseline_file.exists()
    doc = json.loads(baseline_file.read_text(encoding="utf-8"))
    assert doc["agent"]["search"]["latency_ms"]["p50"] == 15.0
    assert doc["agent"]["search"]["latency_ms"]["p95"] == 30.0
    assert doc["agent"]["search"]["stages"]["embed_ms"]["p95"] == 10.0
    assert doc["agent"]["search"]["stages"]["qdrant_ms"]["p95"] == 15.0
    assert doc["vram"]["used_mb"] == 2048.0
    assert doc["vram"]["total_mb"] == 8192.0
    assert "_meta" in doc


def test_export_to_baseline_preserves_existing_keys(tmp_path: Path):
    baseline_file = tmp_path / "baseline.json"
    existing = {
        "_meta": {"note": "existing", "env": {"cpu_count": 8}},
        "ingest": {"peak_rss_mb": 150.0},
        "qdrant": {"mem_mb": 80.0},
        "agent": {
            "search": {"latency_ms": {"p95": 25.0}},
        },
    }
    baseline_file.write_text(json.dumps(existing), encoding="utf-8")

    result = {
        "latency_ms": {"p50": 40.0, "p95": 80.0},
        "stages": {
            "embed_ms": {"p50": 6.0, "p95": 12.0},
            "qdrant_ms": {"p50": 10.0, "p95": 20.0},
            "llm_ms": {"p50": 20.0, "p95": 40.0},
            "ttft_ms": {"p50": 15.0, "p95": 30.0},
        },
        "vram": {"used_mb": 3500.0, "total_mb": 8192.0},
    }
    export_to_baseline(baseline_file, "answer", result)
    doc = json.loads(baseline_file.read_text(encoding="utf-8"))
    # Preserved existing data
    assert doc["ingest"]["peak_rss_mb"] == 150.0
    assert doc["qdrant"]["mem_mb"] == 80.0
    assert doc["agent"]["search"]["latency_ms"]["p95"] == 25.0
    assert doc["_meta"]["env"]["cpu_count"] == 8
    # Added new answer data
    assert doc["agent"]["answer"]["latency_ms"]["p95"] == 80.0
    assert doc["agent"]["answer"]["stages"]["llm_ms"]["p95"] == 40.0
    assert doc["agent"]["answer"]["stages"]["ttft_ms"]["p50"] == 15.0
    assert doc["vram"]["used_mb"] == 3500.0


def test_run_load_extracts_stages_and_percentiles(monkeypatch):
    class FakeResponse:
        def __init__(self):
            self.status_code = 200
            self.headers = {"server-timing": "embed;dur=15.0, qdrant;dur=25.0"}

    def fake_post(self, *a, **k):
        return FakeResponse()

    import httpx2
    monkeypatch.setattr(httpx2.Client, "post", fake_post)
    res = run_load("http://127.0.0.1:8080", "search", ["IEA500I"], concurrency=2, duration_s=0.1)
    assert res["endpoint"] == "search"
    assert res["requests"] > 0
    assert res["errors"] == 0
    assert "embed_ms" in res["stages"]
    assert res["stages"]["embed_ms"]["p50"] == 15.0
    assert res["stages"]["qdrant_ms"]["p50"] == 25.0
