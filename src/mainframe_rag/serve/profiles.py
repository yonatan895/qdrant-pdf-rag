"""Declared serving profiles (serving-budget track, PR-A).

The declared table: every number here is an input to the generic rules in
budget.py, validated empirically, never probed at resolve time.

LOCAL_RT_8GB reproduces today's run_local_vllm.sh operating points
(reasoning 0.65 / embed 0.33, embed pooling + eager) within calibration
slack; tests/test_budget.py pins the relationship. Weight and KV figures are
conservative estimates — replace with measured arch facts / nvidia-smi deltas
when available; the soak backstop (PR-D load tier) catches drift.

OPENSHIFT_PROD is sizing REQUIREMENTS for the platform team (this repo never
deploys vLLM): gemma-4-31B reasoning plus bigger embedding and ranking
servers on an 80GB host. SKU weights are illustrative until the platform team
confirms them; the resolve rules are what this profile exercises.
"""

from __future__ import annotations

from mainframe_rag.serve.budget import HostSpec, ModelSpec, ProfileBundle

# Qwen3-Embedding-0.6B: 0.6B params in bf16 ~= 1150 MiB resident. Pooling
# runner holds no KV cache (kv 0); the 4096 window is pinned by
# tests/test_embed_budget.py (issue #99 sweep measured 2043 worst-case
# tokens, rejecting a 2048 window).
QWEN3_EMBED_06B = ModelSpec(
    model_id="Qwen/Qwen3-Embedding-0.6B",
    role="embed",
    runner="pooling",
    convert="embed",
    weight_mib=1150.0,
    kv_bytes_per_token=0.0,
    context_need=4096,
    max_num_seqs=1,
)

# gemma-4-E4B-it-qat-mobile-ct: 4B-class QAT weights (~2.7 GiB resident upper
# bound). KV figure is a representative 4B-class GQA shape
# (2 x 24 layers x 8 kv_heads x 128 dim x 2 B); replace with measured arch
# facts. Single-query local dev, hence max_num_seqs=1 (matches the launcher).
GEMMA4_E4B_QAT = ModelSpec(
    model_id="google/gemma-4-E4B-it-qat-mobile-ct",
    role="reasoning",
    runner="generate",
    weight_mib=2800.0,
    kv_bytes_per_token=98304.0,
    context_need=4096,
    max_num_seqs=1,
)

# Local 8GB card (nvidia-smi reports 8151 MiB). Order is allocation order:
# the reasoning server claims first, matching the launcher's default flow.
LOCAL_RT_8GB = ProfileBundle(
    name="LOCAL_RT_8GB",
    host=HostSpec(total_vram_mib=8151.0),
    servers=[GEMMA4_E4B_QAT, QWEN3_EMBED_06B],
)

# Illustrative 31B-class reasoning server (bf16 ~= 59 GiB resident upper
# bound) at an 8k window with production concurrency. KV shape is a
# representative large-GQA layout (2 x 48 x 8 x 128 x 2 B); confirm SKUs
# with the platform team before treating the explain output as a purchase
# order.
GEMMA4_31B = ModelSpec(
    model_id="google/gemma-4-31B-it",
    role="reasoning",
    runner="generate",
    weight_mib=59500.0,
    kv_bytes_per_token=196608.0,
    context_need=8192,
    max_num_seqs=8,
)

# Bigger embedding server (4B-class, bf16 ~= 8 GiB resident upper bound).
QWEN3_EMBED_4B = ModelSpec(
    model_id="Qwen/Qwen3-Embedding-4B",
    role="embed",
    runner="pooling",
    convert="embed",
    weight_mib=8100.0,
    kv_bytes_per_token=0.0,
    context_need=4096,
    max_num_seqs=32,
)

# Ranking server served via the pooling runner (score path); cross-encoders
# hold no KV cache. 1k window covers query+passage pairs.
BGE_RERANKER_V2_M3 = ModelSpec(
    model_id="BAAI/bge-reranker-v2-m3",
    role="rerank",
    runner="pooling",
    weight_mib=1200.0,
    kv_bytes_per_token=0.0,
    context_need=1024,
    max_num_seqs=32,
)

OPENSHIFT_PROD = ProfileBundle(
    name="OPENSHIFT_PROD",
    host=HostSpec(total_vram_mib=81920.0, reserve_mib=1024.0),
    servers=[GEMMA4_31B, QWEN3_EMBED_4B, BGE_RERANKER_V2_M3],
)

PROFILES: dict[str, ProfileBundle] = {
    LOCAL_RT_8GB.name: LOCAL_RT_8GB,
    OPENSHIFT_PROD.name: OPENSHIFT_PROD,
}


def list_profiles() -> list[str]:
    return sorted(PROFILES)
