"""Environment configuration. All values come from env / airgap.env (section 5.2).

Fail fast rules: DENSE_DIM is required before any Qdrant collection or embed
call in vLLM mode; EMBED_MODEL / EMBED_BASE_URL before vLLM embeddings;
LLM_MODEL_REASONING before /v1/answer (reasoning model only).

EMBED_MODE=hash is a CI/dev-only in-process embedder (issue #8): deterministic
feature hashing, no network, no model weights. Never the default in prod.
"""

import multiprocessing
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Dense dimension of the hashed embedder. Fixed so CI never depends on the
# vLLM team's DENSE_DIM; prod always overrides via embed_mode=vllm + DENSE_DIM.
HASH_EMBED_DIM = 256


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Qdrant
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    qdrant_collection: str = "mainframe_manuals"
    # Container-internal snapshot directory (docker compose layout). The
    # harness restores pins via file:// URLs against it; override for
    # deployments that mount snapshot storage elsewhere.
    qdrant_snapshots_dir: str = "/qdrant/snapshots"
    # Agent query path vs ingest upsert path: different worst-case call shapes,
    # so each gets its own bounded timeout (issue #20 PR C). Whole seconds —
    # qdrant-client's stub types timeout as int.
    qdrant_timeout_s: int = 30
    qdrant_ingest_timeout_s: int = 120

    # Embeddings. mode "vllm" = internal vLLM (prod, OpenAI-compatible);
    # mode "hash" = local deterministic hashing (CI/dev only, issue #8).
    embed_mode: str = "vllm"
    embed_base_url: str | None = None
    embed_model: str | None = None
    dense_dim: int | None = None
    embed_timeout_s: float = 60.0

    @field_validator("embed_mode")
    @classmethod
    def _normalize_embed_mode(cls, v: str) -> str:
        """Case/whitespace normalization so an operator writing
        EMBED_MODE=VLLM gets vllm behavior everywhere (dispatch, fail-fast,
        air-gap validation), instead of a confusing startup crash."""
        return v.strip().lower()

    # Reasoning LLM (LiteLLM / vLLM)
    llm_base_url: str | None = None
    llm_model_reasoning: str | None = None
    # Reasoning models think; the long timeout is the retry policy — /v1/answer
    # never retries (issue #20 PR C).
    answer_timeout_s: float = 300.0
    # Prompt context length budget: caps total characters of retrieved chunk
    # text sent to the reasoning model to prevent context overflow (4096-token limits).
    prompt_max_context_chars: int = Field(default=8000, ge=1000, le=50000)
    prompt_max_context_chars_complex: int = Field(default=4500, ge=1000, le=50000)
    prompt_max_chunk_chars: int = Field(default=3000, ge=500, le=10000)
    prompt_max_chunk_chars_complex: int = Field(default=1100, ge=300, le=5000)

    # Dense query instruction prefix for asymmetric embedders (e.g. Qwen3-Embedding).
    # Applied to query vector embeddings; chunks during ingest remain raw text.
    dense_query_prefix: str = Field(
        default="Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery: "
    )

    # Hybrid retrieval fusion (local RRF) and diversity parameters
    rrf_k: int = Field(default=2, ge=1, le=100)
    rrf_weight_dense_nl: float = Field(default=1.0, gt=0.0, le=10.0)
    rrf_weight_sparse_nl: float = Field(default=1.0, gt=0.0, le=10.0)
    rrf_weight_dense_identifier: float = Field(default=1.0, gt=0.0, le=10.0)
    rrf_weight_sparse_identifier: float = Field(default=3.0, gt=0.0, le=10.0)
    retrieve_max_chunks_per_page: int = Field(default=1, ge=1, le=10)
    retrieve_max_chunks_per_doc: int = Field(default=3, ge=1, le=10)

    # Reasoning effort control: directs the reasoning model to think more thoroughly
    # on complex operational, diagnostic, and comparative inquiries while conserving
    # latency on simple factoid/message lookups.
    llm_reasoning_effort_simple: Literal["low", "medium", "high"] = Field(default="low")
    llm_reasoning_effort_complex: Literal["low", "medium", "high"] = Field(default="high")
    llm_temperature: float = Field(default=0.2, ge=0.0, le=2.0)

    # Context budgeting via tokenizer accounting:
    # budget = max_model_len - reserved_output_tokens - measured_system_prompt - safety_margin
    llm_max_model_len: int = Field(default=4096, ge=512, le=131072)
    llm_reserved_output_tokens: int = Field(default=1536, ge=128, le=16384)
    llm_token_safety_margin: int = Field(default=128, ge=0, le=1024)
    llm_max_chunk_tokens_narrative: int = Field(default=350, ge=50, le=2048)
    llm_tokenize_timeout_s: float = Field(default=5.0, gt=0.0, le=60.0)
    llm_stream: bool = Field(default=True, description="Stream chat completions to measure TTFT")

    # Bounded httpx2 connection-establishment retries (0-5). These fire only
    # when the request was never sent (DNS/refused), so they are safe for any
    # method. There is deliberately no request-level retry anywhere.
    http_connect_retries: int = Field(default=2, ge=0, le=5)
    http_max_connections: int = Field(default=200, ge=10, le=2000)
    http_max_keepalive_connections: int = Field(default=100, ge=5, le=1000)

    # /healthz probes keep separate budgets: /readyz is a local GET, while a
    # cold vLLM can legitimately take ~10s to answer the embed ping.
    health_qdrant_timeout_s: float = 5.0
    health_embed_timeout_s: float = 10.0

    # Agent startup fail-fast (issue #20 PR D): embed_mode=hash is CI/dev only
    # and must be explicitly allowed (CI overlay sets ALLOW_HASH_MODE=true).
    # Prod (vllm) is validated eagerly at startup: DENSE_DIM / EMBED_* must
    # resolve before the agent listens.
    allow_hash_mode: bool = False
    log_level: str = "INFO"

    # Ingest. batch_size follows the Qdrant skill's 64-256 upsert band
    # (.agents/skills/qdrant-performance-optimization) — bounds enforced here
    # so no call site can grow a magic number outside it.
    ingest_workers: int = Field(
        default_factory=lambda: max(1, (multiprocessing.cpu_count() or 2) - 1)
    )
    batch_size: int = Field(default=128, ge=16, le=256)
    # Embed+upsert ran serially in the parent process (274s of a 281s corpus
    # run while 23 parse workers idled). Parse workers now embed; this many
    # parallel streams drive the Qdrant upserts (Qdrant skill: 2-4 streams).
    ingest_upsert_streams: int = Field(default=4, ge=1, le=8)
    # Bulk-load mode: disable HNSW builds during the initial corpus load and
    # restore after (Qdrant skill guidance) — never for incremental prod runs.
    ingest_bulk_load: bool = False
    bm25_model: str = "Qdrant/bm25"
    bm25_cache_dir: str | None = None

    def require_dense_dim(self) -> int:
        if self.embed_mode == "hash":
            return HASH_EMBED_DIM
        if not self.dense_dim or self.dense_dim <= 0:
            raise RuntimeError(
                "DENSE_DIM is unset; it must match the vLLM embedding model. "
                "Set it in the environment before talking to Qdrant."
            )
        return self.dense_dim

    def require_embed(self) -> tuple[str, str]:
        if self.embed_mode == "hash":
            # Hash mode never contacts an embedding endpoint.
            raise RuntimeError("require_embed() is vLLM-only; hash mode embeds locally.")
        if not self.embed_base_url or not self.embed_model:
            raise RuntimeError(
                "EMBED_BASE_URL and EMBED_MODEL must be set (owned by the vLLM team)."
            )
        return self.embed_base_url, self.embed_model

    def require_reasoning_model(self) -> str:
        if not self.llm_model_reasoning:
            raise RuntimeError(
                "LLM_MODEL_REASONING is unset; /v1/answer must use the reasoning model."
            )
        return self.llm_model_reasoning


def load_settings() -> Settings:
    return Settings()
