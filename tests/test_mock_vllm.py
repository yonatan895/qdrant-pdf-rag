"""scripts/mock_vllm.py — the rehearsal's vLLM stand-in (CI only).

Exercised over a real socket so the runtime path (not just the helper) is
pinned: response shape must match exactly what VllmEmbedder.dense consumes.
"""

import importlib.util
import threading
from pathlib import Path

import httpx
import pytest

SPEC = importlib.util.spec_from_file_location(
    "mock_vllm", Path(__file__).resolve().parent.parent / "scripts" / "mock_vllm.py"
)


@pytest.fixture
def base_url(monkeypatch):
    monkeypatch.setenv("MOCK_DIM", "8")
    monkeypatch.setenv("PORT", "0")
    mod = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(mod)
    server = mod.ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_healthz(base_url):
    r = httpx.get(f"{base_url}/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_embeddings_shape_matches_vllm_client(base_url):
    r = httpx.post(f"{base_url}/v1/embeddings", json={"model": "mock-embed", "input": ["IEA500I", "torque widget"]})
    assert r.status_code == 200
    body = r.json()
    assert [d["index"] for d in body["data"]] == [0, 1]
    for d in body["data"]:
        assert len(d["embedding"]) == 8
        assert abs(sum(v * v for v in d["embedding"]) - 1.0) < 1e-9


def test_deterministic_and_order_sensitive(base_url):
    a = httpx.post(f"{base_url}/v1/embeddings", json={"input": "IEA500I"}).json()
    b = httpx.post(f"{base_url}/v1/embeddings", json={"input": ["IEA500I"]}).json()
    c = httpx.post(f"{base_url}/v1/embeddings", json={"input": ["IEA500I AGAIN"]}).json()
    assert a["data"][0]["embedding"] == b["data"][0]["embedding"]  # str input coerced
    assert a["data"][0]["embedding"] != c["data"][0]["embedding"]  # text changes vector


def test_unknown_path_404(base_url):
    assert httpx.get(f"{base_url}/nope").status_code == 404
    r = httpx.post(f"{base_url}/v1/chat/completions", json={})
    assert r.status_code == 404  # chat is the reasoning path: not served by the mock


def test_bad_input_400(base_url):
    assert httpx.post(f"{base_url}/v1/embeddings", json={"input": 42}).status_code == 400
    assert httpx.post(f"{base_url}/v1/embeddings", content=b"not json",
                      headers={"Content-Type": "application/json"}).status_code == 400
