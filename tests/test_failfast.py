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


def test_refuses_vllm_without_model_revision(monkeypatch):
    """Issue #362 req 3: a mutable gateway alias is not a model identity.
    No store contact happens — attestation raises before any client use."""
    _clean_embed_env(monkeypatch)
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("EMBED_MODE", "vllm")
    monkeypatch.setenv("EMBED_BASE_URL", "http://embed.internal/v1")
    monkeypatch.setenv("EMBED_MODEL", "test-embed-model")
    monkeypatch.setenv("DENSE_DIM", "768")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setenv("LLM_MODEL_REASONING", "test-reasoning-model")
    with pytest.raises(RuntimeError, match="EMBED_MODEL_REVISION"), TestClient(app_mod.app):
        pass


def _lifespan_qdrant(monkeypatch, double):
    """Serve lifespan's store read from a scenario double (inner patch wins
    over the conftest empty-store hermetic guard)."""
    monkeypatch.setattr("qdrant_client.AsyncQdrantClient", lambda *a, **k: double)


def _hash_env(monkeypatch):
    _clean_embed_env(monkeypatch)
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setenv("LLM_MODEL_REASONING", "test-reasoning-model")


def test_lifespan_refuses_drifted_store(monkeypatch):
    """Issue #362 req 4: evidence of an incompatible stored generation
    refuses to listen, with the migration path in the message."""
    from mainframe_rag.config import Settings
    from mainframe_rag.ingest.completion import completion_collection_name
    from mainframe_rag.ingest.rules_version import extraction_rules_version
    from tests.fakes import ServingManifestQdrant, manifest_envelope

    _hash_env(monkeypatch)
    settings = Settings(_env_file=None, embed_mode="hash", allow_hash_mode=True)
    rules = extraction_rules_version()
    envelope = manifest_envelope(
        settings, rules, completion_collection_name(settings),
        embed_model_revision="other-rev",
    )
    _lifespan_qdrant(monkeypatch, ServingManifestQdrant(envelope))
    with pytest.raises(RuntimeError, match="refuses.*reembed_required"), TestClient(app_mod.app):
        pass


def test_lifespan_refuses_legacy_store(monkeypatch):
    """A non-empty store with no contract is legacy — refuse, never serve."""
    from tests.fakes import ServingManifestQdrant

    _hash_env(monkeypatch)
    _lifespan_qdrant(monkeypatch, ServingManifestQdrant(None, points=True))
    with pytest.raises(RuntimeError, match="refuses.*legacy"), TestClient(app_mod.app):
        pass


def test_lifespan_unreachable_store_warns_but_listens(monkeypatch):
    """No evidence either way (store unreadable) is `unknown`: warn-only —
    nothing can be served wrong from a store we cannot read, and /healthz
    stays the live signal."""
    from tests.fakes import ServingManifestQdrant

    _hash_env(monkeypatch)
    _lifespan_qdrant(monkeypatch, ServingManifestQdrant(explode=True))
    with TestClient(app_mod.app):
        pass
