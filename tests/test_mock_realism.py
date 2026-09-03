"""scripts/mock_vllm.py realism knobs for the load tier (PR-C).

Socket-level tests over loopback (same pattern as test_mock_vllm.py):
latency pacing, seeded jitter determinism, failure injection, and the
all-default-off equivalence proof. Timing assertions use generous lower
bounds only — CI slowness must never flake them. Exactness is pinned
through the X-Mock-* timing headers and byte/failure-pattern replay, never
wall-clock equality.
"""

import http.client
import importlib.util
import json
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
import pytest

MOCK_PATH = Path(__file__).resolve().parent.parent / "scripts" / "mock_vllm.py"

FALLBACK_MESSAGES = [{"role": "user", "content": "No retrieved excerpts in this prompt."}]
FALLBACK_CONTENT = "The retrieved excerpts did not contain a usable citation."
# 9 pieces: every word plus its trailing space, minus the final word.
FALLBACK_PIECES = len(re.findall(r"\S+\s*|\s+", FALLBACK_CONTENT))

_HIT_MESSAGES = [
    {
        "role": "user",
        "content": "[1] SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n"
        "IEA500I BEFORE IOS IOSCMDS COMMAND REJECTED, REASON=yy",
    }
]


@pytest.fixture
def mock_server(monkeypatch):
    """Factory: start a mock with the given MOCK_* env. Fresh module per
    start, so SEED always replays from the same RNG state."""
    servers = []

    def _start(**env: str):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("MOCK_DIM", "8")
        spec = importlib.util.spec_from_file_location("mock_vllm_realism", MOCK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        server = mod.ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield _start
    for server in servers:
        server.shutdown()
        server.server_close()


def _stream_raw(base_url: str, messages: list) -> tuple[int, bytes]:
    """Raw stream read: httpx buffers; http.client exposes truncation."""
    parts = urlsplit(base_url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=30)
    try:
        conn.request(
            "POST",
            "/v1/chat/completions",
            body=json.dumps({"messages": messages, "stream": True}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _parse_sse(data: bytes) -> tuple[str, list[dict]]:
    """Reassemble content; the [DONE] terminator must be present."""
    text = data.decode()
    assert "data: [DONE]" in text, f"stream lost its terminator: {text[-120:]!r}"
    chunks = [
        json.loads(line[5:])
        for block in text.split("\n\n")
        for line in block.splitlines()
        if line.startswith("data:") and line[5:].strip() != "[DONE]"
    ]
    content = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    return content, chunks


def test_defaults_stream_reassembles_exactly(mock_server):
    """Default-off equivalence: per-token framing, byte-exact reassembly,
    terminator and final-chunk usage/finish_reason preserved."""
    base_url = mock_server()
    expected = httpx2.post(
        f"{base_url}/v1/chat/completions", json={"messages": _HIT_MESSAGES}
    ).json()["choices"][0]["message"]["content"]
    _, data = _stream_raw(base_url, _HIT_MESSAGES)
    content, chunks = _parse_sse(data)
    assert content == expected
    assert len(chunks) == len(re.findall(r"\S+\s*|\s+", expected)) > 2
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"]["completion_tokens"] > 0
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"


def test_ttft_delays_first_byte(mock_server):
    import time

    base_url = mock_server(MOCK_TTFT_MS="200")
    start = time.monotonic()
    r = httpx2.post(
        f"{base_url}/v1/chat/completions",
        json={"messages": FALLBACK_MESSAGES, "stream": True},
    )
    elapsed = time.monotonic() - start
    assert r.headers["x-mock-ttft-ms"] == "200.0"
    assert elapsed >= 0.15  # generous floor: CI slowness only adds time
    content, _ = _parse_sse(r.content)
    assert content == FALLBACK_CONTENT


def test_token_interval_paces_chunks(mock_server):
    import time

    base_url = mock_server(MOCK_TOKEN_INTERVAL_MS="30")
    start = time.monotonic()
    r = httpx2.post(
        f"{base_url}/v1/chat/completions",
        json={"messages": FALLBACK_MESSAGES, "stream": True},
    )
    elapsed = time.monotonic() - start
    content, chunks = _parse_sse(r.content)
    assert content == FALLBACK_CONTENT
    assert len(chunks) == FALLBACK_PIECES
    # 8 interval sleeps x 30ms = 0.24s paced; floor keeps CI-slowness-safe.
    assert elapsed >= 0.15


def test_jitter_preserves_bytes_and_replays_seed(mock_server):
    """Jitter fires (header deviates from base within its math bounds) yet
    bytes are untouched; same seed replays the header string exactly."""
    env = {"MOCK_SEED": "7", "MOCK_TTFT_MS": "300", "MOCK_JITTER_MS": "100"}
    first = mock_server(**env)
    r1 = httpx2.post(
        f"{first}/v1/chat/completions",
        json={"messages": FALLBACK_MESSAGES, "stream": True},
    )
    header = r1.headers["x-mock-ttft-ms"]
    assert 200.0 <= float(header) <= 400.0  # base +/- jitter, floored at 0
    content, _ = _parse_sse(r1.content)
    assert content == FALLBACK_CONTENT

    second = mock_server(**env)
    r2 = httpx2.post(
        f"{second}/v1/chat/completions",
        json={"messages": FALLBACK_MESSAGES, "stream": True},
    )
    assert r2.headers["x-mock-ttft-ms"] == header
    assert r2.content == r1.content  # same seed -> byte-identical streams


def test_error_rate_full_aborts_stream_mid_flight(mock_server):
    base_url = mock_server(MOCK_ERROR_RATE="1.0")
    status, data = _stream_raw(base_url, FALLBACK_MESSAGES)
    assert status == 200
    text = data.decode()
    assert "data:" in text  # first chunk went out...
    assert "[DONE]" not in text  # ...then the connection died: no final
    first_piece = re.findall(r"\S+\s*|\s+", FALLBACK_CONTENT)[0]
    first = json.loads(
        next(line[5:] for line in text.splitlines() if line.startswith("data:"))
    )
    assert first["choices"][0]["delta"]["content"] == first_piece


def test_error_rate_full_fails_non_stream_with_fixed_shape(mock_server):
    base_url = mock_server(MOCK_ERROR_RATE="1.0")
    r = httpx2.post(f"{base_url}/v1/chat/completions", json={"messages": FALLBACK_MESSAGES})
    assert r.status_code == 500
    assert r.json() == {"error": {"message": "mock injected failure"}}


def test_error_rate_zero_never_aborts(mock_server):
    base_url = mock_server(MOCK_ERROR_RATE="0.0")
    for _ in range(5):
        _, data = _stream_raw(base_url, FALLBACK_MESSAGES)
        assert b"[DONE]" in data


def test_seeded_failure_pattern_replays(mock_server):
    """Same seed replays the same abort pattern across fresh instances —
    failure injection is deterministic, and the pattern actually fires."""

    def pattern(base_url: str) -> list[bool]:
        return [b"[DONE]" not in _stream_raw(base_url, FALLBACK_MESSAGES)[1] for _ in range(12)]

    env = {"MOCK_SEED": "42", "MOCK_ERROR_RATE": "0.5"}
    first, second = pattern(mock_server(**env)), pattern(mock_server(**env))
    assert first == second
    assert any(first) and not all(first)  # the knob fires, both ways


def test_embeddings_tokenize_healthz_immune_to_knobs(mock_server):
    """Knobs are chat-scoped: with everything hot (even ERROR_RATE=1.0),
    embeddings stay instant, deterministic, and 200."""
    hot = mock_server(
        MOCK_TTFT_MS="200",
        MOCK_TOKEN_INTERVAL_MS="50",
        MOCK_JITTER_MS="50",
        MOCK_SEED="1",
        MOCK_ERROR_RATE="1.0",
    )
    plain = mock_server()
    payload = {"model": "mock-embed", "input": ["IEA500I"]}
    hot_vec = httpx2.post(f"{hot}/v1/embeddings", json=payload).json()
    plain_vec = httpx2.post(f"{plain}/v1/embeddings", json=payload).json()
    assert hot_vec["data"][0]["embedding"] == plain_vec["data"][0]["embedding"]
    assert httpx2.post(f"{hot}/tokenize", json={"prompt": "x"}).status_code == 200
    assert httpx2.get(f"{hot}/healthz").status_code == 200


def test_invalid_knobs_fail_closed_at_import(monkeypatch):
    import importlib.util

    for bad in ({"MOCK_TTFT_MS": "-1"}, {"MOCK_ERROR_RATE": "1.5"}):
        for key, value in bad.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("MOCK_DIM", "8")
        spec = importlib.util.spec_from_file_location("mock_vllm_badknob", MOCK_PATH)
        mod = importlib.util.module_from_spec(spec)
        with pytest.raises(ValueError, match="[Ee]rror_rate|>= 0"):
            spec.loader.exec_module(mod)
        for key in bad:
            monkeypatch.delenv(key)
