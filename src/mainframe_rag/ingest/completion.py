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
from mainframe_rag.ingest.identity import AmbiguousRevisionError
from mainframe_rag.ingest.qdrant_io import collection_vector_configs, stored_doc_revisions
from mainframe_rag.ingest.representation import manifest_digest
from mainframe_rag.ports import QdrantPoints

_COMPLETION_SUFFIX = "__completions"

_COMPLETION_KEYWORD_INDEXES = ("doc_id", "sha256", "rules_v", "generation_id", "target_collection")

# Marker-scan bound (issue #361): markers per doc_id are few (one per
# committed revision per CLI-triple variant). A scroll cap keeps revision
# reads bounded; past it the run fails closed via the caller's
# verification, never by silently missing a marker.
_MARKER_SCAN_LIMIT = 100


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
    # Representation contract this generation was verified under (issue
    # #362): ties the completion to the manifest. Pre-manifest markers
    # carry None — an explicit legacy outcome in the 362B gate, never a pass.
    manifest_digest: str | None = None
    # Source revision this marker certifies (issue #361): markers are
    # per-revision, so coexisting revisions under one doc_id verify
    # independently. Pre-361B markers carry None (legacy).
    source_rev: str | None = None
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


def completion_point_id(
    target_collection: str, doc_id: str, source_rev: str | None, generation_id: str
) -> str:
    """Deterministic, idempotent completion point id (chunk UUID5 untouched).

    The id scopes to the source revision (issue #361): two revisions
    sharing a doc_id must not overwrite each other's markers. Pre-361B
    markers used the shorter (target, doc_id, generation) key and are
    unreachable under the new scheme — they read as absent (one re-ingest
    cycle rewrites them), never as valid.
    """
    rev = source_rev if source_rev is not None else ""
    key = f"completion|{target_collection}|{doc_id}|{rev}|{generation_id}"
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


def _doc_markers(
    client: QdrantPoints, settings: Settings, doc_id: str
) -> list[CompletionRecord]:
    """All parseable markers under a doc_id (bounded scan). Corrupt payloads
    read as absent — a corrupt marker is a legacy outcome, not a crash."""
    name = completion_collection_name(settings)
    if not client.collection_exists(name):
        return []
    points, _ = client.scroll(
        name,
        scroll_filter=_doc_id_filter(doc_id),
        limit=_MARKER_SCAN_LIMIT,
        with_payload=True,
    )
    markers: list[CompletionRecord] = []
    for p in points:
        try:
            markers.append(CompletionRecord.model_validate(p.payload or {}))
        except (ValidationError, ValueError):
            continue
    return markers


def read_completion(
    client: QdrantPoints,
    settings: Settings,
    doc_id: str,
    *,
    source_rev: str,
    generation_id: str,
) -> CompletionRecord | None:
    """Scoped marker for one revision generation, else None. Exact match on
    (revision, generation, target) — coexisting revisions never cross-read,
    and a generation certified under another CLI triple never satisfies."""
    for m in _doc_markers(client, settings, doc_id):
        if (
            m.source_rev == source_rev
            and m.generation_id == generation_id
            and m.target_collection == settings.qdrant_collection
        ):
            return m
    return None


def legacy_markers(
    client: QdrantPoints, settings: Settings, doc_id: str
) -> list[CompletionRecord]:
    """Pre-361B markers (no source_rev) under a doc_id. Only
    is_doc_complete's legacy rule interprets them."""
    return [m for m in _doc_markers(client, settings, doc_id) if m.source_rev is None]


def write_completion(
    client: QdrantPoints,
    settings: Settings,
    *,
    doc_id: str,
    sha256: str,
    rules_v: str,
    source_labels: str,
    source_rev: str,
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
        manifest_digest=manifest_digest(settings, rules_v),
        source_rev=source_rev,
    )
    dummy_dim = dim or 1
    point = models.PointStruct(
        id=completion_point_id(settings.qdrant_collection, doc_id, source_rev, generation_id),
        vector={
            "dense": [0.0] * dummy_dim,
            "bm25": models.SparseVector(indices=[0], values=[1.0]),
        },
        payload=record.model_dump(),
    )
    client.upsert(name, points=[point], wait=True)
    return record


def delete_completion(
    client: QdrantPoints,
    settings: Settings,
    doc_id: str,
    *,
    source_rev: str,
    include_legacy: bool = False,
) -> None:
    """Invalidate a revision's markers before its refresh deletes/replaces
    points. Markers are selected client-side and deleted by point id —
    precise, never doc_id-wide (a doc_id-wide delete would wipe coexisting
    revisions' markers). include_legacy additionally drops sourceless
    pre-361B markers; callers set it only when the refresh plan proved sole
    history (plan_refresh_deletes), never beside named others.

    A failed refresh must leave NO valid completion (safe retry), never a
    stale marker over partial data.
    """
    name = completion_collection_name(settings)
    if not client.collection_exists(name):
        return
    points, _ = client.scroll(
        name,
        scroll_filter=_doc_id_filter(doc_id),
        limit=_MARKER_SCAN_LIMIT,
        with_payload=["source_rev"],
    )
    ids: list[int | str | uuid.UUID] = []
    for p in points:
        rev = (p.payload or {}).get("source_rev")
        if rev == source_rev or (include_legacy and rev is None):
            ids.append(p.id)
    if ids:
        client.delete(
            name,
            points_selector=models.PointIdsList(points=ids),
            wait=True,
        )


def _rev_filter(doc_id: str, source_rev: str) -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id)),
            models.FieldCondition(key="source_rev", match=models.MatchValue(value=source_rev)),
        ]
    )


def _match_digests(
    ids: list[str],
    texts: dict[str, str],
    chunk_ids_digest: str,
    content_digest: str,
) -> bool:
    """ID/content digest check shared by both verify paths (one rule)."""
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


def verify_doc_points(
    client: QdrantPoints,
    settings: Settings,
    doc_id: str,
    *,
    sha256: str,
    rules_v: str,
    source_rev: str,
    expected_chunks: int,
    chunk_ids_digest: str,
    content_digest: str,
) -> bool:
    """True only when the stored points exactly match the expected revision
    generation.

    Scoped path first: a revision-keyed scroll (limit = expected + 1, so
    any surplus is observed, not paged past) — coexisting sibling revisions
    under the same doc_id never disturb the count. A stamped point from a
    DIFFERENT revision fails the verify (mixed generations must never
    verify). When nothing is stamped (pure legacy history), the doc_id
    scroll applies the same checks while tolerating absent stamps — the
    legacy-sole admission rule lives in is_doc_complete, not here.
    """
    if expected_chunks < 1:
        return False
    scoped, _ = client.scroll(
        settings.qdrant_collection,
        scroll_filter=_rev_filter(doc_id, source_rev),
        limit=expected_chunks + 1,
        with_payload=["sha256", "rules_v", "source_rev", "text"],
    )
    if scoped:
        if len(scoped) != expected_chunks:
            return False
        return _verify_batch(scoped, sha256, rules_v, source_rev, chunk_ids_digest, content_digest)
    points, _ = client.scroll(
        settings.qdrant_collection,
        scroll_filter=_doc_id_filter(doc_id),
        limit=expected_chunks + 1,
        with_payload=["sha256", "rules_v", "source_rev", "text"],
    )
    if len(points) != expected_chunks:
        return False
    return _verify_batch(points, sha256, rules_v, source_rev, chunk_ids_digest, content_digest)


def _verify_batch(
    points: list,
    sha256: str,
    rules_v: str,
    source_rev: str,
    chunk_ids_digest: str,
    content_digest: str,
) -> bool:
    """Per-point sha/rules/revision checks plus the shared digest match."""
    ids: list[str] = []
    texts: dict[str, str] = {}
    for p in points:
        payload = p.payload or {}
        if payload.get("sha256") != sha256 or payload.get("rules_v") != rules_v:
            return False
        rev = payload.get("source_rev")
        if rev is not None and rev != source_rev:
            return False
        pid = str(p.id)
        ids.append(pid)
        texts[pid] = str(payload.get("text") or "")
    ids.sort()
    return _match_digests(ids, texts, chunk_ids_digest, content_digest)


def is_doc_complete(
    client: QdrantPoints,
    settings: Settings,
    doc_id: str,
    *,
    sha256: str,
    rules_v: str,
    source_labels: str,
    source_rev: str,
) -> bool:
    """Skip gate: valid completion + verified points, same revision generation.

    Scoped path: the revision's own marker for the expected generation.
    Legacy path (lazy upgrade — unchanged docs skip without mass
    re-ingest): a sourceless marker certifies only when no named revision
    other than this one lives under the doc_id; mixed legacy+named fails
    toward re-ingest, never a wrong skip.
    """
    expected_gen = doc_generation_id(settings, sha256, rules_v, source_labels)
    scoped = read_completion(
        client, settings, doc_id, source_rev=source_rev, generation_id=expected_gen
    )
    if scoped is not None:
        if scoped.sha256 != sha256 or scoped.rules_v != rules_v:
            return False
        return verify_doc_points(
            client,
            settings,
            doc_id,
            sha256=sha256,
            rules_v=rules_v,
            source_rev=source_rev,
            expected_chunks=scoped.expected_chunks,
            chunk_ids_digest=scoped.chunk_ids_digest,
            content_digest=scoped.content_digest,
        )
    for marker in legacy_markers(client, settings, doc_id):
        if (
            marker.target_collection != settings.qdrant_collection
            or marker.sha256 != sha256
            or marker.rules_v != rules_v
            or marker.generation_id != expected_gen
        ):
            continue
        revisions = stored_doc_revisions(client, settings, doc_id)
        if any(r is not None and r != source_rev for r in revisions):
            return False
        return verify_doc_points(
            client,
            settings,
            doc_id,
            sha256=sha256,
            rules_v=rules_v,
            source_rev=source_rev,
            expected_chunks=marker.expected_chunks,
            chunk_ids_digest=marker.chunk_ids_digest,
            content_digest=marker.content_digest,
        )
    return False


def is_revision_committed(
    client: QdrantPoints, settings: Settings, doc_id: str, source_rev: str
) -> bool:
    """Self-consistent completeness of one revision under its OWN committed
    generation (no current-run inputs needed): any scoped marker whose
    points verify. Crash residue never passes — refresh wipes markers
    before points, so partial data has no marker to validate."""
    for m in _doc_markers(client, settings, doc_id):
        if m.source_rev != source_rev or m.target_collection != settings.qdrant_collection:
            continue
        if verify_doc_points(
            client,
            settings,
            doc_id,
            sha256=m.sha256,
            rules_v=m.rules_v,
            source_rev=source_rev,
            expected_chunks=m.expected_chunks,
            chunk_ids_digest=m.chunk_ids_digest,
            content_digest=m.content_digest,
        ):
            return True
    return False


def plan_refresh_deletes(
    client: QdrantPoints,
    settings: Settings,
    doc_id: str,
    source_rev: str,
    lineage_rev: str | None,
) -> tuple[set[str], bool]:
    """Delete plan for a refresh (issue #361). Returns (revision deletes,
    legacy_doc_delete). Raises BEFORE any delete — callers delete only
    after this returns:

    - a stale current revision is always replaced (a valid one would have
      skipped already, except under --reingest);
    - inventory lineage replaces precisely (the lineage revision, if
      present and different);
    - committed coexisting revisions are left alone — replacing one needs
      its lineage (replacement without lineage would be the overwrite bug);
    - completion-less residue is deleted (crash-safe retry);
    - unattributable sourceless residue beside named revisions raises
      AmbiguousRevisionError (fail closed: deleting by doc_id could wipe a
      live revision, leaving it serves mixed generations).
    """
    present = stored_doc_revisions(client, settings, doc_id)
    dels = {source_rev} if source_rev in present else set()
    rest = present - {source_rev}
    named = {r for r in rest if r is not None}
    legacy = None in rest
    if lineage_rev is not None and lineage_rev != source_rev and lineage_rev in present:
        dels.add(lineage_rev)
        named.discard(lineage_rev)
    if legacy and not named:
        return dels, True
    if legacy:
        raise AmbiguousRevisionError(doc_id, sorted(named), _stray_sha16s(client, settings, doc_id))
    if lineage_rev is None:
        # No lineage: committed others are coexistence (leave); residue goes.
        for other in sorted(named):
            if not is_revision_committed(client, settings, doc_id, other):
                dels.add(other)
    return dels, False


def _stray_sha16s(client: QdrantPoints, settings: Settings, doc_id: str) -> list[str]:
    """Distinct content ids of sourceless points under a doc_id (raise path
    only): paginated like the revision scan, deterministic order."""
    shas: set[str] = set()
    offset: int | str | uuid.UUID | None = None
    while True:
        points, offset = client.scroll(
            settings.qdrant_collection,
            scroll_filter=_doc_id_filter(doc_id),
            limit=_MARKER_SCAN_LIMIT,
            with_payload=["source_rev", "sha256"],
            offset=offset,
        )
        for p in points:
            payload = p.payload or {}
            if payload.get("source_rev") is None and payload.get("sha256"):
                shas.add(str(payload["sha256"])[:16])
        if offset is None or not points:
            break
    return sorted(shas)


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
