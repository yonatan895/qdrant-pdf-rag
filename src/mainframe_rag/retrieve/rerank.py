"""Cross-encoder reranking (issue #76 PR-02).

RRF provides rank fusion across vector spaces, but does not score relevance.
The cross-encoder scores fused candidates (default top-50) using bge-reranker-v2-m3,
returning the top-k (default 8) relevance-ranked chunks.

Implementations:
- HttpReranker: calls vLLM / TEI (/v1/score or /v1/rerank) over httpx2 (prod GPU path).
- OnnxReranker: offline ONNX Runtime inference using local weights (offline air-gap path).
- HashReranker: deterministic in-process lexical scoring (CI/dev and hash mode).
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from pathlib import Path
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
            raise RuntimeError("RERANK_BASE_URL (or VLLM_BASE_URL) must be set for HttpReranker")

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
            try:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                if "data" in data and isinstance(data["data"], list):
                    items = sorted(data["data"], key=lambda d: d.get("index", 0))
                    scores.extend([float(d["score"]) for d in items])
                    continue
            except (httpx2.HTTPStatusError, httpx2.RequestError):
                # Fallback to Cohere/TEI standard (/v1/rerank or /rerank)
                rerank_url = f"{base}/rerank" if base.endswith("/v1") else f"{base}/v1/rerank"
                rerank_payload = {
                    "model": self._model,
                    "query": query,
                    "documents": batch_texts,
                }
                resp = client.post(rerank_url, json=rerank_payload)
                resp.raise_for_status()
                data = resp.json()
                results = data.get("results", [])
                batch_scores = [0.0] * len(batch_texts)
                for res in results:
                    idx = res.get("index", 0)
                    s = float(res.get("relevance_score", res.get("score", 0.0)))
                    if 0 <= idx < len(batch_scores):
                        batch_scores[idx] = s
                scores.extend(batch_scores)

        return scores


# ------------------------------------------------------- ONNX implementation (local weights)
class OnnxReranker:
    """Local offline reranker running bge-reranker-v2-m3 ONNX model via ONNX Runtime."""

    def __init__(
        self,
        cache_dir: Path,
        settings: Settings,
    ) -> None:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self._np = np
        self._batch_size = settings.rerank_batch_size

        tokenizer_path = cache_dir / "tokenizer.json"
        if not tokenizer_path.exists():
            raise FileNotFoundError(f"Missing tokenizer.json under {cache_dir}")
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))

        model_path = cache_dir / "model.onnx"
        if not model_path.exists():
            candidates = list(cache_dir.glob("*.onnx")) + list(cache_dir.glob("onnx/*.onnx"))
            if candidates:
                model_path = candidates[0]
            else:
                raise FileNotFoundError(f"Missing model.onnx under {cache_dir}")

        opts = ort.SessionOptions()
        opts.enable_mem_pattern = True
        self._session = ort.InferenceSession(
            str(model_path),
            sess_options=opts,
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
        )

    def score(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        scores: list[float] = []
        for i in range(0, len(texts), self._batch_size):
            batch_texts = texts[i : i + self._batch_size]
            pairs = [(query, text) for text in batch_texts]
            encodings = self._tokenizer.encode_batch(pairs)
            max_len = max(len(enc.ids) for enc in encodings)

            input_ids = self._np.zeros((len(encodings), max_len), dtype=self._np.int64)
            attention_mask = self._np.zeros((len(encodings), max_len), dtype=self._np.int64)
            for idx, enc in enumerate(encodings):
                input_ids[idx, : len(enc.ids)] = enc.ids
                attention_mask[idx, : len(enc.attention_mask)] = enc.attention_mask

            inputs = {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
            }
            outputs = self._session.run(None, inputs)
            logits = outputs[0]
            if logits.ndim > 1:
                logits = logits.squeeze(-1)
            # Sigmoid activation for normalized 0..1 relevance scores
            batch_scores = 1.0 / (1.0 + self._np.exp(-logits))
            scores.extend([float(round(s, 4)) for s in batch_scores.tolist()])
        return scores


# ------------------------------------------------------- Dispatch and candidate scoring
def build_reranker(settings: Settings, client: httpx2.Client | None = None) -> Reranker | None:
    """The single dispatch point for reranking. Never branch on reranker flags elsewhere."""
    if not settings.rerank_enabled:
        return None
    if settings.embed_mode == "hash":
        return HashReranker()
    if settings.rerank_base_url or settings.embed_base_url:
        return HttpReranker(settings, client)
    if settings.rerank_cache_dir:
        cache_path = Path(settings.rerank_cache_dir)
        if cache_path.exists():
            return OnnxReranker(cache_path, settings)
    if settings.allow_hash_mode:
        return HashReranker()
    raise RuntimeError(
        "RERANK_ENABLED is true but neither RERANK_BASE_URL nor RERANK_CACHE_DIR is configured. "
        "Set RERANK_BASE_URL or RERANK_CACHE_DIR, or ALLOW_HASH_MODE=true for CI/dev."
    )


def rerank_candidates(
    query: str,
    candidates: Sequence[SearchHit],
    reranker: Reranker,
    top_k: int = 8,
) -> list[SearchHit]:
    """Score candidates using the cross-encoder and sort descending by rerank_score."""
    if not candidates:
        return []
    texts = [format_rerank_text(c) for c in candidates]
    scores = reranker.score(query, texts)
    scored: list[SearchHit] = []
    for cand, score in zip(candidates, scores):
        scored.append(cand.model_copy(update={"rerank_score": score}))
    # Stable sort: rerank_score descending, then original RRF score, then chunk_id
    scored.sort(
        key=lambda h: (
            h.rerank_score if h.rerank_score is not None else float("-inf"),
            h.score,
            h.chunk_id,
        ),
        reverse=True,
    )
    return scored
