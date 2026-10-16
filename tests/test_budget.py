"""Serving-budget sizing (serving-budget track, PR-A).

Hermetic by construction: resolve() takes declared inputs, so these tests
touch no GPU, no network, no env, and no model files. The LOCAL_RT_8GB pins
below reproduce today's run_local_vllm.sh operating points (reasoning 0.65 /
embed 0.33 with embed pooling + eager) within calibration slack — the claim
is fit-within-the-soak-validated-envelope, not byte-exact derivation.
"""

import pytest
from pydantic import ValidationError

from mainframe_rag.serve import (
    BGE_RERANKER_V2_M3,
    GEMMA4_E4B_QAT,
    LOCAL_RT_8GB,
    MAX_UTIL,
    OPENSHIFT_PROD,
    QWEN3_EMBED_06B,
    TRIPLE_8GB,
    BudgetDeficitError,
    HostSpec,
    ModelSpec,
    ProfileBundle,
    resolve,
)


def test_local_profile_resolves_to_validated_operating_points():
    """LOCAL_RT_8GB pins: same flags and windows as the launcher, utils at
    the soak-validated operating points (deterministic given the table)."""
    plan = resolve(LOCAL_RT_8GB)
    assert plan.profile_name == "LOCAL_RT_8GB"
    assert len(plan.servers) == 2
    reasoning, embed = plan.servers

    assert reasoning.model_id == "google/gemma-4-E4B-it-qat-mobile-ct"
    assert reasoning.gpu_memory_utilization == 0.64
    assert reasoning.max_model_len == 4096
    assert reasoning.runner == "generate"
    assert reasoning.enforce_eager is False
    assert reasoning.max_num_batched_tokens is None
    assert reasoning.max_num_seqs == 1

    assert embed.model_id == "Qwen/Qwen3-Embedding-0.6B"
    assert embed.gpu_memory_utilization == 0.33
    assert embed.max_model_len == 4096
    assert embed.runner == "pooling"
    assert embed.convert == "embed"
    assert embed.enforce_eager is True
    assert embed.max_num_batched_tokens == 4096
    assert embed.max_num_seqs == 1

    assert plan.slack_mib >= 0
    assert plan.warnings == []



def test_local_allocation_order_is_not_brittle():
    """Reversed claim order still fits: the LOCAL pack is robust, not an
    artifact of allocation order."""
    reversed_profile = ProfileBundle(
        name="LOCAL_RT_8GB_REVERSED",
        host=LOCAL_RT_8GB.host,
        servers=list(reversed(LOCAL_RT_8GB.servers)),
    )
    plan = resolve(reversed_profile)
    assert plan.slack_mib >= 0
    by_id = {s.model_id: s for s in plan.servers}
    assert by_id["Qwen/Qwen3-Embedding-0.6B"].gpu_memory_utilization == 0.33
    assert by_id["google/gemma-4-E4B-it-qat-mobile-ct"].gpu_memory_utilization == 0.64


def test_generate_falls_back_to_eager_when_compiled_tight():
    """The success path when CUDA-graph workspace does not fit: eager with a
    note and a warning — never a silent downgrade, never an unsafe plan."""
    host = HostSpec(total_vram_mib=4500.0)  # free 4244: eager 3584 fits, compiled 5184 does not
    profile = ProfileBundle(name="TIGHT", host=host, servers=[LOCAL_RT_8GB.servers[0]])
    plan = resolve(profile)
    (server,) = plan.servers
    assert server.enforce_eager is True
    assert server.gpu_memory_utilization == 0.80
    assert any("eager" in n for n in server.notes)
    assert plan.warnings != []


def test_deficit_fails_closed_with_remedies():
    """Even the eager footprint does not fit: refuse with concrete remedies."""
    host = HostSpec(total_vram_mib=2000.0)
    profile = ProfileBundle(name="TINY", host=host, servers=[LOCAL_RT_8GB.servers[0]])
    with pytest.raises(BudgetDeficitError) as exc_info:
        resolve(profile)
    assert "google/gemma-4-E4B-it-qat-mobile-ct" in str(exc_info.value)
    remedies = exc_info.value.remedies
    assert len(remedies) >= 3
    assert any("context_need" in r for r in remedies)
    assert any("total_vram_mib" in r for r in remedies)



def test_zero_free_host_fails():
    """Reserve covering the whole host refuses before any server is sized."""
    host = HostSpec(total_vram_mib=256.0, reserve_mib=256.0)
    profile = ProfileBundle(name="NO_ROOM", host=host, servers=[LOCAL_RT_8GB.servers[1]])
    with pytest.raises(BudgetDeficitError, match="no VRAM"):
        resolve(profile)


def test_max_util_guard_fails_closed():
    """A misconfigured tiny reserve must not emit util=1.0: a server alone
    claiming past MAX_UTIL refuses even when the arithmetic fits."""
    oversized = ModelSpec(
        model_id="wide/model",
        role="embed",
        runner="pooling",
        convert="embed",
        weight_mib=98000.0,
        kv_bytes_per_token=0.0,
        context_need=4096,
    )
    host = HostSpec(total_vram_mib=100000.0, reserve_mib=100.0)
    profile = ProfileBundle(name="CONCENTRATED", host=host, servers=[oversized])
    with pytest.raises(BudgetDeficitError) as exc_info:
        resolve(profile)
    assert any("larger host" in r for r in exc_info.value.remedies)



def test_empty_profile_rejected():
    with pytest.raises(ValidationError):
        ProfileBundle(name="EMPTY", host=HostSpec(total_vram_mib=8151.0), servers=[])



def test_prod_profile_resolves():
    """OPENSHIFT_PROD is requirements math for the platform team: it must
    resolve with compiled reasoning and eager pooling servers."""
    plan = resolve(OPENSHIFT_PROD)
    assert len(plan.servers) == 3
    by_role = {s.role: s for s in plan.servers}
    reasoning = by_role["reasoning"]
    assert reasoning.runner == "generate"
    assert reasoning.enforce_eager is False
    assert reasoning.max_model_len == 8192
    for role in ("embed", "rerank"):
        server = by_role[role]
        assert server.runner == "pooling"
        assert server.enforce_eager is True
        assert server.max_num_batched_tokens == server.max_model_len
    for server in plan.servers:
        assert 0.0 < server.gpu_memory_utilization <= MAX_UTIL
    assert plan.slack_mib >= 0




def test_prefix_cache_flows_to_plan_and_explain():
    spec = ModelSpec(
        model_id="some/reasoning",
        role="reasoning",
        runner="generate",
        weight_mib=2800.0,
        kv_bytes_per_token=98304.0,
        context_need=4096,
        prefix_cache=True,
    )
    plan = resolve(
        ProfileBundle(name="PC", host=HostSpec(total_vram_mib=8151.0), servers=[spec])
    )
    assert plan.servers[0].enable_prefix_caching is True
    assert "--enable-prefix-caching" in plan.explain()



def test_triple_pack_fits_with_measured_operating_points():
    """TRIPLE_8GB: small reasoning + embed + rerank co-reside on 8151 MiB.
    Validated live 2026-09-06 (all three 200 at 4601 MiB resident); the
    resolve must reproduce that pack, not merely avoid a deficit."""
    plan = resolve(TRIPLE_8GB)
    assert plan.profile_name == "TRIPLE_8GB"
    assert len(plan.servers) == 3
    reasoning, embed, rerank = plan.servers

    assert reasoning.model_id == "Qwen/Qwen2.5-0.5B-Instruct"
    assert reasoning.gpu_memory_utilization == 0.20
    assert reasoning.max_model_len == 4096
    assert reasoning.runner == "generate"
    assert reasoning.enforce_eager is False
    assert reasoning.max_num_seqs == 4

    assert embed.model_id == "Qwen/Qwen3-Embedding-0.6B"
    assert embed.gpu_memory_utilization == 0.33

    assert rerank.model_id == "BAAI/bge-reranker-v2-m3"
    assert rerank.gpu_memory_utilization == 0.34
    assert rerank.runner == "pooling"
    assert rerank.enforce_eager is True
    assert rerank.max_num_batched_tokens == 2048

    assert plan.slack_mib >= 0
    assert plan.warnings == []




def test_big_reasoning_triple_refuses_closed():
    """E4B reasoning + embed + rerank demonstrably does not fit 8GB
    (measured 7.0 GB for the pair alone): resolve must refuse, not emit an
    OOM plan. Operators pick TRIPLE_8GB (small reasoning) instead."""
    pack = ProfileBundle(
        name="E4B_TRIPLE",
        host=TRIPLE_8GB.host,
        servers=[GEMMA4_E4B_QAT, QWEN3_EMBED_06B, BGE_RERANKER_V2_M3],
    )
    with pytest.raises(BudgetDeficitError):
        resolve(pack)


def test_crc_profile_preserves_models_and_windows():
    from mainframe_rag.serve import LOCAL_CRC_32GB

    reasoning, embed = resolve(LOCAL_CRC_32GB).servers
    assert [s.model_id for s in LOCAL_CRC_32GB.servers] == [s.model_id for s in LOCAL_RT_8GB.servers]
    assert all(s.max_model_len == 4096 and s.max_num_seqs == 1 and s.enforce_eager for s in (reasoning, embed))
    assert reasoning.gpu_memory_utilization == 0.54
    assert embed.gpu_memory_utilization == 0.43
    assert reasoning.language_model_only and reasoning.mm_processor_cache_gb == 0
    assert embed.enable_prefix_caching is False
    assert embed.enable_chunked_prefill is False
    assert GEMMA4_E4B_QAT.enforce_eager is False


def test_explicit_allocation_below_footprint_refuses():
    spec = GEMMA4_E4B_QAT.model_copy(update={'enforce_eager': True, 'gpu_memory_utilization': 0.1})
    with pytest.raises(BudgetDeficitError, match='below its estimated footprint'):
        resolve(ProfileBundle(name='underallocated', host=LOCAL_RT_8GB.host, servers=[spec]))
