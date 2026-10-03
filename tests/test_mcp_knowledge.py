"""Knowledge MCP adapter (issue #405 MCP1): typed mapping, parity with the HTTP
service it adapts, denial/unknown/refusal outcomes, cancellation and the
no-bypass boundary. Parity runs the REAL agent app through an ASGI transport
over the same hermetic Qdrant double as tests/test_evidence_service.py."""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
from types import SimpleNamespace

import httpx2
import pytest
from fastapi.testclient import TestClient

from mainframe_rag.mcp import knowledge
from mainframe_rag.mcp.knowledge import HttpKnowledgeBackend, handle_request
from tests.test_evidence_service import (  # noqa: F401 — `http` is a fixture
    BUILD_B,
    CHUNK,
    TEXT,
    _Access,
    _payload,
    encode_reference,
    http,
    parse_reference,
)


def _call(backend, name, arguments, id_=1):
    message = {"jsonrpc": "2.0", "id": id_, "method": "tools/call",
               "params": {"name": name, "arguments": arguments}}
    return asyncio.run(handle_request(message, backend))


def _payload_of(reply):
    result = reply["result"]
    return bool(result.get("isError")), json.loads(result["content"][0]["text"])


@pytest.fixture
def mcp(http):  # noqa: F811
    """Backend that reaches the real agent app through ASGI (no socket)."""
    client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=http.app.app), base_url="http://agent"
    )
    http.backend = HttpKnowledgeBackend("http://agent", client=client)
    return http


def test_framing_and_tool_catalog():
    reply = asyncio.run(handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, None))
    names = {t["name"] for t in reply["result"]["tools"]}
    assert names == {"knowledge_search", "evidence_read"}, "read-only knowledge tools only"
    init = asyncio.run(handle_request(
        {"id": 2, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}, None))
    assert init["result"]["capabilities"] == {"tools": {}}
    assert asyncio.run(handle_request({"method": "notifications/initialized"}, None)) is None
    for bad in ({"id": 3, "method": "nope"}, {"id": 4, "method": 7}, [1],
                {"id": 5, "method": "tools/call", "params": {"name": "dataset_read"}},
                {"id": 6, "method": "tools/call", "params": {"name": "evidence_read",
                                                             "arguments": []}}):
        assert "error" in asyncio.run(handle_request(bad, None))


@pytest.mark.parametrize("name,args", [
    ("knowledge_search", {}), ("knowledge_search", {"query": "  "}),
    ("knowledge_search", {"query": "x", "limit": 0}), ("knowledge_search", {"query": "x", "limit": 41}),
    ("knowledge_search", {"query": "x", "limit": True}), ("knowledge_search", {"query": "x", "product": 3}),
    ("evidence_read", {}), ("evidence_read", {"reference": ""}),
    ("evidence_read", {"reference": "r", "max_bytes": 0}),
    ("evidence_read", {"reference": "r", "max_bytes": 2_000_000}),
    ("evidence_read", {"reference": "r", "version": 1}),
])
def test_bad_arguments_are_protocol_errors_before_any_upstream_call(name, args):
    class Boom:
        def __getattr__(self, _):
            raise AssertionError("upstream must not be contacted")

    reply = _call(Boom(), name, args)
    assert reply["error"]["code"] == knowledge.INVALID_PARAMS


def test_search_then_read_matches_the_http_service_exactly(mcp):
    found = _payload_of(_call(mcp.backend, "knowledge_search", {"query": "step"}))
    assert found[0] is False
    hit = found[1]["hits"][0]
    via_mcp = _payload_of(_call(mcp.backend, "evidence_read", {"reference": hit["reference"]}))
    via_http = mcp.client.get(f"/v1/evidence/{hit['reference']}")
    assert via_mcp[0] is False and via_http.status_code == 200
    expected = via_http.json()
    got = via_mcp[1]
    assert got.pop("request_id") and expected.pop("request_id")
    assert got == expected and got["text"] == TEXT


def _outcome(mcp, ref, **kw):
    arguments = {"reference": ref, **{k: v for k, v in kw.items()}}
    is_error, body = _payload_of(_call(mcp.backend, "evidence_read", arguments))
    params = {k: v for k, v in kw.items()}
    http_resp = mcp.client.get(f"/v1/evidence/{ref}", params=params)
    return is_error, body, http_resp


def test_failures_have_identical_status_code_and_message_over_both_transports(mcp):
    ref = _payload_of(_call(mcp.backend, "knowledge_search", {"query": "s"}))[1]["hits"][0]["reference"]
    access = _Access()
    mcp.monkeypatch.setattr(mcp.app, "evidence_access", access)

    cases = []
    cases.append(("malformed", "nonsense", {}))
    cases.append(("unknown", encode_reference(BUILD_B, CHUNK, parse_reference(ref).digest), {}))
    cases.append(("budget", ref, {"max_bytes": 4}))
    cases.append(("scope", ref, {"product": "other"}))
    for label, reference, kw in cases:
        is_error, body, resp = _outcome(mcp, reference, **kw)
        assert is_error and resp.status_code == body["status"], label
        assert (body["code"], body["message"]) == (resp.json()["code"], resp.json()["message"]), label

    for label, setup in (
        ("denied", lambda: setattr(access, "allow", False)),
        ("policy_down", lambda: (setattr(access, "allow", True), setattr(access, "unavailable", True))),
        ("no_identity", lambda: (setattr(access, "unavailable", False),
                                 setattr(access, "unauthenticated", True))),
    ):
        setup()
        is_error, body, resp = _outcome(mcp, ref)
        assert is_error and resp.status_code == body["status"], label
        assert (body["code"], body["message"]) == (resp.json()["code"], resp.json()["message"]), label
    access.unauthenticated = False

    mcp.qd.collections["wt405_gen_a"][CHUNK] = _payload(text="tampered")
    is_error, body, resp = _outcome(mcp, ref)
    assert is_error and body["status"] == resp.status_code == 503
    assert "tampered" not in json.dumps(body)
    # Not a single failure disclosed storage text; the reasoning model stayed unused.
    assert mcp.llm.calls == 0


def test_denial_then_recovery_on_the_next_ordinary_call(mcp):
    ref = _payload_of(_call(mcp.backend, "knowledge_search", {"query": "s"}))[1]["hits"][0]["reference"]
    access = _Access()
    mcp.monkeypatch.setattr(mcp.app, "evidence_access", access)
    access.allow = False
    assert _payload_of(_call(mcp.backend, "evidence_read", {"reference": ref}))[0] is True
    access.allow = True
    assert _payload_of(_call(mcp.backend, "evidence_read", {"reference": ref}))[0] is False


class _RecordingClient:
    def __init__(self, status=200, body=None, error=None, delay=0.0):
        self.requests, self.status, self.body = [], status, body or {}
        self.error, self.delay = error, delay

    async def _respond(self, method, url, **kw):
        self.requests.append((method, url, kw))
        if self.error:
            raise self.error
        await asyncio.sleep(self.delay)
        return SimpleNamespace(status_code=self.status, content=json.dumps(self.body).encode(),
                               json=lambda: self.body)

    async def get(self, url, **kw):
        return await self._respond("GET", url, **kw)

    async def post(self, url, **kw):
        return await self._respond("POST", url, **kw)


def test_reference_is_percent_encoded_and_no_incoming_credentials_are_forwarded():
    client = _RecordingClient(body={"ok": 1})
    backend = HttpKnowledgeBackend("http://agent", client=client)
    app = TestClient(knowledge.create_app(backend))
    r = app.post("/mcp", headers={"Authorization": "Bearer incoming-secret", "Cookie": "s=1"},
                 json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "evidence_read", "arguments": {"reference": "a/b?c#d"}}})
    assert r.status_code == 200 and "error" not in r.json()
    method, url, kw = client.requests[0]
    assert (method, url) == ("GET", "/v1/evidence/a%2Fb%3Fc%23d")
    assert "headers" not in kw and "incoming-secret" not in repr(client.requests)
    assert app.post("/mcp", content="{").status_code == 400
    assert app.post("/mcp", json={"method": "notifications/initialized"}).status_code == 202


@pytest.mark.parametrize("client", [
    _RecordingClient(error=httpx2.ConnectError("internal-host-secret")),
    _RecordingClient(status=500, body={"detail": "internal-stack-secret"}),
    _RecordingClient(status=200, body={}),  # empty success is still a typed result, not a crash
])
def test_upstream_faults_become_fixed_error_results_without_upstream_text(client):
    backend = HttpKnowledgeBackend("http://agent", client=client)
    reply = _call(backend, "evidence_read", {"reference": "r"})
    text = json.dumps(reply)
    assert "secret" not in text
    if reply["result"].get("isError"):
        body = json.loads(reply["result"]["content"][0]["text"])
        assert body["code"] == "upstream_error" and body["message"] == "evidence read failed"


@pytest.mark.anyio
async def test_cancellation_reaches_the_in_flight_upstream_request():
    entered, released = asyncio.Event(), asyncio.Event()

    class Slow:
        async def get(self, url, **kw):
            entered.set()
            try:
                await asyncio.sleep(30)
            finally:
                released.set()

    backend = HttpKnowledgeBackend("http://agent", client=Slow())
    task = asyncio.create_task(handle_request(
        {"id": 1, "method": "tools/call",
         "params": {"name": "evidence_read", "arguments": {"reference": "r"}}}, backend))
    await asyncio.wait_for(entered.wait(), 2)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert released.is_set()


def test_adapter_has_no_storage_model_or_service_internals_to_bypass_with():
    tree = ast.parse(pathlib.Path(knowledge.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    forbidden = ("qdrant_client", "mainframe_rag.agent", "mainframe_rag.ingest",
                 "mainframe_rag.retrieve", "mainframe_rag.config", "mainframe_rag.ports")
    assert not [m for m in imported if m.startswith(forbidden)], imported
    assert {t for t in knowledge.TOOL_SCHEMAS} == {"knowledge_search", "evidence_read"}


def _tool_message(request_id=7, reference="a"):
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {
        "name": "evidence_read", "arguments": {"reference": reference},
    }}


def _cancel_message(request_id=7):
    return {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {
        "requestId": request_id,
    }}


class _CancellableClient:
    def __init__(self):
        self.entered = {key: asyncio.Event() for key in ("a", "b")}
        self.released = {key: asyncio.Event() for key in ("a", "b")}
        self.gates = {key: asyncio.Event() for key in ("a", "b")}
        self.calls = {key: 0 for key in ("a", "b")}
        self.closes = 0

    async def get(self, url, **kw):
        key = url.rsplit("/", 1)[1]
        self.calls[key] += 1
        if self.calls[key] == 1:
            self.entered[key].set()
            try:
                await self.gates[key].wait()
            finally:
                self.released[key].set()
        return SimpleNamespace(status_code=200, content=b'{"ok":true}', json=lambda: {"ok": True})

    async def aclose(self):
        self.closes += 1


@pytest.mark.anyio
async def test_http_notification_cancels_only_its_session_and_next_call_succeeds():
    upstream = _CancellableClient()
    backend = HttpKnowledgeBackend("http://agent", client=upstream)
    app = knowledge.create_app(backend)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://mcp") as client,
    ):
        async def initialize():
            reply = await client.post("/mcp", json={"id": 1, "method": "initialize"})
            return {"Mcp-Session-Id": reply.headers["mcp-session-id"]}

        a, b = await initialize(), await initialize()
        assert a != b
        first = asyncio.create_task(client.post("/mcp", headers=a, json=_tool_message(reference="a")))
        second = asyncio.create_task(client.post("/mcp", headers=b, json=_tool_message(reference="b")))
        await asyncio.wait_for(upstream.entered["a"].wait(), 2)
        await asyncio.wait_for(upstream.entered["b"].wait(), 2)
        unknown = await client.post("/mcp", headers=a, json=_cancel_message("unknown"))
        assert unknown.status_code == 202 and unknown.content == b""
        assert not first.done() and not second.done()
        cancel = await client.post("/mcp", headers=a, json=_cancel_message())
        assert cancel.status_code == 202 and cancel.content == b""
        assert upstream.released["a"].is_set() and not upstream.released["b"].is_set()
        cancelled = await first
        assert cancelled.status_code == 202 and cancelled.content == b""
        assert not second.done()
        # A completed ID and an unknown ID are harmless; ID reuse works.
        assert (await client.post("/mcp", headers=a, json=_cancel_message())).status_code == 202
        recovered = await client.post("/mcp", headers=a, json=_tool_message(reference="a"))
        assert recovered.status_code == 200 and _payload_of(recovered.json()) == (False, {"ok": True})
        upstream.gates["b"].set()
        assert (await second).status_code == 200
        assert (await client.delete("/mcp", headers=a)).status_code == 204
        assert (await client.post("/mcp", headers=a, json=_tool_message())).status_code == 404
    assert upstream.closes == 0, "the HTTP client was borrowed"


@pytest.mark.anyio
async def test_http_waiter_cancellation_is_not_a_protocol_cancellation():
    upstream = _CancellableClient()
    app = knowledge.create_app(HttpKnowledgeBackend("http://agent", client=upstream))
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://mcp") as client,
    ):
        init = await client.post("/mcp", json={"id": 1, "method": "initialize"})
        headers = {"Mcp-Session-Id": init.headers["mcp-session-id"]}
        call = asyncio.create_task(client.post("/mcp", headers=headers, json=_tool_message()))
        await asyncio.wait_for(upstream.entered["a"].wait(), 2)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert not upstream.released["a"].is_set()
        await client.post("/mcp", headers=headers, json=_cancel_message())
        assert upstream.released["a"].is_set()
        assert (await client.post("/mcp", headers=headers, json=_tool_message())).status_code == 200


@pytest.mark.anyio
async def test_stdio_notification_dispatch_and_eof_close_owned_client(monkeypatch):
    import io
    import queue

    upstream = _CancellableClient()
    monkeypatch.setattr(knowledge.httpx2, "AsyncClient", lambda **kw: upstream)
    backend = HttpKnowledgeBackend("http://agent")
    lines = queue.Queue()
    output = io.StringIO()

    class Input:
        def readline(self):
            return lines.get(timeout=5)

    def send(message):
        lines.put(json.dumps(message) + "\n")

    monkeypatch.setattr(knowledge.sys, "stdin", Input())
    monkeypatch.setattr(knowledge.sys, "stdout", output)
    owner = asyncio.create_task(knowledge.serve_stdio(backend))
    try:
        send(_tool_message())
        await asyncio.wait_for(upstream.entered["a"].wait(), 2)
        send(_cancel_message("unknown"))
        send(_cancel_message())
        await asyncio.wait_for(upstream.released["a"].wait(), 2)
        send(_cancel_message())  # completed/unknown IDs produce no reply
        send(_tool_message())
        async with asyncio.timeout(2):
            while not output.getvalue():
                await asyncio.sleep(0.005)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        assert len(replies) == 1 and replies[0]["id"] == 7
        assert _payload_of(replies[0]) == (False, {"ok": True})
        send(_tool_message(8, "b"))
        await asyncio.wait_for(upstream.entered["b"].wait(), 2)
    finally:
        lines.put("")
        await asyncio.wait_for(owner, 3)
    assert upstream.released["b"].is_set() and upstream.closes == 1
    await backend.aclose()
    assert upstream.closes == 1


def test_http_shutdown_closes_created_client_once_and_leaves_borrowed_client_open(monkeypatch):
    upstream = _RecordingClient()
    upstream.closes = 0

    async def close():
        upstream.closes += 1

    upstream.aclose = close
    monkeypatch.setattr(knowledge.httpx2, "AsyncClient", lambda **kw: upstream)
    with TestClient(knowledge.create_app(HttpKnowledgeBackend("http://agent"))):
        pass
    assert upstream.closes == 1
    with TestClient(knowledge.create_app(HttpKnowledgeBackend("http://agent", client=upstream))):
        pass
    assert upstream.closes == 1


@pytest.mark.anyio
async def test_http_shutdown_cancels_pending_work_before_closing_owned_client(monkeypatch):
    client_type = httpx2.AsyncClient
    upstream = _CancellableClient()
    monkeypatch.setattr(knowledge.httpx2, "AsyncClient", lambda **kw: upstream)
    app = knowledge.create_app(HttpKnowledgeBackend("http://agent"))
    client = client_type(transport=httpx2.ASGITransport(app=app), base_url="http://mcp")
    async with app.router.lifespan_context(app):
        init = await client.post("/mcp", json={"id": 1, "method": "initialize"})
        pending = asyncio.create_task(client.post(
            "/mcp", headers={"Mcp-Session-Id": init.headers["mcp-session-id"]}, json=_tool_message(),
        ))
        await asyncio.wait_for(upstream.entered["a"].wait(), 2)
    assert upstream.released["a"].is_set() and upstream.closes == 1
    assert (await pending).status_code == 202
    await client.aclose()
