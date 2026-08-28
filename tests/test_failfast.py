"""Agent startup fail-fast (issue #20 PR D).

The agent refuses to listen on a misconfigured embed path. No Qdrant/vLLM
contact happens — lifespan raises before any client is built.
"""

import pytest
from fastapi.testclient import TestClient

from mainframe_rag.agent import app as app_mod
from mainframe_rag.ingest.embed import HashEmbedder


def _clean_embed_env(monkeypatch):
    for var in ("DENSE_DIM", "EMBED_BASE_URL", "EMBED_MODEL", "EMBED_MODE", "ALLOW_HASH_MODE"):
        monkeypatch.delenv(var, raising=False)


def test_refuses_vllm_without_dense_dim(monkeypatch):
    _clean_embed_env(monkeypatch)
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    with pytest.raises(RuntimeError, match="DENSE_DIM"), TestClient(app_mod.app):
        pass


def test_refuses_vllm_without_embed_endpoint(monkeypatch):
    _clean_embed_env(monkeypatch)
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("DENSE_DIM", "768")
    with pytest.raises(RuntimeError, match="EMBED_"), TestClient(app_mod.app):
        pass


def test_refuses_hash_without_explicit_allow(monkeypatch):
    _clean_embed_env(monkeypatch)
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("EMBED_MODE", "hash")
    with pytest.raises(RuntimeError, match="ALLOW_HASH_MODE"), TestClient(app_mod.app):
        pass


def test_refuses_unknown_embed_mode(monkeypatch):
    _clean_embed_env(monkeypatch)
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("EMBED_MODE", "qdrant-cloud")
    with pytest.raises(RuntimeError, match="hash\\|vllm"), TestClient(app_mod.app):
        pass


def test_allows_hash_when_explicitly_allowed(monkeypatch):
    _clean_embed_env(monkeypatch)
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    with TestClient(app_mod.app):
        assert isinstance(app_mod.embedder, HashEmbedder)
