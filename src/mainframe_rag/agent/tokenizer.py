"""Tokenizer accounting for model context budgeting (issue #20).

Token counting against vLLM's /tokenize endpoint with a deterministic
in-process fallback for air-gapped operations, offline testing, and CI.

The endpoint lives at the SERVER ORIGIN: LLM_BASE_URL is .../v1, and vLLM
serves /tokenize next to /v1, not under it (a LiteLLM front may not expose
it at all). The first failed call is logged as a warning and the instance
permanently downgrades to the in-process estimator — never a silent
per-call fallback, never repeated doomed RPCs. Callers plan prompt packing
with the estimator and use the real tokenizer only to verify the packed
prompt (see answer.build_messages).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import httpx2

from mainframe_rag.ports import ChatMessage

if TYPE_CHECKING:
    from mainframe_rag.config import Settings
    from mainframe_rag.ports import Tokenizer

log = logging.getLogger("agent.tokenizer")

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

    def count_messages(self, messages: list[ChatMessage]) -> int:
        return sum(estimate_tokens(f"{m.role}\n{m.content}") for m in messages)


class VllmTokenizer:
    """Counts tokens via vLLM's /tokenize endpoint at the server origin.

    On the first failure — connection error, non-200, or a body without a
    usable count — logs one warning and pins the in-process estimator for
    the life of this instance. A LiteLLM front without /tokenize therefore
    costs exactly one failed request per process, never per answer.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_s: float = 5.0,
        client: httpx2.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # vLLM serves /tokenize at the server root; /v1/tokenize is a 404.
        self._origin = self._base_url.removesuffix("/v1")
        self._model = model
        self._timeout_s = timeout_s
        self._client = client
        self._fallback = FallbackTokenizer()
        self._downgraded = False

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        count = self._remote_count({"model": self._model, "prompt": text})
        if count is not None:
            return count
        return self._fallback.count_tokens(text)

    def count_messages(self, messages: list[ChatMessage]) -> int:
        """Chat-template-aware count: what /tokenize reports for the message
        list is what actually consumes max_model_len."""
        if not messages:
            return 0
        payload = {
            "model": self._model,
            "messages": [m.model_dump() for m in messages],
        }
        count = self._remote_count(payload)
        if count is not None:
            return count
        return self._fallback.count_messages(messages)

    def _remote_count(self, payload: dict[str, Any]) -> int | None:
        """One /tokenize call; None means the endpoint failed and this
        tokenizer is now pinned to the in-process estimator."""
        if self._downgraded:
            return None
        url = f"{self._origin}/tokenize"
        try:
            if self._client is not None:
                resp = self._client.post(url, json=payload, timeout=self._timeout_s)
            else:
                resp = httpx2.post(url, json=payload, timeout=self._timeout_s)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    if "count" in data:
                        return int(data["count"])
                    if "tokens" in data and isinstance(data["tokens"], list):
                        return len(data["tokens"])
            log.warning(
                "vLLM /tokenize unusable at %s (status %s); pinning in-process token estimation",
                url,
                getattr(resp, "status_code", "?"),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "vLLM /tokenize unreachable at %s (%s); pinning in-process token estimation",
                url,
                str(exc)[:120],
            )
        self._downgraded = True
        return None


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
