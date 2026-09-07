"""Cross-encoder reranking (issue #76 PR-02).

RRF provides rank fusion across vector spaces, but does not score relevance.
The cross-encoder scores fused candidates (default top-50) using bge-reranker-v2-m3.

Implementations:
- HttpReranker: calls vLLM / TEI (/v1/score or /v1/rerank) over httpx2 (prod GPU path).
- HashReranker: deterministic in-process lexical scoring (CI/dev and hash mode).
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

import httpx2

if TYPE_CHECKING:
    from mainframe_rag.config import Settings
    from mainframe_rag.retrieve.query import SearchHit

from mainframe_rag.ports import Reranker

_TOKEN_RE = re.compile(r"[A-Za-z0-9]{2,}")


def format_rerank_text(hit: SearchHit) -> str:
    """Format candidate metadata and body into a passage for cross-encoder scoring."""
    header = " ".join(p for p in (hit.product, hit.version, hit.doc_id) if p)
    return "\n".join(p for p in (header, hit.title, hit.heading, hit.text) if p)


# ------------------------------------------------------- Hash implementation (CI / dev)
class HashReranker:
    """Deterministic in-process scorer for CI and hash mode.

    No network, no GPU, no weights required. Scores candidates based on
    query token overlap and density.
    """

    def score(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        query_tokens = [t.lower() for t in _TOKEN_RE.findall(query)]
        if not query_tokens:
            return [0.0] * len(texts)
        q_set = set(query_tokens)
        scores: list[float] = []
        for text in texts:
            t_tokens = [t.lower() for t in _TOKEN_RE.findall(text)]
            if not t_tokens:
                scores.append(0.0)
                continue
            matches = sum(1 for tok in t_tokens if tok in q_set)
            # Normalization balancing match count against text length
            overlap = matches / (len(query_tokens) + math.log1p(len(t_tokens)))
            scores.append(round(overlap, 4))
        return scores


# ------------------------------------------------------- HTTP implementation (vLLM / TEI)
class HttpReranker:
    """Production reranker: sends candidate pairs to vLLM or TEI scoring endpoint."""

    def __init__(
        self,
        settings: Settings,
        client: httpx2.Client | None = None,
    ) -> None:
        self._settings = settings
        self._base_url = settings.rerank_base_url or settings.embed_base_url
        self._model = settings.rerank_model
        self._batch_size = settings.rerank_batch_size
        self._timeout = settings.rerank_timeout_s
        self._client = client

    def _http(self) -> httpx2.Client:
        if self._client is None:
            self._client = httpx2.Client(
                timeout=self._timeout,
                transport=httpx2.HTTPTransport(retries=self._settings.http_connect_retries),
            )
        return self._client

    def score(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        if not self._base_url:
            raise RuntimeError("RERANK_BASE_URL (or EMBED_BASE_URL) must be set for HttpReranker")

        base = self._base_url.rstrip("/")
        scores: list[float] = []
        client = self._http()

        for i in range(0, len(texts), self._batch_size):
            batch_texts = texts[i : i + self._batch_size]
            url = f"{base}/score" if base.endswith("/v1") else f"{base}/v1/score"
            payload = {
                "model": self._model,
                "text_1": query,
                "text_2": batch_texts,
            }
            batch_scores: list[float] | None = None
            try:
                resp = client.post(url, json=payload, timeout=self._timeout)
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                    items = data["data"]
                    if len(items) == len(batch_texts):
                        sorted_items = sorted(items, key=lambda d: d.get("index", 0))
                        batch_scores = [float(d["score"]) for d in sorted_items]
            except (httpx2.HTTPStatusError, httpx2.RequestError, ValueError, KeyError):
                batch_scores = None

            if batch_scores is not None:
                scores.extend(batch_scores)
                continue

            # Fallback to Cohere/TEI standard (/v1/rerank or /rerank)
            rerank_url = f"{base}/rerank" if base.endswith("/v1") else f"{base}/v1/rerank"
            rerank_payload = {
                "model": self._model,
                "query": query,
                "documents": batch_texts,
            }
            resp = client.post(rerank_url, json=rerank_payload, timeout=self._timeout)
            resp.raise_for_status()
            r_data = resp.json()
            results = r_data.get("results") if isinstance(r_data, dict) else None
            if not isinstance(results, list) or len(results) != len(batch_texts):
                raise RuntimeError(
                    f"Reranker endpoint {rerank_url} returned invalid or mismatched results: {r_data}"
                )
            batch_scores = [0.0] * len(batch_texts)
            for res in results:
                idx = res.get("index")
                if idx is None or not (0 <= idx < len(batch_texts)):
                    raise RuntimeError(f"Reranker returned out-of-bounds index: {idx}")
                batch_scores[idx] = float(res.get("relevance_score", res.get("score", 0.0)))
            scores.extend(batch_scores)

        return scores


# ------------------------------------------------------- Dispatch and candidate scoring
def probe_reranker(reranker: Reranker) -> str | None:
    """Best-effort 1x1 score ping for lifespan startup.

    Returns None when the endpoint answers with one score, else a short
    error string for a loud startup warning. Warn-only by design: rerank
    is opt-in, so a dead endpoint must never keep the agent from listening
    at startup. HashReranker always passes (in-process, nothing to probe).
    """
    try:
        scores = reranker.score("probe", ["probe"])
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"[:200]
    if len(scores) != 1:
        return f"expected 1 score, got {len(scores)}"
    return None


def build_reranker(settings: Settings, client: httpx2.Client | None = None) -> Reranker | None:
    """The single dispatch point for reranking. Never branch on reranker flags elsewhere.

    Hash mode defaults to HashReranker (deterministic, keeps CI/dev
    byte-stable); an explicit RERANK_BASE_URL opts out into HttpReranker
    even in hash mode (issue #193) — knowingly trading determinism for a
    live cross-encoder, e.g. reasoning+ranking stacks with zero GPU embed
    spend.
    """
    if not settings.rerank_enabled:
        return None
    if settings.embed_mode == "hash" and not settings.rerank_base_url:
        return HashReranker()
    if settings.rerank_base_url or settings.embed_base_url:
        return HttpReranker(settings, client)
    if settings.allow_hash_mode:
        return HashReranker()
    raise RuntimeError(
        "RERANK_ENABLED is true but neither RERANK_BASE_URL nor EMBED_BASE_URL is configured. "
        "Set RERANK_BASE_URL (or EMBED_BASE_URL), or ALLOW_HASH_MODE=true for CI/dev."
    )


def _minmax(values: Sequence[float]) -> list[float]:
    """Min-max normalize over the candidate pool. A constant list carries no
    signal, so it normalizes to 0.5 and the other leg decides the blend."""
    lo = min(values)
    hi = max(values)
    if hi == lo:
        return [0.5] * len(values)
    span = hi - lo
    return [(v - lo) / span for v in values]


def rerank_candidates(
    query: str,
    candidates: Sequence[SearchHit],
    reranker: Reranker,
    top_k: int | None = None,
    alpha: float = 1.0,
) -> list[SearchHit]:
    """Score candidates using the cross-encoder and sort descending by a blend
    of the normalized cross-encoder and pre-rerank RRF scores.

    Both legs are min-max normalized over the candidate pool. ``alpha`` is
    the cross-encoder weight: 1.0 reproduces the legacy cross-encoder-only
    order exactly (the normalization is strictly monotonic, so the blended
    key plus the raw-score tie-breaks match the old ``(rerank_score, RRF
    score, chunk_id)`` key); 0.0 keeps RRF order while still attaching
    ``rerank_score``. Out-of-range alphas clamp to [0, 1].

    If top_k is specified, truncates results to top_k.
    """
    if not candidates:
        return []
    alpha = min(1.0, max(0.0, alpha))
    texts = [format_rerank_text(c) for c in candidates]
    scores = reranker.score(query, texts)
    if len(scores) != len(candidates):
        raise RuntimeError(
            f"Reranker returned {len(scores)} scores for {len(candidates)} candidates"
        )
    ce_norm = _minmax(scores)
    rrf_norm = _minmax([c.score for c in candidates])
    ranked: list[tuple[float, float, float, str, SearchHit]] = []
    for cand, ce, cn, rn in zip(candidates, scores, ce_norm, rrf_norm):
        blend = alpha * cn + (1.0 - alpha) * rn
        ranked.append((blend, ce, cand.score, cand.chunk_id, cand.model_copy(update={"rerank_score": ce})))
    # Stable sort: blended score descending, then raw cross-encoder score,
    # then original RRF score, then chunk_id.
    ranked.sort(key=lambda t: (t[0], t[1], t[2], t[3]), reverse=True)
    out = [t[4] for t in ranked]
    if top_k is not None:
        return out[:top_k]
    return out
