"""Downstream knowledge-adapter tests (issue #405 MCP1). Hermetic: the
evidence backend is injected with fakes — no Qdrant, no FTP wire, no
mainframe, no live contact. The FTP-only default is pinned unchanged."""

import asyncio
import json
import uuid

import pytest

from mainframe_rag.config import Settings
from mainframe_rag.ingest.rules_version import extraction_rules_version
from mainframe_rag.mcp import evidence as evidence_mod
from mainframe_rag.mcp import server
from mainframe_rag.mcp.bridge import FTPConfig
from mainframe_rag.mcp.evidence import EvidenceDeps, ResolvedGeneration
from tests.fakes import make_hit


def _uuid(n: int) -> str:
    return str(uuid.UUID(int=n))


def _settings(**extra) -> Settings:
    base = {
        "qdrant_url": "http://localhost:6333",
        "qdrant_collection": "mainframe_manuals",
        "dense_dim": 256,
        "embed_mode": "hash",
        "allow_hash_mode": True,
        "embed_base_url": "http://localhost:8000/v1",
        "embed_model": "test-embed",
        "bm25_model": "Qdrant/bm25",
    }
    base.update(extra)
    return Settings(**base)


def _rules_v() -> str:
    return extraction_rules_version()


def _servable(physical: str = "mainframe_manuals__gen1") -> ResolvedGeneration:
    return ResolvedGeneration(physical=physical, outcome="compatible")


class _RetrieveFake:
    """Qdrant retrieve double keyed by point id (sync, like a sync client)."""

    def __init__(self, by_id):
        self.by_id = by_id
        self.calls = []

    def retrieve(self, collection, ids, *, with_payload, with_vectors=False):
        from types import SimpleNamespace

        self.calls.append({"collection": collection, "ids": list(ids)})
        return [SimpleNamespace(payload=self.by_id[i]) for i in ids if i in self.by_id]


def _payload(**extra):
    base = {
        "doc_id": "SA22-0000-00",
        "title": "Synthetic Reference",
        "heading_path": "Chapter 2 > IEA500I",
        "page_label": "1-6",
        "chunk_type": "message",
        "product": "z/OS",
        "version": "9.9",
        "source": "syn-manuals",
        "source_rev": "syn|z/OS|9.9|" + "a" * 64,
        "message_ids": ["IEA500I"],
        "members": [],
        "text": "IEA500I synthetic procedure text",
    }
    base.update(extra)
    return base


def _deps(**extra):
    base = {
        "client": _RetrieveFake({}),
        "embedder": None,
        "settings": _settings(),
        "rules_v": _rules_v(),
        "resolve": lambda: _servable(),
    }
    base.update(extra)
    return EvidenceDeps(**base)


def _config() -> FTPConfig:
    return FTPConfig(host="mf.example.com", user="READER", password="s3cret")


def _call(name: str, args: dict, evidence=None) -> dict:
    return server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": name, "arguments": args}},
        _config(),
        lambda _cfg: None,
        evidence=evidence,
    )


def _ok_text(reply: dict) -> str:
    assert "error" not in reply, reply
    result = reply["result"]
    assert result["isError"] is False
    return result["content"][0]["text"]


def _err_text(reply: dict) -> str:
    assert "error" not in reply, reply
    result = reply["result"]
    assert result["isError"] is True
    return result["content"][0]["text"]


# ------------------------------------------------------- registration


def test_ftp_default_advertises_no_knowledge_tools() -> None:
    reply = server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        _config(),
        lambda _cfg: None,
    )
    names = [t["name"] for t in reply["result"]["tools"]]
    assert names == ["dataset_read", "uss_read", "job_status", "jes_spool_read"]
    unknown = _call("evidence_search", {"query": "x"})
    assert unknown["error"]["code"] == -32602


def test_configured_backend_advertises_exactly_two_more_tools() -> None:
    reply = server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        _config(),
        lambda _cfg: None,
        evidence=_deps(),
    )
    names = [t["name"] for t in reply["result"]["tools"]]
    assert names == [
        "dataset_read", "uss_read", "job_status", "jes_spool_read",
        "evidence_search", "evidence_read",
    ]
    by_name = {t["name"]: t for t in reply["result"]["tools"]}
    assert by_name["evidence_search"]["inputSchema"]["required"] == ["query"]
    assert by_name["evidence_read"]["inputSchema"]["required"] == ["reference"]
    for tool in by_name.values():
        assert set(tool["inputSchema"]) >= {"type", "properties"}


def test_protocol_version_pinned() -> None:
    reply = server.handle_request(
        {"jsonrpc": "2.0", "id": 7, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18"}},
        _config(),
        lambda _cfg: None,
        evidence=_deps(),
    )
    assert reply["result"]["protocolVersion"] == "2025-06-18"
    assert server.PROTOCOL_VERSION == "2025-03-26"
    assert "2025-06-18" in server.SUPPORTED_PROTOCOLS


def test_cancellation_notification_takes_no_reply() -> None:
    reply = server.handle_request(
        {"jsonrpc": "2.0", "method": "notifications/cancelled",
         "params": {"requestId": 1}},
        _config(),
        lambda _cfg: None,
        evidence=_deps(),
    )
    assert reply is None


# ------------------------------------------------------- evidence_search mapping


class _RecordingSearch:
    def __init__(self, hits, genfp="9f2c4a1b7e031234"):
        self.hits = hits
        self.genfp = genfp
        self.calls = []

    async def __call__(self, client, embedder, collection, query, **kwargs):
        from mainframe_rag.retrieve.evidence import mint_reference

        self.calls.append({"collection": collection, "query": query, **kwargs})
        bound = [
            h.model_copy(update={"reference": mint_reference(self.genfp, h.chunk_id)})
            for h in self.hits
        ]
        return bound, "identifier", {"embed_ms": 1, "qdrant_ms": 2}, self.genfp


def test_evidence_search_maps_scope_limit_and_collection() -> None:
    search = _RecordingSearch([make_hit(chunk_id=_uuid(1))])
    deps = _deps(search_fn=search)
    reply = _call(
        "evidence_search",
        {"query": "IEA500I start", "product": "z/OS", "version": "9.9",
         "source": "syn-manuals", "limit": 3},
        evidence=deps,
    )
    body = json.loads(_ok_text(reply))
    assert body["query_kind"] == "identifier"
    assert body["generation"] == "9f2c4a1b7e031234"
    assert body["hits"][0]["cite"] == make_hit(chunk_id=_uuid(1)).cite
    call = search.calls[0]
    assert call["collection"] == "mainframe_manuals__gen1"  # resolved physical, never the alias
    assert call["product"] == "z/OS"
    assert call["version"] == "9.9"
    assert call["source"] == "syn-manuals"
    assert call["limit"] == 3
    assert call["rules_v"] == _rules_v()


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": 42},
        {"query": "x", "limit": 0},
        {"query": "x", "limit": 41},
        {"query": "x", "limit": "8"},
        {"query": "x", "limit": True},
        {"query": "x", "product": 42},
        {"query": "x", "version": ["9.9"]},
        {"query": "x", "source": {"s": 1}},
        {"query": "y" * 2001},
    ],
)
def test_evidence_search_malformed_args_are_invalid_params(args) -> None:
    reply = _call("evidence_search", args, evidence=_deps())
    assert reply["error"]["code"] == -32602


# ------------------------------------------------------- evidence_read


def test_evidence_read_round_trip() -> None:
    from mainframe_rag.retrieve.evidence import current_genfp_hex, mint_reference

    settings = _settings()
    genfp = current_genfp_hex(settings, _rules_v())
    chunk = _uuid(11)
    deps = _deps(client=_RetrieveFake({chunk: _payload()}), settings=settings)
    reply = _call("evidence_read", {"reference": mint_reference(genfp, chunk)}, evidence=deps)
    record = json.loads(_ok_text(reply))
    assert record["reference"] == mint_reference(genfp, chunk)
    assert record["generation"] == genfp
    assert record["text"] == "IEA500I synthetic procedure text"
    assert record["cite"] == "SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6"
    assert record["source_rev"] == "syn|z/OS|9.9|" + "a" * 64
    assert record["truncated"] is False


def test_evidence_read_unknown_reference_is_not_found() -> None:
    from mainframe_rag.retrieve.evidence import current_genfp_hex, mint_reference

    settings = _settings()
    deps = _deps(client=_RetrieveFake({}), settings=settings)
    ref = mint_reference(current_genfp_hex(settings, _rules_v()), _uuid(12))
    assert _err_text(_call("evidence_read", {"reference": ref}, evidence=deps)) == (
        "not_found: evidence not found"
    )


@pytest.mark.parametrize("bad", ["abc123", "ev_short_deadbeef", 42])
def test_evidence_read_malformed_reference_is_invalid_params(bad) -> None:
    reply = _call("evidence_read", {"reference": bad}, evidence=_deps())
    assert reply["error"]["code"] == -32602


def test_evidence_read_retired_reports_refresh_without_store_contact() -> None:
    chunk = _uuid(13)
    client = _RetrieveFake({chunk: _payload(text="NEW bytes under the current generation")})
    deps = _deps(client=client)
    reply = _call(
        "evidence_read", {"reference": f"ev_deadbeefdeadbeef_{chunk}"}, evidence=deps
    )
    assert _err_text(reply) == "not_found: evidence retired; refresh explicitly"
    assert client.calls == []


# ------------------------------------------------------- denial and faults


def _unservable() -> ResolvedGeneration:
    return ResolvedGeneration(physical="mainframe_manuals__gen1", outcome="reembed_required")


def test_denial_refuses_before_store_contact() -> None:
    client = _RetrieveFake({_uuid(21): _payload()})
    deps = _deps(client=client, resolve=_unservable)
    search = _call("evidence_search", {"query": "IEA500I"}, evidence=deps)
    assert _err_text(search) == (
        "representation_unavailable: the retrieval generation is not available"
    )
    read = _call(
        "evidence_read",
        {"reference": f"ev_9f2c4a1b7e031234_{_uuid(21)}"},
        evidence=deps,
    )
    assert _err_text(read) == (
        "representation_unavailable: the retrieval generation is not available"
    )
    assert client.calls == []


def test_store_fault_maps_to_upstream_and_framing_survives() -> None:
    async def boom(*args, **kwargs):
        raise TimeoutError("qdrant down")

    deps = _deps(search_fn=boom)

    class _BoomRetrieve:
        def retrieve(self, *args, **kwargs):
            raise RuntimeError("store gone")

    deps_read = _deps(client=_BoomRetrieve())
    from mainframe_rag.retrieve.evidence import current_genfp_hex, mint_reference

    ref = mint_reference(current_genfp_hex(_settings(), _rules_v()), _uuid(22))
    assert _err_text(_call("evidence_search", {"query": "x"}, evidence=deps)) == (
        "upstream_error: retrieval failed"
    )
    assert _err_text(_call("evidence_read", {"reference": ref}, evidence=deps_read)) == (
        "upstream_error: retrieval failed"
    )
    listed = server.handle_request(
        {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}},
        _config(),
        lambda _cfg: None,
        evidence=deps,
    )
    assert len(listed["result"]["tools"]) == 6


# ------------------------------------------------------- service helpers


def test_gate_resolver_maps_serving_verdict() -> None:
    from mainframe_rag.agent.serving import ServingGeneration
    from mainframe_rag.mcp.evidence import gate_resolver

    async def fake_generation(client, settings, rules_v, *, fresh=False):
        return ServingGeneration(physical="phys__gen2", outcome="compatible")

    class _Gate:
        async def generation(self, client, settings, rules_v, *, fresh=False):
            return await fake_generation(client, settings, rules_v, fresh=fresh)

    resolve = gate_resolver(_Gate(), object(), _settings(), _rules_v())
    resolved = resolve()
    assert resolved.physical == "phys__gen2"
    assert resolved.servable is True
    assert ResolvedGeneration(physical="p", outcome="pending").servable is False
    assert ResolvedGeneration(physical=None, outcome="compatible").servable is False
    assert ResolvedGeneration(physical="p", outcome="record_only_drift").servable is True


def test_runner_supports_running_loop() -> None:
    """The HTTP transport calls tools inside the server loop: the runner
    must then use a helper thread instead of failing closed."""

    async def _coro() -> str:
        return "done"

    async def outer() -> str:
        return evidence_mod._run(_coro())

    assert evidence_mod._run(_coro()) == "done"  # no running loop: fresh asyncio.run
    assert asyncio.run(outer()) == "done"  # running loop: helper thread


# ------------------------------------------------------- HTTP parity (dual consumer)


def test_http_and_mcp_return_identical_evidence(monkeypatch) -> None:
    """Second-consumer proof: the same store content served over HTTP and
    over MCP carries identical text and citation for one reference."""
    from fastapi.testclient import TestClient

    from mainframe_rag.agent import app as app_mod
    from mainframe_rag.agent import metrics as metrics_mod

    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setenv("LLM_MODEL_REASONING", "test-reasoning-model")
    # Hermetic RED instruments (see tests/test_evidence_service.py): the
    # global provider is already enabled by an earlier test, and
    # test_metrics.py pins its exact series counts.
    monkeypatch.setattr(metrics_mod, "_instruments", None)

    chunk = _uuid(31)
    payload = _payload()
    hit = make_hit(chunk_id=chunk, text=payload["text"])

    class _Search:
        def __call__(self, *a, **k):
            return [hit], "identifier", {"embed_ms": 1, "qdrant_ms": 2}

    monkeypatch.setattr(app_mod, "retrieve_search", _Search())
    with TestClient(app_mod.app) as http:
        # The hermetic lifespan Qdrant is empty: serve the payload under test
        # (lifespan must run first — the global only exists after startup).
        monkeypatch.setattr(app_mod, "qdrant", _RetrieveFake({chunk: payload}))
        found = http.post("/v1/search", json={"query": "IEA500I"})
        assert found.status_code == 200
        reference = found.json()["hits"][0]["reference"]
        exact = http.get(f"/v1/evidence/{reference}")
        assert exact.status_code == 200
        http_body = exact.json()

    deps = _deps(
        client=_RetrieveFake({chunk: payload}),
        settings=app_mod.settings,  # same process config: same generation
    )
    mcp_body = json.loads(_ok_text(_call("evidence_read", {"reference": reference}, evidence=deps)))
    assert mcp_body["text"] == http_body["text"] == payload["text"]
    assert mcp_body["cite"] == http_body["cite"]
    assert mcp_body["generation"] == http_body["generation"]

