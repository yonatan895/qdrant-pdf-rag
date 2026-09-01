"""Embeddings behind the Embedder protocol (issue #20 PR A).

Two implementations, dispatched ONCE by build_embedder():
- VllmEmbedder: dense via the internal vLLM endpoint (OpenAI-compatible),
  sparse via local FastEmbed Qdrant/bm25 (weights baked into the image).
  Prod path; requires EMBED_BASE_URL / EMBED_MODEL (fail fast at build).
- HashEmbedder: deterministic in-process hashing (blake2b bag of words,
  HASH_EMBED_DIM dense + sparse counts). CI/dev only — never in prod.

Air-gap rules preserved: no Qdrant Cloud Document(model=...) inference, no
runtime weight downloads, no network in hash mode. architecture.md 4.4.
"""

from __future__ import annotations

import functools
import hashlib
import math
import re

import httpx2

from mainframe_rag.config import HASH_EMBED_DIM, Settings
from mainframe_rag.ingest.chunk import Chunk
from mainframe_rag.ports import Embedder, SparseVector

_TOKEN_RE = re.compile(r"[A-Za-z0-9]{2,}")
_DENSE_PERSON = b"dense"
_SPARSE_PERSON = b"spars"


def build_embed_text(
    product: str | None,
    version: str | None,
    doc_id: str,
    title: str,
    heading_path: str,
    body: str,
) -> str:
    """Embed this string, not the body alone (architecture.md section 4.2)."""
    header = " ".join(p for p in (product, version, doc_id) if p)
    return "\n".join(p for p in (header, title, heading_path, body) if p)


def chunk_embed_text(chunk: Chunk, product: str | None, version: str | None, title: str) -> str:
    return build_embed_text(product, version, chunk.doc_id, title, chunk.heading_path, chunk.text)


# ------------------------------------------------------- hash implementation
def _token_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for tok in _TOKEN_RE.findall(text):
        low = tok.lower()
        counts[low] = counts.get(low, 0) + 1
    return counts


def _bucket(token: str, person: bytes, dim: int) -> int:
    digest = hashlib.blake2b(token.encode(), digest_size=8, person=person).digest()
    return int.from_bytes(digest, "big") % dim


def hash_dense_embed(texts: list[str], dim: int = HASH_EMBED_DIM) -> list[list[float]]:
    """Deterministic hashed bag-of-words dense vectors, L2 normalized."""
    out: list[list[float]] = []
    for text in texts:
        vec = [0.0] * dim
        for token, count in _token_counts(text).items():
            vec[_bucket(token, _DENSE_PERSON, dim)] += 1.0 + math.log(count)
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        out.append(vec)
    return out


def hash_sparse_embed(texts: list[str]) -> list[SparseVector]:
    """Deterministic hashed sparse count vectors (Qdrant applies IDF)."""
    out: list[SparseVector] = []
    for text in texts:
        buckets: dict[int, float] = {}
        for token, count in _token_counts(text).items():
            idx = _bucket(token, _SPARSE_PERSON, 1 << 31)
            buckets[idx] = buckets.get(idx, 0.0) + float(count)
        indices = sorted(buckets)
        out.append((indices, [buckets[i] for i in indices]))
    return out


class HashEmbedder:
    """CI/dev-only implementer: no network, no weights, no vLLM env needed."""

    def dense(self, texts: list[str]) -> list[list[float]]:
        return hash_dense_embed(texts)

    def dense_query(self, queries: list[str]) -> list[list[float]]:
        return self.dense(queries)

    def sparse(self, texts: list[str]) -> list[SparseVector]:
        return hash_sparse_embed(texts)


# ------------------------------------------------------- vLLM implementation
@functools.lru_cache(maxsize=1)
def _bm25_model(model_name: str, cache_dir: str | None):
    from fastembed import SparseTextEmbedding

    return SparseTextEmbedding(model_name=model_name, cache_dir=cache_dir)


class VllmEmbedder:
    """Prod implementer: dense from the internal vLLM endpoint, sparse from
    local BM25. One shared httpx2.Client per instance. Endpoint/model resolve
    lazily via require_embed — the agent's startup fail-fast (PR D) validates
    them before listening; ingest workers validate on first use."""

    def __init__(self, settings: Settings, client: httpx2.Client | None = None) -> None:
        self._settings = settings
        self._base_url: str | None = None
        self._model: str | None = None
        self._bm25_model_name = settings.bm25_model
        self._bm25_cache_dir = settings.bm25_cache_dir
        self._client = client

    def _resolve(self) -> tuple[str, str]:
        if self._base_url is None or self._model is None:
            self._base_url, self._model = self._settings.require_embed()
        return self._base_url, self._model

    def _http(self) -> httpx2.Client:
        if self._client is None:
            # Bounded connect retries (fire only if the request was never
            # sent); no request-level retries on POST /embeddings.
            self._client = httpx2.Client(
                timeout=self._settings.embed_timeout_s,
                transport=httpx2.HTTPTransport(retries=self._settings.http_connect_retries),
            )
        return self._client

    def dense(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        base_url, model = self._resolve()
        resp = self._http().post(
            f"{base_url.rstrip('/')}/embeddings",
            json={"model": model, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        ordered = sorted(data, key=lambda d: d["index"])
        return [d["embedding"] for d in ordered]

    def dense_query(self, queries: list[str]) -> list[list[float]]:
        if not queries:
            return []
        prefix = self._settings.dense_query_prefix
        prefixed = [f"{prefix}{q}" for q in queries] if prefix else queries
        return self.dense(prefixed)

    def sparse(self, texts: list[str]) -> list[SparseVector]:
        if not texts:
            return []
        model = _bm25_model(self._bm25_model_name, self._bm25_cache_dir)
        return [(doc.indices.tolist(), doc.values.tolist()) for doc in model.embed(texts)]


def build_embedder(settings: Settings, client: httpx2.Client | None = None) -> Embedder:
    """The single dispatch point for embed_mode. Never branch on embed_mode
    anywhere else. Construction is cheap and side-effect free; env fail-fast
    happens on first use (preserving current request-time behavior)."""
    if settings.embed_mode == "hash":
        return HashEmbedder()
    return VllmEmbedder(settings, client)


def embed_batch(
    chunks: list[Chunk],
    product: str | None,
    version: str | None,
    title: str,
    embedder: Embedder,
) -> list[tuple[list[float], SparseVector]]:
    """Dense + sparse vectors for a batch of chunks, aligned by index."""
    texts = [chunk_embed_text(c, product, version, title) for c in chunks]
    dense = embedder.dense(texts)
    sparse = embedder.sparse(texts)
    return list(zip(dense, sparse))
