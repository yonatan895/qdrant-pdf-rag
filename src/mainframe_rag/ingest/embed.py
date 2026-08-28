"""Dense embeddings via internal vLLM (OpenAI-compatible) + local BM25 sparse.

Air-gap rule: dense vectors come from the internal endpoint; sparse vectors use
FastEmbed Qdrant/bm25 with weights baked into the image (no runtime download).
Never use Qdrant Cloud Document(model=...) inference. architecture.md 4.4.

EMBED_MODE=hash (issue #8): minimal deterministic in-process embedder for CI
and local dev. Hashes tokens into a fixed-dim dense bag-of-words vector (L2
normalized) and a sparse count vector, using blake2b so results are stable
across processes and runs. No network, no weights, no vLLM dependency. It is
NOT a semantic model: only lexical overlap works. Never the default in prod.
"""

from __future__ import annotations

import functools
import hashlib
import math
import re

import httpx

from mainframe_rag.config import HASH_EMBED_DIM, Settings
from mainframe_rag.ingest.chunk import Chunk

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


# --------------------------------------------------------------------- hash mode
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


def hash_sparse_embed(texts: list[str]) -> list[tuple[list[int], list[float]]]:
    """Deterministic hashed sparse count vectors (Qdrant applies IDF)."""
    out: list[tuple[list[int], list[float]]] = []
    for text in texts:
        buckets: dict[int, float] = {}
        for token, count in _token_counts(text).items():
            idx = _bucket(token, _SPARSE_PERSON, 1 << 31)
            buckets[idx] = buckets.get(idx, 0.0) + float(count)
        indices = sorted(buckets)
        out.append((indices, [buckets[i] for i in indices]))
    return out


# ------------------------------------------------------------------- dispatch
def dense_embed(
    texts: list[str], settings: Settings, client: httpx.Client | None = None
) -> list[list[float]]:
    """Hash mode embeds locally; otherwise POST {EMBED_BASE_URL}/embeddings."""
    if settings.embed_mode == "hash":
        return hash_dense_embed(texts)
    base_url, model = settings.require_embed()
    if not texts:
        return []

    own_client = client is None
    client = client or httpx.Client(timeout=settings.embed_timeout_s)
    try:
        resp = client.post(
            f"{base_url.rstrip('/')}/embeddings",
            json={"model": model, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        ordered = sorted(data, key=lambda d: d["index"])
        return [d["embedding"] for d in ordered]
    finally:
        if own_client:
            client.close()


@functools.lru_cache(maxsize=1)
def _bm25_model(model_name: str, cache_dir: str | None):
    from fastembed import SparseTextEmbedding

    return SparseTextEmbedding(model_name=model_name, cache_dir=cache_dir)


def sparse_embed(
    texts: list[str], settings: Settings
) -> list[tuple[list[int], list[float]]]:
    """Hash mode embeds locally; otherwise local BM25 sparse vectors."""
    if settings.embed_mode == "hash":
        return hash_sparse_embed(texts)
    if not texts:
        return []
    model = _bm25_model(settings.bm25_model, settings.bm25_cache_dir)
    results: list[tuple[list[int], list[float]]] = []
    for doc in list(model.embed(texts)):
        results.append((doc.indices.tolist(), doc.values.tolist()))
    return results


def embed_batch(
    chunks: list[Chunk], product: str | None, version: str | None, title: str,
    settings: Settings, client: httpx.Client | None = None,
) -> list[tuple[list[float], list[int], list[float]]]:
    """Dense + sparse vectors for a batch of chunks, aligned by index."""
    texts = [chunk_embed_text(c, product, version, title) for c in chunks]
    dense = dense_embed(texts, settings, client)
    sparse = sparse_embed(texts, settings)
    return list(zip(dense, [s[0] for s in sparse], [s[1] for s in sparse]))
