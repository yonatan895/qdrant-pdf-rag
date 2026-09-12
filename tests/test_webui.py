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
