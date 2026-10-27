"""Operator console tests (ADR-0004): /ui gating, CSP, form and HTMX flows,
SSE streaming, static-asset pin verification, and traversal defense."""

from __future__ import annotations

import contextlib
import hashlib
import html
import json
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mainframe_rag.agent import app as app_mod
from mainframe_rag.agent.tokenizer import FallbackTokenizer
from mainframe_rag.ports import ChatResult, TokenUsage
from tests.fakes import parse_sse_events as _parse_sse_events
from tests.helpers_console_browser import MockSearch, _hit

_VENDOR_DIR = Path(app_mod.__file__).parents[1] / "webui" / "static" / "vendor"


class UiFakeLLM:
    def __init__(self):
        self.calls: list[dict] = []
        self.stream_calls: list[dict] = []

    def chat(self, messages, reasoning_effort=None, temperature=None):
        self.calls.append({
            "messages": messages,
            "reasoning_effort": reasoning_effort,
            "temperature": temperature,
        })
        return ChatResult(
            content=(
                "Reissue the command after initialization.\n\n"
                "Citations:\n"
                "- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n"
            ),
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    async def chat_stream(self, messages, reasoning_effort=None, temperature=None) -> AsyncIterator[dict]:
        self.stream_calls.append({
            "messages": messages,
            "reasoning_effort": reasoning_effort,
            "temperature": temperature,
        })
        yield {"type": "token", "delta": "Reissue the ", "ttft_ms": 5}
        yield {
            "type": "token",
            "delta": (
                "command after initialization.\n\nCitations:\n"
                "- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n"
            ),
        }
        yield {
            "type": "done",
            "finish_reason": "stop",
            "usage": TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            "ttft_ms": 5,
        }


class ExplodingStreamLLM:
    async def chat_stream(self, messages, reasoning_effort=None, temperature=None) -> AsyncIterator[dict]:
        yield {"type": "token", "delta": "partial ", "ttft_ms": 5}
        raise RuntimeError("stream exploded: internal detail")


def _client(monkeypatch, *, ui_enabled: bool, synthetic_pdf, llm=None):
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setenv("LLM_MODEL_REASONING", "test-reasoning-model")
    monkeypatch.setenv("UI_ENABLED", "true" if ui_enabled else "false")
    mock_search = MockSearch()
    monkeypatch.setattr(app_mod, "retrieve_search", mock_search.search)
    with TestClient(app_mod.app) as c:
        monkeypatch.setattr(app_mod, "llm", llm or UiFakeLLM())
        monkeypatch.setattr(app_mod, "tokenizer", FallbackTokenizer())
        c.mock_search = mock_search  # type: ignore[attr-defined]
        yield c


@pytest.fixture
def ui_client(monkeypatch, synthetic_pdf, servable_representation_gate):
    yield from _client(monkeypatch, ui_enabled=True, synthetic_pdf=synthetic_pdf)


@pytest.fixture
def ui_disabled_client(monkeypatch, synthetic_pdf, servable_representation_gate):
    yield from _client(monkeypatch, ui_enabled=False, synthetic_pdf=synthetic_pdf)


def test_ui_disabled_blocks_stream_and_history_with_zero_model_calls(
    monkeypatch, synthetic_pdf, servable_representation_gate
):
    """Issue #479: with UI disabled, every console entry point — including
    stream and history-carrying form posts — serves the unavailable contract
    over actual HTTP and initiates zero model/retrieval calls."""
    llm = UiFakeLLM()
    gen = _client(monkeypatch, ui_enabled=False, synthetic_pdf=synthetic_pdf, llm=llm)
    client = next(gen)
    try:
        history = json.dumps([{"role": "user", "content": "earlier"}])
        for method, path, kwargs in (
            ("get", "/ui", {}),
            ("get", "/ui/healthz", {}),
            ("get", "/ui/static/js/console.js", {}),
            ("post", "/ui/chat", {"data": {"message": "hi", "messages": history}}),
            ("post", "/ui/chat/stream", {"json": {"messages": [{"role": "user", "content": "hi"}]}}),
        ):
            resp = getattr(client, method)(path, **kwargs)
            assert resp.status_code == 404, path
            assert resp.json() == {"code": "not_found", "message": "not found"}, path
    finally:
        with contextlib.suppress(StopIteration):
            next(gen)
    assert llm.calls == []
    assert llm.stream_calls == []
    assert client.mock_search.calls == []


def test_ui_shell_has_strict_csp_and_no_remote_assets(ui_client):
    resp = ui_client.get("/ui")
    assert resp.status_code == 200
    csp = resp.headers["content-security-policy"]
    for directive in (
        "default-src 'self'",
        "script-src 'self'",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ):
        assert directive in csp
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    body = resp.text
    assert "/ui/static/vendor/htmx.min.js" in body
    assert "/ui/static/js/console.js" in body
    assert "/ui/static/css/console.css" in body
    assert "http://" not in body and "https://" not in body
    assert "fonts.googleapis" not in body


@pytest.mark.parametrize("htmx", [False, True], ids=["full-page", "htmx-fragment"])
def test_ui_chat_form_and_htmx_render_the_answer(ui_client, htmx):
    """The no-JS form post renders the answer, citations and history field; an
    HTMX post returns just the fragment with the out-of-band history update."""
    resp = ui_client.post(
        "/ui/chat",
        data={"message": "What is IEA500I?", "messages": ""},
        headers={"HX-Request": "true"} if htmx else {},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "What is IEA500I?" in body
    assert "Reissue the command after initialization." in body
    assert "SA22-0000-00 Synthetic Reference" in body
    if htmx:
        assert "<html" not in body
        assert 'hx-swap-oob="true"' in body and 'id="history-json"' in body
    else:
        assert 'name="messages"' in body


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        "[" * 100_000,
        '[{"role":"user","content":' + "9" * 6000 + "}]",
        '[{"role":"user","content":"ok","x":' + "[" * 50_000 + "}]",
    ],
    ids=["malformed", "deep-nesting", "huge-int", "nested-extra"],
)
def test_ui_chat_hostile_history_degrades_to_empty_history(ui_client, raw):
    """Issue #1032: client-owned history is untrusted; malformed JSON, recursion
    and int-limit failures behave like no history, never a 500."""
    resp = ui_client.post("/ui/chat", data={"message": "What is IEA500I?", "messages": raw})
    assert resp.status_code == 200
    assert "Reissue the command after initialization." in resp.text


def test_ui_chat_stream_final_matches_answer_stream_final(ui_client):
    """The console stream and the /v1/answer stream share one final-frame
    mapping: for the same retrieved hits and model output they stream with the
    same headers and their `final` payloads are identical apart from the request id."""
    question = "What is IEA500I?"
    console = ui_client.post(
        "/ui/chat/stream", json={"messages": [{"role": "user", "content": question}]}
    )
    answer = ui_client.post("/v1/answer", json={"query": question, "stream": True})
    assert console.status_code == answer.status_code == 200
    assert "text/event-stream" in console.headers["content-type"]
    assert console.headers["x-accel-buffering"] == "no"
    finals = []
    for resp in (console, answer):
        events = _parse_sse_events(resp.text)
        assert [name for name, _ in events].count("final") == 1
        final = next(payload for name, payload in events if name == "final")
        finals.append({k: v for k, v in final.items() if k != "request_id"})
    console_final, answer_final = finals
    assert console_final == answer_final
    assert "Reissue the command" in console_final["answer"]
    assert console_final["hits"] and console_final["citations"] == [_hit().cite]
    assert console_final["hits"][0]["doc_id"] == "SA22-0000-00"
    assert console_final["verification_state"] == "accepted"
    assert console_final["script_review_required"] is False
    assert console_final["supplied_evidence"]


def test_ui_chat_stream_error_emits_error_event_without_final(
    monkeypatch, synthetic_pdf, servable_representation_gate
):
    client = next(_client(monkeypatch, ui_enabled=True, synthetic_pdf=synthetic_pdf, llm=ExplodingStreamLLM()))
    resp = client.post(
        "/ui/chat/stream",
        json={"messages": [{"role": "user", "content": "What is IEA500I?"}]},
    )
    assert resp.status_code == 200
    assert "event: error" in resp.text
    assert "event: final" not in resp.text
    assert "stream exploded" not in resp.text


def test_ui_chat_stream_parser_failure_carries_incomplete_state(
    monkeypatch, synthetic_pdf, servable_representation_gate
):
    """Issue #365: a real client whose upstream stream fails after content
    (error frame then [DONE]) ends the console stream with the fixed error
    event and the incomplete state; the upstream error text never reaches
    the browser."""
    from mainframe_rag.agent.answer import HttpxLLMClient
    from mainframe_rag.config import Settings
    from tests.fakes import HttpxStreamFake, settings_kw

    llm = HttpxLLMClient(
        Settings(**settings_kw(llm_base_url="http://llm.internal/v1", llm_stream=True)),
        client=HttpxStreamFake(
            lines=[
                'data: {"choices": [{"delta": {"content": "partial "}}]}',
                'data: {"error": {"message": "SECRET-UPSTREAM-TEXT"}}',
                "data: [DONE]",
            ]
        ),
    )
    client = next(
        _client(monkeypatch, ui_enabled=True, synthetic_pdf=synthetic_pdf, llm=llm)
    )
    resp = client.post(
        "/ui/chat/stream",
        json={"messages": [{"role": "user", "content": "What is IEA500I?"}]},
    )
    assert resp.status_code == 200
    events = _parse_sse_events(resp.text)
    names = [name for name, _ in events]
    assert "token" in names
    assert names[-1] == "error"
    assert "final" not in names
    error = events[-1][1]
    assert error["verification_state"] == "generation_incomplete"
    assert "SECRET-UPSTREAM-TEXT" not in resp.text


def test_ui_chat_retrieval_failure_renders_fixed_banner(ui_client, monkeypatch):
    """Full page and HTMX paths both surface the fixed banner (the HTMX path
    must not silently swap only the user turn); upstream text never leaks."""

    def boom(*_a, **_k):
        raise RuntimeError("qdrant exploded: internal detail")

    monkeypatch.setattr(app_mod, "retrieve_search", boom)
    resp = ui_client.post("/ui/chat", data={"message": "What is IEA500I?", "messages": ""})
    assert resp.status_code == 502
    assert "The reasoning agent could not complete this request." in resp.text
    assert "qdrant exploded" not in resp.text
    resp = ui_client.post(
        "/ui/chat",
        data={"message": "What is IEA500I?", "messages": ""},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "error-banner" in resp.text
    assert "The reasoning agent could not complete this request." in resp.text
    assert "qdrant exploded" not in resp.text
    assert "<html" not in resp.text


def test_ui_chat_nonstream_timeout_is_model_timeout(monkeypatch, synthetic_pdf, servable_representation_gate):
    """#588: the non-stream console labels a model wait timeout `model_timeout`
    (as the stream path does), still behind the fixed banner."""
    import httpx2 as httpx

    recorded = []
    real = app_mod._record_endpoint

    def spy(request, endpoint, outcome, *a, **k):
        recorded.append((endpoint, outcome))
        return real(request, endpoint, outcome, *a, **k)

    class TimingOutLLM:
        async def chat(self, messages, reasoning_effort=None, temperature=None):
            raise httpx.ReadTimeout("read timed out: internal detail")

    ui_client = next(_client(monkeypatch, ui_enabled=True, synthetic_pdf=synthetic_pdf, llm=TimingOutLLM()))
    monkeypatch.setattr(app_mod, "_record_endpoint", spy)
    resp = ui_client.post("/ui/chat", data={"message": "What is IEA500I?", "messages": ""})
    assert resp.status_code == 502
    assert "The reasoning agent could not complete this request." in resp.text
    assert "internal detail" not in resp.text
    assert recorded == [("console", "model_timeout")]


class _FaultLLM:
    """A model leg that fails before any output: `timeout` like an expired
    wait, `error` like any other upstream fault."""

    def __init__(self, fault):
        self.fault = fault

    def _raise(self):
        import httpx2 as httpx

        if self.fault == "model_timeout":
            raise httpx.ReadTimeout("read timed out: SECRET-detail")
        raise RuntimeError("model exploded: SECRET-detail")

    async def chat(self, messages, reasoning_effort=None, temperature=None):
        self._raise()

    async def chat_stream(self, messages, reasoning_effort=None, temperature=None):
        self._raise()
        yield {}


# Independent expectations per fault (#985): the RED outcome, whether the
# fault is a server-side failure logged at ERROR, and the status of each
# route. The console form keeps its fixed banner statuses (422 only for the
# operator-correctable budget refusal); its stream matches /v1/chat's.
_FAULTS = {
    "embed_refusal": ("invalid_request", False, {"chat": 422, "chat_stream": 422, "ui_form": 502, "ui_stream": 422}),
    "retrieval": ("upstream_error", True, {"chat": 502, "chat_stream": 502, "ui_form": 502, "ui_stream": 502}),
    "model_timeout": ("model_timeout", False, {"chat": 504, "chat_stream": 200, "ui_form": 502, "ui_stream": 200}),
    "model_error": ("upstream_error", True, {"chat": 502, "chat_stream": 200, "ui_form": 502, "ui_stream": 200}),
    "budget": ("prompt_budget_exceeded", False, {"chat": 422, "chat_stream": 200, "ui_form": 422, "ui_stream": 200}),
}


@pytest.mark.parametrize("route", ["chat", "chat_stream", "ui_form", "ui_stream"])
@pytest.mark.parametrize("fault", list(_FAULTS))
def test_console_classifies_each_fault_like_chat(ui_client, monkeypatch, caplog, fault, route):
    """#985: the console runs the /v1/chat transport path, so one fault has
    one RED outcome and one log level on every chat route; the client sees
    only fixed text."""
    from mainframe_rag.ingest.bounds import EmbedInputTooLarge
    from tests.test_prompt_packing_units import _RouteOvershootTokenizer

    outcome, server_fault, statuses = _FAULTS[fault]
    if fault.startswith("model_"):
        monkeypatch.setattr(app_mod, "llm", _FaultLLM(fault))

    def refuse(*_a, **_k):
        raise EmbedInputTooLarge(100, 150, 1)

    def boom(*_a, **_k):
        raise RuntimeError("qdrant exploded: SECRET-detail")

    if fault == "embed_refusal":
        monkeypatch.setattr(app_mod, "retrieve_search", refuse)
    elif fault == "retrieval":
        monkeypatch.setattr(app_mod, "retrieve_search", boom)
    elif fault == "budget":
        monkeypatch.setattr(app_mod, "tokenizer", _RouteOvershootTokenizer())
    recorded = []
    real = app_mod.record_request

    def spy(endpoint, code, *a, **k):
        recorded.append((endpoint, code))
        return real(endpoint, code, *a, **k)

    monkeypatch.setattr(app_mod, "record_request", spy)
    messages = [{"role": "user", "content": "What is IEA500I?"}]
    caplog.clear()
    with caplog.at_level(logging.INFO):
        if route == "ui_form":
            resp = ui_client.post("/ui/chat", data={"message": "What is IEA500I?", "messages": ""})
        elif route == "ui_stream":
            resp = ui_client.post("/ui/chat/stream", json={"messages": messages})
        else:
            resp = ui_client.post("/v1/chat", json={"messages": messages, "stream": route == "chat_stream"})

    endpoint = "console" if route.startswith("ui_") else "chat"
    assert resp.status_code == statuses[route]
    assert recorded == [(endpoint, outcome)]
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert bool(errors) is server_fault, [r.getMessage() for r in errors]
    assert "SECRET" not in resp.text
    if route == "ui_stream" and resp.status_code == 200:
        names = [name for name, _ in _parse_sse_events(resp.text)]
        assert names.count("error") == 1 and "final" not in names


def test_ui_chat_body_cap_counts_splunk_context(ui_client, monkeypatch):
    """Review fix: the UI cap must match /v1/chat and include the attached
    context, so an oversized drawer is rejected the same way."""
    monkeypatch.setattr(app_mod.settings, "chat_max_body_chars", 40)
    payload = {"message": "hi", "messages": "", "splunk_context": "x" * 60}
    resp = ui_client.post("/ui/chat", data=payload)
    assert resp.status_code == 502
    assert "The reasoning agent could not complete this request." in resp.text

    resp = ui_client.post(
        "/ui/chat/stream",
        json={"messages": [{"role": "user", "content": "hi"}], "splunk_context": "x" * 60},
    )
    assert resp.status_code == 422
    assert resp.json() == {"code": "invalid_request", "message": "request body failed validation"}


def test_ui_healthz_badge_reflects_agent_status(ui_client, monkeypatch):
    async def ok_health():
        return app_mod.HealthzResponse(status="ok", qdrant=True, embed=True), 200

    monkeypatch.setattr(app_mod, "evaluate_healthz", ok_health)
    resp = ui_client.get("/ui/healthz")
    assert resp.status_code == 200
    assert "Online" in resp.text

    async def down_health():
        raise app_mod.AppError(503, "qdrant_unready", "qdrant is not ready")

    monkeypatch.setattr(app_mod, "evaluate_healthz", down_health)
    resp = ui_client.get("/ui/healthz")
    assert resp.status_code == 200
    assert "Offline" in resp.text


def test_ui_chat_refused_when_generation_not_servable(ui_client, monkeypatch):
    """Issue #391 F3: the console is gated like the API — a non-servable
    generation never reaches retrieval or the LLM. The HTML form keeps its
    fixed error-banner contract; the SSE endpoint refuses with the stable
    503 JSON before the stream opens."""
    from tests.fakes import ServingGateFake

    monkeypatch.setattr(app_mod, "serving_gate", ServingGateFake(outcome="pending"))
    resp = ui_client.post("/ui/chat", data={"message": "hello"})
    assert resp.status_code == 502
    assert "could not complete this request" in resp.text
    assert ui_client.mock_search.calls == []
    assert app_mod.llm is not None

    resp = ui_client.post(
        "/ui/chat/stream",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {
        "code": "representation_unavailable",
        "message": "the retrieval generation is not available",
    }


def test_ui_static_serves_pinned_assets_and_blocks_traversal(ui_client):
    resp = ui_client.get("/ui/static/vendor/htmx.min.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    resp = ui_client.get("/ui/static/css/console.css")
    assert resp.status_code == 200

    resp = ui_client.get("/ui/static/%2e%2e/%2e%2e/pyproject.toml")
    assert resp.status_code == 404
    assert "[project]" not in resp.text


def test_vendor_assets_match_pinned_sha256sums():
    sums = (_VENDOR_DIR / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert sums, "vendored assets must ship a checksum manifest"
    for line in sums:
        digest, name = line.split(maxsplit=1)
        payload = (_VENDOR_DIR / name.strip()).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == digest, name


def test_vendored_fonts_are_local_pinned_and_served(ui_client):
    """Console fonts (ADR-0004 §4 amendment): every face fonts.css declares is
    a vendored, checksum-pinned file served from /ui/static as font/woff2,
    the stylesheet names no remote host, and the shell links it ahead of
    console.css so the face is declared before any rule uses it."""
    import re

    css_path = _VENDOR_DIR.parent / "css" / "fonts.css"
    css = css_path.read_text(encoding="utf-8")
    assert "http" not in css and "//" not in css.replace("/*", "").replace("*/", "")
    urls = re.findall(r'url\("\.\./vendor/([^"]+)"\)', css)
    assert len(urls) == css.count("@font-face") == 6
    pinned = {line.split(maxsplit=1)[1].strip() for line in (_VENDOR_DIR / "SHA256SUMS").read_text().splitlines()}
    assert "LICENSE.ibm-plex" in pinned
    for name in urls:
        assert name in pinned, name
        resp = ui_client.get(f"/ui/static/vendor/{name}")
        assert resp.status_code == 200, name
        assert resp.headers["content-type"] == "font/woff2", name
        assert resp.content[:4] == b"wOF2", name
    shell = ui_client.get("/ui").text
    assert shell.index("/ui/static/css/fonts.css") < shell.index("/ui/static/css/console.css")


# ---------------------------------------------------------------- markdown (P1)

# One row per equivalence class of the served markdown vocabulary:
# (id, source, must-contain, must-not-contain, exact). `exact` is the whole
# render where the document shape is the contract; the other rows assert every
# fragment the renderer must emit and every one it must never emit.
# Streamed/reloaded parity with this renderer is owned cross-layer by
# tests/console_browser_contracts.py::test_pipe_tables_render_identically_to_the_server_subset.
_COPY = '<pre><button class="copy-btn" type="button">Copy</button>'
_MD_ROWS: list[tuple[str, str, tuple[str, ...], tuple[str, ...], str | None]] = [
    ("h3", "### Program Function", ("<h3>Program Function</h3>",), ("###",), None),
    ("h2", "## Tail", ("<h2>Tail</h2>",), ("## ",), None),
    ("strong", "Use **IKJEFT01** now", ("<strong>IKJEFT01</strong>",), ("**",), None),
    ("em", "a *b* c", ("<em>b</em>",), (), None),
    ("code-span", "Run `IKJEFT01` here", ("<code>IKJEFT01</code>",), ("`IKJEFT01`",), None),
    ("ul", "* one\n* two", ("<ul>", "<li>one</li>", "<li>two</li>", "</ul>"), (), None),
    ("li-strong", "- **x:** y", ("<li><strong>x:</strong> y</li>",), (), None),
    ("ol", "1. first\n2. second", ("<ol>", "<li>first</li>", "</ol>"), (), None),
    # Lazy continuation: a plain line under an item stays in that item, never a paragraph ahead of the list (escaped console defect).
    ("lazy-continuation", "1. **A:**\nbody a\n2. **B:**\n  body b",
     ("<li><strong>A:</strong> body a</li>", "<li><strong>B:</strong> body b</li>"), ("<p>",), None),
    ("list-then-paragraph", "- one\n\nafter", ("<li>one</li>\n</ul>\n<p>after</p>",), (), None),
    # Unclosed fence still renders (streaming midpoint), never leaks raw; a code span protects markup-like content.
    ("jcl-fence", "```jcl\n//STEP1 EXEC PGM=IEFBR14\n```",
     (_COPY + '<code class="language-jcl">//STEP1 <span class="tok-keyword">EXEC</span>'
      ' <span class="tok-keyword">PGM</span>=IEFBR14</code></pre>',), ("```",), None),
    ("unclosed-fence", "```\ncode here", (_COPY + "<code>code here</code></pre>",), ("```",), None),
    ("code-span-protects-markup", "`**not bold**`", ("<code>**not bold**</code>",), ("<strong>",), None),
    # Mainframe noise stays literal.
    ("mainframe-noise", "C# and a#b and 2*3", ("C# and a#b and 2*3",), ("<em>", "<strong>"), None),
    # Issue #749: pipe tables render as a real table, alignment and inline cells intact, never swallowing the prose after.
    ("gfm-table",
     ("The HSM category is subdivided into:\n\n| Sub-category | Description |\n|---|:---:|\n"
      "| **HSM SFF** | Named `hlq.HMIGTAPE.DATASET`. |\n| **HSM GEN** | Generated data sets, such as *VTOCDUMPs*. |\n"
      "\nAfter the table."), (), (),
     ('<p>The HSM category is subdivided into:</p>\n<div class="md-table"><table><thead><tr><th>Sub-category</th>'
      '<th class="align-center">Description</th></tr></thead><tbody><tr><td><strong>HSM SFF</strong></td>'
      '<td class="align-center">Named <code>hlq.HMIGTAPE.DATASET</code>.</td></tr><tr><td><strong>HSM GEN</strong></td>'
      '<td class="align-center">Generated data sets, such as <em>VTOCDUMPs</em>.</td></tr></tbody></table></div>\n'
      "<p>After the table.</p>")),
    # Ragged rows, an escaped pipe and no leading pipes; cells beyond the delimiter count are dropped, not row-wrapped.
    ("table-ragged-and-aligned",
     ("Code | Meaning | Action\n---|---:|:---\n04 | short \\| row\n"
      "| 08 | error | retry | extra |\nNot a row"), (), ("extra",),
     ('<div class="md-table"><table><thead><tr><th>Code</th><th class="align-right">Meaning</th>'
      '<th class="align-left">Action</th></tr></thead><tbody><tr><td>04</td>'
      '<td class="align-right">short | row</td><td class="align-left"></td></tr><tr><td>08</td>'
      '<td class="align-right">error</td><td class="align-left">retry</td></tr></tbody></table></div>\n'
      "<p>Not a row</p>")),
    # No delimiter row, a second row that is not a delimiter, a delimiter width mismatch, fewer than three dashes: all inert.
    ("table-no-delimiter", "| a | b |", (), ("<table",), "<p>| a | b |</p>"),
    ("table-second-row", "| a | b |\n| c | d |", (), ("<table",), "<p>| a | b | | c | d |</p>"),
    ("table-width-mismatch", "| a | b |\n|---|", (), ("<table",), "<p>| a | b | |---|</p>"),
    ("table-short-dashes", "| a | b |\n|--|--|", (), ("<table",), "<p>| a | b | |--|--|</p>"),
    # Spool dumps and retrieved chunks are untrusted: escaped text, our tags only, no links anywhere, also in table cells.
    ("hostile-inline", "<script>alert(1)</script>\n<img src=x onerror=alert(2)>\n[click](javascript:alert(3))\nAT&T stays & fine",
     ("&lt;script&gt;alert(1)&lt;/script&gt;", "AT&amp;T stays &amp; fine"), ("<script>", "<img", "<a ", "<a>"), None),
    ("hostile-table", "| <script>alert(1)</script> | [x](javascript:alert(2)) |\n|---|---|\n| <img src=x onerror=alert(3)> | AT&T |",
     ("<table>", "&lt;script&gt;alert(1)&lt;/script&gt;", "AT&amp;T"), ("<script>", "<img", "<a "), None),
    # Issue #337: JCL comment, string, keyword and number spans.
    ("jcl-highlight",
     ("```jcl\n//* Clean up old datasets\n//STEP1   EXEC PGM=IEFBR14\n"
      "//DD1     DD   DSN='SYS1.PARMLIB',DISP=SHR,SPACE=(CYL,(10,5))\n```"),
     ('<code class="language-jcl">', '<span class="tok-comment">//* Clean up old datasets</span>',
      '<span class="tok-keyword">EXEC</span>', '<span class="tok-keyword">PGM</span>', '<span class="tok-keyword">DD</span>',
      '<span class="tok-keyword">DSN</span>', '<span class="tok-string">\'SYS1.PARMLIB\'</span>',
      '<span class="tok-keyword">DISP</span>', '<span class="tok-keyword">SHR</span>', '<span class="tok-keyword">SPACE</span>',
      '<span class="tok-keyword">CYL</span>', '<span class="tok-number">10</span>', '<span class="tok-number">5</span>'), (), None),
    # Issue #337: an unlabeled fence is sniffed (detect_code_region); plain prose stays unclassified.
    ("sniff-jcl", "```\n//STEP1 EXEC PGM=IEFBR14\n//DD1 DD DSN='A.B',DISP=SHR\n```",
     ('<code class="language-jcl">', '<span class="tok-keyword">EXEC</span>'), (), None),
    ("sniff-rexx", "```\n/* REXX */\nsay 'hi'\n```",
     ('<code class="language-rexx">', '<span class="tok-comment">/* REXX */</span>'), (), None),
    ("sniff-console", "```\n   IKJ56228I DATA SET NOT FOUND\n   READY\n```", ('<code class="language-console">',), ("tok-",), None),
    ("sniff-prose", "```\nThis is just some plain text without any code markers.\n```", ("<code",), ("language-", "tok-"), None),
    # Issue #337: hostile markup, unclosed or empty fences and an unknown language stay inert and classed as given.
    ("fence-hostile-markup", "```jcl\n//STEP1 EXEC PGM=TEST,PARM='<script>alert(1)</script>'\n//DD1 DD <img src=x onerror=alert(2)>\n```",
     ("&lt;script&gt;alert(1)&lt;/script&gt;", "&lt;img src=x onerror=alert("), ("<script>", "<img"), None),
    ("fence-unclosed-at-eof", "```jcl\n//STEP1 EXEC PGM=IEFBR14",
     ('<code class="language-jcl">', '<span class="tok-keyword">EXEC</span>'), ("```",), None),
    ("fence-empty-labeled", "```jcl\n```", ('<code class="language-jcl"></code>',), (), None),
    ("fence-empty-unlabeled", "```\n```", ("<code></code>",), (), None),
    ("fence-unknown-language", "```unknownlang\nsome arbitrary text\n```",
     ('<code class="language-unknownlang">some arbitrary text</code>',), ("tok-",), None),
]


@pytest.mark.parametrize("case, source, present, absent, exact", _MD_ROWS, ids=[row[0] for row in _MD_ROWS])
def test_markdown_subset_renders_the_served_vocabulary(case, source, present, absent, exact):
    from mainframe_rag.webui.routes import render_markdown_subset

    rendered = render_markdown_subset(source)
    if exact is not None:
        assert rendered == exact, case
    for needle in present:
        assert needle in rendered, (case, needle)
    for needle in absent:
        assert needle not in rendered, (case, needle)


class MarkdownFakeLLM:
    def chat(self, messages, reasoning_effort=None, temperature=None):
        return ChatResult(
            content=(
                "Both `IKJEFT01` and `IKJEFT1B` are TMPs.\n\n"
                "### Program Function\n\n"
                "* **IKJEFT01:** standard TMP\n"
                "* plain item\n\n"
                "```jcl\n//STEP1 EXEC PGM=IEFBR14\n```\n\n"
                "Citations:\n"
                "- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n"
            ),
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    async def chat_stream(self, messages, reasoning_effort=None, temperature=None) -> AsyncIterator[dict]:
        yield {
            "type": "token",
            "delta": (
                "Both `IKJEFT01` and `IKJEFT1B` are TMPs.\n\n"
                "```jcl\n//STEP1 EXEC PGM=IEFBR14\n```\n\n"
                "Citations:\n- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n"
            ),
            "ttft_ms": 5,
        }
        yield {
            "type": "done",
            "finish_reason": "stop",
            "usage": TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            "ttft_ms": 5,
        }


def _assistant_html(body: str) -> str:
    """The rendered assistant turn only. Raw markdown legitimately survives
    elsewhere (the hidden history input carries source text for the LLM),
    so leak assertions must scope to this block. The subset emits no divs,
    so the first close tag ends it."""
    start = body.index('<div class="turn-content md">')
    return body[start : body.index("</div>", start)]


def test_ui_chat_fragment_renders_markdown_not_markers(ui_client, monkeypatch):
    """HTMX path: assistant markdown arrives as HTML (no ### / * leaks),
    user turns stay plain, citations keep their structured block."""
    monkeypatch.setattr(app_mod, "llm", MarkdownFakeLLM())
    resp = ui_client.post(
        "/ui/chat",
        data={"message": "Diff <b>bold</b>?", "messages": ""},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    body = resp.text
    assistant = _assistant_html(body)
    assert "<h3>Program Function</h3>" in assistant
    assert "<li><strong>IKJEFT01:</strong> standard TMP</li>" in assistant
    assert "<code>IKJEFT01</code>" in assistant
    assert "###" not in assistant
    # The operator's own markup renders inert inside a plain <pre>.
    assert '<pre class="turn-content">Diff &lt;b&gt;bold&lt;/b&gt;?</pre>' in body
    assert "Source-matched manual citations" in body


def test_ui_invalid_reasoning_effort_rejected(ui_client):
    """An unknown reasoning effort is a 422 on both the stream and form paths."""
    resp = ui_client.post(
        "/ui/chat/stream",
        json={
            "messages": [{"role": "user", "content": "What is IEA500I?"}],
            "reasoning_effort": "ultra",
        },
    )
    assert resp.status_code == 422
    resp = ui_client.post(
        "/ui/chat",
        data={"message": "What is IEA500I?", "reasoning_effort": "ultra"},
    )
    assert resp.status_code == 422


def test_console_offers_only_served_efforts_and_refuses_others_before_the_model(ui_client, monkeypatch):
    """#748: with Qwen3.8's template domain the console offers no High, refuses
    it with a fixed client error before any model call, and xhigh is sent
    verbatim; an omitted effort still takes the server default."""
    from mainframe_rag.webui import routes

    fake_llm = UiFakeLLM()
    monkeypatch.setattr(app_mod, "llm", fake_llm)
    monkeypatch.setattr(app_mod.settings, "llm_reasoning_efforts", "low,medium,xhigh")
    # A non-default simple effort, so "omitted takes the server default" is
    # distinguishable from the explicit xhigh request below (#777).
    monkeypatch.setattr(app_mod.settings, "llm_reasoning_effort_simple", "low")
    monkeypatch.setattr(app_mod.settings, "llm_reasoning_effort_complex", "medium")

    body = ui_client.get("/ui").text
    assert re.findall(r'data-effort="([a-z]+)"', body) == ["low", "medium", "xhigh"]
    assert '<button type="button" class="seg" data-effort="xhigh" aria-pressed="false">Extra high</button>' in body
    assert body.count('aria-pressed="true"') == 1

    page = ui_client.post("/ui/chat", data={"message": "What is IEA500I?", "reasoning_effort": "high"})
    assert page.status_code == 422
    assert html.escape(routes._EFFORT_ERROR_TEXT, quote=False) in page.text
    assert routes._ERROR_TEXT not in page.text  # not presented as a model outage
    assert 'data-effort="low" aria-pressed="true"' in page.text  # unoffered value never stays selected
    fragment = ui_client.post("/ui/chat", data={"message": "What is IEA500I?", "reasoning_effort": "high"},
                              headers={"HX-Request": "true"})
    assert html.escape(routes._EFFORT_ERROR_TEXT, quote=False) in fragment.text
    stream = ui_client.post("/ui/chat/stream", json={
        "messages": [{"role": "user", "content": "What is IEA500I?"}], "reasoning_effort": "high"})
    assert stream.status_code == 422
    assert stream.json() == {"code": "unsupported_reasoning_effort",
                             "message": "the reasoning effort is not offered by this deployment"}
    assert fake_llm.calls == [] and fake_llm.stream_calls == []

    accepted = ui_client.post("/ui/chat/stream", json={
        "messages": [{"role": "user", "content": "What is IEA500I?"}], "reasoning_effort": "xhigh"})
    assert accepted.status_code == 200
    assert fake_llm.stream_calls[-1]["reasoning_effort"] == "xhigh"
    default = ui_client.post("/ui/chat/stream", json={"messages": [{"role": "user", "content": "What is IEA500I?"}]})
    assert default.status_code == 200
    assert fake_llm.stream_calls[-1]["reasoning_effort"] == "low"
    # The no-JS form forwards what it offers verbatim (#777).
    for effort in ("xhigh", "low"):
        form = ui_client.post("/ui/chat", data={"message": "What is IEA500I?", "messages": "",
                                                "reasoning_effort": effort})
        assert form.status_code == 200 and fake_llm.calls[-1]["reasoning_effort"] == effort


# ---------------------------------------------------------------------------
# Verification-state presentation (issue #365)
# ---------------------------------------------------------------------------


class FluentNoCiteUiLLM:
    def chat(self, messages, reasoning_effort=None, temperature=None):
        return ChatResult(
            content="Reissue the command after initialization completes.",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )


class RefusalUiLLM:
    def chat(self, messages, reasoning_effort=None, temperature=None):
        return ChatResult(
            content="The excerpts do not contain this procedure.",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )


def test_ui_fragment_verification_state_presentation(ui_client, monkeypatch):
    """Accepted + script (MarkdownFakeLLM): the source-matched title is
    preserved exactly, nothing claims the citations were verified (#838), no
    draft badge appears, and the script carries its review badge."""
    monkeypatch.setattr(app_mod, "llm", MarkdownFakeLLM())
    body = ui_client.post(
        "/ui/chat",
        data={"message": "What is IKJEFT01?", "messages": ""},
        headers={"HX-Request": "true"},
    ).text
    assert "Source-matched manual citations (1)" in body
    assert "Source-matched &middot; 1 citation" in body
    assert "Verified" not in body
    assert "state-unverified_draft" not in body
    assert "state-insufficient_evidence" not in body
    assert "state-generation_incomplete" not in body
    assert "Human review required" in body

    # Fluent prose with zero eligible citations: draft badge, never "verified".
    monkeypatch.setattr(app_mod, "llm", FluentNoCiteUiLLM())
    body = ui_client.post(
        "/ui/chat",
        data={"message": "What is IEA500I?", "messages": ""},
        headers={"HX-Request": "true"},
    ).text
    assert "Unverified draft" in body
    assert "Source-matched manual citations" not in body
    assert "Verified" not in body
    assert "Inferred citations" not in body

    monkeypatch.setattr(app_mod, "llm", RefusalUiLLM())
    body = ui_client.post(
        "/ui/chat",
        data={"message": "What is IEA500I?", "messages": ""},
        headers={"HX-Request": "true"},
    ).text
    assert "Insufficient evidence" in body
    assert "Source-matched manual citations" not in body
    assert "Verified" not in body


@pytest.mark.parametrize("condense", [False, True])
@pytest.mark.parametrize("tail_role", ["assistant", "system"])
def test_ui_stream_retrieval_and_prompt_use_same_active_user(ui_client, monkeypatch, condense, tail_role):
    monkeypatch.setattr(app_mod.settings, "chat_condense_enabled", condense)
    result = ui_client.post("/ui/chat/stream", json={"messages": [
        {"role": "user", "content": "Earlier question"},
        {"role": "assistant", "content": "Earlier answer"},
        {"role": "user", "content": "  What does IEA500I mean?\n"},
        {"role": tail_role, "content": "LATER_NON_USER S0C4"},
    ]})
    assert result.status_code == 200
    events = _parse_sse_events(result.text)
    assert events[-1][0] == "final"
    assert [c["query"] for c in ui_client.mock_search.calls] == ["What does IEA500I mean?"]
    assert app_mod.llm.calls == []
    assert len(app_mod.llm.stream_calls) == 1
    prompt = app_mod.llm.stream_calls[0]["messages"]
    assert "Question: What does IEA500I mean?\n" in prompt[-1].content
    assert all("LATER_NON_USER" not in m.content for m in prompt)


@pytest.mark.parametrize("messages", [
    pytest.param([{"role": "assistant", "content": "No user question was supplied"}], id="missing-user"),
    pytest.param([{"role": "user", "content": " \t\n "}, {"role": "assistant", "content": "Short tail"}], id="blank"),
    pytest.param([{"role": "user", "content": "x" * 2001}, {"role": "assistant", "content": "Short tail"}],
                 id="over-length"),
    pytest.param([{"role": "user", "content": "\x00x"}], id="nul"),
    pytest.param([{"role": "user", "content": "a\x1bb"}], id="escape"),
])
def test_ui_stream_unsearchable_question_refuses_before_work(ui_client, messages):
    """Issue #579: no user turn, blank, over-length or control-character text is
    a fixed 422 before retrieval or a model call."""
    result = ui_client.post("/ui/chat/stream", json={"messages": messages})
    assert result.status_code == 422
    assert result.json() == {"code": "invalid_request", "message": "request body failed validation"}
    assert ui_client.mock_search.calls == []
    assert app_mod.llm.calls == app_mod.llm.stream_calls == []


@pytest.mark.parametrize("message,htmx", [("   ", False), ("a\x1bb", True)])
def test_ui_form_unsearchable_message_refuses_before_work(ui_client, message, htmx):
    """Issue #579: the console form shows its fixed banner and does no work."""
    result = ui_client.post("/ui/chat", headers={"HX-Request": "true"} if htmx else {}, data={
        "message": message, "messages": "",
    })
    assert "could not complete this request" in result.text
    assert ui_client.mock_search.calls == []
    assert app_mod.llm.calls == app_mod.llm.stream_calls == []


def test_ui_form_normalizes_current_message_with_real_history(ui_client):
    result = ui_client.post("/ui/chat", data={
        "message": " \tWhat does IEA500I mean?\n",
        "messages": json.dumps([
            {"role": "user", "content": "Earlier question"},
            {"role": "assistant", "content": "Earlier answer"},
        ]),
    })
    assert result.status_code == 200
    assert [c["query"] for c in ui_client.mock_search.calls] == ["What does IEA500I mean?"]
    prompt = app_mod.llm.calls[0]["messages"]
    assert prompt[1].content == "Earlier question"
    assert prompt[2].content == "Earlier answer"
    assert "Question: What does IEA500I mean?\n" in prompt[-1].content


# ---------------------------------------------------------------------------
# Retention and accessibility structure (issue #372). Hermetic; the executed
# behavior lives in tests/console_browser_contracts.py (real browser, skips
# without a runtime).
# ---------------------------------------------------------------------------


def test_ui_shell_declares_retention_notice_clear_control_and_status_region(ui_client):
    body = ui_client.get("/ui").text
    assert '<html lang="en">' in body
    assert '<aside class="sidebar" aria-label="Incidents">' in body
    assert 'id="console-status"' in body and 'role="status"' in body
    # The message list is not a live region: per-token re-rendering would flood AT.
    assert 'id="messages" class="messages" role="region"' in body
    assert 'id="messages" class="messages" aria-live' not in body
    assert 'id="clear-history-btn"' in body
    note = body.split('id="retention-note">', 1)[1].split("</p>", 1)[0]
    for phrase in ("this browser only", "30 incidents", "until you clear", "shared workstation"):
        assert phrase in note
    assert 'id="storage-notice" role="status" hidden' in body


def test_console_turn_text_never_reaches_server_logs(ui_client, caplog):
    marker = "ZXQ-SENSITIVE-4417"
    with caplog.at_level("DEBUG"):
        resp = ui_client.post(
            "/ui/chat/stream",
            json={
                "messages": [{"role": "user", "content": f"What is IEA500I? {marker}"}],
                "splunk_context": f"JOB {marker}",
            },
        )
    assert resp.status_code == 200 and "Reissue" in resp.text
    assert marker not in caplog.text
    assert "Reissue" not in caplog.text and "IEA500I BEFORE" not in caplog.text


# ---------------------------------------------------------------------------
# Supplied-excerpt view (issue #635)
# ---------------------------------------------------------------------------


def _hostile_hit():
    return _hit().model_copy(
        update={
            "text": '<script>alert(1)</script><img src="http://x.invalid/a">',
            "version": None,
        }
    )


def test_ui_stream_final_carries_supplied_evidence_projection(ui_client):
    resp = ui_client.post(
        "/ui/chat/stream", json={"messages": [{"role": "user", "content": "IEA500I"}]}
    )
    final = next(p for n, p in _parse_sse_events(resp.text) if n == "final")
    [item] = final["supplied_evidence"]
    assert item["index"] == 1 and item["text"] == _hit().text
    assert item["end_char"] == len(_hit().text)
    assert final["evidence_omitted"] == 0
    assert "chunk_id" not in item


def test_ui_fragment_renders_supplied_excerpt_as_data(ui_client, monkeypatch):
    """The server fragment shows the current response's excerpts with
    product/version/location; hostile excerpt markup is escaped, and an
    unknown version is not replaced by the request's."""
    monkeypatch.setattr(
        app_mod,
        "retrieve_search",
        lambda *a, **k: ([_hostile_hit()], "identifier", {}),
    )
    resp = ui_client.post(
        "/ui/chat",
        data={"message": "IEA500I", "messages": ""},
        headers={"HX-Request": "true"},
    )
    body = resp.text
    assert "<script>alert(1)</script>" not in body
    assert '<img src="http://x.invalid/a">' not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert 'class="evidence" data-evidence="current"' in body
    assert "Source product: z/OS · version: unknown" in body
    assert "Recorded location: Chapter 2 &gt; IEA500I" in body


def test_ui_history_payload_never_carries_excerpt_text(ui_client):
    """Excerpts are current-response-only: the history the full page hands
    back to the browser is role/content only."""
    resp = ui_client.post("/ui/chat", data={"message": "IEA500I", "messages": ""})
    assert _hit().text in resp.text  # shown in the view...
    marker = 'id="history-json" value="'
    start = resp.text.index(marker) + len(marker)
    history = resp.text[start : resp.text.index('"', start)]
    assert _hit().text not in history  # ...but not in the carried history
