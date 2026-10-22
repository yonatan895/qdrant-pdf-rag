"""Config fail-fast behavior (architecture.md section 5.2)."""

import pytest
from pydantic import ValidationError

from mainframe_rag.config import Settings


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


def test_collection_policy_env_override(monkeypatch):
    """Issue #360: explicitly selected distribution policy loads from the
    operator environment; unset keeps the server-default (None) behavior."""
    s = Settings(_env_file=None)
    assert s.collection_distribution_kwargs() == {}
    monkeypatch.setenv("QDRANT_SHARD_NUMBER", "6")
    monkeypatch.setenv("QDRANT_REPLICATION_FACTOR", "2")
    monkeypatch.setenv("QDRANT_WRITE_CONSISTENCY_FACTOR", "1")
    s = Settings(_env_file=None)
    assert s.qdrant_shard_number == 6
    assert s.qdrant_replication_factor == 2
    assert s.qdrant_write_consistency_factor == 1
    assert s.collection_distribution_kwargs() == {
        "shard_number": 6,
        "replication_factor": 2,
        "write_consistency_factor": 1,
    }


def test_peer_endpoints_env_override(monkeypatch):
    """Issue #360: direct peer endpoints load from the operator environment;
    unset means unconfigured (never inferred)."""
    assert Settings(_env_file=None).qdrant_peer_endpoints() == ()
    monkeypatch.setenv(
        "QDRANT_PEER_URLS", "http://qdrant-0:6333,http://qdrant-1:6333"
    )
    s = Settings(_env_file=None)
    assert s.qdrant_peer_endpoints() == (
        "http://qdrant-0:6333",
        "http://qdrant-1:6333",
    )


def test_collection_policy_rejects_non_positive(monkeypatch):
    """Production numbers are explicit owner decisions, but zero/negative
    values are never a policy: fail closed at settings load."""
    monkeypatch.setenv("QDRANT_REPLICATION_FACTOR", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.delenv("QDRANT_REPLICATION_FACTOR")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, qdrant_shard_number=0)


def test_alias_publication_is_the_only_ingest_mode(monkeypatch):
    """Issue #359/#360: alias publication is the only write mode. In-place
    ingest was retired, and an explicit opt-out fails closed instead of
    being silently ignored."""
    assert Settings(_env_file=None).ingest_alias_publish is True
    monkeypatch.setenv("INGEST_ALIAS_PUBLISH", "true")
    assert Settings(_env_file=None).ingest_alias_publish is True
    monkeypatch.setenv("INGEST_ALIAS_PUBLISH", "false")
    with pytest.raises(ValueError, match="in-place ingest was retired"):
        Settings(_env_file=None)


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


@pytest.mark.parametrize("field", ["llm_reasoning_effort_simple", "llm_reasoning_effort_complex"])
@pytest.mark.parametrize("effort", ["xhigh"])
@pytest.mark.parametrize("from_env", [False, True])
def test_reasoning_effort_round_trips_configuration(monkeypatch, field, effort, from_env):
    monkeypatch.delenv(field.upper(), raising=False)
    domain = "low,medium,high,xhigh"  # every value is a member of the served set
    if from_env:
        monkeypatch.setenv(field.upper(), effort)
        monkeypatch.setenv("LLM_REASONING_EFFORTS", domain)
        settings = Settings(_env_file=None)
    else:
        settings = Settings(_env_file=None, **{field: effort, "llm_reasoning_efforts": domain})
    assert getattr(settings, field) == effort


@pytest.mark.parametrize(("efforts", "simple", "complex_", "expected"), [
    ("low,medium,high", "low", "high", ("low", "medium", "high")),
    (" xhigh , low,medium ", "low", "xhigh", ("low", "medium", "xhigh")),  # ascending, stripped
])
def test_served_efforts_parse_in_vocabulary_order(monkeypatch, efforts, simple, complex_, expected):
    for name in ("LLM_REASONING_EFFORTS", "LLM_REASONING_EFFORT_SIMPLE", "LLM_REASONING_EFFORT_COMPLEX"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None, llm_reasoning_efforts=efforts,
                        llm_reasoning_effort_simple=simple, llm_reasoning_effort_complex=complex_)
    assert settings.reasoning_efforts() == expected


@pytest.mark.parametrize(("overrides", "field"), [
    # #748/#777: a served domain without the default xhigh must not start,
    # on either path.
    ({"llm_reasoning_efforts": "low,medium,high"}, "LLM_REASONING_EFFORT_SIMPLE"),
    ({"llm_reasoning_efforts": "low,medium,high", "llm_reasoning_effort_simple": "low"},
     "LLM_REASONING_EFFORT_COMPLEX"),
    ({"llm_reasoning_efforts": "low,low,high"}, "LLM_REASONING_EFFORTS"),
    ({"llm_reasoning_efforts": "low,maximal-private,high"}, "LLM_REASONING_EFFORTS"),
    ({"llm_reasoning_efforts": ""}, "LLM_REASONING_EFFORTS"),
])
def test_efforts_outside_the_served_set_refuse_startup(monkeypatch, overrides, field):
    from pydantic import ValidationError

    for name in ("LLM_REASONING_EFFORTS", "LLM_REASONING_EFFORT_SIMPLE", "LLM_REASONING_EFFORT_COMPLEX"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, **overrides)
    message = "; ".join(error["msg"] for error in caught.value.errors())
    assert field in message and "maximal-private" not in message


def test_reasoning_effort_validation():
    """Unsupported effort values fail at configuration time."""
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


def test_otel_bounds_are_enforced():
    """Issue #83: tracing ships fail-closed — no OTEL_EXPORTER_OTLP_ENDPOINT
    means no exporter, no network. The exporter queue/timeout and the sample
    ratio are bounded so a dead collector can neither grow the heap nor
    disable sampling silently beyond the configured ratio."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, otel_sample_ratio=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, otel_export_queue_size=32)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, otel_export_timeout_ms=50)  # type: ignore[arg-type]


def test_chat_bounds_are_enforced():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, chat_max_body_chars=500)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, chat_max_body_chars=200000)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, chat_max_turns=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, chat_max_turns=51)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, chat_max_prior_turn_chars=100)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, chat_max_prior_turn_chars=6000)


def test_bearer_auth_headers_matrix():
    """Single auth helper for every model leg: unset/empty/whitespace-only
    keys yield no header (never `Bearer None`); a pasted key is stripped."""
    from mainframe_rag.config import bearer_auth_headers

    assert bearer_auth_headers(None) == {}
    assert bearer_auth_headers("") == {}
    assert bearer_auth_headers("   ") == {}
    assert bearer_auth_headers("sk-test-key") == {"Authorization": "Bearer sk-test-key"}
    assert bearer_auth_headers("  sk-test-key\n") == {"Authorization": "Bearer sk-test-key"}


@pytest.mark.parametrize("reserve", [16384])
def test_simple_thinking_reserve_loads_from_env(monkeypatch, reserve):
    monkeypatch.setenv("LLM_THINKING_RESERVE_TOKENS_SIMPLE", str(reserve))
    settings = Settings(_env_file=None)
    assert settings.llm_thinking_reserve_tokens_simple == reserve
    assert settings.llm_thinking_reserve_tokens_complex == 8192
    assert settings.llm_max_model_len == 32768


@pytest.mark.parametrize("reserve", [-1, 16385])
def test_simple_thinking_reserve_rejects_out_of_range(reserve):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_thinking_reserve_tokens_simple=reserve)


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
    "qdrant_shard_number": None,
    "qdrant_replication_factor": None,
    "qdrant_write_consistency_factor": None,
    "qdrant_peer_urls": None,
    "embed_mode": "vllm",
    "embed_base_url": None,
    "embed_model": None,
    "embed_api_key": None,
    "dense_dim": 4096,
    "embed_timeout_s": 60.0,
    "embed_model_revision": "",
    "llm_base_url": None,
    "llm_model_reasoning": None,
    "llm_api_key": None,
    "answer_timeout_s": 300.0,
    "prompt_max_context_chars": 32000,
    "prompt_max_context_chars_complex": 32000,
    "prompt_max_chunk_chars": 4000,
    "prompt_max_chunk_chars_complex": 4000,
    "query_max_chars": 2000,
    "splunk_context_max_chars": 4000,
    "prompt_order": "retrieval",
    "dense_query_prefix": (
        "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery: "
    ),
    "acronym_expansion_enabled": False,
    "comparative_split_enabled": True,
    "diagnostic_dualpath_enabled": False,
    "rrf_k": 60,
    "rrf_weight_dense_nl": 1.0,
    "rrf_weight_sparse_nl": 1.0,
    "rrf_weight_dense_identifier": 1.0,
    "rrf_weight_sparse_identifier": 3.0,
    "rrf_sparse_boost_syntax": 1.0,
    "rrf_sparse_boost_table": 1.0,
    "retrieve_max_chunks_per_page": 1,
    "retrieve_max_chunks_per_doc": 3,
    "llm_reasoning_effort_simple": "xhigh",
    "llm_reasoning_effort_complex": "xhigh",
    "llm_reasoning_efforts": "low,medium,xhigh",
    "llm_temperature": 0.2,
    "llm_max_model_len": 32768,
    "llm_reserved_output_tokens": 2560,
    "llm_thinking_reserve_tokens_simple": 8192,
    "llm_thinking_reserve_tokens_complex": 8192,
    "llm_token_safety_margin": 128,
    "llm_max_chunk_tokens_narrative": 1200,
    "llm_tokenize_timeout_s": 5.0,
    "llm_stream": False,
    "chat_condense_enabled": False,
    "chat_max_body_chars": 32768,
    "chat_max_turns": 10,
    "chat_max_prior_turn_chars": 1000,
    "ui_enabled": False,
    "feedback_enabled": False,
    "feedback_url": None,
    "feedback_api_key": None,
    "feedback_queue_size": 256,
    "feedback_timeout_s": 2.0,
    "feedback_drain_timeout_s": 2.0,
    "feedback_store_path": "/data/feedback.sqlite3",
    "feedback_service_token": None,
    "feedback_retention_days": 180,
    "feedback_purge_first_delay_s": 30.0,
    "feedback_purge_interval_s": 86400.0,
    "request_max_concurrent": 0,
    "request_queue_max": 0,
    "request_queue_wait_s": 5.0,
    "request_deadline_s": 0.0,
    "embed_max_input_chars": 0,
    "ingest_max_pdf_bytes": 0,
    "ingest_max_doc_pages": 0,
    "ingest_max_doc_chunks": 0,
    "http_connect_retries": 2,
    "http_max_connections": 200,
    "http_max_keepalive_connections": 100,
    "health_qdrant_timeout_s": 5.0,
    "health_embed_timeout_s": 10.0,
    "health_rerank_timeout_s": 5.0,
    "health_rerank_ttl_s": 15.0,
    "representation_cache_ttl_s": 5.0,
    "allow_hash_mode": False,
    "log_level": "INFO",
    "otel_exporter_otlp_endpoint": None,
    "otel_sample_ratio": 1.0,
    "otel_export_queue_size": 2048,
    "otel_export_timeout_ms": 5000,
    "metrics_enabled": False,
    "batch_size": 128,
    "ingest_upsert_streams": 4,
    "ingest_scan_page_size": 1000,
    "ingest_bulk_load": False,
    "ingest_alias_publish": True,
    "bm25_model": "Qdrant/bm25",
    "bm25_cache_dir": None,
    "bm25_weights_revision": "22b8d2af71a76161e18dd432d2cee0eefa66e412",
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
    "evidence_max_bytes": 65536,
    "evidence_timeout_s": 10.0,
    "contextual_embed_enabled": False,
    "context_llm_base_url": None,
    "context_llm_model": None,
    "context_llm_api_key": None,
    "context_llm_timeout_s": 30.0,
    "context_max_chars": 500,
    "context_cache_path": None,
}


def test_every_setting_default_is_pinned(tmp_path, monkeypatch):
    import os

    from mainframe_rag import config

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
    # Without a cgroup quota the default stays CPU-1 (#840).
    monkeypatch.setattr(config, "_CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(config, "_PROC_CGROUP", tmp_path / "absent")
    monkeypatch.setattr(os, "process_cpu_count", lambda: 8)
    assert fields["ingest_workers"].get_default(call_default_factory=True) == 7


def _cgroup(root, files):
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


@pytest.mark.parametrize(
    ("files", "affinity", "expected"),
    [
        ({}, 64, 64),  # no cgroup files: affinity/cpu_count only
        ({"cpu.max": "max 100000\n"}, 64, 64),  # v2, unlimited
        ({"cpu.max": "200000 100000\n"}, 64, 2),  # v2, 2 CPUs
        ({"cpu.max": "150000 100000\n"}, 64, 1),  # fractional quota rounds down
        ({"cpu.max": "50000 100000\n"}, 64, 1),  # never below one
        ({"cpu.max": "800000 100000\n"}, 4, 4),  # affinity lower than quota
        ({"cpu.max": "garbage\n"}, 64, 64),  # unreadable quota is ignored
        ({"cpu.max": "200000 0\n"}, 64, 64),  # a zero period is unreadable too
        ({"cpu/cpu.cfs_quota_us": "300000\n", "cpu/cpu.cfs_period_us": "100000\n"}, 64, 3),  # v1
        ({"cpu/cpu.cfs_quota_us": "-1\n", "cpu/cpu.cfs_period_us": "100000\n"}, 64, 64),  # v1, unlimited
        # Not namespaced: this process's own cgroup, where a parent's quota binds.
        ({"proc": "0::/pods/job\n", "pods/cpu.max": "300000 100000\n",
          "pods/job/cpu.max": "max 100000\n", "cpu.max": "max 100000\n"}, 64, 3),
        ({"proc": "0::/pods/job\n", "pods/job/cpu.max": "200000 100000\n"}, 64, 2),
        # Quotas on both levels: the tighter one binds, wherever it sits.
        ({"proc": "0::/pods/job\n", "pods/cpu.max": "300000 100000\n",
          "pods/job/cpu.max": "400000 100000\n"}, 64, 3),
    ],
)
def test_available_cpus_takes_the_lowest_of_quota_and_affinity(
    tmp_path, monkeypatch, files, affinity, expected
):
    """#840: the cgroup CPU quota (v2 cpu.max or v1 cfs quota/period) and the
    process affinity both bound the CPUs ingest may use."""
    import os

    from mainframe_rag.config import available_cpus

    monkeypatch.setattr(os, "process_cpu_count", lambda: affinity)
    root = _cgroup(tmp_path / "cgroup", {k: v for k, v in files.items() if k != "proc"})
    proc = tmp_path / "proc-self-cgroup"
    if "proc" in files:
        proc.write_text(files["proc"])
    assert available_cpus(root, proc) == expected


def test_two_cpu_quota_bounds_the_default_not_an_explicit_count(tmp_path, monkeypatch):
    """#840: an ingest Job limited to 2 CPUs on a larger node defaults to one
    parse worker, while an explicit INGEST_WORKERS above the quota is used as
    given: the operator may oversubscribe a network-bound ingest."""
    import multiprocessing
    import os

    from mainframe_rag import config
    from mainframe_rag.ingest.run_ingest import resolve_workers

    monkeypatch.setattr(config, "_CGROUP_ROOT", _cgroup(tmp_path, {"cpu.max": "200000 100000\n"}))
    monkeypatch.setattr(config, "_PROC_CGROUP", tmp_path / "absent")
    monkeypatch.setattr(os, "process_cpu_count", lambda: 64)
    monkeypatch.delenv("INGEST_WORKERS", raising=False)
    settings = Settings(_env_file=None)
    assert settings.ingest_workers == 1
    assert resolve_workers(None, settings) == 1
    monkeypatch.setattr(multiprocessing, "cpu_count", lambda: 64)  # the node
    monkeypatch.setenv("INGEST_WORKERS", "8")
    assert resolve_workers(None, Settings(_env_file=None)) == 8
