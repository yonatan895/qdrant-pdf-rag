"""Operator console tests (ADR-0004): /ui gating, CSP, form and HTMX flows,
SSE streaming, static-asset pin verification, and traversal defense."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mainframe_rag.agent import app as app_mod
from mainframe_rag.agent.tokenizer import FallbackTokenizer
from mainframe_rag.ports import ChatResult, TokenUsage
from mainframe_rag.retrieve.query import SearchHit

_VENDOR_DIR = Path(app_mod.__file__).parents[1] / "webui" / "static" / "vendor"


def _hit() -> SearchHit:
    return SearchHit(
        chunk_id="abc123",
        score=0.42,
        cite="SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6",
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


class MockSearch:
    def __init__(self, return_hits: bool = True):
        self.calls = []

    def search(self, qdrant, embedder, collection, query, product=None, version=None, limit=8, *args, **kwargs):
        self.calls.append({"query": query})
        return [_hit()], "identifier", {"embed_ms": 1, "qdrant_ms": 2}


class UiFakeLLM:
    def chat(self, messages, reasoning_effort=None, temperature=None):
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
def ui_client(monkeypatch, synthetic_pdf):
    yield from _client(monkeypatch, ui_enabled=True, synthetic_pdf=synthetic_pdf)


@pytest.fixture
def ui_disabled_client(monkeypatch, synthetic_pdf):
    yield from _client(monkeypatch, ui_enabled=False, synthetic_pdf=synthetic_pdf)


def _parse_sse_events(text: str) -> list[tuple[str, dict]]:
    events = []
    for frame in text.split("\n\n"):
        name = None
        data = None
        for line in frame.split("\n"):
            if line.startswith("event: "):
                name = line[7:].strip()
            elif line.startswith("data: "):
                data = line[6:]
        if name and data:
            events.append((name, json.loads(data)))
    return events


def test_ui_disabled_serves_404_envelope_on_every_path(ui_disabled_client):
    for method, path, kwargs in (
        ("get", "/ui", {}),
        ("get", "/ui/static/css/console.css", {}),
        ("get", "/ui/healthz", {}),
        ("post", "/ui/chat", {"data": {"message": "hi"}}),
    ):
        resp = getattr(ui_disabled_client, method)(path, **kwargs)
        assert resp.status_code == 404, path
        assert resp.json() == {"code": "not_found", "message": "not found"}, path


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


def test_ui_chat_form_flow_renders_full_page_with_answer(ui_client):
    resp = ui_client.post("/ui/chat", data={"message": "What is IEA500I?", "messages": ""})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "What is IEA500I?" in body
    assert "Reissue the command after initialization." in body
    assert "SA22-0000-00 Synthetic Reference" in body
    assert 'name="messages"' in body


def test_ui_chat_htmx_returns_fragment_with_oob_history(ui_client):
    resp = ui_client.post(
        "/ui/chat",
        data={"message": "What is IEA500I?", "messages": ""},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "<html" not in body
    assert 'hx-swap-oob="true"' in body
    assert 'id="history-json"' in body
    assert "Reissue the command after initialization." in body


def test_ui_chat_bad_history_json_is_ignored(ui_client):
    resp = ui_client.post("/ui/chat", data={"message": "What is IEA500I?", "messages": "{not json"})
    assert resp.status_code == 200
    assert "Reissue the command after initialization." in resp.text


def test_ui_chat_stream_sse_token_final_contract(ui_client):
    resp = ui_client.post(
        "/ui/chat/stream",
        json={"messages": [{"role": "user", "content": "What is IEA500I?"}]},
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert resp.headers["x-accel-buffering"] == "no"
    events = _parse_sse_events(resp.text)
    names = [name for name, _ in events]
    assert names.count("final") == 1
    assert "token" in names
    final = next(payload for name, payload in events if name == "final")
    assert "Reissue the command" in final["answer"]
    assert final["citations"] == [_hit().cite]
    assert final["hits"][0]["doc_id"] == "SA22-0000-00"


def test_ui_chat_stream_error_emits_error_event_without_final(monkeypatch, synthetic_pdf):
    client = next(_client(monkeypatch, ui_enabled=True, synthetic_pdf=synthetic_pdf, llm=ExplodingStreamLLM()))
    resp = client.post(
        "/ui/chat/stream",
        json={"messages": [{"role": "user", "content": "What is IEA500I?"}]},
    )
    assert resp.status_code == 200
    assert "event: error" in resp.text
    assert "event: final" not in resp.text
    assert "stream exploded" not in resp.text


def test_ui_chat_retrieval_failure_renders_fixed_banner(ui_client, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("qdrant exploded: internal detail")

    monkeypatch.setattr(app_mod, "retrieve_search", boom)
    resp = ui_client.post("/ui/chat", data={"message": "What is IEA500I?", "messages": ""})
    assert resp.status_code == 502
    assert "The reasoning agent could not complete this request." in resp.text
    assert "qdrant exploded" not in resp.text


def test_ui_chat_htmx_retrieval_failure_renders_error_fragment(ui_client, monkeypatch):
    """Review fix: the HTMX path must surface the failure, not silently swap
    only the user turn (the pre-fix behavior)."""

    def boom(*_a, **_k):
        raise RuntimeError("qdrant exploded: internal detail")

    monkeypatch.setattr(app_mod, "retrieve_search", boom)
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
        return app_mod.HealthzResponse(status="ok", qdrant=True, embed=True)

    monkeypatch.setattr(app_mod, "healthz", ok_health)
    resp = ui_client.get("/ui/healthz")
    assert resp.status_code == 200
    assert "Online" in resp.text

    async def down_health():
        raise app_mod.AppError(503, "qdrant_unready", "qdrant is not ready")

    monkeypatch.setattr(app_mod, "healthz", down_health)
    resp = ui_client.get("/ui/healthz")
    assert resp.status_code == 200
    assert "Offline" in resp.text


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


def test_dead_sse_extension_stays_removed(ui_client):
    """Issue #326 P0: streaming uses native fetch — the unreferenced sse.js
    must not come back as dead weight (file, manifest, or shell reference)."""
    assert not (_VENDOR_DIR / "sse.js").exists()
    manifest = (_VENDOR_DIR / "SHA256SUMS").read_text(encoding="utf-8")
    assert "sse.js" not in manifest
    resp = ui_client.get("/ui/static/vendor/sse.js")
    assert resp.status_code == 404
    shell = ui_client.get("/ui").text
    assert "sse.js" not in shell


def _css_rule(css: str, selector: str) -> str:
    """Declaration block for an exact selector. Pins assert rule + property
    together so a bare property passing from an unrelated rule cannot fool
    them (review on #333). Prefix the selector with "\\n" to anchor a
    top-level rule and skip indented overrides inside @media blocks."""

    start = css.index("{", css.index(selector))
    return css[start : css.index("}", start)]


def test_console_css_layout_survival_rules():
    """Issue #326 P0: narrow viewports clipped the topbar and SEND button
    because flex/grid children default to min-width: auto. Pin the
    shrinkability rules so the fix cannot silently regress."""
    css = (
        Path(app_mod.__file__).parents[1] / "webui" / "static" / "css" / "console.css"
    ).read_text(encoding="utf-8")
    assert "min-width: 0;" in _css_rule(css, ".layout > *")
    assert "flex-wrap: wrap;" in _css_rule(css, ".topbar-controls")
    assert "min-width: 0;" in _css_rule(css, ".composer textarea")
    # Sidebar owns its scroll instead of riding the page (follow-up: the
    # tool section vanished off-screen on long conversations).
    assert "position: sticky;" in _css_rule(css, ".sidebar {")
    assert "overflow-y: auto;" in _css_rule(css, ".sidebar {")


def test_console_css_theme_polish():
    """Issue #326 P4: readability invariants — visible focus, glow scoped
    off body copy, primary Send, reduced-motion respect."""
    css = (
        Path(app_mod.__file__).parents[1] / "webui" / "static" / "css" / "console.css"
    ).read_text(encoding="utf-8")
    assert ":focus-visible" in css
    assert ".topbar h1 { text-shadow: var(--glow); }" in css
    # Exactly one text-shadow in the sheet: the header glow. Body copy in
    # the 3270 theme stays crisp (dark theme sets --glow: none anyway).
    assert css.count("text-shadow") == 1
    assert "#send-btn:not(.stop)" in css
    assert "prefers-reduced-motion" in css
    assert "::selection" in css


def test_console_css_chrome_pass():
    """Issue #332: sidebar tool section, circular glyph send, pill composer
    with styled placeholder."""
    css = (
        Path(app_mod.__file__).parents[1] / "webui" / "static" / "css" / "console.css"
    ).read_text(encoding="utf-8")
    assert ".sidebar-tools {" in css
    assert "border-radius: 50%" in _css_rule(css, "\n#send-btn {")
    assert ".composer textarea::placeholder" in css
    assert ".composer textarea:focus" in css


def test_console_css_beauty_pass():
    """Issue #334: send previews green / stop previews red (hover and focus
    agree), and code leaves Courier behind for a modern system mono stack
    with per-theme chip tokens."""
    css = (
        Path(app_mod.__file__).parents[1] / "webui" / "static" / "css" / "console.css"
    ).read_text(encoding="utf-8")
    assert "ui-monospace" in _css_rule(css, ":root")
    assert "--send-hover:" in _css_rule(css, "body.theme-dark {")
    assert "--stop-hover:" in _css_rule(css, "body.theme-dark {")
    assert "--send-hover:" in _css_rule(css, "body.theme-3270 {")
    assert "--stop-hover:" in _css_rule(css, "body.theme-3270 {")
    assert "var(--send-hover)" in _css_rule(css, "#send-btn:not(.stop):hover")
    assert "var(--send-hover)" in _css_rule(css, "#send-btn:not(.stop):focus-visible")
    assert "var(--stop-hover)" in _css_rule(css, "#send-btn.stop:hover")
    assert "var(--stop-hover)" in _css_rule(css, "#send-btn.stop:focus-visible")
    code = _css_rule(css, ".turn-content.md code")
    assert "var(--font-code)" in code
    assert "color: var(--code-text);" in code
    assert "Courier" not in code
    assert "var(--font-code)" in _css_rule(css, ".turn-content.md pre code")
    # Reduced-motion override must follow the base #send-btn rule: same
    # specificity means file order decides (review on #335 caught the
    # transition being re-enabled by the later base rule).
    reduced = css.index("@media (prefers-reduced-motion: reduce)")
    assert reduced > css.index("\n#send-btn {")
    assert "transition: none;" in css[reduced:]


# ---------------------------------------------------------------- markdown (P1)


_MD_CASES = [
    # (source, must-contain, must-not-contain)
    ("### Program Function", ["<h3>Program Function</h3>"], ["###"]),
    ("## Tail", ["<h2>Tail</h2>"], ["## "]),
    ("Use **IKJEFT01** now", ["<strong>IKJEFT01</strong>"], ["**"]),
    ("a *b* c", ["<em>b</em>"], []),
    ("Run `IKJEFT01` here", ["<code>IKJEFT01</code>"], ["`IKJEFT01`"]),
    ("* one\n* two", ["<ul>", "<li>one</li>", "<li>two</li>", "</ul>"], []),
    ("- **x:** y", ["<li><strong>x:</strong> y</li>"], []),
    ("1. first\n2. second", ["<ol>", "<li>first</li>", "</ol>"], []),
    (
        "```jcl\n//STEP1 EXEC PGM=IEFBR14\n```",
        [
            (
                '<pre><button class="copy-btn" type="button">Copy</button>'
                '<code class="language-jcl">//STEP1 <span class="tok-keyword">EXEC</span> <span class="tok-keyword">PGM</span>=IEFBR14</code></pre>'
            )
        ],
        ["```"],
    ),
    # Unclosed fence still renders (streaming midpoint), never leaks raw.
    (
        "```\ncode here",
        ['<pre><button class="copy-btn" type="button">Copy</button><code>code here</code></pre>'],
        ["```"],
    ),
    # Code spans protect markup-like content.
    ("`**not bold**`", ["<code>**not bold**</code>"], ["<strong>"]),
    # Mainframe noise stays literal.
    ("C# and a#b and 2*3", ["C# and a#b and 2*3"], ["<em>", "<strong>"]),
]


def test_markdown_subset_renders_blocks_and_inline():
    from mainframe_rag.webui.routes import render_markdown_subset

    for source, present, absent in _MD_CASES:
        rendered = render_markdown_subset(source)
        for needle in present:
            assert needle in rendered, (source, needle)
        for needle in absent:
            assert needle not in rendered, (source, needle)


def test_markdown_subset_neutralizes_hostile_markup():
    """Spool dumps and retrieved chunks are untrusted: markup must render
    inert — escaped text, our tags only, no links anywhere."""
    from mainframe_rag.webui.routes import render_markdown_subset

    rendered = render_markdown_subset(
        '<script>alert(1)</script>\n'
        '<img src=x onerror=alert(2)>\n'
        '[click](javascript:alert(3))\n'
        'AT&T stays & fine'
    )
    assert "<script>" not in rendered
    assert "<img" not in rendered
    assert "<a " not in rendered and "<a>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "AT&amp;T stays &amp; fine" in rendered


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
    assert "Verified manual citations" in body


def test_ui_nojs_page_renders_markdown(ui_client, monkeypatch):
    """Plain-form path renders the same safe HTML in the full page."""
    monkeypatch.setattr(app_mod, "llm", MarkdownFakeLLM())
    resp = ui_client.post("/ui/chat", data={"message": "What is IKJEFT01?", "messages": ""})
    assert resp.status_code == 200
    assistant = _assistant_html(resp.text)
    assert "<h3>Program Function</h3>" in assistant
    assert "###" not in assistant


def test_ui_chat_fragment_message_design(ui_client, monkeypatch):
    """P2: avatar header, timestamp, code copy button, citation count and
    per-cite copy buttons arrive in the fragment."""
    monkeypatch.setattr(app_mod, "llm", MarkdownFakeLLM())
    resp = ui_client.post(
        "/ui/chat",
        data={"message": "What is IKJEFT01?", "messages": ""},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    body = resp.text
    assert '<span class="avatar" aria-hidden="true">O</span>' in body
    assert '<span class="avatar" aria-hidden="true">C</span>' in body
    assert 'class="turn-time" data-ts="' in body
    # The ```jcl block leaves the answer body as output.script and is
    # re-attached with its threaded language tag (issue #337) — operators
    # see the highlighted code.
    assert "//STEP1" in _assistant_html(body)
    assert (
        '<pre><button class="copy-btn" type="button">Copy</button>'
        '<code class="language-jcl">//STEP1 <span class="tok-keyword">EXEC</span> <span class="tok-keyword">PGM</span>=IEFBR14</code></pre>'
        in body
    )
    assert "Verified manual citations (1)" in body
    assert '<button class="copy-btn copy-cite" type="button">Copy</button>' in body


def test_ui_shell_has_session_filter(ui_client):
    """P3: the sidebar carries the incident filter input (browser-only)."""
    body = ui_client.get("/ui").text
    assert 'id="session-filter"' in body


def test_ui_sidebar_tools_replace_topbar_controls(ui_client):
    """Issue #332.1: theme + export live in the sidebar (reachable while
    scrolled); the topbar keeps only the health badge. IDs are unchanged
    so the client wiring is untouched."""
    body = ui_client.get("/ui").text
    topbar, _, rest = body.partition("<aside")
    assert 'id="theme-select"' not in topbar
    assert 'id="export-btn"' not in topbar
    assert 'id="health-badge"' in topbar
    assert 'id="theme-select"' in rest
    assert 'id="export-btn"' in rest


def test_ui_send_button_glyph(ui_client):
    """Issue #332.3: glyph button with an accessible name, not a text label."""
    body = ui_client.get("/ui").text
    assert 'id="send-btn" aria-label="Send">&#9650;' in body


def test_ui_chat_fragment_turn_copy_buttons(ui_client, monkeypatch):
    """Issue #332.2: every turn header carries a Copy button."""
    monkeypatch.setattr(app_mod, "llm", MarkdownFakeLLM())
    resp = ui_client.post(
        "/ui/chat",
        data={"message": "What is IKJEFT01?", "messages": ""},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert resp.text.count('<button class="copy-btn copy-turn" type="button">Copy</button>') == 2


def test_console_js_streaming_ux_wiring():
    """P3: streaming UX behaviors are wired in console.js — thinking
    placeholder, abort/stop, Ctrl+Enter submit, per-turn meta footer,
    session rename + filter, empty state. (No JS runtime in CI; this pins
    presence, live probes exercise the behavior.)"""
    js = (
        Path(app_mod.__file__).parents[1] / "webui" / "static" / "js" / "console.js"
    ).read_text(encoding="utf-8")
    for token in (
        "Thinking…",
        "AbortController",
        "requestSubmit",
        "turn-meta",
        "empty-state",
        "EMPTY_EXAMPLES",
        "startRename",
        "sessionFilter",
        "stickScroll",
        # Issue #332: per-turn copy + glyph send/stop.
        "copy-turn",
        "aria-label",
        "■",
        "▲",
        # Follow-up: Stop must survive native validation (required toggle).
        'removeAttribute("required")',
    ):
        assert token in js, token


def test_markdown_subset_highlights_jcl():
    """Issue #337: JCL code block renders highlighted comment, string, keyword, and number spans."""
    from mainframe_rag.webui.routes import render_markdown_subset

    source = (
        "```jcl\n"
        "//* Clean up old datasets\n"
        "//STEP1   EXEC PGM=IEFBR14\n"
        "//DD1     DD   DSN='SYS1.PARMLIB',DISP=SHR,SPACE=(CYL,(10,5))\n"
        "```"
    )
    rendered = render_markdown_subset(source)
    assert '<code class="language-jcl">' in rendered
    assert '<span class="tok-comment">//* Clean up old datasets</span>' in rendered
    assert '<span class="tok-keyword">EXEC</span>' in rendered
    assert '<span class="tok-keyword">PGM</span>' in rendered
    assert '<span class="tok-keyword">DD</span>' in rendered
    assert '<span class="tok-keyword">DSN</span>' in rendered
    assert '<span class="tok-string">\'SYS1.PARMLIB\'</span>' in rendered
    assert '<span class="tok-keyword">DISP</span>' in rendered
    assert '<span class="tok-keyword">SHR</span>' in rendered
    assert '<span class="tok-keyword">SPACE</span>' in rendered
    assert '<span class="tok-keyword">CYL</span>' in rendered
    assert '<span class="tok-number">10</span>' in rendered
    assert '<span class="tok-number">5</span>' in rendered


def test_markdown_subset_highlights_rexx():
    """Issue #337: REXX code block renders highlighted comment, string, keyword, and number spans."""
    from mainframe_rag.webui.routes import render_markdown_subset

    source = (
        "```rexx\n"
        "/* REXX sample */\n"
        "say 'Hello, world!'\n"
        "count = 42\n"
        "do i = 1 to count\n"
        '  say "item" i\n'
        "end\n"
        "```"
    )
    rendered = render_markdown_subset(source)
    assert '<code class="language-rexx">' in rendered
    assert '<span class="tok-comment">/* REXX sample */</span>' in rendered
    assert '<span class="tok-string">\'Hello, world!\'</span>' in rendered
    assert '<span class="tok-number">42</span>' in rendered
    assert '<span class="tok-keyword">say</span>' in rendered or '<span class="tok-keyword">SAY</span>' in rendered
    assert '<span class="tok-keyword">do</span>' in rendered
    assert '<span class="tok-keyword">to</span>' in rendered
    assert '<span class="tok-keyword">end</span>' in rendered
    assert '<span class="tok-string">"item"</span>' in rendered


def test_markdown_subset_unlabeled_fence_content_sniffing():
    """Issue #337: unlabeled fences sniff content via detect_code_region."""
    from mainframe_rag.webui.routes import render_markdown_subset

    # Unlabeled fence with JCL cards -> sniffed as jcl with highlighting
    jcl_unlabeled = "```\n//STEP1 EXEC PGM=IEFBR14\n//DD1 DD DSN='A.B',DISP=SHR\n```"
    rendered_jcl = render_markdown_subset(jcl_unlabeled)
    assert '<code class="language-jcl">' in rendered_jcl
    assert '<span class="tok-keyword">EXEC</span>' in rendered_jcl

    # Unlabeled fence with REXX header -> sniffed as rexx with highlighting
    rexx_unlabeled = "```\n/* REXX */\nsay 'hi'\n```"
    rendered_rexx = render_markdown_subset(rexx_unlabeled)
    assert '<code class="language-rexx">' in rendered_rexx
    assert '<span class="tok-comment">/* REXX */</span>' in rendered_rexx

    # Unlabeled fence with console indent -> sniffed as console, NO spans
    console_unlabeled = "```\n   IKJ56228I DATA SET NOT FOUND\n   READY\n```"
    rendered_console = render_markdown_subset(console_unlabeled)
    assert '<code class="language-console">' in rendered_console
    assert "tok-" not in rendered_console

    # Unlabeled fence with plain prose -> remains plain, NO language class, NO spans
    prose_unlabeled = "```\nThis is just some plain text without any code markers.\n```"
    rendered_prose = render_markdown_subset(prose_unlabeled)
    assert "<code" in rendered_prose
    assert "language-" not in rendered_prose
    assert "tok-" not in rendered_prose


def test_markdown_subset_other_languages_keep_class_no_spans():
    """Issue #337: languages other than JCL/REXX keep their class but emit no spans."""
    from mainframe_rag.webui.routes import render_markdown_subset

    py_source = "```python\ndef hello():\n    return 42\n```"
    rendered_py = render_markdown_subset(py_source)
    assert '<code class="language-python">' in rendered_py
    assert "tok-" not in rendered_py
    assert "def hello():" in rendered_py

    json_source = '```json\n{"status": "ok", "code": 200}\n```'
    rendered_json = render_markdown_subset(json_source)
    assert '<code class="language-json">' in rendered_json
    assert "tok-" not in rendered_json


def test_markdown_subset_code_adversarial():
    """Issue #337: adversarial code cases — hostile markup, unclosed fence, empty body, unknown lang."""
    from mainframe_rag.webui.routes import render_markdown_subset

    # Hostile markup inside JCL code block stays strictly escaped
    hostile = (
        "```jcl\n"
        "//STEP1 EXEC PGM=TEST,PARM='<script>alert(1)</script>'\n"
        "//DD1 DD <img src=x onerror=alert(2)>\n"
        "```"
    )
    rendered = render_markdown_subset(hostile)
    assert "<script>" not in rendered
    assert "<img" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "&lt;img src=x onerror=alert(" in rendered

    # Unclosed fence at EOF
    unclosed = "```jcl\n//STEP1 EXEC PGM=IEFBR14"
    rendered_unclosed = render_markdown_subset(unclosed)
    assert '<code class="language-jcl">' in rendered_unclosed
    assert '<span class="tok-keyword">EXEC</span>' in rendered_unclosed
    assert "```" not in rendered_unclosed

    # Empty body in labeled fence
    empty_jcl = "```jcl\n```"
    rendered_empty = render_markdown_subset(empty_jcl)
    assert '<code class="language-jcl"></code>' in rendered_empty

    # Empty body in unlabeled fence
    empty_unlabeled = "```\n```"
    rendered_empty_unlabeled = render_markdown_subset(empty_unlabeled)
    assert "<code></code>" in rendered_empty_unlabeled

    # Unknown language tag
    unknown = "```unknownlang\nsome arbitrary text\n```"
    rendered_unknown = render_markdown_subset(unknown)
    assert '<code class="language-unknownlang">some arbitrary text</code>' in rendered_unknown
    assert "tok-" not in rendered_unknown


def test_ui_chat_stream_final_includes_script_lang(ui_client, monkeypatch):
    """Issue #337 & #339 should-fix: UI chat stream final event payload carries script_lang."""
    monkeypatch.setattr(app_mod, "llm", MarkdownFakeLLM())
    resp = ui_client.post(
        "/ui/chat/stream",
        json={"messages": [{"role": "user", "content": "What is IKJEFT01?"}]},
    )
    assert resp.status_code == 200
    events = [line for line in resp.text.split("\n\n") if line.strip()]
    final_event = next(e for e in events if "event: final" in e)
    payload_line = next(line for line in final_event.split("\n") if line.startswith("data: "))
    payload = json.loads(payload_line[6:])
    assert payload["script"] == "//STEP1 EXEC PGM=IEFBR14"
    assert payload["script_lang"] == "jcl"


def test_ui_chat_script_lang_fallback_unlabeled(ui_client, monkeypatch):
    """Issue #337: when script_lang is None, script append falls back to unlabeled fence."""
    class FakeLLMNoLang:
        def chat(self, messages, reasoning_effort=None, temperature=None):
            return ChatResult(
                content="Here is code:\n\n```\n//UNLABELED EXEC PGM=IEFBR14\n```\n\nCitations:\n- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n",
                finish_reason="stop",
                usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            )

    monkeypatch.setattr(app_mod, "llm", FakeLLMNoLang())
    resp = ui_client.post(
        "/ui/chat",
        data={"message": "Show code", "messages": ""},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    body = resp.text
    # Bare fence unwraps to prose in parse_answer (no script extracted, rendered directly)
    assert "//UNLABELED" in _assistant_html(body)


def test_console_js_code_tokenizer_parity():
    """Issue #337: console.js contains client-side sniffing and tokenization mirroring server."""
    js = (
        Path(app_mod.__file__).parents[1] / "webui" / "static" / "js" / "console.js"
    ).read_text(encoding="utf-8")
    for token in (
        "detectCodeRegion",
        "tokenizeCode",
        "JCL_TOKEN",
        "REXX_TOKEN",
        "tok-comment",
        "tok-string",
        "tok-keyword",
        "tok-number",
    ):
        assert token in js, token
