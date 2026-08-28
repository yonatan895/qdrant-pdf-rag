"""Qdrant I/O: ensure_collection, upsert, delete-by-doc.

Collection mainframe_manuals (architecture.md section 4.3):
- named vector 'dense'  : size=DENSE_DIM, Cosine, on_disk, HNSW m=16 ef=128, int8 scalar quant
- named sparse 'bm25'   : modifier=IDF, on_disk
- payload indexes BEFORE load (unindexed filters become scans)
"""

from __future__ import annotations

from qdrant_client import models

from mainframe_rag.config import Settings
from mainframe_rag.ingest.chunk import Chunk
from mainframe_rag.ingest.embed import build_embed_text
from mainframe_rag.ingest.ibm_pdf import ParsedDoc
from mainframe_rag.ports import QdrantPoints, SparseVector

HNSW_M = 16
HNSW_EF_CONSTRUCT = 128
UPSERT_BATCH = 64

_KEYWORD_INDEXES = ("vendor", "product", "version", "doc_id", "chunk_type", "message_ids", "members", "sha256")


class DimMismatchError(RuntimeError):
    """Existing collection vector size does not match DENSE_DIM."""


def _dense_params(dim: int) -> models.VectorParams:
    return models.VectorParams(
        size=dim,
        distance=models.Distance.COSINE,
        on_disk=True,
        hnsw_config=models.HnswConfigDiff(m=HNSW_M, ef_construct=HNSW_EF_CONSTRUCT),
        quantization_config=models.ScalarQuantization(
            scalar=models.ScalarQuantizationConfig(
                type=models.ScalarType.INT8, quantile=0.99, always_ram=True
            )
        ),
    )


def _sparse_params() -> models.SparseVectorParams:
    return models.SparseVectorParams(
        modifier=models.Modifier.IDF,
        index=models.SparseIndexParams(on_disk=True),
    )


def ensure_payload_indexes(client: QdrantPoints, collection: str) -> None:
    for field in _KEYWORD_INDEXES:
        client.create_payload_index(
            collection, field_name=field, field_schema=models.PayloadSchemaType.KEYWORD
        )
    client.create_payload_index(
        collection, field_name="page_start", field_schema=models.PayloadSchemaType.INTEGER
    )


def ensure_collection(client: QdrantPoints, settings: Settings) -> None:
    """Create collection + payload indexes if missing; verify dim if present."""
    dim = settings.require_dense_dim()
    collection = settings.qdrant_collection

    if client.collection_exists(collection):
        info = client.get_collection(collection)
        dense_cfg = info.config.params.vectors
        if isinstance(dense_cfg, dict):
            actual = dense_cfg.get("dense")
            actual_size = actual.size if actual is not None else None
        else:
            actual_size = dense_cfg.size if dense_cfg is not None else None
        if actual_size != dim:
            raise DimMismatchError(
                f"Collection '{collection}' dense dim is {actual_size}, DENSE_DIM={dim}. "
                "Recreate the collection or fix DENSE_DIM."
            )
        return

    client.create_collection(
        collection,
        vectors_config={"dense": _dense_params(dim)},
        sparse_vectors_config={"bm25": _sparse_params()},
        on_disk_payload=True,
    )
    ensure_payload_indexes(client, collection)


def doc_sha256(client: QdrantPoints, settings: Settings, doc_id: str) -> str | None:
    """Stored sha256 for doc_id (first hit), or None if the doc is absent."""
    points, _ = client.scroll(
        settings.qdrant_collection,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))]
        ),
        limit=1,
        with_payload=["sha256"],
    )
    if not points:
        return None
    return (points[0].payload or {}).get("sha256")


def delete_by_doc(client: QdrantPoints, settings: Settings, doc_id: str) -> None:
    client.delete(
        settings.qdrant_collection,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))]
            )
        ),
        wait=True,
    )


def upsert_chunks(
    client: QdrantPoints,
    settings: Settings,
    parsed: ParsedDoc,
    chunks: list[Chunk],
    vectors: list[tuple[list[float], SparseVector]],
) -> int:
    """Upsert chunk points in batches of UPSERT_BATCH. Returns point count."""
    collection = settings.qdrant_collection
    points: list[models.PointStruct] = []
    for chunk, (dense, (sparse_idx, sparse_val)) in zip(chunks, vectors):
        payload = {
            "vendor": parsed.vendor,
            "product": parsed.product,
            "version": parsed.version,
            "doc_id": chunk.doc_id,
            "title": parsed.title,
            "heading_path": chunk.heading_path,
            "page_label": chunk.page_label,
            "page_start": chunk.page_start,
            "chunk_type": chunk.chunk_type,
            "message_ids": chunk.message_ids,
            "members": chunk.members,
            "sha256": parsed.sha256,
            "text": chunk.text,
            "embed_text": build_embed_text(
                parsed.product, parsed.version, chunk.doc_id, parsed.title,
                chunk.heading_path, chunk.text,
            ),
        }
        points.append(
            models.PointStruct(
                id=chunk.chunk_id,
                vector={
                    "dense": dense,
                    "bm25": models.SparseVector(indices=sparse_idx, values=sparse_val),
                },
                payload=payload,
            )
        )

    for i in range(0, len(points), UPSERT_BATCH):
        client.upsert(collection, points=points[i : i + UPSERT_BATCH], wait=True)
    return len(points)
