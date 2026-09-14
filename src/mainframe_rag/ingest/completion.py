"""Document-generation completion records (issue #359).

Deterministic chunk UUID5s permit replay; they do not prove completeness.
A single surviving point (or an unbound inventory line) must never cause a
skip. Each successfully published document generation therefore carries an
explicit completion point in a separate collection derived from the target:

    <qdrant_collection>__completions

The completion is written only after every expected batch is acknowledged
and the stored points verify (count + chunk-ID/content digests). Every skip
path requires a valid completion tied to the actual target generation;
missing, legacy, or mismatched completions re-ingest, never skip.

In-place runs still delete before re-upserting, so a crash mid-refresh
leaves no completion (safe retry) but no atomic old-or-new visibility —
that needs the versioned-collection + alias publication path
(`ingest/publish.py`, `INGEST_ALIAS_PUBLISH`). Rollback/GC are deliberate
operator actions.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import time
import uuid
from pathlib import Path
from typing import IO, Any

from pydantic import BaseModel, Field, ValidationError
from qdrant_client import models

from mainframe_rag.config import Settings
from mainframe_rag.ingest.chunk import Chunk
from mainframe_rag.ingest.qdrant_io import collection_vector_configs
from mainframe_rag.ports import QdrantPoints

_COMPLETION_SUFFIX = "__completions"

_COMPLETION_KEYWORD_INDEXES = ("doc_id", "sha256", "rules_v", "generation_id", "target_collection")


class CompletionRecord(BaseModel):
    """Verified publication marker for one document generation."""

    doc_id: str
    sha256: str
    rules_v: str
    target_collection: str
    generation_id: str
    expected_chunks: int = Field(ge=1)
    chunk_ids_digest: str
    content_digest: str
    embed_mode: str = ""
    embed_model: str | None = None
    dense_dim: int | None = None
    finished_at: float = Field(default_factory=time.time)


def completion_collection_name(settings: Settings) -> str:
    """Separate collection tied to the actual target (req 3)."""
    return completion_collection_for(settings.qdrant_collection)


def completion_collection_for(collection: str) -> str:
    """Completion collection for an arbitrary physical name (publish staging)."""
    return f"{collection}{_COMPLETION_SUFFIX}"


def representation_fingerprint(settings: Settings, rules_v: str) -> str:
    """Generation identity for the stored representation (req 1).

    Extensible `|`-joined fingerprint: extraction rules + embed coordinates
    that change stored vectors. Coordinate format changes with #362 owners;
    adding segments is backward-compatible because equality is exact-match
    and mismatches re-ingest (never skip).
    """
    try:
        dim = settings.require_dense_dim()
    except RuntimeError:
        dim = None
    return "|".join(
        [
            rules_v,
            settings.embed_mode,
            settings.embed_model or "",
            str(dim) if dim is not None else "",
            "ctx1" if settings.contextual_embed_enabled else "ctx0",
        ]
    )


def doc_generation_id(settings: Settings, sha256: str, rules_v: str, source_labels: str) -> str:
    """Bind a source revision to its representation generation.

    source_labels is the `source_labels()` CLI triple: vendor/product/version
    overrides change point payloads AND embed headers, so a generation
    certified under one triple must never satisfy a run under another.
    Pre-triple markers carry a shorter id and mismatch exactly once
    (fail-closed re-ingest, never a wrong skip)."""
    return f"{sha256}|{representation_fingerprint(settings, rules_v)}|{source_labels}"


def source_labels(vendor: str | None, product: str | None, version: str | None) -> str:
    """CLI source triple (one rule per concept): the only ingest input that
    varies independently of file bytes. Path/text-derived labels are
    deterministic functions of (path, content) and need no separate binding."""
    return f"{vendor or ''}|{product or ''}|{version or ''}"


def expected_digests(chunks: list[Chunk]) -> tuple[int, str, str]:
    """(count, chunk-IDs digest, content digest) for a chunk list.

    Sorted by chunk_id so point order never matters. Content digest covers
    chunk_id + text per chunk; vectors are derived from the same text, so
    text equality plus count plus ID equality establishes the representation
    for the verify pass.
    """
    ids = sorted(c.chunk_id for c in chunks)
    h_ids = hashlib.sha256()
    for cid in ids:
        h_ids.update(cid.encode("utf-8"))
        h_ids.update(b"\0")
    by_id = {c.chunk_id: c.text for c in chunks}
    h_content = hashlib.sha256()
    for cid in ids:
        h_content.update(cid.encode("utf-8"))
        h_content.update(b"\0")
        h_content.update(by_id[cid].encode("utf-8"))
        h_content.update(b"\0")
    return len(chunks), h_ids.hexdigest(), h_content.hexdigest()


def completion_point_id(target_collection: str, doc_id: str, generation_id: str) -> str:
    """Deterministic, idempotent completion point id (chunk UUID5 untouched)."""
    key = f"completion|{target_collection}|{doc_id}|{generation_id}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def ensure_completion_collection(client: QdrantPoints, settings: Settings) -> str:
    """Create the completions collection + indexes if missing (idempotent)."""
    name = completion_collection_name(settings)
    if client.collection_exists(name):
        for field in _COMPLETION_KEYWORD_INDEXES:
            client.create_payload_index(
                name, field_name=field, field_schema=models.PayloadSchemaType.KEYWORD
            )
        return name
    dim = settings.require_dense_dim()
    vectors_config, sparse_vectors_config = collection_vector_configs(dim)
    client.create_collection(
        name,
        vectors_config=vectors_config,
        sparse_vectors_config=sparse_vectors_config,
        on_disk_payload=True,
    )
    for field in _COMPLETION_KEYWORD_INDEXES:
        client.create_payload_index(
            name, field_name=field, field_schema=models.PayloadSchemaType.KEYWORD
        )
    return name


def _doc_id_filter(doc_id: str) -> models.Filter:
    return models.Filter(
        must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))]
    )


def read_completion(
    client: QdrantPoints, settings: Settings, doc_id: str
) -> CompletionRecord | None:
    """Latest completion for doc_id, or None when absent/legacy (fail closed)."""
    name = completion_collection_name(settings)
    if not client.collection_exists(name):
        return None
    points, _ = client.scroll(
        name,
        scroll_filter=_doc_id_filter(doc_id),
        limit=1,
        with_payload=True,
    )
    if not points:
        return None
    payload = points[0].payload or {}
    try:
        return CompletionRecord.model_validate(payload)
    except (ValidationError, ValueError):
        return None


def write_completion(
    client: QdrantPoints,
    settings: Settings,
    *,
    doc_id: str,
    sha256: str,
    rules_v: str,
    source_labels: str,
    expected_chunks: int,
    chunk_ids_digest: str,
    content_digest: str,
) -> CompletionRecord:
    """Persist the completion only after verification (caller verifies first)."""
    name = completion_collection_name(settings)
    generation_id = doc_generation_id(settings, sha256, rules_v, source_labels)
    try:
        dim = settings.require_dense_dim()
    except RuntimeError:
        dim = None
    record = CompletionRecord(
        doc_id=doc_id,
        sha256=sha256,
        rules_v=rules_v,
        target_collection=settings.qdrant_collection,
        generation_id=generation_id,
        expected_chunks=expected_chunks,
        chunk_ids_digest=chunk_ids_digest,
        content_digest=content_digest,
        embed_mode=settings.embed_mode,
        embed_model=settings.embed_model,
        dense_dim=dim,
    )
    dummy_dim = dim or 1
    point = models.PointStruct(
        id=completion_point_id(settings.qdrant_collection, doc_id, generation_id),
        vector={
            "dense": [0.0] * dummy_dim,
            "bm25": models.SparseVector(indices=[0], values=[1.0]),
        },
        payload=record.model_dump(),
    )
    client.upsert(name, points=[point], wait=True)
    return record


def delete_completion(client: QdrantPoints, settings: Settings, doc_id: str) -> None:
    """Invalidate a generation before a refresh deletes/replaces points.

    A failed refresh must leave NO valid completion (safe retry), never a
    stale marker over partial data.
    """
    name = completion_collection_name(settings)
    if not client.collection_exists(name):
        return
    client.delete(
        name,
        points_selector=models.FilterSelector(filter=_doc_id_filter(doc_id)),
        wait=True,
    )


def verify_doc_points(
    client: QdrantPoints,
    settings: Settings,
    doc_id: str,
    *,
    sha256: str,
    rules_v: str,
    expected_chunks: int,
    chunk_ids_digest: str,
    content_digest: str,
) -> bool:
    """True only when the stored points exactly match the expected generation.

    Single scroll bounded by the document itself (limit = expected + 1, so
    any surplus is observed, not paged past). Checks count, per-point
    sha256/rules_v, and recomputed ID/content digests.
    """
    if expected_chunks < 1:
        return False
    points, _ = client.scroll(
        settings.qdrant_collection,
        scroll_filter=_doc_id_filter(doc_id),
        limit=expected_chunks + 1,
        with_payload=["sha256", "rules_v", "text"],
    )
    if len(points) != expected_chunks:
        return False
    ids: list[str] = []
    texts: dict[str, str] = {}
    for p in points:
        payload = p.payload or {}
        if payload.get("sha256") != sha256 or payload.get("rules_v") != rules_v:
            return False
        pid = str(p.id)
        ids.append(pid)
        texts[pid] = str(payload.get("text") or "")
    ids.sort()
    h_ids = hashlib.sha256()
    for cid in ids:
        h_ids.update(cid.encode("utf-8"))
        h_ids.update(b"\0")
    if h_ids.hexdigest() != chunk_ids_digest:
        return False
    h_content = hashlib.sha256()
    for cid in ids:
        h_content.update(cid.encode("utf-8"))
        h_content.update(b"\0")
        h_content.update(texts[cid].encode("utf-8"))
        h_content.update(b"\0")
    return h_content.hexdigest() == content_digest


def is_doc_complete(
    client: QdrantPoints,
    settings: Settings,
    doc_id: str,
    *,
    sha256: str,
    rules_v: str,
    source_labels: str,
) -> bool:
    """Skip gate (req 3): valid completion + verified points, same generation."""
    completion = read_completion(client, settings, doc_id)
    if completion is None:
        return False
    if completion.target_collection != settings.qdrant_collection:
        return False
    if completion.sha256 != sha256 or completion.rules_v != rules_v:
        return False
    if completion.generation_id != doc_generation_id(settings, sha256, rules_v, source_labels):
        return False
    return verify_doc_points(
        client,
        settings,
        doc_id,
        sha256=sha256,
        rules_v=rules_v,
        expected_chunks=completion.expected_chunks,
        chunk_ids_digest=completion.chunk_ids_digest,
        content_digest=completion.content_digest,
    )


def acquire_run_lock(progress_path: Path) -> IO[Any]:
    """Cross-process single-writer guard (req 6): fcntl LOCK_EX|LOCK_NB.

    Held for the whole run; a second process sharing the progress directory
    fails closed instead of interleaving check-delete-upsert sequences.
    Distributed Jobs on disjoint filesystems must still run serially
    (operator discipline; documented in docs/ingest.md).
    """
    lock_path = progress_path.with_name(progress_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "w", encoding="utf-8")  # noqa: SIM115 — handle outlives the call
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError(
            f"another ingest run holds {lock_path} — concurrent ingest Jobs "
            "for the same collection are rejected; run serially."
        ) from exc
    return handle


def release_run_lock(handle: IO[Any]) -> None:
    """Release the run lock (idempotent, never raises)."""
    with contextlib.suppress(Exception):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    with contextlib.suppress(Exception):
        handle.close()
