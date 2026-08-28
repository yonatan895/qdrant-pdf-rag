"""Environment configuration. All values come from env / airgap.env (section 5.2).

Fail fast rules: DENSE_DIM is required before any Qdrant collection or embed
call; EMBED_MODEL / EMBED_BASE_URL before embeddings; LLM_MODEL_REASONING
before /v1/answer (reasoning model only).
"""

import multiprocessing

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Qdrant
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    qdrant_collection: str = "mainframe_manuals"

    # Embeddings (internal vLLM, OpenAI-compatible)
    embed_base_url: str | None = None
    embed_model: str | None = None
    dense_dim: int | None = None
    embed_timeout_s: float = 60.0

    # Reasoning LLM (LiteLLM / vLLM)
    llm_base_url: str | None = None
    llm_model_reasoning: str | None = None

    # Ingest
    ingest_workers: int = Field(
        default_factory=lambda: max(1, (multiprocessing.cpu_count() or 2) - 1)
    )
    batch_size: int = 64
    bm25_model: str = "Qdrant/bm25"
    bm25_cache_dir: str | None = None

    def require_dense_dim(self) -> int:
        if not self.dense_dim or self.dense_dim <= 0:
            raise RuntimeError(
                "DENSE_DIM is unset; it must match the vLLM embedding model. "
                "Set it in the environment before talking to Qdrant."
            )
        return self.dense_dim

    def require_embed(self) -> tuple[str, str]:
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
