"""Qdrant I/O: ensure_collection, upsert, delete-by-doc.

Collection mainframe_manuals (architecture.md section 4.3):
- named vector 'dense'  : size=DENSE_DIM, Cosine, on_disk, HNSW m=16 ef=128, int8 scalar quant
- named sparse 'bm25'   : modifier=IDF, on_disk
- payload indexes BEFORE load (unindexed filters become scans)
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from qdrant_client import models

from mainframe_rag.config import Settings
from mainframe_rag.ingest.chunk import Chunk
from mainframe_rag.ingest.ibm_pdf import ParsedDoc
from mainframe_rag.ingest.identity import source_rev_key
from mainframe_rag.ingest.rules_version import extraction_rules_version
from mainframe_rag.ports import QdrantPoints, SparseVector

HNSW_M = 16
HNSW_EF_CONSTRUCT = 128
# Bulk-load guidance (Qdrant skill): raise indexing_threshold so HNSW builds
# do not compete with upserts, restore to the server default afterwards.
# Never m=0 — that drops existing HNSW on an existing collection.
BULK_INDEXING_THRESHOLD_KB = 1 << 30
DEFAULT_INDEXING_THRESHOLD_KB = 20000

_KEYWORD_INDEXES = ("vendor", "product", "version", "doc_id", "chunk_type", "message_ids", "members", "sha256", "source_rev")


def scroll_all_points(
    client: QdrantPoints,
    collection: str,
    *,
    scroll_filter: models.Filter | None,
    with_payload: bool | list[str],
    page_size: int,
) -> list[models.Record]:
    """One rule for every paginated observer scan (issue #361 review): page
    through scroll to exhaustion with the caller's filter. The page size is
    a throughput knob (`Settings.ingest_scan_page_size`); listings never cap
    at a fixed count — a truncated scan would miss revisions or markers and
    mis-target deletes."""
    gathered: list[models.Record] = []
    offset: int | str | UUID | None = None
    while True:
        page, offset = client.scroll(
            collection,
            scroll_filter=scroll_filter,
            limit=page_size,
            with_payload=with_payload,
            offset=offset,
        )
        gathered.extend(page)
        if offset is None or not page:
            break
    return gathered


class DimMismatchError(RuntimeError):
    """Existing collection vector size does not match DENSE_DIM."""


class CollectionPolicyMismatchError(RuntimeError):
    """Existing collection distribution does not match the selected policy."""


# Selected-policy key -> live CollectionParams attribute, in the order the
# remediation message reports them.
_POLICY_ATTRS = (
    ("shard_number", "shard_number"),
    ("replication_factor", "replication_factor"),
    ("write_consistency_factor", "write_consistency_factor"),
)


def check_collection_distribution(
    client: QdrantPoints, collection: str, settings: Settings
) -> None:
    """Read-only policy examination (issue #360): when the operator selected
    an explicit distribution policy, an existing collection whose configured
    values differ fails closed — running a mismatched generation silently
    is how three Ready pods end up serving one copy. Never recreates or
    mutates: the remediation is a snapshot-gated migration (later slice),
    never automatic recreation. Unset policy and unreadable (None) live
    values are not mismatches — absence of evidence is not evidence."""
    policy = settings.collection_distribution_kwargs()
    if not policy:
        return
    params = client.get_collection(collection).config.params
    for kwarg, attr in _POLICY_ATTRS:
        if kwarg not in policy:
            continue
        live = getattr(params, attr, None)
        if live is None or live == policy[kwarg]:
            continue
        raise CollectionPolicyMismatchError(
            f"Collection '{collection}' {attr} is {live}, selected policy wants "
            f"{policy[kwarg]}. Refusing to run against a mismatched generation: "
            "migrate with a snapshot-gated replica operation (issue #360) or "
            "align the policy — never auto-recreate a populated collection."
        )


def set_bulk_indexing(client: QdrantPoints, collection: str, *, bulk: bool) -> None:
    """bulk=True: effectively disable HNSW builds for the load; bulk=False:
    restore the server-default threshold so the optimizer catches up.

    Measured caveat (371-doc z/OS corpus, 246k payload-heavy points, single
    node): bulk=True made the load ~3x SLOWER wall-clock — with indexing
    disabled, upsert time grew superlinearly as unindexed segments grew
    (0.01s -> 30s/doc), while indexed loads stayed flat (~0.7s/doc). The
    skill's bulk-load guidance pays off on multi-shard/remote targets, not
    on a single-shard local node. Default is OFF; keep it off unless your
    target actually parallelizes writes."""
    client.update_collection(
        collection,
        optimizer_config=models.OptimizersConfigDiff(
            indexing_threshold=(
                BULK_INDEXING_THRESHOLD_KB if bulk else DEFAULT_INDEXING_THRESHOLD_KB
            )
        ),
    )


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


def collection_vector_configs(
    dim: int,
) -> tuple[dict[str, models.VectorParams], dict[str, models.SparseVectorParams]]:
    """Named dense + BM25 sparse configs shared by the corpus collection and
    the ingest completion collection (issue #359): one helper so the two
    collections cannot drift apart."""
    return {"dense": _dense_params(dim)}, {"bm25": _sparse_params()}


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
        # Selected distribution policy holds for pre-existing collections
        # too: read-only examination, never recreation (issue #360).
        check_collection_distribution(client, collection, settings)
        # Indexes-before-load holds for pre-existing collections too: retrieve
        # filters on these payload fields; unindexed filters become scans.
        ensure_payload_indexes(client, collection)
        return

    vectors_config, sparse_vectors_config = collection_vector_configs(dim)
    client.create_collection(
        collection,
        vectors_config=vectors_config,
        sparse_vectors_config=sparse_vectors_config,
        on_disk_payload=True,
        **settings.collection_distribution_kwargs(),
    )
    ensure_payload_indexes(client, collection)


def stored_doc_revisions(
    client: QdrantPoints, settings: Settings, doc_id: str
) -> set[str | None]:
    """Distinct source revisions stored under a printed doc_id (issue #361):
    the `source_rev` payload of every point, with None for legacy points
    that predate the stamp. Paginated to exhaustion (a doc_id can hold
    hundreds of chunks); the empty set means absent. Callers decide
    attribution — this function only observes."""
    revisions: set[str | None] = set()
    for p in scroll_all_points(
        client,
        settings.qdrant_collection,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))]
        ),
        with_payload=["source_rev"],
        page_size=settings.ingest_scan_page_size,
    ):
        revisions.add((p.payload or {}).get("source_rev"))
    return revisions


def stored_rules_version(client: QdrantPoints, settings: Settings) -> str | None:
    """Extraction-rules version carried by the collection's points (issue
    #124). Returns None when the collection is EMPTY (fresh — nothing to
    compare) and the empty string when points exist but predate versioning
    (legacy — a mismatch, never a pass): the two must not blur, or an old
    collection would silently serve mixed-rule payloads."""
    points, _ = client.scroll(
        settings.qdrant_collection,
        limit=1,
        with_payload=["rules_v"],
    )
    if not points:
        return None
    return str((points[0].payload or {}).get("rules_v") or "")


def delete_by_doc(client: QdrantPoints, settings: Settings, doc_id: str) -> None:
    """Delete every point under a printed doc_id. Legacy-sole-migration use
    only (issue #361): sourceless pre-361B residue that the planner proved
    is the sole history under the doc_id. Named revisions always delete by
    their own revision — a doc_id-wide delete over coexisting revisions is
    the overwrite bug, not a refresh."""
    client.delete(
        settings.qdrant_collection,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))]
            )
        ),
        wait=True,
    )


def delete_by_revision(client: QdrantPoints, settings: Settings, source_rev: str) -> None:
    """Delete exactly one source revision's points (issue #361): the only
    destructive point selector for named revisions. Coexisting revisions
    under the same doc_id are untouched."""
    client.delete(
        settings.qdrant_collection,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="source_rev", match=models.MatchValue(value=source_rev)
                    )
                ]
            )
        ),
        wait=True,
    )


def live_collection_from(
    alias: str, alias_target: str | None, target_exists: bool
) -> tuple[str | None, bool]:
    """One alias-resolution decision rule (sync ingest + async serving):
    (physical, legacy). `alias_target` is the alias's collection name (None
    when no alias is defined); `target_exists` says whether that resolved
    candidate — the target when defined, else the alias name — exists.
    A dangling alias resolves as absent so a fresh staging can be published
    over it."""
    if alias_target is not None:
        return (alias_target, False) if target_exists else (None, False)
    return (alias, True) if target_exists else (None, False)


def resolve_live_collection(client: QdrantPoints, settings: Settings) -> tuple[str | None, bool]:
    """Physical collection behind the `<collection>` alias.

    Returns (physical, legacy): (name, False) for the alias target, (None,
    False) when neither alias nor collection exists (first publish), and
    (name, True) when a physical collection carries the alias name with no
    alias defined (pre-publication legacy layout — migration required). A
    dangling alias (target deleted) resolves as absent so a fresh staging
    can be published over it.
    """
    alias = settings.qdrant_collection
    target = next(
        (
            desc.collection_name
            for desc in client.get_aliases().aliases
            if desc.alias_name == alias
        ),
        None,
    )
    candidate = target if target is not None else alias
    return live_collection_from(alias, target, client.collection_exists(candidate))


def snapshot_collection(client: QdrantPoints, collection: str) -> str:
    """Server-side snapshot; returns the server-assigned name. Snapshots are
    the preservation mechanism for superseded generations (issue #359 req 5):
    retained until an operator deletes them, never GC'd by ingest."""
    snap = client.create_snapshot(collection, wait=True)
    if snap is None or not snap.name:
        raise RuntimeError(f"snapshot of {collection!r} returned no name — refusing to proceed.")
    return snap.name


def clone_collection(
    client: QdrantPoints, settings: Settings, src: str, dst: str
) -> None:
    """Server-side copy src -> dst (created by recover) + count verification.

    Fail closed on any count mismatch: a partial clone must never become a
    publish base. The snapshot location is the server-side snapshots dir
    (`Settings.qdrant_snapshots_dir`), the same formula the harness restore
    uses.
    """
    snap = snapshot_collection(client, src)
    location = f"file://{settings.qdrant_snapshots_dir.rstrip('/')}/{src}/{snap}"
    client.recover_snapshot(dst, location, priority=models.SnapshotPriority.SNAPSHOT, wait=True)
    want = client.get_collection(src).points_count
    got = client.get_collection(dst).points_count
    if got != want:
        raise RuntimeError(
            f"clone {src!r} -> {dst!r} unverified: {got} != {want} points — refusing to publish from it."
        )


def swap_alias_to(
    client: QdrantPoints, settings: Settings, new_physical: str, old_physical: str | None
) -> dict[str, str | None]:
    """Point the `<collection>` alias at a verified generation (issue #359
    req 4/5). The delete+create pair rides one atomic alias call, so readers
    see the complete old or the complete new generation — a failed swap
    leaves the previous live generation serving. The superseded physical is
    KEPT (plus a safety snapshot): rollback and GC are operator actions.
    A legacy physical squatting on the alias name must be snapshotted and
    deleted by the caller first (a delete-alias op for a non-existent alias
    would fail the batch). Returns the publication summary for the run log."""
    alias = settings.qdrant_collection
    safety_snapshot: str | None = None
    if old_physical is not None:
        safety_snapshot = snapshot_collection(client, old_physical)
    # Alias existence is checked here (not trusted from resolve time): a
    # dangling alias still occupies the name and needs the delete half.
    alias_exists = any(desc.alias_name == alias for desc in client.get_aliases().aliases)
    ops: list[models.CreateAliasOperation | models.DeleteAliasOperation] = []
    if alias_exists:
        ops.append(models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias)))
    ops.append(
        models.CreateAliasOperation(
            create_alias=models.CreateAlias(collection_name=new_physical, alias_name=alias)
        )
    )
    if not client.update_collection_aliases(ops):
        raise RuntimeError(
            f"alias swap {alias!r} -> {new_physical!r} rejected — previous generation still live."
        )
    return {
        "alias": alias,
        "physical": new_physical,
        "previous": old_physical,
        "safety_snapshot": safety_snapshot,
    }


def upsert_chunks(
    client: QdrantPoints,
    settings: Settings,
    parsed: ParsedDoc,
    chunks: list[Chunk],
    vectors: list[tuple[list[float], SparseVector]],
    contexts: dict[str, str] | None = None,
) -> int:
    """Upsert chunk points in settings.batch_size batches (Qdrant skill
    64-256 band, bounded in Settings). Returns point count.

    Upserts are idempotent by construction — point ids are UUID5 of the chunk
    key — so a connection-level retry/replay of a batch is safe. There is no
    application-level retry loop; the client timeout bounds each call
    (issue #20 PR C). The contextual prefix (issue #78) is stored for
    observability when present; it is never filtered on, so it takes no
    payload index.

    Pair-length contract (issue #359): chunks and vectors must align exactly;
    a mismatch raises instead of silently truncating via zip."""
    collection = settings.qdrant_collection
    rules_v = extraction_rules_version()
    if len(chunks) != len(vectors):
        raise ValueError(
            f"chunks/vectors length mismatch: {len(chunks)} chunks vs "
            f"{len(vectors)} vectors — refusing to truncate."
        )
    points: list[models.PointStruct] = []
    for chunk, (dense, (sparse_idx, sparse_val)) in zip(chunks, vectors):
        payload: dict[str, Any] = {
            "vendor": parsed.vendor,
            "product": parsed.product,
            "version": parsed.version,
            "doc_id": chunk.doc_id,
            # Source-revision key (issue #361): the destructive key since
            # the 361B migration — locks, deletes, completions, and chunk
            # ids all scope to it. Computed from the same labels the
            # planner gates on.
            "source_rev": source_rev_key(
                parsed.vendor, parsed.product, parsed.version, parsed.sha256
            ),
            "title": parsed.title,
            "heading_path": chunk.heading_path,
            "page_label": chunk.page_label,
            "page_start": chunk.page_start,
            "chunk_type": chunk.chunk_type,
            "message_ids": chunk.message_ids,
            "members": chunk.members,
            "sha256": parsed.sha256,
            "rules_v": rules_v,
            "text": chunk.text,
        }
        # Atomic-unit spans (issue #368): stored only for structured chunks
        # (non-empty span list). Prose chunks omit the key, so their
        # payloads stay byte-identical to before; legacy points without the
        # key pack via the shared fallback detector. Never indexed.
        if chunk.units:
            payload["units"] = [
                [span.start, span.end, span.kind] for span in chunk.units
            ]
        if contexts and (context := contexts.get(chunk.chunk_id)):
            payload["context"] = context
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

    batch = settings.batch_size
    for i in range(0, len(points), batch):
        client.upsert(collection, points=points[i : i + batch], wait=True)
    return len(points)
