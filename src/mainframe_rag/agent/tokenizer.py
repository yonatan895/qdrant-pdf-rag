"""Tokenizer accounting for model context budgeting (issue #20).

Provides token counting against vLLM's /tokenize endpoint with a deterministic
in-process fallback for air-gapped operations, offline testing, and CI.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import httpx2

if TYPE_CHECKING:
    from mainframe_rag.config import Settings
    from mainframe_rag.ports import Tokenizer

_TOKEN_PATTERN = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def estimate_tokens(text: str) -> int:
    """Deterministic token estimation fallback approximating BPE tokenization."""
    if not text:
        return 0
    return max(1, len(_TOKEN_PATTERN.findall(text)))


class FallbackTokenizer:
    """In-process tokenizer for air-gapped / offline testing or fallback."""

    def count_tokens(self, text: str) -> int:
        return estimate_tokens(text)


class VllmTokenizer:
    """Calls vLLM's /tokenize endpoint with transparent fallback if unreachable."""

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_s: float = 5.0,
        client: httpx2.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_s = timeout_s
        self._client = client
        self._fallback = FallbackTokenizer()

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        try:
            if self._client is not None:
                resp = self._client.post(
                    f"{self._base_url}/tokenize",
                    json={"model": self._model, "prompt": text},
                    timeout=self._timeout_s,
                )
            else:
                resp = httpx2.post(
                    f"{self._base_url}/tokenize",
                    json={"model": self._model, "prompt": text},
                    timeout=self._timeout_s,
                )
            if resp.status_code == 200:
                data = resp.json()
                if "count" in data:
                    return int(data["count"])
                if "tokens" in data and isinstance(data["tokens"], list):
                    return len(data["tokens"])
        except Exception:  # noqa: BLE001, S110
            pass
        return self._fallback.count_tokens(text)


def build_tokenizer(
    settings: Settings, client: httpx2.Client | None = None
) -> Tokenizer:
    """Builds a tokenizer configured for the served reasoning model."""
    if settings.llm_base_url and settings.llm_model_reasoning:
        return VllmTokenizer(
            base_url=settings.llm_base_url,
            model=settings.llm_model_reasoning,
            timeout_s=settings.llm_tokenize_timeout_s,
            client=client,
        )
    return FallbackTokenizer()
