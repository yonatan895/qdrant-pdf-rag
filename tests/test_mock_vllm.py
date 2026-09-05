"""scripts/mock_vllm.py — the rehearsal's model stand-in (CI only).

Exercised over a real socket so the runtime path (not just the helper) is
pinned: response shapes must match exactly what VllmEmbedder.dense and
HttpxLLMClient.chat consume.
"""

import importlib.util
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx2
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
    r = httpx2.get(f"{base_url}/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_embeddings_shape_matches_vllm_client(base_url):
    r = httpx2.post(f"{base_url}/v1/embeddings", json={"model": "mock-embed", "input": ["IEA500I", "torque widget"]})
    assert r.status_code == 200
    body = r.json()
    assert [d["index"] for d in body["data"]] == [0, 1]
    for d in body["data"]:
        assert len(d["embedding"]) == 8
        assert abs(sum(v * v for v in d["embedding"]) - 1.0) < 1e-9


def test_deterministic_and_order_sensitive(base_url):
    a = httpx2.post(f"{base_url}/v1/embeddings", json={"input": "IEA500I"}).json()
    b = httpx2.post(f"{base_url}/v1/embeddings", json={"input": ["IEA500I"]}).json()
    c = httpx2.post(f"{base_url}/v1/embeddings", json={"input": ["IEA500I AGAIN"]}).json()
    assert a["data"][0]["embedding"] == b["data"][0]["embedding"]  # str input coerced
    assert a["data"][0]["embedding"] != c["data"][0]["embedding"]  # text changes vector


def test_chat_completions_shape_matches_httpx_llm_client(base_url):
    """Deliberate contract change (PR #38): chat was pinned 404; the mock is
    now the full model stand-in for the simulation tier.
    HttpxLLMClient.chat reads resp.json()['choices'][0]['message']['content']."""
    cite = "SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6"
    messages = [
        {"role": "system", "content": "You are a mainframe operations expert."},
        {"role": "user", "content": f"Question: reissue?\n\n[1] {cite}\nIEA500I BEFORE IOS IOSCMDS COMMAND REJECTED, REASON=yy"},
    ]
    r = httpx2.post(f"{base_url}/v1/chat/completions", json={"model": "mock-reasoning", "messages": messages})
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
        return httpx2.post(
            f"{base_url}/v1/chat/completions", json={"messages": messages}
        ).json()["choices"][0]["message"]["content"]

    assert ask(cite_a) == ask(cite_a)  # same prompt, byte-identical body
    assert ask(cite_a) != ask(cite_b)  # different prompt, different body


def test_chat_without_hit_blocks_is_deterministic_fallback(base_url):
    messages = [{"role": "user", "content": "No retrieved excerpts in this prompt."}]
    r = httpx2.post(f"{base_url}/v1/chat/completions", json={"messages": messages})
    assert r.status_code == 200
    content = r.json()["choices"][0]["message"]["content"]
    assert content == "The retrieved excerpts did not contain a usable citation."


def test_chat_multi_hit_echoes_first_only(base_url):
    """Only hit 1 is echoed; hit 2 must not appear in the citations list."""
    cite_a = "SA22-0000-00 First Reference, Chapter 1 > IEA500I, p. 1-6"
    cite_b = "SA22-7777-01 Second Reference, Chapter 2 > IEB700I, p. 2-3"
    messages = [{"role": "user", "content": f"[1] {cite_a}\nFirst text.\n\n[2] {cite_b}\nSecond text."}]
    content = httpx2.post(
        f"{base_url}/v1/chat/completions", json={"messages": messages}
    ).json()["choices"][0]["message"]["content"]
    assert f"- {cite_a}" in content
    assert cite_b not in content
    assert "First text." in content and "Second text." not in content


def test_chat_long_hit_text_is_truncated(base_url):
    """_first_line truncates at 160 chars on a word boundary — reachability
    for the truncation branch."""
    long_line = "word " * 60  # 300 chars
    cite = "SA22-0000-00 Ref, Chapter 1 > IEA500I, p. 1-1"
    messages = [{"role": "user", "content": f"[1] {cite}\n{long_line}"}]
    content = httpx2.post(
        f"{base_url}/v1/chat/completions", json={"messages": messages}
    ).json()["choices"][0]["message"]["content"]
    snippet = content.split("the manual states: ", 1)[1].split("\n", 1)[0]
    assert snippet.endswith(" ...")
    assert len(snippet) <= 164


def test_unknown_path_404(base_url):
    assert httpx2.get(f"{base_url}/nope").status_code == 404
    assert httpx2.post(f"{base_url}/v1/nope", json={}).status_code == 404


def test_bad_input_400(base_url):
    assert httpx2.post(f"{base_url}/v1/embeddings", json={"input": 42}).status_code == 400
    assert httpx2.post(f"{base_url}/v1/chat/completions", json={"messages": 42}).status_code == 400
    assert httpx2.post(f"{base_url}/v1/embeddings", content=b"not json",
                      headers={"Content-Type": "application/json"}).status_code == 400


def test_non_object_body_400(base_url):
    """_read_json's non-object branch: valid JSON that is not an object."""
    assert httpx2.post(f"{base_url}/v1/embeddings", json=[1, 2]).status_code == 400
    assert httpx2.post(f"{base_url}/v1/chat/completions", json=[1, 2]).status_code == 400


def test_chat_messages_of_non_dicts_400(base_url):
    """'messages' must be a list of objects — a list of scalars is rejected."""
    r = httpx2.post(f"{base_url}/v1/chat/completions", json={"messages": [1, 2]})
    assert r.status_code == 400


def test_tokenize_endpoint(base_url):
    r = httpx2.post(f"{base_url}/tokenize", json={"model": "mock-reasoning", "prompt": "Hello world token test"})
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 4
    assert data["max_model_len"] == 4096
    assert len(data["tokens"]) == 4


def test_tokenize_accepts_messages_shape(base_url):
    """The agent's verification loop tokenizes the message list (chat-template
    aware); the mock must accept it like real vLLM does."""
    r = httpx2.post(
        f"{base_url}/tokenize",
        json={
            "model": "mock-reasoning",
            "messages": [
                {"role": "system", "content": "You are terse."},
                {"role": "user", "content": "Hello world token test"},
            ],
        },
    )
    assert r.status_code == 200
    assert r.json()["count"] > 4  # system message adds tokens


def test_tokenize_served_at_origin_root_only(base_url):
    """vLLM serves /tokenize at the server origin, not under /v1. A client
    regression that posts /v1/tokenize must 404, not match a loose suffix."""
    assert httpx2.post(f"{base_url}/v1/tokenize", json={"prompt": "x"}).status_code == 404


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _tokenize_over_stdio(prompt, seed):
    """Run a real mock_vllm process (own hash seed) and tokenize once."""
    port = _free_port()
    env = dict(os.environ, PORT=str(port), PYTHONHASHSEED=seed)
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve().parent.parent / "scripts" / "mock_vllm.py")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        body = None
        for _ in range(100):
            try:
                r = httpx2.post(
                    f"http://127.0.0.1:{port}/tokenize",
                    json={"model": "mock-reasoning", "prompt": prompt},
                    timeout=0.2,
                )
                if r.status_code == 200:
                    body = r.json()
                    break
            except Exception:  # noqa: BLE001 — server still starting
                time.sleep(0.1)
        assert body is not None, "mock server did not start"
        return body
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_tokenize_ids_stable_across_processes():
    """Issue #160: token ids must not depend on the per-process hash salt.
    Two fresh interpreters with different PYTHONHASHSEED values serve
    byte-identical ids for the same prompt."""
    prompt = "Hello world token test"
    first = _tokenize_over_stdio(prompt, "1")
    second = _tokenize_over_stdio(prompt, "2")
    assert first["tokens"] == second["tokens"]
    assert first["count"] == 4 == len(first["tokens"])
    assert all(1 <= t <= 10000 for t in first["tokens"])


def test_httpx_llm_client_returns_chat_result(base_url, monkeypatch):
    from mainframe_rag.agent.answer import HttpxLLMClient
    from mainframe_rag.config import Settings
    from mainframe_rag.ports import ChatMessage, ChatResult

    settings = Settings(
        llm_base_url=f"{base_url}/v1",
        llm_model_reasoning="mock-reasoning",
        _env_file=None,
    )
    client = HttpxLLMClient(settings)
    try:
        messages = [ChatMessage(role="user", content="Question: test\n\n[1] cite\ntext")]
        result = client.chat(messages)
        assert isinstance(result, ChatResult)
        assert result.finish_reason == "stop"
        assert result.content
        assert result.usage.prompt_tokens > 0
        assert result.usage.completion_tokens > 0
        assert result.usage.total_tokens > 0
    finally:
        client.close()
