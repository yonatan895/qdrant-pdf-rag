"""Config fail-fast behavior (architecture.md section 5.2)."""

import pytest

from mainframe_rag.config import Settings


def test_dense_dim_required(monkeypatch):
    s = Settings(dense_dim=None, _env_file=None)
    with pytest.raises(RuntimeError, match="DENSE_DIM"):
        s.require_dense_dim()


def test_embed_required(monkeypatch):
    s = Settings(embed_base_url=None, embed_model=None, _env_file=None)
    with pytest.raises(RuntimeError, match="EMBED_"):
        s.require_embed()


def test_reasoning_model_required():
    s = Settings(llm_model_reasoning=None, _env_file=None)
    with pytest.raises(RuntimeError, match="reasoning"):
        s.require_reasoning_model()


def test_env_loads(monkeypatch):
    monkeypatch.setenv("DENSE_DIM", "768")
    s = Settings(_env_file=None)
    assert s.require_dense_dim() == 768
    assert s.qdrant_collection == "mainframe_manuals"


def test_outbound_timeout_defaults_bounded():
    """Every outbound call has a bounded timeout (issue #20 PR C)."""
    s = Settings(_env_file=None)
    assert s.qdrant_timeout_s > 0
    assert s.qdrant_ingest_timeout_s > 0
    assert s.embed_timeout_s > 0
    assert s.answer_timeout_s > 0
    assert s.health_qdrant_timeout_s > 0
    assert s.health_embed_timeout_s > 0
    assert 0 <= s.http_connect_retries <= 5
