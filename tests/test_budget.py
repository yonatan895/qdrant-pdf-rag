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
    COMPILED_MARGIN_FLOOR_MIB,
    DEFAULT_RESERVE_MIB,
    EAGER_GENERATE_MARGIN_MIB,
    LOCAL_RT_8GB,
    MAX_UTIL,
    OPENSHIFT_PROD,
    POOLING_EAGER_MARGIN_MIB,
    BudgetDeficitError,
    HostSpec,
    ModelSpec,
    ProfileBundle,
    list_profiles,
    resolve,
)

# Today's run_local_vllm.sh operating points (scripts/run_local_vllm.sh):
# reasoning GPU_MEM=0.65 compiled, embed GPU_MEM=0.33 pooling + eager.
TODAY_REASONING_UTIL = 0.65
TODAY_EMBED_UTIL = 0.33
CALIBRATION_SLACK = 0.05


def test_engine_margin_defaults():
    """New constants get default assertions (repo test_config convention)."""
    assert DEFAULT_RESERVE_MIB == 256.0
    assert COMPILED_MARGIN_FLOOR_MIB == 2000.0
    assert EAGER_GENERATE_MARGIN_MIB == 400.0
    assert POOLING_EAGER_MARGIN_MIB == 1500.0
    assert MAX_UTIL == 0.99


def test_profiles_registered():
    assert list_profiles() == ["LOCAL_RT_8GB", "OPENSHIFT_PROD"]


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


def test_local_utils_within_soak_validated_envelope():
    """Resolved utils stay within calibration slack of the soak-validated
    operating points, on the safe (packable) side."""
    plan = resolve(LOCAL_RT_8GB)
    reasoning, embed = plan.servers
    assert abs(reasoning.gpu_memory_utilization - TODAY_REASONING_UTIL) <= CALIBRATION_SLACK
    assert abs(embed.gpu_memory_utilization - TODAY_EMBED_UTIL) <= CALIBRATION_SLACK
    assert reasoning.gpu_memory_utilization <= TODAY_REASONING_UTIL
    assert embed.gpu_memory_utilization <= TODAY_EMBED_UTIL


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


def test_pooling_deficit_fails_closed():
    """Pooling has no eager fallback to try (it is already eager): a tight
    host refuses with remedies on the same path generate uses."""
    host = HostSpec(total_vram_mib=2000.0)  # free 1744 < embed 2650
    profile = ProfileBundle(name="TINY_EMBED", host=host, servers=[LOCAL_RT_8GB.servers[1]])
    with pytest.raises(BudgetDeficitError) as exc_info:
        resolve(profile)
    assert "Qwen/Qwen3-Embedding-0.6B" in str(exc_info.value)
    assert exc_info.value.remedies != []


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


def test_weight_alone_exceeding_host_fails():
    oversized = ModelSpec(
        model_id="huge/model",
        role="reasoning",
        runner="generate",
        weight_mib=8000.0,
        kv_bytes_per_token=98304.0,
        context_need=4096,
    )
    profile = ProfileBundle(name="OVER", host=HostSpec(total_vram_mib=8151.0), servers=[oversized])
    with pytest.raises(BudgetDeficitError) as exc_info:
        resolve(profile)
    assert exc_info.value.remedies != []


def test_empty_profile_rejected():
    with pytest.raises(ValidationError):
        ProfileBundle(name="EMPTY", host=HostSpec(total_vram_mib=8151.0), servers=[])


def test_pooling_batched_tokens_follow_window():
    """Pooling prefill memory scales with in-flight tokens: cap at the
    window. Generate leaves the server default."""
    plan = resolve(LOCAL_RT_8GB)
    reasoning, embed = plan.servers
    assert reasoning.max_num_batched_tokens is None
    assert embed.max_num_batched_tokens == embed.max_model_len == 4096


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


def test_explain_contains_sizing_math():
    text = resolve(LOCAL_RT_8GB).explain()
    assert "Budget LOCAL_RT_8GB" in text
    assert "8151 MiB" in text
    assert "google/gemma-4-E4B-it-qat-mobile-ct" in text
    assert "Qwen/Qwen3-Embedding-0.6B" in text
    assert "gpu-memory-utilization 0.64" in text
    assert "gpu-memory-utilization 0.33" in text
    assert "max-model-len 4096" in text
    assert "--runner pooling --convert embed --enforce-eager" in text
    assert "FIT" in text


def test_prefix_cache_pinned_on_for_local_reasoning_only():
    """Prefix caching is pinned ON for the LOCAL reasoning server (issue
    #80: vLLM already caches by default; the explicit flag documents
    effective state). Embed stays off: single-shot prefill, unmeasured."""
    plan = resolve(LOCAL_RT_8GB)
    assert [s.enable_prefix_caching for s in plan.servers] == [True, False]


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


def test_specs_are_immutable():
    """Declared facts are frozen: resolve inputs cannot be mutated mid-flight."""
    with pytest.raises(ValidationError):
        LOCAL_RT_8GB.servers[0].weight_mib = 1.0  # type: ignore[misc]
    with pytest.raises(ValidationError):
        LOCAL_RT_8GB.host.total_vram_mib = 1.0  # type: ignore[misc]
