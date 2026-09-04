"""Agent API tests: /v1/search (no LLM), /v1/answer (reasoning model only).

Qdrant and the LLM are faked; citation enforcement is checked against the
retrieved hit set.
"""

import json
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from mainframe_rag.agent import app as app_mod
from mainframe_rag.agent.answer import parse_answer
from mainframe_rag.agent.cites import extract_citation_lines, valid_citations
from mainframe_rag.agent.tokenizer import FallbackTokenizer
from mainframe_rag.ports import TokenUsage
from mainframe_rag.retrieve.query import SearchHit


def _hit(cite_suffix: str = "p. 1-6", text: str = "IEA500I BEFORE IOS IOSCMDS COMMAND REJECTED, REASON=yy") -> SearchHit:
    return SearchHit(
        chunk_id="abc123",
        score=0.42,
        cite=f"SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, {cite_suffix}",
        heading="Chapter 2 > IEA500I",
        text=text,
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
        # The lifespan builds a real VllmTokenizer pointed at llm.internal;
        # swap in the in-process fallback so no test ever dials the network.
        monkeypatch.setattr(app_mod, "tokenizer", FallbackTokenizer())
        yield c


class MagicMockSearch:
    def __init__(self):
        self.calls = []

    def search(self, qdrant, embedder, collection, query, product=None, version=None, limit=8, *args, **kwargs):
        self.calls.append({"query": query, "product": product, "version": version})
        return [_hit()], "identifier", {"embed_ms": 1, "qdrant_ms": 2}


class FakeLLM:
    """LLMClient double: asserts the reasoning prompt shape and records calls
    so tests can prove /v1/search never reaches it."""

    def __init__(self):
        self.calls = 0
        self.last_reasoning_effort = None
        self.last_temperature = None
        self.last_messages = None

    def chat(self, messages, reasoning_effort=None, temperature=None):
        self.calls += 1
        self.last_reasoning_effort = reasoning_effort
        self.last_temperature = temperature
        self.last_messages = messages
        assert messages[0].role == "system"
        return (
            "Reissue the command after initialization completes.\n\n"
            "```jcl\n// example only\nIOSCMDS LIST\n```\n\n"
            "Citations:\n"
            "- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n"
            "- SA22-9999-99 Not Retrieved, Made Up > Path, p. 9-9\n"
        )


class FabricatingBodyLLM:
    """Quotes a full citation line that is not in the hit set, mid-answer."""

    def chat(self, messages, *args, **kwargs):
        return (
            "Answer text.\n"
            "SA22-9999-99 Not Retrieved, Made Up > Path, p. 9-9\n"
            "SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n\n"
            "Citations:\n"
            "- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n"
        )


class FabricatingScriptLLM:
    """Puts a fabricated citation inside the fenced script block."""

    def chat(self, messages, *args, **kwargs):
        return (
            "Answer text.\n\n"
            "```jcl\n// see SA22-9999-99 Not Retrieved, Made Up > Path, p. 9-9\n"
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

    class ReadyPool:
        async def get(self, *a, **k):
            return Ready()

    # healthz uses the pooled async client only (review S2): the double
    # replaces app_mod.http, never the httpx2 module functions.
    monkeypatch.setattr(app_mod, "http", ReadyPool())
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

    class NotReadyPool:
        async def get(self, *a, **k):
            return NotReady()

    class Boom:
        async def post(self, *a, **k):
            raise RuntimeError("vllm said: token=abc")

        def close(self):
            pass

    monkeypatch.setattr(app_mod, "http", NotReadyPool())
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
    {"code": "internal"}. Response construction runs after the handler's try, so breaking it
    yields a genuinely unhandled exception."""

    def boom_response(*_a, **_k):
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
    monkeypatch.setattr(app_mod, "SearchResponse", boom_response)
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
        def chat(self, _messages, *args, **kwargs):
            raise RuntimeError("llm exploded: internal detail")

    monkeypatch.setattr(app_mod, "llm", ExplodingLLM())
    resp = client.post("/v1/answer", json={"query": "IEA500I"})
    assert resp.status_code == 502
    assert resp.json() == {"code": "upstream_error", "message": "answer failed"}
    assert "explode" not in resp.text


def test_prompt_build_failure_maps_to_internal(monkeypatch):
    """Review S5: prompt construction is local work, deliberately outside the
    upstream try — a build failure is an internal fault (500 "internal"),
    never mislabeled as 502 "answer failed" / "retrieval failed". Like the
    unhandled-error contract, ServerErrorMiddleware re-raises after the 500
    is sent, so the client must be built with raise_server_exceptions=False."""

    def boom_response(*_a, **_k):
        raise ValueError("budget invariant violated: internal detail")

    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    # CI/dev embed profile: hash mode, explicitly allowed (PR D fail-fast)
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setenv("LLM_MODEL_REASONING", "test-reasoning-model")
    monkeypatch.setattr(app_mod, "retrieve_search", MagicMockSearch().search)
    with TestClient(app_mod.app, raise_server_exceptions=False) as c:
        monkeypatch.setattr(app_mod, "llm", FakeLLM())
        monkeypatch.setattr(app_mod, "tokenizer", FallbackTokenizer())
        monkeypatch.setattr(app_mod, "build_messages", boom_response)
        resp = c.post("/v1/answer", json={"query": "IEA500I"})
    assert resp.status_code == 500
    assert resp.json() == {"code": "internal", "message": "internal error"}
    assert "budget invariant" not in resp.text


def test_parse_answer_shape():
    content = (
        "Answer text.\n\nCitations:\n"
        "- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n"
        "- garbage line without format\n"
    )
    allowed = {_hit().cite}
    parsed = parse_answer(content, allowed)
    assert parsed.answer == "Answer text."
    assert parsed.citations == [_hit().cite]
    assert parsed.script is None
    assert parsed.citations_inferred is False


def test_parse_answer_bracketed_fallback():
    """Item 1: [n] fallback resolves ordered_cites[n-1] when Citations: is absent."""
    cite1 = "SA22-0000-00 Synthetic Reference, Chapter 1 > System parameters, p. 1-3"
    cite2 = "SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6"
    allowed = {cite1, cite2}
    ordered = [cite1, cite2]

    # 1. [2] only -> ordered_cites[1]
    res1 = parse_answer("Details in [2].", allowed, ordered_cites=ordered)
    assert res1.citations == [cite2]
    assert res1.citations_inferred is True
    assert res1.inferred_indices == [2]

    # 2. Citations: exact line still wins (not inferred)
    res2 = parse_answer(f"Answer text based on [2].\n\nCitations:\n{cite1}", allowed, ordered_cites=ordered)
    assert res2.citations == [cite1]
    assert res2.citations_inferred is False

    # 3. z/OS (3.1), (2), APARs (1, 2) in parentheses with no Citations: -> zero inferred cites
    res3 = parse_answer("Runs on z/OS (3.1) with APARs (1, 2) and option (2).", allowed, ordered_cites=ordered)
    assert res3.citations == []
    assert res3.citations_inferred is False

    # 4. Mixed [1] and [2] and [1, 2] -> both, de-duped
    res4 = parse_answer("Points from [1] and [2], summarized in [1, 2].", allowed, ordered_cites=ordered)
    assert res4.citations == [cite1, cite2]
    assert res4.citations_inferred is True
    assert res4.inferred_indices == [1, 2]

    # 5. Out of bounds index [99] -> zero inferred
    res5 = parse_answer("See [99].", allowed, ordered_cites=ordered)
    assert res5.citations == []
    assert res5.citations_inferred is False


def test_parse_answer_citations_positions_and_case():
    """Citations block at top, middle, or uppercase CITATIONS: must not eat prose."""
    cite1 = "SA22-0000-00 Synthetic Reference, Chapter 1 > System parameters, p. 1-3"
    allowed = {cite1}

    # 1. Top-placed Citations: without blank line before prose
    raw_top = f"Citations:\n{cite1}\nActual explanation text here."
    res_top = parse_answer(raw_top, allowed)
    assert res_top.citations == [cite1]
    assert res_top.answer == "Actual explanation text here."

    # 2. Middle-placed Citations: without blank line before subsequent prose
    raw_mid = f"Intro paragraph.\n\nCitations:\n{cite1}\nMore operational detail."
    res_mid = parse_answer(raw_mid, allowed)
    assert res_mid.citations == [cite1]
    assert res_mid.answer == "Intro paragraph.\n\nMore operational detail."

    # 3. Uppercase CITATIONS: header
    raw_upper = f"Intro paragraph.\n\nCITATIONS:\n{cite1}\nMore detail."
    res_upper = parse_answer(raw_upper, allowed)
    assert res_upper.citations == [cite1]
    assert res_upper.answer == "Intro paragraph.\n\nMore detail."


def test_parse_answer_code_fence_and_script_extraction():
    """Script languages extract to script; unlabeled/prose fences unwrap to answer."""
    cite1 = "SA22-0000-00 Synthetic Reference, Chapter 1 > System parameters, p. 1-3"
    allowed = {cite1}

    # 1. Labeled ```jcl block returns both non-empty answer and extracted script
    raw_jcl = f"To apply parameter updates:\n\n```jcl\n//JOB1 JOB ...\n//STEP1 EXEC PGM=IEFBR14\n```\n\nCitations:\n{cite1}"
    res_jcl = parse_answer(raw_jcl, allowed)
    assert res_jcl.citations == [cite1]
    assert res_jcl.answer == "To apply parameter updates:"
    assert res_jcl.script == "//JOB1 JOB ...\n//STEP1 EXEC PGM=IEFBR14"

    # 2. Bare unlabeled fence unwraps to answer body; script is None
    raw_bare = f"```\nAll text in code fence\n```\n\nCitations:\n{cite1}"
    res_bare = parse_answer(raw_bare, allowed)
    assert res_bare.citations == [cite1]
    assert res_bare.answer == "All text in code fence"
    assert res_bare.script is None

    # 3. Thinking block dropped, JCL script extracted, prose answer preserved
    raw_think = f"```thought\nAnalyzing parmlib member...\n```\nFinal operational guidance.\n```rexx\n/* REXX */\nSAY 'HELLO'\n```\nCitations:\n{cite1}"
    res_think = parse_answer(raw_think, allowed)
    assert res_think.citations == [cite1]
    assert res_think.answer == "Final operational guidance."
    assert res_think.script == "/* REXX */\nSAY 'HELLO'"


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


def test_build_messages_context_budgeting():
    from mainframe_rag.agent.answer import build_messages

    hit1 = _hit().model_copy(update={"text": "A" * 5000})
    hit2 = _hit(cite_suffix="p. 1-7").model_copy(update={"text": "B" * 5000})

    # Per-chunk max caps chunk text to 100 chars
    msgs1 = build_messages("test query", [hit1], max_chunk_chars=100, max_context_chars=1000)
    user_prompt1 = msgs1[1].content
    assert "... [truncated]" in user_prompt1
    assert len(user_prompt1) < 500

    # Total context max truncates subsequent hits
    msgs2 = build_messages("test query", [hit1, hit2], max_chunk_chars=400, max_context_chars=500)
    user_prompt2 = msgs2[1].content
    assert "[1]" in user_prompt2
    assert len(user_prompt2) < 1500


def test_classify_query_complexity():
    from mainframe_rag.agent.answer import classify_query_complexity

    # Simple queries
    assert classify_query_complexity("IEA500I") == "simple"
    assert classify_query_complexity("What does operator message IEA500I indicate?") == "simple"
    assert classify_query_complexity("What parameter in IEASYSxx defines 1MB large page frames?") == "simple"
    assert classify_query_complexity("What return code does NFS mount fail with?") == "simple"
    # Operator inquiry with message ID stays simple (not penalized with complex protocol/thinner context)
    assert classify_query_complexity("How do I resolve IEA500I IOSCMDS command rejected and what operator action is needed?") == "simple"

    # Complex queries (diagnostic, troubleshooting, abend, recovery, procedural, tuning)
    assert classify_query_complexity("How do I diagnose and recover when DFSMShsm journal fills up?") == "complex"
    assert classify_query_complexity("Troubleshooting S0C4 abend in batch job") == "complex"
    assert classify_query_complexity("Explain how to configure 1MB and 2GB page frames with LFAREA") == "complex"
    assert classify_query_complexity("Compare DFSORT memory options HIPRMAX vs MOSIZE and how to tune") == "complex"
    assert classify_query_complexity("How do I create an OPS TOD rule that runs in intervals of 4 hours, starting from 12:10 AM?") == "complex"


def test_build_messages_complexity():
    from mainframe_rag.agent.answer import (
        SYSTEM_PROMPT,
        SYSTEM_PROMPT_COMPLEX_EXTENSION,
        build_messages,
    )

    simple_msgs = build_messages("IEA500I", [_hit()], complexity="simple")
    assert simple_msgs[0].content == SYSTEM_PROMPT

    complex_msgs = build_messages(
        "How do I diagnose and recover when DFSMShsm journal fills up?",
        [_hit()],
        complexity="complex",
    )
    assert SYSTEM_PROMPT_COMPLEX_EXTENSION in complex_msgs[0].content


def test_build_messages_context_budgeting_complex_vs_simple():
    from mainframe_rag.agent.answer import build_messages

    hits = [
        _hit(f"p. 1-{i}", text="X" * 2000)
        for i in range(3)
    ]
    # Simple query uses 8000 budget - all 3 hits fit
    simple_msgs = build_messages("IEA500I", hits, complexity="simple", max_context_chars=8000)
    user_content_simple = simple_msgs[1].content
    assert "[1]" in user_content_simple
    assert "[2]" in user_content_simple
    assert "[3]" in user_content_simple

    # Complex query uses 4500 budget - 3rd hit is truncated or omitted to stay within budget
    complex_msgs = build_messages("How to configure large pages", hits, complexity="complex", max_context_chars=4500)
    user_content_complex = complex_msgs[1].content
    assert len(user_content_complex) < len(user_content_simple)
    excerpts_part = user_content_complex.split("Retrieved manual excerpts:\n")[1].split("Please answer")[0]
    assert len(excerpts_part) <= 4600


def test_answer_dispatches_reasoning_effort_by_complexity(client, monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(app_mod, "llm", fake)

    # Simple query dispatches simple reasoning effort (low) and configured temperature
    resp_simple = client.post("/v1/answer", json={"query": "IEA500I"})
    assert resp_simple.status_code == 200
    assert fake.last_reasoning_effort == "low"
    assert fake.last_temperature == 0.2

    # Complex query dispatches complex reasoning effort (high) and configured temperature
    resp_complex = client.post(
        "/v1/answer",
        json={"query": "How do I diagnose and recover when DFSMShsm journal fills up during migration?"},
    )
    assert resp_complex.status_code == 200
    assert fake.last_reasoning_effort == "high"
    assert fake.last_temperature == 0.2


def test_httpx_llm_client_passes_reasoning_params(monkeypatch):
    from mainframe_rag.agent.answer import HttpxLLMClient
    from mainframe_rag.config import Settings
    from mainframe_rag.ports import ChatMessage

    recorded_payload: dict[str, object] = {}

    class MockHttpResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [
                    {"message": {"content": "Test answer\n\nCitations:\nSA22-0000-00, p. 1-6"}}
                ]
            }

    class MockHttpClient:
        def post(self, url, json=None, timeout=None):
            recorded_payload.update(json or {})
            return MockHttpResp()

        def close(self):
            pass

    s = Settings(
        llm_base_url="http://mock-llm/v1",
        llm_model_reasoning="mock-reasoning-model",
        _env_file=None,
    )
    client = HttpxLLMClient(s, client=MockHttpClient())  # type: ignore[arg-type]
    msgs = [ChatMessage(role="user", content="Hello")]
    ans = client.chat(msgs, reasoning_effort="high", temperature=0.2)
    assert ans.content and "Test answer" in ans.content
    assert ans.finish_reason == "stop"
    assert recorded_payload["model"] == "mock-reasoning-model"
    assert recorded_payload["reasoning_effort"] == "high"
    assert recorded_payload["temperature"] == 0.2


def test_parse_answer_markdown_heading_citations():
    from mainframe_rag.agent.answer import parse_answer

    allowed = {"SA22-7592-05 z/OS MVS Init, IEASYSxx > LFAREA, p. 1-17"}
    content = (
        "Here is the answer.\n\n"
        "### Citations:\n"
        "* SA22-7592-05 z/OS MVS Init, IEASYSxx > LFAREA, p. 1-17\n"
    )
    parsed = parse_answer(content, allowed, ordered_cites=list(allowed))
    assert parsed.citations == ["SA22-7592-05 z/OS MVS Init, IEASYSxx > LFAREA, p. 1-17"]
    assert not parsed.citations_inferred


def test_parse_answer_trailing_citations_without_header():
    from mainframe_rag.agent.answer import parse_answer

    allowed = {
        "ca-ops-14-0 OPS/MVS Using, p. 596",
        "ca-ops-14-0 OPS/MVS Using > Rules > TOD, p. 589",
    }
    content = (
        "Here is the synthesized TOD rule:\n"
        ")TOD 00:10,4 HOURS\n\n"
        "ca-ops-14-0 OPS/MVS Using, p. 596\n"
        "ca-ops-14-0 OPS/MVS Using > Rules > TOD, p. 589\n"
    )
    parsed = parse_answer(content, allowed, ordered_cites=list(allowed))
    assert len(parsed.citations) == 2
    assert "ca-ops-14-0 OPS/MVS Using, p. 596" in parsed.citations
    assert "ca-ops-14-0 OPS/MVS Using > Rules > TOD, p. 589" in parsed.citations
    assert not parsed.citations_inferred
    assert "ca-ops-14-0" not in parsed.answer


def test_parse_answer_does_not_strip_non_citation_trailing_lines():
    from mainframe_rag.agent.answer import parse_answer

    allowed = {"ca-ops-14-0 OPS/MVS Using, p. 596"}
    content = (
        "Here is the synthesized TOD rule:\n"
        ")TOD 00:10,4 HOURS\n\n"
        "ca-ops-14-0 OPS/MVS Using, p. 596\n\n"
        "Run DISPLAY M=CPU to verify."
    )
    parsed = parse_answer(content, allowed, ordered_cites=list(allowed))
    # Last line is "Run DISPLAY M=CPU to verify." - must NOT be eaten or treated as a cite
    assert "Run DISPLAY M=CPU to verify." in parsed.answer
    assert parsed.citations == []


def test_script_langs_supports_ops_and_rule():
    from mainframe_rag.agent.answer import parse_answer

    content = "Here is the rule:\n```ops\n)TOD 00:10,4 HOURS\n```\nDone."
    parsed = parse_answer(content, set())
    assert parsed.script == ")TOD 00:10,4 HOURS"
    assert "```ops" not in parsed.answer


def test_text_and_markdown_fences_unwrap_without_script():
    from mainframe_rag.agent.answer import parse_answer

    content = "Here is explanation:\n```text\nSome plain text prose\n```\nAnd:\n```markdown\n* bullet point\n```\nDone."
    parsed = parse_answer(content, set())
    assert parsed.script is None
    assert "Some plain text prose" in parsed.answer
    assert "* bullet point" in parsed.answer


def test_normalize_citation_line_peels_bracketed_numbers():
    from mainframe_rag.agent.cites import normalize_citation_line

    assert (
        normalize_citation_line("[1] SA22-7592-05 z/OS MVS Init, IEASYSxx, p. 1-17")
        == "SA22-7592-05 z/OS MVS Init, IEASYSxx, p. 1-17"
    )
    assert (
        normalize_citation_line("[1]: SA22-7592-05 z/OS MVS Init, IEASYSxx, p. 1-17")
        == "SA22-7592-05 z/OS MVS Init, IEASYSxx, p. 1-17"
    )
    assert (
        normalize_citation_line("* [2] SA22-7592-05 z/OS MVS Init, IEASYSxx, p. 1-17")
        == "SA22-7592-05 z/OS MVS Init, IEASYSxx, p. 1-17"
    )


def test_build_messages_preserves_syntax_and_message_fidelity():
    from mainframe_rag.agent.answer import build_messages
    from mainframe_rag.retrieve.query import SearchHit

    syntax_hit = SearchHit(
        chunk_id="s1",
        score=1.0,
        cite="SA23-1380-70 z/OS MVS Reference, p. 444",
        heading="LFAREA syntax",
        text="LFAREA = {xM | xG | xT | x%} " + "A" * 1800,
        doc_id="SA23-1380-70",
        title="Title",
        page_label="444",
        chunk_type="syntax",
        message_ids=(),
    )
    narrative_hit = SearchHit(
        chunk_id="n1",
        score=0.9,
        cite="SA23-1379-70 z/OS MVS Guide, p. 32",
        heading="Overview",
        text="Narrative explanation " + "B" * 1800,
        doc_id="SA23-1379-70",
        title="Title",
        page_label="32",
        chunk_type="narrative",
        message_ids=(),
    )

    msgs = build_messages(
        "How to configure large pages",
        [syntax_hit, narrative_hit],
        complexity="complex",
        max_context_chars=4500,
        max_chunk_chars=3000,
        max_chunk_chars_narrative=1100,
    )
    content = msgs[1].content
    # Syntax chunk was NOT truncated down to 1100 chars; kept full 1800+ chars
    assert "..." not in content.split("[1]")[1].split("[2]")[0]
    # Narrative chunk was truncated to narrative_cap (1100 chars)
    assert "[truncated]" in content.split("[2]")[1]


def test_answer_audit_log_with_chat_result(client, monkeypatch, caplog):
    import json
    import logging

    from mainframe_rag.ports import ChatResult, TokenUsage

    class UsageLLM:
        def chat(self, messages, *args, **kwargs):
            return ChatResult(
                content="Reissue command.\n\nCitations:\n- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n",
                finish_reason="stop",
                usage=TokenUsage(
                    prompt_tokens=120,
                    completion_tokens=45,
                    reasoning_tokens=20,
                    total_tokens=165,
                ),
            )

    monkeypatch.setattr(app_mod, "llm", UsageLLM())

    with caplog.at_level(logging.INFO):
        resp = client.post("/v1/answer", json={"query": "IEA500I"})

    assert resp.status_code == 200

    # Find audit log record
    records = []
    for r in caplog.records:
        try:
            d = json.loads(r.message)
            if d.get("action") == "answer":
                records.append(d)
        except (ValueError, KeyError):
            pass
    assert records
    audit = records[-1]
    assert audit["query_complexity"] == "simple"
    assert audit["finish_reason"] == "stop"
    assert audit["prompt_tokens"] == 120
    assert audit["completion_tokens"] == 45
    assert audit["reasoning_tokens"] == 20
    assert audit["total_tokens"] == 165


def test_answer_alert_on_non_stop_finish_reason(client, monkeypatch, caplog):
    import json
    import logging

    from mainframe_rag.ports import ChatResult, TokenUsage

    class TruncatedLLM:
        def chat(self, messages, *args, **kwargs):
            return ChatResult(
                content="Partial answer without conclusion.\n\nCitations:\n- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n",
                finish_reason="length",
                usage=TokenUsage(prompt_tokens=200, completion_tokens=100, reasoning_tokens=0, total_tokens=300),
            )

    monkeypatch.setattr(app_mod, "llm", TruncatedLLM())

    with caplog.at_level(logging.WARNING):
        resp = client.post("/v1/answer", json={"query": "IEA500I"})

    assert resp.status_code == 200

    alert_records = []
    for r in caplog.records:
        try:
            d = json.loads(r.message)
            if d.get("action") == "answer_alert":
                alert_records.append(d)
        except (ValueError, KeyError):
            pass
    assert alert_records
    alert = alert_records[-1]
    assert alert["alert"] == "finish_reason_non_stop"
    assert alert["finish_reason"] == "length"


def test_build_messages_tokenizer_budget_respected():
    """Packing keeps sum(count_tokens(chunk)) <= budget after the last
    truncated chunk, and the full prompt fits model_len - reserved. The
    fallback tokenizer is exactly the planner's counter, so the invariant is
    checkable without any network."""
    import re

    from mainframe_rag.agent.answer import build_messages
    from mainframe_rag.config import Settings

    hits = [
        SearchHit(
            chunk_id=f"c{i}",
            score=1.0 - i * 0.1,
            cite=f"SA22-0000-0{i} Manual, p. {i}",
            heading=f"Heading {i}",
            text="word " * 170,  # ~183 tokens with the [i] header
            doc_id=f"DOC{i}",
            title="Title",
            page_label=str(i),
            chunk_type="narrative",
            message_ids=(),
        )
        for i in range(1, 6)
    ]

    settings = Settings(
        llm_max_model_len=1500,
        llm_reserved_output_tokens=250,
        llm_token_safety_margin=100,
        _env_file=None,
    )
    tok = FallbackTokenizer()
    msgs = build_messages(
        "How to configure mainframe storage",
        hits,
        tokenizer=tok,
        settings=settings,
        complexity="simple",
    )

    user_content = msgs[1].content
    # All five excerpts survive packing; the last one is cut, not dropped.
    for i in range(1, 6):
        assert f"[{i}]" in user_content
    prefix, rest = user_content.split("Retrieved manual excerpts:\n", 1)
    block, _tail = rest.split("\n\nPlease answer based strictly", 1)
    chunk_texts = re.split(r"\n\n(?=\[\d+\] )", block)
    assert len(chunk_texts) == 5
    assert "[truncated]" in chunk_texts[-1]

    tail = "Please answer based strictly" + rest.rsplit("Please answer based strictly", 1)[1]
    measured_fixed = tok.count_tokens(msgs[0].content + "\n" + prefix) + tok.count_tokens(tail)
    budget = (
        settings.llm_max_model_len
        - settings.llm_reserved_output_tokens
        - settings.llm_token_safety_margin
        - measured_fixed
    )
    # The invariant: packing (including the truncated tail) stays inside the
    # budget measured against the same counter.
    assert sum(tok.count_tokens(c) for c in chunk_texts) <= budget
    # And the whole prompt fits the window left after the output reservation.
    assert tok.count_messages(msgs) <= settings.llm_max_model_len - settings.llm_reserved_output_tokens


def test_build_messages_tokenizer_requires_settings():
    from mainframe_rag.agent.answer import build_messages

    with pytest.raises(ValueError, match="settings"):
        build_messages("IEA500I", [], tokenizer=FallbackTokenizer())


def test_build_messages_ignores_none_settings_without_tokenizer():
    """The char path must be unaffected by the settings parameter."""
    from mainframe_rag.agent.answer import build_messages

    msgs = build_messages("IEA500I", [_hit()], complexity="simple", settings=None)
    assert "[1]" in msgs[1].content


def test_as_chat_result_wraps_bare_string():
    from mainframe_rag.agent.answer import as_chat_result
    from mainframe_rag.ports import ChatResult, TokenUsage

    res = as_chat_result("plain answer")
    assert isinstance(res, ChatResult)
    assert res.content == "plain answer"
    assert res.finish_reason == "stop"
    assert res.usage == TokenUsage()


def test_as_chat_result_passthrough():
    from mainframe_rag.agent.answer import as_chat_result
    from mainframe_rag.ports import ChatResult, TokenUsage

    original = ChatResult(content="x", finish_reason="length", usage=TokenUsage(prompt_tokens=3))
    assert as_chat_result(original) is original


def test_chat_content_none_becomes_empty_string():
    """A reasoning model that returns content: None must surface as "",
    never the string "None"."""
    from mainframe_rag.agent.answer import HttpxLLMClient
    from mainframe_rag.config import Settings
    from mainframe_rag.ports import ChatMessage, ChatResult

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": None}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
            }

    class FakeClient:
        def post(self, url, json=None):
            return FakeResp()

    settings = Settings(
        llm_base_url="http://llm.internal/v1",
        llm_model_reasoning="test-reasoning-model",
        _env_file=None,
    )
    result = HttpxLLMClient(settings, client=FakeClient()).chat(
        [ChatMessage(role="user", content="q")]
    )
    assert isinstance(result, ChatResult)
    assert result.content == ""


def test_server_timing_headers_emitted_on_search_and_answer(client, monkeypatch):
    """L3 testing harness requirement: Server-Timing header must expose per-stage
    timings (embed_ms, qdrant_ms, llm_ms, ttft_ms) on /v1/search and /v1/answer."""
    from mainframe_rag.ports import ChatResult

    monkeypatch.setattr(
        app_mod, "retrieve_search",
        lambda *a, **k: ([_hit()], "identifier", {"embed_ms": 12, "qdrant_ms": 34}),
    )
    resp = client.post("/v1/search", json={"query": "IEA500I"})
    assert resp.status_code == 200
    st = resp.headers.get("server-timing", "")
    assert "embed;dur=12" in st
    assert "qdrant;dur=34" in st

    class FakeLLMWithTTFT:
        def chat(self, *a, **k):
            return ChatResult(
                content="Answer text.\n\nCitations:\n- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n",
                finish_reason="stop",
                ttft_ms=45,
            )

    monkeypatch.setattr(app_mod, "llm", FakeLLMWithTTFT())
    resp_ans = client.post("/v1/answer", json={"query": "IEA500I"})
    assert resp_ans.status_code == 200
    st_ans = resp_ans.headers.get("server-timing", "")
    assert "embed;dur=12" in st_ans
    assert "qdrant;dur=34" in st_ans
    assert "llm;dur=" in st_ans
    assert "ttft;dur=45" in st_ans


def test_httpx_llm_client_streaming_measures_ttft_on_first_content_token():
    from contextlib import contextmanager

    from mainframe_rag.agent.answer import HttpxLLMClient
    from mainframe_rag.config import Settings
    from mainframe_rag.ports import ChatMessage

    lines = [
        'data: {"choices": [{"delta": {}}]}',
        'data: {"choices": [{"delta": {"content": "Hello "}}]}',
        'data: {"choices": [{"delta": {"content": "World"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}',
        "data: [DONE]",
    ]

    class FakeStreamResp:
        def raise_for_status(self):
            return None

        def iter_lines(self):
            return iter(lines)

    class FakeStreamingClient:
        @contextmanager
        def stream(self, method, url, json=None):
            yield FakeStreamResp()

    settings = Settings(
        llm_base_url="http://llm.internal/v1",
        llm_model_reasoning="test-reasoning-model",
        llm_stream=True,
        _env_file=None,
    )
    client = HttpxLLMClient(settings, client=FakeStreamingClient())
    res = client.chat([ChatMessage(role="user", content="hi")])
    assert res.content == "Hello World"
    assert res.ttft_ms is not None
    assert res.usage.total_tokens == 7


def test_httpx_llm_client_streaming_empty_content_falls_back_to_post():
    from contextlib import contextmanager

    from mainframe_rag.agent.answer import HttpxLLMClient
    from mainframe_rag.config import Settings
    from mainframe_rag.ports import ChatMessage

    class FakeEmptyStreamResp:
        def raise_for_status(self):
            return None

        def iter_lines(self):
            return iter(['data: {"choices": [{"delta": {}}]}', "data: [DONE]"])

    class FakePostResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"role": "assistant", "content": "Fallback content"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
            }

    class FakeFallbackClient:
        def __init__(self):
            self.stream_called = False
            self.post_called = False

        @contextmanager
        def stream(self, method, url, json=None):
            self.stream_called = True
            yield FakeEmptyStreamResp()

        def post(self, url, json=None):
            self.post_called = True
            return FakePostResp()

    settings = Settings(
        llm_base_url="http://llm.internal/v1",
        llm_model_reasoning="test-reasoning-model",
        llm_stream=True,
        _env_file=None,
    )
    fake_client = FakeFallbackClient()
    client = HttpxLLMClient(settings, client=fake_client)
    res = client.chat([ChatMessage(role="user", content="hi")])
    assert fake_client.stream_called is True
    assert fake_client.post_called is True
    assert res.content == "Fallback content"


def test_httpx_llm_client_streaming_error_falls_back_to_post():
    from contextlib import contextmanager

    import httpx2

    from mainframe_rag.agent.answer import HttpxLLMClient
    from mainframe_rag.config import Settings
    from mainframe_rag.ports import ChatMessage

    class FakePostResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"role": "assistant", "content": "Post after stream error"}}],
                "usage": {"total_tokens": 5},
            }

    class FakeErrorStreamClient:
        def __init__(self):
            self.post_called = False

        @contextmanager
        def stream(self, method, url, json=None):
            if False:
                yield None
            raise httpx2.ConnectError("Connection dropped during streaming")

        def post(self, url, json=None):
            self.post_called = True
            return FakePostResp()

    settings = Settings(
        llm_base_url="http://llm.internal/v1",
        llm_model_reasoning="test-reasoning-model",
        llm_stream=True,
        _env_file=None,
    )
    fake_client = FakeErrorStreamClient()
    client = HttpxLLMClient(settings, client=fake_client)
    res = client.chat([ChatMessage(role="user", content="hi")])
    assert fake_client.post_called is True
    assert res.content == "Post after stream error"


class StreamingFakeLLM:
    def __init__(self, deltas: list[str] | None = None):
        self.deltas = deltas or [
            "Reissue the command ",
            "after initialization completes.\n\n",
            "```jcl\n// example only\nIOSCMDS LIST\n```\n\n",
            "Citations:\n",
            "- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n",
            "- SA22-9999-99 Not Retrieved, Made Up > Path, p. 9-9\n",
        ]

    def chat(self, messages, *args, **kwargs):
        return "".join(self.deltas)

    async def chat_stream(self, messages, *args, **kwargs):
        for i, delta in enumerate(self.deltas):
            yield {
                "type": "token",
                "delta": delta,
                "token": delta,
                "ttft_ms": 12 if i == 0 else 24,
            }
        yield {
            "type": "done",
            "finish_reason": "stop",
            "usage": TokenUsage(prompt_tokens=10, completion_tokens=25, reasoning_tokens=5, total_tokens=35),
            "ttft_ms": 12,
        }


def _parse_sse_events(text: str) -> list[tuple[str, dict]]:
    """Parse raw SSE text into a list of (event_type, json_data) tuples."""
    events = []
    current_event = "message"
    current_data: list[str] = []

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            if current_data:
                data_str = "\n".join(current_data)
                events.append((current_event, json.loads(data_str)))
                current_event = "message"
                current_data = []
            continue
        if line.startswith("event:"):
            current_event = line[6:].strip()
        elif line.startswith("data:"):
            current_data.append(line[5:].strip())

    if current_data:
        data_str = "\n".join(current_data)
        events.append((current_event, json.loads(data_str)))

    return events


def test_v1_answer_streaming_sse_token_deltas_and_final_event(client, monkeypatch):
    monkeypatch.setattr(app_mod, "llm", StreamingFakeLLM())
    resp = client.post("/v1/answer?stream=true", json={"query": "IEA500I command"})
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert "Cache-Control" in resp.headers and resp.headers["Cache-Control"] == "no-cache"

    events = _parse_sse_events(resp.text)
    assert len(events) >= 2

    # Verify token deltas
    token_events = [e for e in events if e[0] == "token"]
    assert len(token_events) > 0
    reconstructed = "".join(e[1]["delta"] for e in token_events)
    assert "Reissue the command" in reconstructed

    # Verify final event
    final_events = [e for e in events if e[0] == "final"]
    assert len(final_events) == 1
    final_data = final_events[0][1]
    assert final_data["type"] == "final"
    assert "request_id" in final_data
    assert final_data["citations"] == ["SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6"]
    assert final_data["script"] == "// example only\nIOSCMDS LIST"
    assert final_data["query_kind"] == "identifier"
    assert len(final_data["hits"]) == 1
    assert final_data["ttft_ms"] == 12
    assert final_data["usage"]["total_tokens"] == 35


def test_v1_answer_streaming_citations_identical_to_non_streaming(client, monkeypatch):
    llm = StreamingFakeLLM()
    monkeypatch.setattr(app_mod, "llm", llm)

    # 1. Non-streaming call
    resp_sync = client.post("/v1/answer", json={"query": "IEA500I command", "stream": False})
    assert resp_sync.status_code == 200
    data_sync = resp_sync.json()

    # 2. Streaming call
    resp_stream = client.post("/v1/answer", json={"query": "IEA500I command", "stream": True})
    assert resp_stream.status_code == 200
    assert "text/event-stream" in resp_stream.headers["content-type"]
    events = _parse_sse_events(resp_stream.text)
    final_events = [e for e in events if e[0] == "final"]
    assert len(final_events) == 1
    data_stream = final_events[0][1]

    # Citations and script must match exactly
    assert data_stream["citations"] == data_sync["citations"]
    assert data_stream["script"] == data_sync["script"]
    assert data_stream["answer"] == data_sync["answer"]


def test_v1_answer_stream_query_param_and_body_precedence(client, monkeypatch):
    monkeypatch.setattr(app_mod, "llm", StreamingFakeLLM())

    # Default is non-streaming
    r1 = client.post("/v1/answer", json={"query": "IEA500I command"})
    assert r1.headers["content-type"] == "application/json"
    assert isinstance(r1.json(), dict)

    # stream: true in body
    r2 = client.post("/v1/answer", json={"query": "IEA500I command", "stream": True})
    assert "text/event-stream" in r2.headers["content-type"]

    # ?stream=true query param overrides body stream: false
    r3 = client.post("/v1/answer?stream=true", json={"query": "IEA500I command", "stream": False})
    assert "text/event-stream" in r3.headers["content-type"]

    # ?stream=false query param overrides body stream: true
    r4 = client.post("/v1/answer?stream=false", json={"query": "IEA500I command", "stream": True})
    assert r4.headers["content-type"] == "application/json"


def test_v1_answer_empty_hits_streaming(client, monkeypatch):
    class EmptySearch:
        def search(self, *a, **kw):
            return [], "nl", {"embed_ms": 1, "qdrant_ms": 1}

    monkeypatch.setattr(app_mod, "retrieve_search", EmptySearch().search)
    resp = client.post("/v1/answer?stream=true", json={"query": "random obscure thing"})
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    events = _parse_sse_events(resp.text)
    assert len(events) == 1
    event_type, payload = events[0]
    assert event_type == "final"
    assert payload["answer"] == "No supporting manual excerpts were found for this question."
    assert payload["citations"] == []
    assert payload["hits"] == []
    # Schema parity with the non-empty final event (review S6): same keys,
    # no tokens were streamed, usage is all zeros.
    assert payload["finish_reason"] == "stop"
    assert payload["ttft_ms"] is None
    assert payload["usage"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
    }


@pytest.mark.anyio
async def test_v1_answer_20_concurrent_requests_no_threadpool_starvation(monkeypatch):
    import asyncio

    import httpx2

    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setenv("LLM_MODEL_REASONING", "test-reasoning-model")

    class SlowAsyncLLM:
        async def chat(self, messages, *args, **kwargs):
            await asyncio.sleep(0.05)
            return "Async response\n\nCitations:\nSA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6"

    monkeypatch.setattr(app_mod, "retrieve_search", MagicMockSearch().search)
    monkeypatch.setattr(app_mod, "llm", SlowAsyncLLM())
    monkeypatch.setattr(app_mod, "tokenizer", FallbackTokenizer())

    transport = httpx2.ASGITransport(app=app_mod.app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as ac:
        t0 = time.monotonic()
        reqs = [
            ac.post("/v1/answer", json={"query": f"IEA500I test {i}"})
            for i in range(20)
        ]
        responses = await asyncio.gather(*reqs)
        total_time = time.monotonic() - t0

    for r in responses:
        assert r.status_code == 200
        assert r.json()["answer"] == "Async response"

    # 20 sequential calls of 0.05s would take >= 1.0s.
    # Cooperatively run on the event loop, all 20 finish together well under 0.6s.
    assert total_time < 0.6


@pytest.mark.anyio
async def test_httpx_llm_client_chat_stream_async():
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    from mainframe_rag.agent.answer import HttpxLLMClient
    from mainframe_rag.config import Settings
    from mainframe_rag.ports import ChatMessage

    class FakeAsyncStreamResp:
        def raise_for_status(self):
            return None

        async def aiter_lines(self) -> AsyncIterator[str]:
            lines = [
                'data: {"choices": [{"delta": {"content": "First "}}]}',
                'data: {"choices": [{"delta": {"content": "second"}}]}',
                'data: {"choices": [{"finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}',
                "data: [DONE]",
            ]
            for l in lines:
                yield l

    class FakeAsyncHttpClient:
        @asynccontextmanager
        async def stream(self, method, url, json=None):
            yield FakeAsyncStreamResp()

    settings = Settings(
        llm_base_url="http://llm.internal/v1",
        llm_model_reasoning="test-reasoning-model",
        _env_file=None,
    )
    client = HttpxLLMClient(settings, client=FakeAsyncHttpClient())
    items = []
    async for item in client.chat_stream([ChatMessage(role="user", content="hi")]):
        items.append(item)

    assert len(items) == 3
    assert items[0]["type"] == "token" and items[0]["delta"] == "First "
    assert items[0]["ttft_ms"] is not None
    assert items[1]["type"] == "token" and items[1]["delta"] == "second"
    assert items[2]["type"] == "done" and items[2]["finish_reason"] == "stop"
    assert items[2]["usage"].total_tokens == 7


@pytest.mark.anyio
async def test_httpx_llm_client_chat_stream_empty_content_recovers_via_post():
    """Review S7: a reasoning model whose whole output lands in the reasoning
    channel yields zero content deltas. chat_stream must recover through the
    non-streaming POST (mirroring achat) instead of ending with an empty
    answer — recovery only fires before any token was yielded, so nothing is
    ever duplicated."""

    from contextlib import asynccontextmanager

    class EmptyStreamResp:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'data: {"choices": [{"delta": {}}]}'
            yield 'data: {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}'
            yield "data: [DONE]"

    class PostResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": "Recovered answer"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            }

    class RecoveringClient:
        def __init__(self):
            self.posts = 0

        @asynccontextmanager
        async def stream(self, method, url, json=None):
            yield EmptyStreamResp()

        async def post(self, url, json=None):
            self.posts += 1
            return PostResp()

    from mainframe_rag.agent.answer import HttpxLLMClient
    from mainframe_rag.config import Settings
    from mainframe_rag.ports import ChatMessage

    fake = RecoveringClient()
    settings = Settings(
        llm_base_url="http://llm.internal/v1",
        llm_model_reasoning="test-reasoning-model",
        _env_file=None,
    )
    llm = HttpxLLMClient(settings, client=fake)
    items = [item async for item in llm.chat_stream([ChatMessage(role="user", content="hi")])]

    assert fake.posts == 1
    assert len(items) == 2
    assert items[0]["type"] == "token" and items[0]["delta"] == "Recovered answer"
    assert items[0]["ttft_ms"] is not None
    assert items[1]["type"] == "done" and items[1]["finish_reason"] == "stop"
    assert items[1]["usage"].total_tokens == 8



def test_search_rejects_overlong_query_before_retrieval(client, monkeypatch):
    """Issue #87: overlong queries fail closed with the validation envelope
    before any retrieval work runs."""
    called = []
    orig = app_mod.retrieve_search

    def recording(*a, **k):
        called.append(1)
        return orig(*a, **k)

    monkeypatch.setattr(app_mod, "retrieve_search", recording)
    resp = client.post("/v1/search", json={"query": "x" * 2001})
    assert resp.status_code == 422
    assert resp.json() == {"code": "invalid_request", "message": "request body failed validation"}
    assert called == []


def test_search_accepts_boundary_length_query(client):
    resp = client.post("/v1/search", json={"query": "x" * 2000})
    assert resp.status_code == 200


def test_query_cap_is_settings_driven(client, monkeypatch):
    """The cap comes from Settings, not a hardcoded literal at the call site."""
    monkeypatch.setattr(app_mod.settings, "query_max_chars", 10)
    assert client.post("/v1/search", json={"query": "12345678901"}).status_code == 422
    assert client.post("/v1/search", json={"query": "1234567890"}).status_code == 200


def test_answer_rejects_overlong_query(client):
    resp = client.post("/v1/answer", json={"query": "x" * 2001})
    assert resp.status_code == 422
    assert resp.json() == {"code": "invalid_request", "message": "request body failed validation"}


def test_build_messages_truncates_overlong_splunk_context():
    """Issue #87: unbounded caller-supplied telemetry truncates with the
    standard suffix instead of starving excerpts out of the window."""
    from mainframe_rag.agent.answer import build_messages

    msgs = build_messages("IEA500I", [_hit()], splunk_context="y" * 5000, splunk_context_max_chars=100)
    user = msgs[1].content
    assert "... [truncated]" in user
    assert "y" * 101 not in user


def test_build_messages_leaves_bounded_splunk_context_alone():
    from mainframe_rag.agent.answer import build_messages

    splunk = "z" * 4000
    msgs = build_messages("IEA500I", [_hit()], splunk_context=splunk, splunk_context_max_chars=4000)
    assert splunk in msgs[1].content
    assert "[truncated]" not in msgs[1].content
