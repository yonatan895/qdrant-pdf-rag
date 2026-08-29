"""End-to-end simulation tier (marker: ``integration``, run via ``make sim``).

Real PDFs -> real ingest into a real Qdrant server (docker, the images.txt
pin) -> agent endpoints over the real app. The model is the only stand-in:
scripts/mock_vllm.py serves deterministic embeddings and a deterministic
reasoning chat over real loopback HTTP, and retrieval runs hash or vLLM-shaped
embed paths exactly as configured. No retrieval/LLM code is monkeypatched.

Skips cleanly when docker (or the pinned image) is unavailable, so the
required pytest gate stays hermetic: plain `pytest` deselects this module via
`-m 'not integration'` (pyproject addopts). Corpus is generated at runtime;
no PDFs ever reach git.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[1]
QDRANT_READY_TIMEOUT_S = 60.0
MOCK_DIM = 32  # must equal the DENSE_DIM the vLLM-shaped variant declares

MOCK_SPEC = importlib.util.spec_from_file_location(
    "mock_vllm_sim", REPO_ROOT / "scripts" / "mock_vllm.py"
)


def _run(cmd: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess:
    # Return codes are checked by each caller — docker failures must skip, not raise.
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def _qdrant_image_pin() -> str:
    """The images.txt name column, first qdrant line — the sim always runs the
    pinned image, so a pin bump is picked up automatically."""
    for line in (REPO_ROOT / "images.txt").read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#") or not line.strip():
            continue
        fields = line.split()
        if fields and "qdrant" in fields[0]:
            return fields[0]
    pytest.skip("no qdrant image pin in images.txt")


def _wait_ready(url: str) -> None:
    deadline = time.monotonic() + QDRANT_READY_TIMEOUT_S
    last = ""
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{url.rstrip('/')}/readyz", timeout=2.0)
            if r.status_code == 200 and "all shards are ready" in r.text.lower():
                return
            last = f"/readyz {r.status_code}"
        except httpx.HTTPError as exc:
            last = str(exc)[:120]
        time.sleep(0.5)
    pytest.skip(f"Qdrant at {url} not ready within {QDRANT_READY_TIMEOUT_S:.0f}s (last: {last})")


@pytest.fixture(scope="session")
def qdrant_url():
    """QDRANT_SIM_URL wins (a running server, e.g. `make sim-qdrant`);
    otherwise run the pinned image on an ephemeral loopback port."""
    env_url = os.environ.get("QDRANT_SIM_URL")
    if env_url:
        _wait_ready(env_url)
        yield env_url.rstrip("/")
        return

    if shutil.which("docker") is None:
        pytest.skip("docker CLI not found; set QDRANT_SIM_URL to reuse a running server")
    info = _run(["docker", "info"], timeout=15)
    if info.returncode != 0:
        pytest.skip("docker daemon not reachable; set QDRANT_SIM_URL to reuse a running server")

    image = _qdrant_image_pin()
    if not _run(["docker", "images", "-q", image]).stdout.strip():
        pull = _run(["docker", "pull", image], timeout=300)
        if pull.returncode != 0:
            pytest.skip(f"could not pull {image}: {pull.stderr.strip()[:200]}")
    run = _run(["docker", "run", "-d", "--rm", "-p", "127.0.0.1::6333", image])
    if run.returncode != 0:
        pytest.skip(f"could not start the qdrant container: {run.stderr.strip()[:200]}")
    cid = run.stdout.strip()
    try:
        deadline = time.monotonic() + 15
        port = ""
        while time.monotonic() < deadline:
            out = _run(["docker", "port", cid, "6333/tcp"]).stdout.strip()
            if out:
                port = out.splitlines()[0].rsplit(":", 1)[1]
                break
            time.sleep(0.3)
        if not port:
            pytest.skip("container did not publish a host port in time")
        url = f"http://127.0.0.1:{port}"
        _wait_ready(url)
        yield url
    finally:
        _run(["docker", "stop", cid], timeout=30)


@pytest.fixture(scope="session")
def mock_url():
    """The model stand-in: deterministic embeddings + chat over real HTTP."""
    old = os.environ.get("MOCK_DIM")
    os.environ["MOCK_DIM"] = str(MOCK_DIM)
    try:
        mod = importlib.util.module_from_spec(MOCK_SPEC)
        MOCK_SPEC.loader.exec_module(mod)
    finally:
        if old is None:
            os.environ.pop("MOCK_DIM", None)
        else:
            os.environ["MOCK_DIM"] = old
    server = mod.ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="session")
def corpus(tmp_path_factory) -> Path:
    from scripts.make_synthetic_pdf import build, build_plain

    root = tmp_path_factory.mktemp("sim-corpus")
    build(root / "SA22-0000-00.pdf")
    build(
        root / "SA22-7777-01.pdf",
        doc_id="SA22-7777-01",
        title="Synthetic Initialization and Tuning Reference",
    )
    build_plain(root / "widget-guide.pdf")
    return root


def _bm25_cache_dir() -> str | None:
    """A fastembed cache containing Qdrant/bm25 (make bm25-weights layout)."""
    env = os.environ.get("SIM_BM25_CACHE_DIR")
    candidates = ([Path(env)] if env else []) + [REPO_ROOT / "bundles" / "bm25-weights"]
    for candidate in candidates:
        if (candidate / "models--Qdrant--bm25").is_dir():
            return str(candidate)
    return None


def _ingest(
    monkeypatch,
    qdrant_url: str,
    collection: str,
    corpus: Path,
    progress: Path,
    embed: str = "hash",
    mock_url: str | None = None,
    bm25_cache: str | None = None,
) -> list[dict]:
    from mainframe_rag.ingest import run_ingest

    monkeypatch.setenv("QDRANT_URL", qdrant_url)
    monkeypatch.setenv("QDRANT_COLLECTION", collection)
    monkeypatch.setenv("EMBED_MODE", embed)
    if embed == "vllm":
        assert mock_url, "the vLLM-shaped variant needs the mock endpoint URL"
        monkeypatch.setenv("EMBED_BASE_URL", f"{mock_url}/v1")
        monkeypatch.setenv("EMBED_MODEL", "mock-embed")
        monkeypatch.setenv("DENSE_DIM", str(MOCK_DIM))
        if bm25_cache:
            monkeypatch.setenv("BM25_CACHE_DIR", bm25_cache)
    # Cached parent-side globals from a previous variant must not leak in.
    monkeypatch.setattr(run_ingest, "_worker_qdrant", None)
    monkeypatch.setattr(run_ingest, "_worker_embedder", None)
    rc = run_ingest.main(
        ["--src", str(corpus), "--progress", str(progress), "--workers", "1"]
    )
    assert rc == 0, f"ingest into {collection} failed"
    return [json.loads(line) for line in progress.read_text().splitlines() if line.strip()]


@contextmanager
def _agent(monkeypatch, qdrant_url: str, mock_url: str, collection: str, *, embed: str = "hash", bm25_cache: str | None = None):
    from mainframe_rag.agent import app as app_mod

    monkeypatch.setenv("QDRANT_URL", qdrant_url)
    monkeypatch.setenv("QDRANT_COLLECTION", collection)
    monkeypatch.setenv("LLM_BASE_URL", f"{mock_url}/v1")
    monkeypatch.setenv("LLM_MODEL_REASONING", "mock-reasoning")
    if embed == "hash":
        monkeypatch.setenv("EMBED_MODE", "hash")
        monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    else:
        monkeypatch.setenv("EMBED_MODE", "vllm")
        monkeypatch.setenv("EMBED_BASE_URL", f"{mock_url}/v1")
        monkeypatch.setenv("EMBED_MODEL", "mock-embed")
        monkeypatch.setenv("DENSE_DIM", str(MOCK_DIM))
        if bm25_cache:
            monkeypatch.setenv("BM25_CACHE_DIR", bm25_cache)
    with TestClient(app_mod.app) as client:
        yield client


_MESSAGE_CITE = (
    "SA22-0000-00 Synthetic Operating System Reference, "
    "Chapter 2 Operator messages > IEA500I, p. 1-6"
)


def test_ingest_real_server_and_resume(qdrant_url, corpus, tmp_path, monkeypatch):
    records = _ingest(monkeypatch, qdrant_url, "sim-hash", corpus, tmp_path / "inv.jsonl")
    assert [r["status"] for r in records] == ["upserted"] * 3
    assert all(r["chunks"] > 0 for r in records)
    assert sorted(r["doc_id"] for r in records) == ["SA22-0000-00", "SA22-7777-01", "widget-guide"]

    # Resume: fresh inventory, warm Qdrant -> qdrant-level sha skip, no new work.
    resume = _ingest(monkeypatch, qdrant_url, "sim-hash", corpus, tmp_path / "inv2.jsonl")
    assert [r["status"] for r in resume] == ["skipped"] * 3


def test_search_end_to_end_deterministic(qdrant_url, mock_url, corpus, tmp_path, monkeypatch):
    _ingest(monkeypatch, qdrant_url, "sim-hash", corpus, tmp_path / "inv.jsonl")
    with _agent(monkeypatch, qdrant_url, mock_url, "sim-hash") as client:
        body = client.post("/v1/search", json={"query": "IEA500I operator message"}).json()
        assert body["query_kind"] == "identifier"
        assert body["hits"], "message_ids filter must match the ingested message chunk"
        assert body["hits"][0]["cite"] == _MESSAGE_CITE
        assert "IEA500I" in body["hits"][0]["message_ids"]

        again = client.post("/v1/search", json={"query": "IEA500I operator message"}).json()
        # request_id is per-request by design; the result set must be identical.
        assert again["query_kind"] == body["query_kind"]
        assert again["hits"] == body["hits"], "retrieval must be deterministic for a fixed corpus+query"

        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["qdrant"] is True


def test_answer_deterministic(qdrant_url, mock_url, corpus, tmp_path, monkeypatch):
    _ingest(monkeypatch, qdrant_url, "sim-hash", corpus, tmp_path / "inv.jsonl")
    with _agent(monkeypatch, qdrant_url, mock_url, "sim-hash") as client:
        body = client.post("/v1/answer", json={"query": "IEA500I operator message"}).json()
        assert body["citations"] == [_MESSAGE_CITE], "mock echoes a retrieved cite -> validates"
        assert body["script"] is not None and "IOSCMDS LIST" in body["script"]
        assert body["answer"].startswith("Based on the retrieved excerpts")
        assert "Citations:" not in body["answer"]

        again = client.post("/v1/answer", json={"query": "IEA500I operator message"}).json()
        # request_id is per-request by design; answer/citations/script must be identical.
        stable = {k: v for k, v in body.items() if k != "request_id"}
        stable_again = {k: v for k, v in again.items() if k != "request_id"}
        assert stable_again == stable, "the answer path must be deterministic end to end"


def test_doc_id_filter_scopes_to_second_doc(qdrant_url, mock_url, corpus, tmp_path, monkeypatch):
    _ingest(monkeypatch, qdrant_url, "sim-hash", corpus, tmp_path / "inv.jsonl")
    with _agent(monkeypatch, qdrant_url, mock_url, "sim-hash") as client:
        body = client.post(
            "/v1/search", json={"query": "SA22-7777-01 initialization parameters"}
        ).json()
        assert body["query_kind"] == "identifier"
        hits = body["hits"]
        assert hits, "doc_id filter must match the second ingested document"
        assert {h["doc_id"] for h in hits} == {"SA22-7777-01"}
        assert all("SA22-7777-01 Synthetic Initialization and Tuning Reference" in h["cite"] for h in hits)


def test_plain_doc_retrievable_by_stem_doc_id(qdrant_url, mock_url, corpus, tmp_path, monkeypatch):
    _ingest(monkeypatch, qdrant_url, "sim-hash", corpus, tmp_path / "inv.jsonl")
    with _agent(monkeypatch, qdrant_url, mock_url, "sim-hash") as client:
        body = client.post("/v1/search", json={"query": "widget torque buffer"}).json()
        assert body["query_kind"] == "nl"
        assert body["hits"], "lexical overlap must rank the plain doc"
        assert "widget-guide" in {h["doc_id"] for h in body["hits"]}


def test_vllm_shaped_embed_variant(qdrant_url, mock_url, corpus, tmp_path, monkeypatch):
    """The prod embed path: dense over real HTTP to the mock vLLM endpoint,
    sparse via local fastembed BM25 (weights must already be cached)."""
    cache = _bm25_cache_dir()
    if not cache:
        pytest.skip("fastembed BM25 weights not cached; run `make bm25-weights` first")

    records = _ingest(
        monkeypatch, qdrant_url, "sim-vllm", corpus, tmp_path / "inv.jsonl",
        embed="vllm", mock_url=mock_url, bm25_cache=cache,
    )
    assert [r["status"] for r in records] == ["upserted"] * 3

    with _agent(monkeypatch, qdrant_url, mock_url, "sim-vllm", embed="vllm", bm25_cache=cache) as client:
        body = client.post("/v1/search", json={"query": "IEA500I operator message"}).json()
        assert body["hits"], "vLLM-shaped embeds must retrieve the ingested message chunk"
        assert body["hits"][0]["cite"] == _MESSAGE_CITE

        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "qdrant": True, "embed": True}
