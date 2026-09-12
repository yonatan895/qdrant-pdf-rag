"""Config fail-fast behavior (architecture.md section 5.2)."""

import pytest
from pydantic import ValidationError

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
    assert s.llm_thinking_reserve_tokens_complex == 1000
    assert s.llm_token_safety_margin == 128
    assert s.llm_max_chunk_tokens_narrative == 350
    assert s.llm_tokenize_timeout_s == 5.0
    assert s.llm_stream is False
    assert s.rerank_enabled is False
    assert s.rerank_model == "BAAI/bge-reranker-v2-m3"
    assert s.rerank_base_url is None
    assert s.rerank_endpoint_order == "score_first"
    assert s.rerank_candidates == 50
    assert s.rerank_batch_size == 32
    assert s.rerank_timeout_s == 5.0
    assert s.rerank_fusion_alpha == 1.0
    assert s.rrf_sparse_boost_syntax == 1.0
    assert s.rrf_sparse_boost_table == 1.0
    assert s.zowe_mcp_enabled is False
    assert s.zowe_mcp_base_url is None
    assert s.zowe_mcp_timeout_s == 15.0
    assert s.zowe_mcp_max_bytes == 262144
    assert s.zowe_mcp_dry_run is False


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


def test_contextual_embed_defaults():
    """Contextual retrieval (issue #78): default off with bounded knobs; the
    context model endpoint is unset until an operator enables the flag."""
    s = Settings(_env_file=None)
    assert s.contextual_embed_enabled is False
    assert s.context_llm_base_url is None
    assert s.context_llm_model is None
    assert s.context_llm_timeout_s == 30.0
    assert s.context_max_chars == 500
    assert s.context_cache_path is None


def test_require_context_llm_fails_closed():
    """Enabled without an endpoint configured is a startup error, never a
    silent fallback to header-only embeddings."""
    s = Settings(
        _env_file=None,
        contextual_embed_enabled=True,
        context_llm_base_url=None,
        context_llm_model=None,
    )
    with pytest.raises(RuntimeError, match="CONTEXT_LLM_BASE_URL"):
        s.require_context_llm()


def test_reasoning_effort_validation():
    """llm_reasoning_effort_* are constrained to Literal['low', 'medium', 'high']."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_reasoning_effort_simple="ultra")  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_reasoning_effort_complex="extreme")  # type: ignore[arg-type]


def test_rerank_endpoint_order_validation():
    """rerank_endpoint_order accepts only the two known legs; anything else
    fails at startup, never as a silent score-first at request time."""
    from pydantic import ValidationError

    assert Settings(_env_file=None, rerank_endpoint_order="rerank_first").rerank_endpoint_order == "rerank_first"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rerank_endpoint_order="bogus")  # type: ignore[arg-type]


def test_request_size_guardrail_defaults():
    """Issue #87: bounded request-size guardrails — overlong queries fail
    closed, caller-supplied splunk_context truncates. Defaults clear the
    longest golden query (165 chars) by an order of magnitude."""
    s = Settings(_env_file=None)
    assert s.query_max_chars == 2000
    assert s.splunk_context_max_chars == 4000


def test_acronym_expansion_defaults_off():
    """Issue #82 PR-A: deterministic acronym expansion ships default-off;
    identifier-heavy queries bypass rewriting entirely (pinned in
    test_rewrite.py, asserted here as a Settings contract)."""
    s = Settings(_env_file=None)
    assert s.acronym_expansion_enabled is False


def test_multipath_split_defaults():
    """Issue #214/#270: comparative split ships ON (measured holdout win);
    diagnostic dual-path stays OFF (measured neutral-negative). Every leg
    shares the original filter, so enabling never widens the constraint
    allowlist."""
    s = Settings(_env_file=None)
    assert s.comparative_split_enabled is True
    assert s.diagnostic_dualpath_enabled is False


def test_otel_defaults_off_and_bounded():
    """Issue #83: tracing ships fail-closed — no OTEL_EXPORTER_OTLP_ENDPOINT
    means no exporter, no network. The exporter queue/timeout and the sample
    ratio are bounded so a dead collector can neither grow the heap nor
    disable sampling silently beyond the configured ratio."""
    s = Settings(_env_file=None)
    assert s.otel_exporter_otlp_endpoint is None
    assert s.otel_sample_ratio == 1.0
    assert s.otel_export_queue_size == 2048
    assert s.otel_export_timeout_ms == 5000
    with pytest.raises(ValidationError):
        Settings(_env_file=None, otel_sample_ratio=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, otel_export_queue_size=32)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, otel_export_timeout_ms=50)  # type: ignore[arg-type]


def test_metrics_defaults_off():
    """Issue #187: the Prometheus endpoint ships fail-closed — UWM scrapes
    404 until the operator opts in."""
    s = Settings(_env_file=None)
    assert s.metrics_enabled is False
    assert Settings(_env_file=None, metrics_enabled=True).metrics_enabled is True


def test_gateway_api_key_defaults_unset():
    """LiteLLM gateway virtual keys ship unset: the keyless wire shape stays
    byte-identical to the pre-gateway path until an operator sets one."""
    s = Settings(_env_file=None)
    assert s.llm_api_key is None
    assert s.embed_api_key is None
    assert s.rerank_api_key is None
    assert s.context_llm_api_key is None


def test_bearer_auth_headers_matrix():
    """Single auth helper for every model leg: unset/empty/whitespace-only
    keys yield no header (never `Bearer None`); a pasted key is stripped."""
    from mainframe_rag.config import bearer_auth_headers

    assert bearer_auth_headers(None) == {}
    assert bearer_auth_headers("") == {}
    assert bearer_auth_headers("   ") == {}
    assert bearer_auth_headers("sk-test-key") == {"Authorization": "Bearer sk-test-key"}
    assert bearer_auth_headers("  sk-test-key\n") == {"Authorization": "Bearer sk-test-key"}


def test_gateway_api_keys_load_from_env(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-llm")
    monkeypatch.setenv("EMBED_API_KEY", "sk-embed")
    monkeypatch.setenv("RERANK_API_KEY", "sk-rerank")
    monkeypatch.setenv("CONTEXT_LLM_API_KEY", "sk-context")
    s = Settings(_env_file=None)
    assert s.llm_api_key == "sk-llm"
    assert s.embed_api_key == "sk-embed"
    assert s.rerank_api_key == "sk-rerank"
    assert s.context_llm_api_key == "sk-context"


# AGENTS.md lethal rule: no default flips. Every Settings field must appear
# here with its exact default, so a changed or added field fails CI instead of
# depending on reviewer vigilance. `ingest_workers` is host-dependent and pins
# its factory instead of a value (asserted separately).
PINNED_SETTING_DEFAULTS: dict[str, object] = {
    "qdrant_url": "http://localhost:6333",
    "qdrant_api_key": None,
    "qdrant_collection": "mainframe_manuals",
    "qdrant_snapshots_dir": "/qdrant/snapshots",
    "qdrant_timeout_s": 30,
    "qdrant_ingest_timeout_s": 120,
    "embed_mode": "vllm",
    "embed_base_url": None,
    "embed_model": None,
    "embed_api_key": None,
    "dense_dim": None,
    "embed_timeout_s": 60.0,
    "llm_base_url": None,
    "llm_model_reasoning": None,
    "llm_api_key": None,
    "answer_timeout_s": 300.0,
    "prompt_max_context_chars": 8000,
    "prompt_max_context_chars_complex": 4500,
    "prompt_max_chunk_chars": 3000,
    "prompt_max_chunk_chars_complex": 1100,
    "query_max_chars": 2000,
    "splunk_context_max_chars": 4000,
    "prompt_order": "retrieval",
    "dense_query_prefix": (
        "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery: "
    ),
    "acronym_expansion_enabled": False,
    "comparative_split_enabled": True,
    "diagnostic_dualpath_enabled": False,
    "rrf_k": 2,
    "rrf_weight_dense_nl": 1.0,
    "rrf_weight_sparse_nl": 1.0,
    "rrf_weight_dense_identifier": 1.0,
    "rrf_weight_sparse_identifier": 3.0,
    "rrf_sparse_boost_syntax": 1.0,
    "rrf_sparse_boost_table": 1.0,
    "retrieve_max_chunks_per_page": 1,
    "retrieve_max_chunks_per_doc": 3,
    "llm_reasoning_effort_simple": "low",
    "llm_reasoning_effort_complex": "high",
    "llm_temperature": 0.2,
    "llm_max_model_len": 4096,
    "llm_reserved_output_tokens": 1536,
    "llm_thinking_reserve_tokens_complex": 1000,
    "llm_token_safety_margin": 128,
    "llm_max_chunk_tokens_narrative": 350,
    "llm_tokenize_timeout_s": 5.0,
    "llm_stream": False,
    "http_connect_retries": 2,
    "http_max_connections": 200,
    "http_max_keepalive_connections": 100,
    "health_qdrant_timeout_s": 5.0,
    "health_embed_timeout_s": 10.0,
    "allow_hash_mode": False,
    "log_level": "INFO",
    "otel_exporter_otlp_endpoint": None,
    "otel_sample_ratio": 1.0,
    "otel_export_queue_size": 2048,
    "otel_export_timeout_ms": 5000,
    "metrics_enabled": False,
    "batch_size": 128,
    "ingest_upsert_streams": 4,
    "ingest_bulk_load": False,
    "bm25_model": "Qdrant/bm25",
    "bm25_cache_dir": None,
    "rerank_enabled": False,
    "rerank_model": "BAAI/bge-reranker-v2-m3",
    "rerank_base_url": None,
    "rerank_api_key": None,
    "rerank_endpoint_order": "score_first",
    "rerank_candidates": 50,
    "rerank_batch_size": 32,
    "rerank_timeout_s": 5.0,
    "rerank_fusion_alpha": 1.0,
    "zowe_mcp_enabled": False,
    "zowe_mcp_base_url": None,
    "zowe_mcp_timeout_s": 15.0,
    "zowe_mcp_max_bytes": 262144,
    "zowe_mcp_dry_run": False,
    "contextual_embed_enabled": False,
    "context_llm_base_url": None,
    "context_llm_model": None,
    "context_llm_api_key": None,
    "context_llm_timeout_s": 30.0,
    "context_max_chars": 500,
    "context_cache_path": None,
}


def test_every_setting_default_is_pinned():
    import multiprocessing

    fields = Settings.model_fields
    factory_fields = {name for name, f in fields.items() if f.default_factory is not None}
    assert factory_fields == {"ingest_workers"}, (
        f"unexpected default_factory fields: {factory_fields - {'ingest_workers'}}"
    )
    assert set(fields) == set(PINNED_SETTING_DEFAULTS) | factory_fields
    for name, want in PINNED_SETTING_DEFAULTS.items():
        got = fields[name].get_default()
        assert got == want, f"{name} default changed: {got!r} != pinned {want!r}"
    # Host-dependent by design: pin the factory, not the machine's core count.
    workers = fields["ingest_workers"].get_default(call_default_factory=True)
    assert workers == max(1, (multiprocessing.cpu_count() or 2) - 1)
