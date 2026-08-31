"""Timeout + retry plumbing for every outbound call (issue #20 PR C).

No live Qdrant/vLLM: client objects are constructed but never dial out.
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
    """Capture HTTPTransport kwargs without reaching into httpx2 internals
    (round-7 nit 7: `_pool._retries` is private and bump-brittle)."""
    captured: dict = {}
    real = httpx2.HTTPTransport

    def spy(**kw):
        captured.update(kw)
        return real(**kw)

    monkeypatch.setattr(httpx2, "HTTPTransport", spy)
    return captured


def test_answer_client_uses_setting_and_never_retries(monkeypatch):
    captured = _spy_transport(monkeypatch)
    s = _settings(answer_timeout_s=12.5)
    llm = HttpxLLMClient(s)
    client = llm._http()
    assert client.timeout.read == 12.5
    # /v1/answer is single shot: connection retries are off.
    assert captured["retries"] == 0


def test_chat_after_close_fails_loudly(monkeypatch):
    """Round-7 nit 9: close() must not null the client — a post-shutdown
    chat() hits the closed pool and raises, never silently rebuilds."""
    captured = _spy_transport(monkeypatch)
    llm = HttpxLLMClient(_settings())
    client = llm._http()
    client.close()
    llm.close()
    assert captured["retries"] == 0
    with pytest.raises(RuntimeError, match="closed"):
        llm.chat([ChatMessage(role="user", content="q")])


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

    class FakeQdrantClient:
        def __init__(self, **kw):
            captured.update(kw)

        def close(self):
            pass

    monkeypatch.setattr("qdrant_client.QdrantClient", FakeQdrantClient)
    with TestClient(app_mod.app):
        assert captured["timeout"] == 11.0
        assert app_mod.http.timeout.read == 7.0
        assert transport_kw["retries"] == 3


def test_ingest_qdrant_timeout_from_settings(monkeypatch):
    from mainframe_rag.ingest import run_ingest

    monkeypatch.setenv("QDRANT_INGEST_TIMEOUT_S", "44")
    captured: dict = {}

    class FakeQdrantClient:
        def __init__(self, **kw):
            captured.update(kw)

        def close(self):
            pass

    monkeypatch.setattr("qdrant_client.QdrantClient", FakeQdrantClient)
    s = run_ingest.load_settings()
    client = run_ingest._get_qdrant(s)
    try:
        assert captured["timeout"] == 44.0
        assert client is not None
    finally:
        run_ingest._worker_qdrant = None


def test_connect_retries_setting_is_bounded():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _settings(http_connect_retries=50)
