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
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

import httpx2

from mainframe_rag.config import bearer_auth_headers
from mainframe_rag.logs import error_type

if TYPE_CHECKING:
    from mainframe_rag.config import Settings
    from mainframe_rag.retrieve.query import SearchHit

from mainframe_rag.ports import Reranker

_TOKEN_RE = re.compile(r"[A-Za-z0-9]{2,}")

# Longest passage sent in a normal rerank call. The server window is 2048
# tokens (Budget rerank role). Measured 2026-10-04 (issue #664) with the
# bge-reranker-v2-m3 tokenizer over 226,917 real-corpus passages: worst case
# 1.18 chars/token, and 31 passages overflow a 2048 window (60-token query +
# special tokens) at this cap; a 2000-char cut overflows none. A static cut
# to 2000 would shorten 62% of passages, so the cap stays and the rare
# overflow is handled by a one-shot retry (RERANK_RETRY_PASSAGE_MAX_CHARS in
# HttpReranker.score) when a whole batch is rejected with HTTP 4xx.
RERANK_PASSAGE_MAX_CHARS = 3000

# Retry-only passage cut: applied to the tail of the already-formatted
# passage (the header lines come first, so they stay whole unless the header
# alone exceeds this cap), only for a batch that failed on both legs with a
# 4xx status.
RERANK_RETRY_PASSAGE_MAX_CHARS = 2000


def _rerank_body_label(chunk_type: str) -> str | None:
    """Type-distinct body framing (issue #215): table/syntax bodies score
    differently shaped text than prose, so they get a declared template
    label instead of the bare prose shape. Message/narrative (and unknown
    types) keep the prose shape so existing passages stay byte-identical."""
    if chunk_type == "syntax":
        return "Syntax:"
    if chunk_type == "table":
        return "Table:"
    return None


def format_rerank_text(hit: SearchHit) -> str:
    """Format candidate metadata and body into a passage for cross-encoder scoring.

    The header carries type signals the body alone does not state (issue
    #215): the chunk_type (message/syntax/table/narrative score differently
    shaped text, so the cross-encoder sees which template it is grading)
    and message_ids (exact codes it would otherwise have to rediscover in
    body prose). Brackets fence the type tag; codes stay bare for exact
    token match. Table/syntax bodies carry a distinct one-line template
    label (``Table:``/``Syntax:``) between heading and body; message and
    narrative keep the bare prose shape.

    Passages cap at RERANK_PASSAGE_MAX_CHARS: header/title/heading/label stay
    whole (the discriminative part), the body tail is cut. Short passages
    are byte-identical to uncapped formatting.
    """
    meta = " ".join(p for p in (hit.product, hit.version, hit.doc_id) if p)
    tags = " ".join(t for t in (f"[{hit.chunk_type}]", *hit.message_ids) if t)
    header = " ".join(p for p in (meta, tags) if p)
    label = _rerank_body_label(hit.chunk_type)
    head = "\n".join(p for p in (header, hit.title, hit.heading, label) if p)
    if not hit.text:
        return head[:RERANK_PASSAGE_MAX_CHARS]
    budget = RERANK_PASSAGE_MAX_CHARS - (len(head) + 1 if head else 0)
    if budget < 0:
        return head[:RERANK_PASSAGE_MAX_CHARS]
    return head + "\n" + hit.text[:budget] if head else hit.text[:budget]


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


def _is_client_error(exc: BaseException) -> bool:
    """True for an HTTP 4xx status failure (not 5xx, transport or shape)."""
    return isinstance(exc, httpx2.HTTPStatusError) and 400 <= exc.response.status_code < 500


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
        headers = bearer_auth_headers(self._settings.rerank_api_key)
        scores: list[float] = []
        client = self._http()
        if self._settings.rerank_endpoint_order == "rerank_first":
            legs: tuple[
                Callable[[httpx2.Client, str, dict[str, str], str, list[str]], list[float] | None],
                Callable[[httpx2.Client, str, dict[str, str], str, list[str]], list[float] | None],
            ] = (self._rerank_batch, self._score_batch)
        else:
            legs = (self._score_batch, self._rerank_batch)

        for i in range(0, len(texts), self._batch_size):
            batch_texts = texts[i : i + self._batch_size]
            outcomes: list[bool] = []
            try:
                batch_scores = self._run_legs(
                    legs, client, base, headers, query, batch_texts, outcomes
                )
            except (
                httpx2.HTTPStatusError,
                httpx2.RequestError,
                ValueError,
                KeyError,
                RuntimeError,
            ):
                # Issue #664: only when every leg was rejected with HTTP 4xx
                # (not 5xx, transport or shape errors) the batch is retried
                # once with passages cut to RERANK_RETRY_PASSAGE_MAX_CHARS;
                # a failing retry raises exactly as the first attempt would.
                if len(outcomes) != len(legs) or not all(outcomes):
                    raise
                cut = [t[:RERANK_RETRY_PASSAGE_MAX_CHARS] for t in batch_texts]
                batch_scores = self._run_legs(legs, client, base, headers, query, cut, [])
            scores.extend(batch_scores)

        return scores

    def _run_legs(
        self,
        legs: Sequence[Callable[..., list[float] | None]],
        client: httpx2.Client,
        base: str,
        headers: dict[str, str],
        query: str,
        batch_texts: list[str],
        outcomes: list[bool],
    ) -> list[float]:
        """Score one batch through the ordered legs, failing closed when the
        last leg fails. `outcomes` gets one entry per failed leg: True when
        that leg was rejected with an HTTP 4xx status."""
        batch_scores: list[float] | None = None
        for index, leg in enumerate(legs):
            last = index == len(legs) - 1
            rejected: list[BaseException] = []
            try:
                if leg == self._score_batch:
                    batch_scores = self._score_batch(
                        client, base, headers, query, batch_texts, rejected
                    )
                else:
                    batch_scores = leg(client, base, headers, query, batch_texts)
            except (
                httpx2.HTTPStatusError,
                httpx2.RequestError,
                ValueError,
                KeyError,
                RuntimeError,
            ) as exc:
                outcomes.append(_is_client_error(exc))
                # A spent leg falls through to the next one; the last
                # leg failing fails the search closed, exactly as the
                # legacy single-fallback path did.
                if last:
                    raise
                batch_scores = None
            else:
                if batch_scores is None:
                    outcomes.append(bool(rejected) and _is_client_error(rejected[-1]))
            if batch_scores is not None:
                break
        if batch_scores is None:
            # Reachable only when the trailing leg yields None instead
            # of raising (the score leg never raises): both legs are
            # spent, so fail closed rather than scoring silently empty.
            raise RuntimeError(
                f"Reranker endpoints under {base} returned no usable scores "
                f"for a batch of {len(batch_texts)} texts"
            )
        return batch_scores

    def _score_batch(
        self,
        client: httpx2.Client,
        base: str,
        headers: dict[str, str],
        query: str,
        batch_texts: list[str],
        rejected: list[BaseException] | None = None,
    ) -> list[float] | None:
        """vLLM proprietary leg (`/v1/score`). None means unusable here —
        transport failure or unexpected shape — so the caller tries the
        next leg. Never raises for server behavior; an HTTP status failure
        is appended to `rejected` when given (the 4xx retry rule)."""
        url = f"{base}/score" if base.endswith("/v1") else f"{base}/v1/score"
        payload = {
            "model": self._model,
            "text_1": query,
            "text_2": batch_texts,
        }
        batch_scores: list[float] | None = None
        try:
            resp = client.post(url, json=payload, timeout=self._timeout, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                items = data["data"]
                if len(items) == len(batch_texts):
                    sorted_items = sorted(items, key=lambda d: d.get("index", 0))
                    batch_scores = [float(d["score"]) for d in sorted_items]
        except httpx2.HTTPStatusError as exc:
            if rejected is not None:
                rejected.append(exc)
            batch_scores = None
        except (httpx2.RequestError, ValueError, KeyError):
            batch_scores = None
        return batch_scores

    def _rerank_batch(
        self,
        client: httpx2.Client,
        base: str,
        headers: dict[str, str],
        query: str,
        batch_texts: list[str],
    ) -> list[float]:
        """Cohere/TEI standard leg (`/v1/rerank` or `/rerank`). Raises on
        anything unusable — the caller only catches it when another leg
        remains, otherwise it fails the search closed with the diagnosis."""
        rerank_url = f"{base}/rerank" if base.endswith("/v1") else f"{base}/v1/rerank"
        rerank_payload = {
            "model": self._model,
            "query": query,
            "documents": batch_texts,
        }
        resp = client.post(rerank_url, json=rerank_payload, timeout=self._timeout, headers=headers)
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
        return batch_scores


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
        return error_type(exc)
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
    score, chunk_id)`` key); 0.0 keeps RRF order exactly (blend ties break
    by RRF score, then stable input order which is already RRF order —
    never by the cross-encoder or chunk_id) while still attaching
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
    # Stable sort: blended score descending. At alpha=0.0 the blend IS the
    # RRF leg, so a raw-CE (or chunk_id) tie-break would reorder RRF ties
    # away from RRF order — break by RRF only and let the stable sort keep
    # input order, which is already RRF order. Otherwise break blend ties
    # by raw cross-encoder score, then RRF, then chunk_id (at alpha=1.0
    # the blend ties imply raw-CE ties, so this matches the legacy
    # (rerank_score, RRF, chunk_id) key exactly).
    if alpha == 0.0:
        ranked.sort(key=lambda t: (t[0], t[2]), reverse=True)
    else:
        ranked.sort(key=lambda t: (t[0], t[1], t[2], t[3]), reverse=True)
    out = [t[4] for t in ranked]
    if top_k is not None:
        return out[:top_k]
    return out
