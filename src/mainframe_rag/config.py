"""Environment configuration. All values come from env / airgap.env (section 5.2).

Fail fast rules: DENSE_DIM is required before any Qdrant collection or embed
call in vLLM mode; EMBED_MODEL / EMBED_BASE_URL before vLLM embeddings;
LLM_MODEL_REASONING before /v1/answer (reasoning model only).

EMBED_MODE=hash is a CI/dev-only in-process embedder (issue #8): deterministic
feature hashing, no network, no model weights. Never the default in prod.
"""

import multiprocessing

from pydantic import Field
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

    # Reasoning LLM (LiteLLM / vLLM)
    llm_base_url: str | None = None
    llm_model_reasoning: str | None = None
    # Reasoning models think; the long timeout is the retry policy — /v1/answer
    # never retries (issue #20 PR C).
    answer_timeout_s: float = 300.0

    # Bounded httpx connection-establishment retries (0-5). These fire only
    # when the request was never sent (DNS/refused), so they are safe for any
    # method. There is deliberately no request-level retry anywhere.
    http_connect_retries: int = Field(default=2, ge=0, le=5)

    # /healthz probe budget: qdrant /readyz GET and the embed endpoint ping.
    health_timeout_s: float = 5.0

    # Ingest. batch_size follows the Qdrant skill's 64-256 upsert band
    # (.agents/skills/qdrant-performance-optimization) — bounds enforced here
    # so no call site can grow a magic number outside it.
    ingest_workers: int = Field(
        default_factory=lambda: max(1, (multiprocessing.cpu_count() or 2) - 1)
    )
    batch_size: int = Field(default=64, ge=16, le=256)
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
