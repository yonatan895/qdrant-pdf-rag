"""Shared evidence service tests (issue #405 E1): opaque references, exact
reads with retired semantics, and the one HTTP adapter.

Service fakes are hermetic (no Qdrant, no LLM — the service never calls a
model). HTTP tests reuse the agent lifespan doubles with hash-mode settings.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from mainframe_rag.agent import app as app_mod
from mainframe_rag.config import Settings
from mainframe_rag.ingest.rules_version import extraction_rules_version
from mainframe_rag.retrieve.evidence import (
    EVIDENCE_DEFAULT_CONTEXT_BUDGET,
    EvidenceNotFound,
    EvidenceRetired,
    attach_references,
    current_genfp_hex,
    mint_reference,
    parse_reference,
    read_evidence,
    search_evidence,
)
from tests.fakes import make_hit


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


def _uuid(n: int) -> str:
    return str(uuid.UUID(int=n))


# ------------------------------------------------------- reference mint/parse


def test_reference_round_trip():
    genfp = "9f2c4a1b7e031234"
    chunk = _uuid(7)
    ref = mint_reference(genfp, chunk)
    assert ref.startswith("ev_")
    assert parse_reference(ref) == (genfp, chunk)


@pytest.mark.parametrize(
    "bad",
    [
        "abc123",  # raw point id is never a reference
        "9f2c4a1b7e031234_" + "0" * 32,  # missing prefix
        "ev_short_deadbeef",  # short fingerprint
        "ev_zzzzzzzzzzzzzzzz_" + "0" * 32,  # non-hex fingerprint
        "ev_9f2c4a1b7e031234_not-a-uuid",  # bad chunk id
        "ev_9f2c4a1b7e031234_",  # empty chunk id
        "/mainframe_manuals/abc123",  # collection layout is not a reference
        "http://localhost:6333/collections/x/points/abc123",  # nor a URL
        "",
        None,
        123,
    ],
)
def test_parse_reference_rejects_non_references(bad):
    with pytest.raises(ValueError):
        parse_reference(bad)


# ------------------------------------------------------- search_evidence


class _RecordingRetrieve:
    """retrieve_fn double: records scope/limit, returns fixed hits."""

    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    def __call__(self, client, embedder, collection, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        return list(self.hits), "identifier", {"embed_ms": 1, "qdrant_ms": 2}


def test_search_evidence_preserves_scope_and_mints_refs():
    settings = _settings()
    rules_v = _rules_v()
    retrieve = _RecordingRetrieve([make_hit(chunk_id=_uuid(1)), make_hit(chunk_id=_uuid(2))])
    hits, kind, _timings, genfp = _run(
        search_evidence(
            None,
            None,
            "mainframe_manuals",
            "IEA500I start",
            product="z/OS",
            version="9.9",
            source="syn-manuals",
            limit=8,
            settings=settings,
            rules_v=rules_v,
            retrieve_fn=retrieve,
        )
    )
    call = retrieve.calls[0]
    assert call["product"] == "z/OS"
    assert call["version"] == "9.9"
    assert call["source"] == "syn-manuals"
    assert call["limit"] == 8
    assert kind == "identifier"
    assert genfp == current_genfp_hex(settings, rules_v)
    assert [h.reference for h in hits] == [
        mint_reference(genfp, _uuid(1)),
        mint_reference(genfp, _uuid(2)),
    ]
    for ref in (h.reference for h in hits):
        assert parse_reference(ref)[0] == genfp


def test_search_evidence_default_scope_is_open():
    settings = _settings()
    retrieve = _RecordingRetrieve([])
    _run(
        search_evidence(
            None,
            None,
            "mainframe_manuals",
            "how to start",
            settings=settings,
            rules_v=_rules_v(),
            retrieve_fn=retrieve,
        )
    )
    call = retrieve.calls[0]
    assert call["product"] is None
    assert call["version"] is None
    assert call["source"] is None


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_attach_references_is_pure():
    hits = [make_hit(chunk_id=_uuid(3))]
    out = attach_references(hits, "9f2c4a1b7e031234")
    assert hits[0].reference is None
    assert out[0].reference == mint_reference("9f2c4a1b7e031234", _uuid(3))


# ------------------------------------------------------- read_evidence


class _RetrieveFake:
    """Qdrant retrieve double keyed by point id."""

    def __init__(self, by_id):
        self.by_id = by_id
        self.calls = []

    async def retrieve(self, collection, ids, *, with_payload, with_vectors=False):
        from types import SimpleNamespace

        self.calls.append(
            {"collection": collection, "ids": list(ids), "with_payload": list(with_payload)}
        )
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
        "members": ["IEASYSxx"],
        "text": "IEA500I synthetic procedure text",
    }
    base.update(extra)
    return base


def test_read_evidence_returns_record():
    settings = _settings()
    rules_v = _rules_v()
    genfp = current_genfp_hex(settings, rules_v)
    chunk = _uuid(11)
    client = _RetrieveFake({chunk: _payload()})
    record = _run(
        read_evidence(client, "mainframe_manuals", mint_reference(genfp, chunk), settings, rules_v)
    )
    assert record.reference == mint_reference(genfp, chunk)
    assert record.generation == genfp
    assert record.doc_id == "SA22-0000-00"
    assert record.source_rev == "syn|z/OS|9.9|" + "a" * 64
    assert record.source == "syn-manuals"
    assert record.members == ("IEASYSxx",)
    assert record.truncated is False
    call = client.calls[0]
    assert call["collection"] == "mainframe_manuals"
    assert "source_rev" in call["with_payload"]
    assert "source" in call["with_payload"]


def test_read_evidence_unknown_point_is_not_found():
    settings = _settings()
    rules_v = _rules_v()
    genfp = current_genfp_hex(settings, rules_v)
    client = _RetrieveFake({})
    with pytest.raises(EvidenceNotFound):
        _run(
            read_evidence(
                client, "mainframe_manuals", mint_reference(genfp, _uuid(12)), settings, rules_v
            )
        )


def test_read_evidence_superseded_generation_is_retired_not_substituted():
    """Counterexample for silent substitution: the same chunk id exists with
    NEW text under the current generation, but the old reference must report
    retired instead of resolving to the new bytes."""
    old_settings = _settings(embed_model="test-embed-old")
    new_settings = _settings(embed_model="test-embed-new")
    rules_v = _rules_v()
    old_genfp = current_genfp_hex(old_settings, rules_v)
    new_genfp = current_genfp_hex(new_settings, rules_v)
    assert old_genfp != new_genfp
    chunk = _uuid(13)
    client = _RetrieveFake({chunk: _payload(text="NEW text under the new representation")})
    with pytest.raises(EvidenceRetired):
        _run(
            read_evidence(
                client,
                "mainframe_manuals",
                mint_reference(old_genfp, chunk),
                new_settings,
                rules_v,
            )
        )
    assert client.calls == []


def test_read_evidence_malformed_reference_is_rejected_before_store_contact():
    settings = _settings()
    client = _RetrieveFake({})
    with pytest.raises(ValueError):
        _run(read_evidence(client, "mainframe_manuals", "abc123", settings, _rules_v()))
    assert client.calls == []


def test_read_evidence_truncation_is_explicit():
    settings = _settings()
    rules_v = _rules_v()
    genfp = current_genfp_hex(settings, rules_v)
    chunk = _uuid(14)
    client = _RetrieveFake({chunk: _payload(text="x" * 100)})
    record = _run(
        read_evidence(
            client,
            "mainframe_manuals",
            mint_reference(genfp, chunk),
            settings,
            rules_v,
            context_budget=20,
        )
    )
    assert record.truncated is True
    assert record.text.endswith("\n... [truncated]")
    assert len(record.text) <= 20


def test_read_evidence_default_budget_holds_real_chunks():
    assert EVIDENCE_DEFAULT_CONTEXT_BUDGET >= 3500


# ------------------------------------------------------- HTTP adapter


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setenv("LLM_MODEL_REASONING", "test-reasoning-model")
    # Hermetic RED instruments: an earlier test enables the global metrics
    # provider via lifespan, and test_metrics.py pins exact global series
    # counts — our adapter calls must not leak into those series.
    from mainframe_rag.agent import metrics as metrics_mod

    monkeypatch.setattr(metrics_mod, "_instruments", None)
    with TestClient(app_mod.app) as c:
        yield c


def _search_hit(chunk_id, text="IEA500I synthetic text"):
    return make_hit(chunk_id=chunk_id, text=text)


def test_v1_search_returns_generation_and_references(client, monkeypatch):
    from mainframe_rag.retrieve.evidence import parse_reference as parse_ref

    chunk = _uuid(21)
    monkeypatch.setattr(app_mod, "retrieve_search", _RecordingRetrieve([_search_hit(chunk)]))
    resp = client.post(
        "/v1/search", json={"query": "IEA500I", "product": "z/OS", "source": "syn-manuals"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["query_kind"] == "identifier"
    assert body["generation"] is not None
    assert len(body["generation"]) == 16
    assert len(body["hits"]) == 1
    assert body["hits"][0]["reference"] is not None
    genfp, back = parse_ref(body["hits"][0]["reference"])
    assert genfp == body["generation"]
    assert back == chunk


def test_v1_search_without_source_stays_open(client, monkeypatch):
    retrieve = _RecordingRetrieve([_search_hit(_uuid(22))])
    monkeypatch.setattr(app_mod, "retrieve_search", retrieve)
    resp = client.post("/v1/search", json={"query": "IEA500I"})
    assert resp.status_code == 200
    assert retrieve.calls[0]["source"] is None


def _evidence_qdrant(monkeypatch, by_id):
    monkeypatch.setattr(app_mod, "qdrant", _RetrieveFake(by_id))


def test_v1_evidence_round_trip(client, monkeypatch):
    chunk = _uuid(31)
    search_fake = _RecordingRetrieve([_search_hit(chunk)])
    monkeypatch.setattr(app_mod, "retrieve_search", search_fake)
    found = client.post("/v1/search", json={"query": "IEA500I"})
    reference = found.json()["hits"][0]["reference"]
    _evidence_qdrant(monkeypatch, {chunk: _payload()})
    resp = client.get(f"/v1/evidence/{reference}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["reference"] == reference
    assert body["doc_id"] == "SA22-0000-00"
    assert body["source_rev"] == "syn|z/OS|9.9|" + "a" * 64
    assert body["truncated"] is False


def test_v1_evidence_unknown_reference_is_not_found(client, monkeypatch):
    _evidence_qdrant(monkeypatch, {})
    from mainframe_rag.retrieve.evidence import current_genfp_hex as live_genfp

    live = live_genfp(app_mod.settings, extraction_rules_version())
    resp = client.get(f"/v1/evidence/{mint_reference(live, _uuid(32))}")
    assert resp.status_code == 404
    assert resp.json() == {"code": "not_found", "message": "evidence not found"}


def test_v1_evidence_retired_reference_reports_refresh(client, monkeypatch):
    _evidence_qdrant(monkeypatch, {_uuid(33): _payload(text="new bytes")})
    resp = client.get(f"/v1/evidence/{mint_reference('9f2c4a1b7e031234', _uuid(33))}")
    assert resp.status_code == 404
    assert resp.json() == {"code": "not_found", "message": "evidence retired; refresh explicitly"}


def test_v1_evidence_malformed_reference_is_rejected(client, monkeypatch):
    _evidence_qdrant(monkeypatch, {})
    resp = client.get("/v1/evidence/abc123")
    assert resp.status_code == 422
    assert resp.json() == {"code": "invalid_request", "message": "request body failed validation"}


def test_v1_evidence_never_exposes_physical_collection(client, monkeypatch):
    chunk = _uuid(34)
    monkeypatch.setattr(app_mod, "retrieve_search", _RecordingRetrieve([_search_hit(chunk)]))
    found = client.post("/v1/search", json={"query": "IEA500I"})
    body = found.json()
    assert app_mod.settings.qdrant_collection not in body["hits"][0]["reference"]
    assert body["generation"] != app_mod.settings.qdrant_collection
    _evidence_qdrant(monkeypatch, {chunk: _payload()})
    exact = client.get(f"/v1/evidence/{body['hits'][0]['reference']}")
    assert app_mod.settings.qdrant_collection not in exact.text
