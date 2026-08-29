"""Agent API tests: /v1/search (no LLM), /v1/answer (reasoning model only).

Qdrant and the LLM are faked; citation enforcement is checked against the
retrieved hit set.
"""

import pytest
from fastapi import HTTPException
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
    # CI/dev embed profile: hash mode, explicitly allowed (PR D fail-fast)
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.internal/v1")
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
    """LLMClient double: asserts the reasoning prompt shape and records calls
    so tests can prove /v1/search never reaches it."""

    def __init__(self):
        self.calls = 0

    def chat(self, messages):
        self.calls += 1
        assert messages[0]["role"] == "system"
        return (
            "Reissue the command after initialization completes.\n\n"
            "```\n// example only\nIOSCMDS LIST\n```\n\n"
            "Citations:\n"
            "- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n"
            "- SA22-9999-99 Not Retrieved, Made Up > Path, p. 9-9\n"
        )


class FabricatingBodyLLM:
    """Quotes a full citation line that is not in the hit set, mid-answer."""

    def chat(self, messages):
        return (
            "Answer text.\n"
            "SA22-9999-99 Not Retrieved, Made Up > Path, p. 9-9\n"
            "SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n\n"
            "Citations:\n"
            "- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n"
        )


class FabricatingScriptLLM:
    """Puts a fabricated citation inside the fenced script block."""

    def chat(self, messages):
        return (
            "Answer text.\n\n"
            "```\n// see SA22-9999-99 Not Retrieved, Made Up > Path, p. 9-9\n"
            "IOSCMDS LIST\n```\n\n"
            "Citations:\n"
            "- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n"
        )


def test_search_returns_cite_fields(client):
    resp = client.post("/v1/search", json={"query": "IEA500I", "product": "z/OS"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["query_kind"] == "identifier"
    assert body["hits"][0]["cite"] == _hit().cite
    assert body["hits"][0]["heading"] == "Chapter 2 > IEA500I"


def test_search_never_calls_llm(client):
    """Issue #20 PR C: /v1/search is lexical+dense retrieval only."""
    assert app_mod.llm.calls == 0  # type: ignore[attr-defined]
    resp = client.post("/v1/search", json={"query": "IEA500I"})
    assert resp.status_code == 200
    assert app_mod.llm.calls == 0  # type: ignore[attr-defined]


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


def test_answer_refuses_without_reasoning_model(monkeypatch):
    monkeypatch.delenv("LLM_MODEL_REASONING", raising=False)
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    # CI/dev embed profile: hash mode, explicitly allowed (PR D fail-fast)
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    with TestClient(app_mod.app) as c:
        resp = c.post("/v1/answer", json={"query": "IEA500I"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["code"] == "not_configured"
    assert "reasoning" in body["message"].lower()


def test_answer_strips_fabricated_body_citation(client, monkeypatch):
    """No cite outside the hit set may reach the client — including ones the
    model quotes mid-answer (issue #20 PR C)."""
    monkeypatch.setattr(app_mod, "llm", FabricatingBodyLLM())
    resp = client.post("/v1/answer", json={"query": "IEA500I"})
    assert resp.status_code == 200
    body = resp.json()
    fabricated = "SA22-9999-99 Not Retrieved, Made Up > Path, p. 9-9"
    assert fabricated not in body["answer"]
    assert _hit().cite in body["answer"]  # retrieved cite survives mid-answer
    assert body["citations"] == [_hit().cite]


def test_strip_unauthorized_handles_multi_digit_markers():
    """Regression: '11.' / '12)' list markers must not smuggle a fabricated
    cite through the body strip (issue #20 PR C review round 5)."""
    from mainframe_rag.agent.cites import strip_unauthorized_citations

    allowed = {_hit().cite}
    fabricated = "SA22-9999-99 Not Retrieved, Made Up > Path, p. 9-9"
    body = "\n".join(
        [f"{i}. filler line {i}" for i in range(1, 11)]
        + [f"11. {fabricated}", f"12) {fabricated}", _hit().cite]
    )
    out = strip_unauthorized_citations(body, allowed)
    assert fabricated not in out
    assert _hit().cite in out
    assert "filler line 5" in out  # numbered prose lines survive


def test_strip_wrapped_fabricated_citations():
    """Round-7 item 1: blockquote/backtick/quote/paren wrapping must not
    smuggle a fabricated cite through the body strip, and the list parser
    must normalize the same way (one shared normalizer)."""
    from mainframe_rag.agent.cites import strip_unauthorized_citations, valid_citations

    allowed = {_hit().cite}
    fabricated = "SA22-9999-99 Not Retrieved, Made Up > Path, p. 9-9"
    body = "\n".join(
        [
            f"> {fabricated}",
            f"`{fabricated}`",
            f'"{fabricated}"',
            f"({fabricated})",
            f"- {fabricated}",
            f"  {fabricated}",
            _hit().cite,
        ]
    )
    out = strip_unauthorized_citations(body, allowed)
    assert fabricated not in out
    assert _hit().cite in out

    # List parser agreement: wrapped forms normalize to the bare cite.
    assert valid_citations("Citations:\n> " + fabricated, allowed) == []
    assert valid_citations("Citations:\n> " + _hit().cite, allowed) == [_hit().cite]
    assert valid_citations("Citations:\n`" + _hit().cite + "`", allowed) == [_hit().cite]


def test_strip_enclosing_markup_and_inline_mentions():
    """Markup-wrapped fabricated cites are stripped cleanly (no half-peeled
    leftovers), bolded allowed cites validate unwrapped, and mid-sentence
    inline mentions are deliberately left alone (standalone-line rule)."""
    from mainframe_rag.agent.cites import strip_unauthorized_citations, valid_citations

    allowed = {_hit().cite}
    fabricated = "SA22-9999-99 Not Retrieved, Made Up > Path, p. 9-9"
    body = "\n".join(
        [f"**{fabricated}**", f"__{fabricated}__", f"*{fabricated}*", _hit().cite]
    )
    out = strip_unauthorized_citations(body, allowed)
    assert fabricated not in out
    assert "*" not in out.replace("**", "")  # no half-peeled leftovers
    assert _hit().cite in out

    # A bolded allowed cite validates to the bare citation.
    assert valid_citations("Citations:\n- **" + _hit().cite + "**", allowed) == [_hit().cite]

    # Standalone-line rule: mid-sentence inline mentions survive untouched.
    inline = f"Refer to {fabricated} for details."
    assert strip_unauthorized_citations(inline, allowed) == inline

    # Marker peel is a discrete prefix: digit-led prose keeps its number.
    prose = "3.5 inches is the recommended gap."
    assert strip_unauthorized_citations(prose, allowed) == prose


def test_strip_angle_bracket_and_markdown_link_wrapping():
    """Round 8: markdown-link and angle-bracket wrapping are wrapping, so
    they strip; prefix/suffix prose stays under the standalone-line rule."""
    from mainframe_rag.agent.cites import strip_unauthorized_citations

    allowed = {_hit().cite}
    fabricated = "SA22-9999-99 Not Retrieved, Made Up > Path, p. 9-9"
    body = "\n".join(
        [
            f"[<{fabricated}>](http://x)",
            f"[{fabricated}](http://x)",
            f"<<{fabricated}>>",
            f"<{fabricated}>",
            _hit().cite,
        ]
    )
    out = strip_unauthorized_citations(body, allowed)
    assert fabricated not in out
    assert _hit().cite in out

    # Prefix/suffix prose is not wrapping: the standalone-line rule applies.
    prose = "\n".join([f"| <{fabricated}> |", f"Cited: <{fabricated}>"])
    assert strip_unauthorized_citations(prose, allowed) == prose


def test_answer_script_block_passes_through_unvalidated(client, monkeypatch):
    """script is code: citation-shaped lines inside the fence are returned
    verbatim (documented behavior, issue #20 PR C)."""
    monkeypatch.setattr(app_mod, "llm", FabricatingScriptLLM())
    resp = client.post("/v1/answer", json={"query": "IEA500I"})
    assert resp.status_code == 200
    body = resp.json()
    assert "SA22-9999-99" in body["script"]
    assert body["citations"] == [_hit().cite]
    assert "SA22-9999-99" not in body["answer"]


def test_healthz_ready_returns_clean_200(client, monkeypatch):
    class Ready:
        status_code = 200
        text = "all shards are ready"

    monkeypatch.setattr(app_mod.httpx, "get", lambda *a, **k: Ready())
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["qdrant"] is True and body["embed"] is None
    assert "qdrant_detail" not in body


def test_healthz_degraded_paths_leak_no_upstream_text(client, monkeypatch):
    """Round-7 item 2: degraded-but-200 /healthz must not forward Qdrant
    response bodies or exception text to the caller."""

    class NotReady:
        status_code = 503
        text = "Internal Server Error: secret bits"

    monkeypatch.setattr(app_mod.httpx, "get", lambda *a, **k: NotReady())

    class Boom:
        def post(self, *a, **k):
            raise RuntimeError("vllm said: token=abc")

        def close(self):
            pass

    monkeypatch.setattr(app_mod, "http", Boom())
    monkeypatch.setattr(app_mod.settings, "embed_base_url", "http://embed.internal/v1")
    monkeypatch.setattr(app_mod.settings, "embed_model", "test-embed")
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["qdrant"] is False and body["embed"] is False
    assert "secret bits" not in resp.text and "token=abc" not in resp.text
    assert set(body) == {"status", "qdrant", "embed"}


def test_http_exception_handler_shape(client):
    """The HTTPException handler must use a fixed message — developer text in
    exc.detail never reaches a client body (round 8)."""

    @app_mod.app.get("/teapot")
    def teapot():
        raise HTTPException(status_code=418, detail="I'm a teapot with secrets")

    resp = client.get("/teapot")
    assert resp.status_code == 418
    assert resp.json() == {"code": "http_error", "message": "request failed"}
    assert "teapot with secrets" not in resp.text


def test_request_id_flows_from_middleware(client):
    """Round-7 item 5: one id per request, generated by the middleware and
    used by handlers + the unhandled-error log."""
    resp = client.post("/v1/search", json={"query": "IEA500I"})
    request_id = resp.json()["request_id"]
    assert len(request_id) == 12
    int(request_id, 16)


def test_404_405_use_structured_shape(client):
    """Router-level 404/405 must use the stable shape too (round 5, item 3)."""
    resp = client.get("/nope")
    assert resp.status_code == 404
    assert resp.json() == {"code": "not_found", "message": "not found"}
    resp = client.post("/healthz")
    assert resp.status_code == 405
    assert resp.json() == {"code": "method_not_allowed", "message": "method not allowed"}


def test_unhandled_error_returns_internal_shape(monkeypatch):
    """An exception escaping the handler logs server-side; client sees only
    {"code": "internal"}. asdict runs after the handler's try, so breaking it
    yields a genuinely unhandled exception."""

    def boom_asdict(_x):
        raise RuntimeError("serializer down")

    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    # CI/dev embed profile: hash mode, explicitly allowed (PR D fail-fast)
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setenv("LLM_MODEL_REASONING", "test-reasoning-model")
    monkeypatch.setattr(
        app_mod, "retrieve_search",
        lambda *a, **k: ([_hit()], "identifier", {"embed_ms": 1, "qdrant_ms": 1}),
    )
    monkeypatch.setattr(app_mod, "asdict", boom_asdict)
    # ServerErrorMiddleware re-raises after sending the 500; the client must
    # not surface that re-raise.
    with TestClient(app_mod.app, raise_server_exceptions=False) as c:
        resp = c.post("/v1/search", json={"query": "IEA500I"})
    assert resp.status_code == 500
    assert resp.json() == {"code": "internal", "message": "internal error"}
    assert "serializer" not in resp.text


def test_healthz_qdrant_unready_structured(client, monkeypatch):
    monkeypatch.setattr(app_mod.settings, "qdrant_url", "http://127.0.0.1:1")
    resp = client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body == {"code": "qdrant_unready", "message": "qdrant is not ready"}
    assert "refused" not in resp.text  # diagnostics stay in logs


def test_error_shape_is_structured(client, monkeypatch):
    """Stable {"code", "message"} JSON — no stack traces to the client."""

    def boom(*_a, **_k):
        raise RuntimeError("qdrant exploded: select * from secrets")

    monkeypatch.setattr(app_mod, "retrieve_search", boom)
    resp = client.post("/v1/search", json={"query": "IEA500I"})
    assert resp.status_code == 502
    body = resp.json()
    assert body == {"code": "upstream_error", "message": "retrieval failed"}
    assert "explode" not in resp.text and "secrets" not in resp.text

    resp = client.post("/v1/search", json={})  # missing query
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "invalid_request"
    assert set(body) == {"code", "message"}


def test_answer_retrieval_failure_reads_retrieval_failed(client, monkeypatch):
    """Same fault, same code+message on every endpoint: a retrieval failure on
    /v1/answer reads exactly like one on /v1/search."""

    def boom(*_a, **_k):
        raise RuntimeError("qdrant exploded: internal detail")

    monkeypatch.setattr(app_mod, "retrieve_search", boom)
    resp = client.post("/v1/answer", json={"query": "IEA500I"})
    assert resp.status_code == 502
    assert resp.json() == {"code": "upstream_error", "message": "retrieval failed"}
    assert "explode" not in resp.text


def test_answer_llm_failure_reads_answer_failed(client, monkeypatch):
    """A model or parse failure is not mislabeled as a retrieval fault."""

    class ExplodingLLM:
        def chat(self, _messages):
            raise RuntimeError("llm exploded: internal detail")

    monkeypatch.setattr(app_mod, "llm", ExplodingLLM())
    resp = client.post("/v1/answer", json={"query": "IEA500I"})
    assert resp.status_code == 502
    assert resp.json() == {"code": "upstream_error", "message": "answer failed"}
    assert "explode" not in resp.text


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


def test_strip_unauthorized_body_citations():
    from mainframe_rag.agent.cites import strip_unauthorized_citations

    allowed = {_hit().cite}
    fabricated = "SA22-9999-99 Not Retrieved, Made Up > Path, p. 9-9"
    text = f"lead in\n{fabricated}\nkeep this\n- {_hit().cite}\n1. {fabricated}\ntail"
    out = strip_unauthorized_citations(text, allowed)
    assert fabricated not in out
    assert "lead in" in out and "keep this" in out and "tail" in out
    assert _hit().cite in out
    # Non-citation lines that merely mention a doc number survive untouched.
    out2 = strip_unauthorized_citations("refer to SA22-9999-99 for details", allowed)
    assert out2 == "refer to SA22-9999-99 for details"
