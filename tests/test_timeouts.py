"""Timeout + retry plumbing for every outbound call (issue #20 PR C).

No live Qdrant/vLLM: clients dial only in-process loopback recorders.
"""

import httpx2
import pytest
from fastapi.testclient import TestClient

from mainframe_rag.agent import app as app_mod
from mainframe_rag.agent.answer import HttpxLLMClient
from mainframe_rag.config import Settings
from mainframe_rag.ports import ChatMessage


def _settings(**kw) -> Settings:
    return Settings(
        qdrant_url="http://localhost:6333",
        llm_base_url="http://llm.internal/v1",
        llm_model_reasoning="test-reasoning-model",
        _env_file=None,
        **kw,
    )


def _spy_transport(monkeypatch) -> dict:
    """Capture HTTPTransport / AsyncHTTPTransport kwargs without reaching into httpx2 internals
    (round-7 nit 7: `_pool._retries` is private and bump-brittle)."""
    captured: dict = {}
    real = httpx2.HTTPTransport
    real_async = httpx2.AsyncHTTPTransport

    def spy(**kw):
        captured.update(kw)
        return real(**kw)

    def spy_async(**kw):
        captured.update(kw)
        return real_async(**kw)

    monkeypatch.setattr(httpx2, "HTTPTransport", spy)
    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", spy_async)
    return captured


def test_answer_client_uses_setting_and_never_retries(monkeypatch):
    captured = _spy_transport(monkeypatch)
    s = _settings(answer_timeout_s=12.5)
    llm = HttpxLLMClient(s)
    client = llm._sync_http()
    assert client.timeout.read == 12.5
    # /v1/answer is single shot: connection retries are off.
    assert captured["retries"] == 0


def test_chat_after_close_fails_loudly(monkeypatch):
    """Round-7 nit 9: close() must not null the client — a post-shutdown
    chat() hits the closed pool and raises, never silently rebuilds."""
    captured = _spy_transport(monkeypatch)
    llm = HttpxLLMClient(_settings())
    client = llm._sync_http()
    client.close()
    llm.close()
    assert captured["retries"] == 0
    with pytest.raises(RuntimeError, match="closed"):
        llm.chat([ChatMessage(role="user", content="q")])


@pytest.mark.anyio
async def test_llm_client_closes_its_own_pools_but_never_a_borrowed_one():
    """Issue #369: pools HttpxLLMClient built are closed by it; a client
    injected via `client=` is borrowed and stays open for its creator."""
    class Borrowed:
        closes = 0

        def close(self):
            Borrowed.closes += 1

        async def aclose(self):
            Borrowed.closes += 1

    borrowed = HttpxLLMClient(_settings(), client=Borrowed())  # type: ignore[arg-type]
    borrowed.close()
    await borrowed.aclose()
    assert Borrowed.closes == 0

    owned = HttpxLLMClient(_settings())
    sync_pool, async_pool = owned._sync_http(), owned._async_http()
    owned.close()
    await owned.aclose()
    assert sync_pool.is_closed and async_pool.is_closed


def test_answer_chat_retries_nothing_on_connect_error():
    s = _settings()

    class RaisingClient:
        def __init__(self):
            self.posts = 0

        def post(self, *a, **k):
            self.posts += 1
            raise httpx2.ConnectError("boom")

        def close(self):
            pass

    rc = RaisingClient()
    llm = HttpxLLMClient(s, rc)  # type: ignore[arg-type]
    with pytest.raises(httpx2.ConnectError):
        llm.chat([ChatMessage(role="user", content="q")])
    assert rc.posts == 1


def test_agent_lifespan_timeouts_and_retries(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    # CI/dev embed profile: hash mode, explicitly allowed (PR D fail-fast)
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("LLM_MODEL_REASONING", "test-reasoning-model")
    monkeypatch.setenv("QDRANT_TIMEOUT_S", "11")
    monkeypatch.setenv("EMBED_TIMEOUT_S", "7")
    monkeypatch.setenv("HTTP_CONNECT_RETRIES", "3")

    captured: dict = {}
    transport_kw = _spy_transport(monkeypatch)

    class FakeAsyncQdrantClient:
        def __init__(self, **kw):
            captured.update(kw)

        def close(self):
            pass

    # Only the async class is swapped: lifespan must construct AsyncQdrantClient
    # unconditionally — the sync QdrantClient class is never consulted (review S2).
    monkeypatch.setattr("qdrant_client.AsyncQdrantClient", FakeAsyncQdrantClient)

    # The retrieval-leg sync pool is built by lifespan and closed on shutdown
    # (review S4): spy httpx2.Client to prove construction + teardown.
    sync_closed = []
    real_client_cls = httpx2.Client

    class SpySyncClient(real_client_cls):
        def close(self):
            sync_closed.append(True)
            super().close()

    monkeypatch.setattr(app_mod.httpx2, "Client", SpySyncClient)
    with TestClient(app_mod.app) as _c:
        assert captured["timeout"] == 11.0
        assert app_mod.http.timeout.read == 7.0
        # The sync retrieval-leg pool carries the embed timeout (embedder uses
        # the client default; tokenizer/reranker pass their own per call).
        assert app_mod.http_sync.timeout.read == 7.0
        assert transport_kw["retries"] == 3
    assert sync_closed == [True]


def test_agent_qdrant_client_requests_uncompressed_responses(monkeypatch):
    """Qdrant answers gzip with a chunked body written after the headers; the
    agent then waited out ~40 ms delayed-ACK stalls on payload reads. The
    lifespan's client must ask for identity encoding on the wire."""
    import asyncio
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from qdrant_client.async_qdrant_client import AsyncQdrantClient as RealAsyncQdrantClient

    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("LLM_MODEL_REASONING", "test-reasoning-model")

    captured: dict = {}

    class SpyAsyncQdrantClient:
        def __init__(self, **kw):
            captured.update(kw)

        def close(self):
            pass

    monkeypatch.setattr("qdrant_client.AsyncQdrantClient", SpyAsyncQdrantClient)
    with TestClient(app_mod.app):
        pass

    seen: list[str | None] = []

    class Recorder(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Accept-Encoding"))
            body = b'{"result":{"collections":[]},"status":"ok","time":0}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        kw = {**captured, "url": f"http://127.0.0.1:{server.server_address[1]}"}

        async def call():
            client = RealAsyncQdrantClient(**kw)
            try:
                await client.get_collections()
            finally:
                await client.close()

        asyncio.run(call())
    finally:
        server.shutdown()
        server.server_close()
    # Every request on the wire (the client may also probe the server version).
    assert seen and set(seen) == {"identity"}


def test_agent_qdrant_pool_drops_idle_connections_before_the_server_does(monkeypatch):
    """#1128: Qdrant closes an idle keep-alive connection after ~4.5-5 s.
    The lifespan's Qdrant client must expire idle connections first: reused
    within the expiry, never reused after it."""
    import asyncio
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from qdrant_client.async_qdrant_client import AsyncQdrantClient as RealAsyncQdrantClient

    from mainframe_rag.config import QDRANT_CLIENT_KEEPALIVE_EXPIRY_S, QDRANT_SERVER_KEEPALIVE_S

    assert QDRANT_CLIENT_KEEPALIVE_EXPIRY_S + 1.0 < QDRANT_SERVER_KEEPALIVE_S
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("LLM_MODEL_REASONING", "test-reasoning-model")

    captured: dict = {}

    class SpyAsyncQdrantClient:
        def __init__(self, **kw):
            captured.update(kw)

        def close(self):
            pass

    monkeypatch.setattr("qdrant_client.AsyncQdrantClient", SpyAsyncQdrantClient)
    with TestClient(app_mod.app):
        pass
    assert captured["limits"].keepalive_expiry == QDRANT_CLIENT_KEEPALIVE_EXPIRY_S

    peers: list[int] = []

    class Recorder(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive, like Qdrant

        def do_GET(self):
            peers.append(self.client_address[1])
            body = b'{"result":{"collections":[]},"status":"ok","time":0}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        kw = {**captured, "url": f"http://127.0.0.1:{server.server_address[1]}"}

        async def phases() -> list[set[int]]:
            client = RealAsyncQdrantClient(**kw)
            seen: list[set[int]] = []
            try:
                for idle in (0.0, 0.2, QDRANT_CLIENT_KEEPALIVE_EXPIRY_S + 0.5):
                    await asyncio.sleep(idle)  # the pool sits idle
                    start = len(peers)
                    await client.get_collections()
                    seen.append(set(peers[start:]))
            finally:
                await client.close()
            return seen

        first, within, after = asyncio.run(phases())
    finally:
        server.shutdown()
        server.server_close()
    assert within and within <= first  # reused inside the expiry
    assert after and after.isdisjoint(first | within)  # fresh connection after it


def test_healthz_qdrant_probe_never_reuses_a_pooled_connection(monkeypatch):
    """#1128: the /readyz probe rides the shared pool, whose expiry stays at
    the 5 s default. It must close its connection after each scrape, so a
    later scrape never reuses one Qdrant may have closed."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    probes: list[tuple[int, str | None]] = []

    class Qdrant(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive, like Qdrant

        def do_GET(self):
            if self.path == "/readyz":
                probes.append((self.client_address[1], self.headers.get("Connection")))
                body = b"all shards are ready"
            else:
                body = b'{"result":{"collections":[]},"status":"ok","time":0}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Qdrant)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("QDRANT_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("LLM_MODEL_REASONING", "test-reasoning-model")
    try:
        with TestClient(app_mod.app) as client:
            for _ in range(3):
                client.get("/healthz")
    finally:
        server.shutdown()
        server.server_close()
    assert len(probes) == 3
    assert len({port for port, _ in probes}) == 3  # a fresh connection per scrape
    assert {header for _, header in probes} == {"close"}


def _keepalive_recorder(host: str):
    """HTTP/1.1 keep-alive server (like Qdrant) recording each request's
    client port, so a test can tell reuse from a fresh connection."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    peers: list[int] = []

    class Recorder(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            peers.append(self.client_address[1])
            body = b'{"result":{"collections":[]},"status":"ok","time":0,"version":"1.19.2"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((host, 0), Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, peers


@pytest.mark.parametrize("host", ["127.0.0.2", "127.0.0.1"])
def test_ingest_and_eval_qdrant_pool_never_reuses_past_the_expiry(host):
    """#1131: the sync Qdrant client with qdrant_pool_limits reuses a
    connection inside QDRANT_CLIENT_KEEPALIVE_EXPIRY_S and never after it.
    127.0.0.2 takes the remote branch; on 127.0.0.1 or localhost,
    qdrant-client's own no-keep-alive choice is preserved."""
    import time

    from qdrant_client import QdrantClient

    from mainframe_rag.config import QDRANT_CLIENT_KEEPALIVE_EXPIRY_S, qdrant_pool_limits

    server, peers = _keepalive_recorder(host)
    url = f"http://{host}:{server.server_address[1]}"
    client = QdrantClient(url=url, timeout=5, limits=qdrant_pool_limits(url), check_compatibility=False)
    seen: list[set[int]] = []
    try:
        for idle in (0.0, 0.2, QDRANT_CLIENT_KEEPALIVE_EXPIRY_S + 0.5):
            time.sleep(idle)
            start = len(peers)
            client.get_collections()
            seen.append(set(peers[start:]))
    finally:
        client.close()
        server.shutdown()
        server.server_close()
    first, within, after = seen
    assert after and after.isdisjoint(first | within)  # never reused past the expiry
    if host == "127.0.0.1":
        assert within.isdisjoint(first)  # loopback: no keep-alive at all
    else:
        assert within and within <= first  # remote: reused inside the expiry


def _qdrant_construction_paths():
    """Every Qdrant client construction in src/ outside the agent, as
    (name, url, call). Each call builds its client and then proceeds; the
    spy stops it right at construction."""
    from mainframe_rag.eval import chat as eval_chat
    from mainframe_rag.eval import retrieval as eval_retrieval
    from mainframe_rag.ingest import run_ingest

    remote = "http://qdrant.example:6333"
    peer = "http://qdrant-1.example:6333"

    def placement():
        settings = _settings(
            qdrant_shard_number=6,
            qdrant_replication_factor=3,
            qdrant_write_consistency_factor=2,
            qdrant_peer_urls=peer,
        )
        with run_ingest._placement_clients(settings, object()):
            pass

    return [
        ("ingest-writer", remote, lambda: run_ingest._get_qdrant(_settings().model_copy(update={"qdrant_url": remote}))),
        ("ingest-placement-peer", peer, placement),
        ("eval-chat", remote, lambda: eval_chat.evaluate_sessions([], _settings().model_copy(update={"qdrant_url": remote}))),
        ("eval-retrieval", remote, lambda: eval_retrieval.evaluate([], _settings().model_copy(update={"qdrant_url": remote}))),
    ]


@pytest.mark.parametrize("index", range(4), ids=["ingest-writer", "ingest-placement-peer", "eval-chat", "eval-retrieval"])
def test_every_ingest_and_eval_qdrant_client_gets_the_shared_expiry(monkeypatch, index):
    """#1131: each construction path hands its client qdrant_pool_limits for
    the URL it dials, so none keeps httpx's 5 s default expiry."""
    import qdrant_client

    from mainframe_rag.config import QDRANT_CLIENT_KEEPALIVE_EXPIRY_S, qdrant_pool_limits
    from mainframe_rag.ingest import run_ingest

    class Built(Exception):
        pass

    captured: dict = {}

    class SpyQdrantClient:
        def __init__(self, **kw):
            captured.update(kw)
            raise Built

    monkeypatch.setattr(qdrant_client, "QdrantClient", SpyQdrantClient)
    monkeypatch.setattr(run_ingest, "_worker_qdrant", None)
    name, url, call = _qdrant_construction_paths()[index]
    with pytest.raises(Built):
        call()
    assert captured["url"] == url, name
    assert captured["limits"] == qdrant_pool_limits(url), name
    assert captured["limits"].keepalive_expiry == QDRANT_CLIENT_KEEPALIVE_EXPIRY_S


def test_no_qdrant_client_in_src_bypasses_the_keepalive_expiry():
    """#1131 guard: every QdrantClient/AsyncQdrantClient construction in src/
    passes limits= carrying QDRANT_CLIENT_KEEPALIVE_EXPIRY_S, either through
    qdrant_pool_limits(...) or an httpx2.Limits(keepalive_expiry=...) call."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"

    def callee(node: ast.Call) -> str:
        f = node.func
        return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")

    def carries_expiry(value: ast.expr) -> bool:
        if not isinstance(value, ast.Call):
            return False
        if callee(value) == "qdrant_pool_limits":
            return True
        return callee(value) == "Limits" and any(
            kw.arg == "keepalive_expiry"
            and isinstance(kw.value, ast.Name)
            and kw.value.id == "QDRANT_CLIENT_KEEPALIVE_EXPIRY_S"
            for kw in value.keywords
        )

    sites, bypasses = [], []
    for path in sorted(src.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and callee(node) in ("QdrantClient", "AsyncQdrantClient"):
                where = f"{path.relative_to(src)}:{node.lineno}"
                sites.append(where)
                limits = next((kw.value for kw in node.keywords if kw.arg == "limits"), None)
                if limits is None or not carries_expiry(limits):
                    bypasses.append(where)
    assert len(sites) >= 5  # agent, ingest writer, placement peers, eval chat, eval retrieval
    assert bypasses == []


def test_manifest_version_probe_opens_a_fresh_connection_per_call():
    """#1131: get_qdrant_version is a one-shot httpx2.get (its own client,
    closed after the call), so it never holds a pooled Qdrant connection."""
    from mainframe_rag.manifest import get_qdrant_version

    server, peers = _keepalive_recorder("127.0.0.2")
    url = f"http://127.0.0.2:{server.server_address[1]}"
    try:
        assert [get_qdrant_version(url) for _ in range(2)] == ["1.19.2", "1.19.2"]
    finally:
        server.shutdown()
        server.server_close()
    assert len(peers) == 2 and len(set(peers)) == 2


def test_connect_retries_setting_is_bounded():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _settings(http_connect_retries=50)
