"""scripts/mock_vllm.py — the rehearsal's model stand-in (CI only).

Exercised over a real socket so the runtime path (not just the helper) is
pinned: response shapes must match exactly what VllmEmbedder.dense and
HttpxLLMClient.chat consume.
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


def test_chat_completions_shape_matches_httpx_llm_client(base_url):
    """HttpxLLMClient.chat reads resp.json()['choices'][0]['message']['content']."""
    cite = "SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6"
    messages = [
        {"role": "system", "content": "You are a mainframe operations expert."},
        {"role": "user", "content": f"Question: reissue?\n\n[1] {cite}\nIEA500I BEFORE IOS IOSCMDS COMMAND REJECTED, REASON=yy"},
    ]
    r = httpx.post(f"{base_url}/v1/chat/completions", json={"model": "mock-reasoning", "messages": messages})
    assert r.status_code == 200
    content = r.json()["choices"][0]["message"]["content"]
    assert isinstance(content, str) and content
    assert f"- {cite}" in content  # echoes a retrieved cite -> survives validation
    assert "Citations:" in content
    assert "```" in content  # fenced script block for parse_answer


def test_chat_deterministic(base_url):
    cite_a = "SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6"
    cite_b = "SA22-7777-01 Second Reference, Chapter 1 > Parameters, p. 2-3"

    def ask(cite: str) -> str:
        messages = [{"role": "user", "content": f"[1] {cite}\nSome invented fixture text."}]
        return httpx.post(
            f"{base_url}/v1/chat/completions", json={"messages": messages}
        ).json()["choices"][0]["message"]["content"]

    assert ask(cite_a) == ask(cite_a)  # same prompt, byte-identical body
    assert ask(cite_a) != ask(cite_b)  # different prompt, different body


def test_chat_without_hit_blocks_is_deterministic_fallback(base_url):
    messages = [{"role": "user", "content": "No retrieved excerpts in this prompt."}]
    r = httpx.post(f"{base_url}/v1/chat/completions", json={"messages": messages})
    assert r.status_code == 200
    content = r.json()["choices"][0]["message"]["content"]
    assert content == "The retrieved excerpts did not contain a usable citation."


def test_unknown_path_404(base_url):
    assert httpx.get(f"{base_url}/nope").status_code == 404
    assert httpx.post(f"{base_url}/v1/nope", json={}).status_code == 404


def test_bad_input_400(base_url):
    assert httpx.post(f"{base_url}/v1/embeddings", json={"input": 42}).status_code == 400
    assert httpx.post(f"{base_url}/v1/chat/completions", json={"messages": 42}).status_code == 400
    assert httpx.post(f"{base_url}/v1/embeddings", content=b"not json",
                      headers={"Content-Type": "application/json"}).status_code == 400
