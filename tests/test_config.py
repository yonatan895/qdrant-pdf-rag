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
    assert s.qdrant_snapshots_dir == "/qdrant/snapshots"


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
    assert s.http_max_connections == 200
    assert s.http_max_keepalive_connections == 100
    assert s.prompt_max_context_chars == 8000
    assert s.prompt_max_context_chars_complex == 4500
    assert s.prompt_max_chunk_chars == 3000
    assert s.prompt_max_chunk_chars_complex == 1100
    assert s.prompt_order == "retrieval"
    assert "Instruct:" in s.dense_query_prefix
    assert s.rrf_k == 2
    assert s.rrf_weight_dense_nl == 1.0
    assert s.rrf_weight_sparse_nl == 1.0
    assert s.rrf_weight_dense_identifier == 1.0
    assert s.rrf_weight_sparse_identifier == 3.0
    assert s.retrieve_max_chunks_per_page == 1
    assert s.retrieve_max_chunks_per_doc == 3
    assert s.llm_reasoning_effort_simple == "low"
    assert s.llm_reasoning_effort_complex == "high"
    assert s.llm_temperature == 0.2
    assert s.llm_max_model_len == 4096
    assert s.llm_reserved_output_tokens == 1536
    assert s.llm_token_safety_margin == 128
    assert s.llm_max_chunk_tokens_narrative == 350
    assert s.llm_tokenize_timeout_s == 5.0
    assert s.llm_stream is False
    assert s.rerank_enabled is False
    assert s.rerank_model == "BAAI/bge-reranker-v2-m3"
    assert s.rerank_base_url is None
    assert s.rerank_candidates == 50
    assert s.rerank_batch_size == 32
    assert s.rerank_timeout_s == 5.0


def test_hash_mode_requires_explicit_allow():
    """PR D: hash embed mode is CI/dev only and opt-in at startup."""
    s = Settings(_env_file=None)
    assert s.allow_hash_mode is False
    assert s.log_level == "INFO"


def test_ingest_tuning_defaults():
    """Ingest pipeline knobs: bounded defaults, no magic numbers at call sites
    (AGENTS rule 5). batch_size default 128 (Qdrant skill 64-256 band)."""
    s = Settings(_env_file=None)
    assert s.batch_size == 128
    assert s.ingest_upsert_streams == 4
    assert s.ingest_bulk_load is False


def test_reasoning_effort_validation():
    """llm_reasoning_effort_* are constrained to Literal['low', 'medium', 'high']."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_reasoning_effort_simple="ultra")  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_reasoning_effort_complex="extreme")  # type: ignore[arg-type]
