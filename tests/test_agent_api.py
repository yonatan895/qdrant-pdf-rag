"""Agent API tests: /v1/search (no LLM), /v1/answer (reasoning model only).

Qdrant and the LLM are faked; citation enforcement is checked against the
retrieved hit set.
"""

import pytest
from fastapi.testclient import TestClient

from mainframe_rag.agent import app as app_mod
from mainframe_rag.agent.answer import parse_answer
from mainframe_rag.agent.cites import extract_citation_lines, valid_citations
from mainframe_rag.retrieve.query import SearchHit


def _hit(cite_suffix: str = "p. 1-6") -> SearchHit:
    return SearchHit(
        chunk_id="abc123",
        score=0.42,
        cite=f"SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, {cite_suffix}",
        heading="Chapter 2 > IEA500I",
        text="IEA500I BEFORE IOS IOSCMDS COMMAND REJECTED, REASON=yy",
        doc_id="SA22-0000-00",
        title="Synthetic Reference",
        page_label="1-6",
        chunk_type="message",
        product="z/OS",
        version="9.9",
        message_ids=("IEA500I",),
    )


@pytest.fixture
def client(monkeypatch, synthetic_pdf):
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("LLM_MODEL_REASONING", "test-reasoning-model")
    monkeypatch.setattr(app_mod, "retrieve_search", MagicMockSearch().search)
    # patch the LLM client AFTER lifespan built it (module global exists then)
    with TestClient(app_mod.app) as c:
        monkeypatch.setattr(app_mod, "llm", FakeLLM())
        yield c


class MagicMockSearch:
    def __init__(self):
        self.calls = []

    def search(self, qdrant, embedder, collection, query, product=None, version=None, limit=8):
        self.calls.append({"query": query, "product": product, "version": version})
        return [_hit()], "identifier", {"embed_ms": 1, "qdrant_ms": 2}


class FakeLLM:
    """LLMClient double: asserts the reasoning prompt shape."""

    def chat(self, messages):
        assert messages[0]["role"] == "system"
        return (
            "Reissue the command after initialization completes.\n\n"
            "```\n// example only\nIOSCMDS LIST\n```\n\n"
            "Citations:\n"
            "- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n"
            "- SA22-9999-99 Not Retrieved, Made Up > Path, p. 9-9\n"
        )


def test_search_returns_cite_fields(client):
    resp = client.post("/v1/search", json={"query": "IEA500I", "product": "z/OS"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["query_kind"] == "identifier"
    assert body["hits"][0]["cite"] == _hit().cite
    assert body["hits"][0]["heading"] == "Chapter 2 > IEA500I"


def test_answer_validates_citations_and_script(client):
    resp = client.post(
        "/v1/answer",
        json={"query": "IEA500I", "product": "z/OS", "version": "9.9",
              "splunk_context": "IEA500I job IOSCMDS failed"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # Only the retrieved citation survives; the fabricated one is dropped.
    assert body["citations"] == [_hit().cite]
    assert body["script"] is not None and "IOSCMDS LIST" in body["script"]
    assert "Citations:" not in body["answer"]


def test_answer_refuses_without_reasoning_model(monkeypatch, synthetic_pdf):
    monkeypatch.delenv("LLM_MODEL_REASONING", raising=False)
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    with TestClient(app_mod.app) as c:
        resp = c.post("/v1/answer", json={"query": "IEA500I"})
    assert resp.status_code == 503
    assert "reasoning" in resp.json()["detail"].lower()


def test_parse_answer_shape():
    content = (
        "Answer text.\n\nCitations:\n"
        "SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n"
        "garbage line without format\n"
    )
    allowed = {_hit().cite}
    parsed = parse_answer(content, allowed)
    assert parsed["answer"] == "Answer text."
    assert parsed["citations"] == [_hit().cite]
    assert parsed["script"] is None


def test_citation_validation():
    lines = extract_citation_lines(
        "x\nCitations:\n- " + _hit().cite + "\n- bad\n"
    )
    assert lines == [_hit().cite, "bad"]
    assert valid_citations("Citations:\n- " + _hit().cite, {_hit().cite}) == [_hit().cite]
