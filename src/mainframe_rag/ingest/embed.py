"""Dense embeddings via internal vLLM (OpenAI-compatible) + local BM25 sparse.

Air-gap rule: dense vectors come from the internal endpoint; sparse vectors use
FastEmbed Qdrant/bm25 with weights baked into the image (no runtime download).
Never use Qdrant Cloud Document(model=...) inference. architecture.md 4.4.
"""

from __future__ import annotations

import functools

import httpx

from mainframe_rag.config import Settings
from mainframe_rag.ingest.chunk import Chunk


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


def dense_embed(
    texts: list[str], settings: Settings, client: httpx.Client | None = None
) -> list[list[float]]:
    """POST {EMBED_BASE_URL}/embeddings; returns one vector per input."""
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
    """Local BM25 sparse vectors. Values are (indices, values) per input."""
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
