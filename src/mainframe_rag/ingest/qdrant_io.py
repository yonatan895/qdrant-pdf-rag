"""Qdrant I/O: ensure_collection, upsert, delete-by-doc.

Collection mainframe_manuals (architecture.md section 4.3):
- named vector 'dense'  : size=DENSE_DIM, Cosine, on_disk, HNSW m=16 ef=128, int8 scalar quant
- named sparse 'bm25'   : modifier=IDF, on_disk
- payload indexes BEFORE load (unindexed filters become scans)
"""

from __future__ import annotations

import json
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Any
from uuid import UUID

from qdrant_client import models

from mainframe_rag.config import Settings
from mainframe_rag.ingest.chunk import Chunk
from mainframe_rag.ingest.rules_version import extraction_rules_version
from mainframe_rag.ports import QdrantPoints, QdrantReader, SparseVector

if TYPE_CHECKING:
    # Annotation-only: ibm_pdf imports PyMuPDF, which the serving agent reaches
    # this module through (publish/completion reads) and must not load (#1318).
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc

HNSW_M = 16
HNSW_EF_CONSTRUCT = 128
# Bulk-load mode (project policy; defaults in docs/agent.md): raise
# indexing_threshold so HNSW builds do not compete with upserts, restore to
# the server default afterwards.
# Never m=0 — that drops existing HNSW on an existing collection.
BULK_INDEXING_THRESHOLD_KB = 1 << 30
DEFAULT_INDEXING_THRESHOLD_KB = 20000

_KEYWORD_INDEXES = ("vendor", "product", "version", "doc_id", "chunk_type", "message_ids", "members", "system_codes", "sha256", "source_rev")


def scroll_all_points(
    client: QdrantReader,
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
    client: QdrantReader, collection: str, settings: Settings
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
    generic bulk-load guidance pays off on multi-shard/remote targets, not
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


def collection_vector_configs(
    dim: int,
) -> tuple[dict[str, models.VectorParams], dict[str, models.SparseVectorParams]]:
    """Named dense + BM25 sparse configs shared by the corpus collection and
    the ingest completion collection (issue #359): one helper so the two
    collections cannot drift apart."""
    return {"dense": _dense_params(dim)}, {
        "bm25": models.SparseVectorParams(
            modifier=models.Modifier.IDF, index=models.SparseIndexParams(on_disk=True)
        )
    }


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
    client: QdrantReader, settings: Settings, doc_id: str
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


def stored_rules_version(client: QdrantReader, settings: Settings) -> str | None:
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


def resolve_live_collection(client: QdrantReader, settings: Settings) -> tuple[str | None, bool]:
    """Physical collection behind the `<collection>` alias.

    Returns (physical, legacy): (name, False) for the alias target, (None,
    False) when neither alias nor collection exists (first publish), and
    (name, True) when a physical collection carries the alias name with no
    alias defined (the pre-publication layout — unsupported; callers refuse
    it). A
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
    retained until an operator deletes them with the guarded snapshot-only
    disposal (`ingest.generations`, issue #1373), never GC'd by ingest."""
    snap = client.create_snapshot(collection, wait=True)
    if snap is None or not snap.name:
        raise RuntimeError(f"snapshot of {collection!r} returned no name — refusing to proceed.")
    return snap.name


def _point_ids(client: QdrantReader, settings: Settings, collection: str) -> set[str]:
    return {str(p.id) for p in scroll_all_points(
        client, collection, scroll_filter=None, with_payload=False,
        page_size=settings.ingest_scan_page_size)}


def _records_to_points(
    page: list[models.Record], want: set[str]
) -> list[models.PointStruct]:
    points: list[models.PointStruct] = []
    for record in page:
        want.add(str(record.id))
        vector = getattr(record, "vector", None)
        named: dict[str, Any] = dict(vector) if isinstance(vector, dict) else {}
        points.append(models.PointStruct(id=record.id, vector=named, payload=record.payload))
    return points


# Request sizing for bulk point copies (issue #1379). Upper bounds on the
# JSON bytes qdrant-client's REST transport sends for one point: the longest
# shortest-repr of a finite float64 is 24 characters
# ("-2.2250738585072014e-308") plus its separator, and a sparse index is a u32
# (at most 10 digits) plus its separator. The fixed allowances cover the
# point's id, key names and braces, and the request envelope.
_FLOAT_JSON_BOUND = 25
_INDEX_JSON_BOUND = 11
_POINT_JSON_OVERHEAD = 256
_REQUEST_JSON_OVERHEAD = 1024


class PointTooLargeError(RuntimeError):
    """One point's estimated request size exceeds the copy request budget."""


def _vector_json_bound(vector: Any) -> int:
    if isinstance(vector, models.SparseVector):
        return 32 + len(vector.indices) * _INDEX_JSON_BOUND + len(vector.values) * _FLOAT_JSON_BOUND
    if isinstance(vector, dict):
        return 2 + sum(
            len(json.dumps(str(name))) + 2 + _vector_json_bound(value)
            for name, value in vector.items()
        )
    if isinstance(vector, list):
        if vector and isinstance(vector[0], (list, dict)):
            return 2 + sum(_vector_json_bound(value) + 1 for value in vector)
        return 2 + len(vector) * _FLOAT_JSON_BOUND
    return _FLOAT_JSON_BOUND


def estimate_point_bytes(point: models.PointStruct) -> int:
    """Upper bound on one point's JSON size in an upsert request (issue
    #1379): the named vectors from their dimensions and sparse entry counts
    (worst-case number formatting, not the observed values), plus the
    payload's UTF-8 bytes from the same pydantic serializer and options the
    REST transport uses (Python's json module formats some floats shorter,
    e.g. 1e-05 against 0.00001)."""
    payload = (
        len(point.model_dump_json(include={"payload"}, by_alias=True, exclude_none=True).encode("utf-8"))
        if point.payload
        else 4
    )
    return _POINT_JSON_OVERHEAD + _vector_json_bound(point.vector) + payload


def byte_bounded_batches(
    points: list[models.PointStruct], budget: int
) -> list[list[models.PointStruct]]:
    """Split points, in order, into upsert batches whose estimated request
    size stays within budget bytes (issue #1379). A point that cannot fit a
    request on its own raises before any batch is returned: a copy never
    skips a point."""
    batches: list[list[models.PointStruct]] = []
    batch: list[models.PointStruct] = []
    size = _REQUEST_JSON_OVERHEAD
    for point in points:
        need = estimate_point_bytes(point)
        if _REQUEST_JSON_OVERHEAD + need > budget:
            raise PointTooLargeError(
                f"point {point.id} needs an estimated {need} request bytes, over the "
                f"{budget}-byte copy request budget (ingest_copy_request_bytes) — "
                "refusing to copy without it."
            )
        if batch and size + need > budget:
            batches.append(batch)
            batch, size = [], _REQUEST_JSON_OVERHEAD
        batch.append(point)
        size += need
    if batch:
        batches.append(batch)
    return batches


def _upsert_each(client: QdrantPoints, dst: str, batches: list[list[models.PointStruct]]) -> None:
    for batch in batches:
        client.upsert(dst, points=batch, wait=True)


def clone_collection(
    client: QdrantPoints, settings: Settings, src: str, dst: str
) -> None:
    """Copy src -> dst through the points API, then verify (issue #360).

    Every point (id, named vectors, payload) is read with scroll and written
    with upsert, so the copy covers every shard of a distributed collection;
    node-local snapshots are not involved. dst must not exist. It is created
    with src's dense dimension (a model change is still refused downstream,
    as for any existing target), this run's distribution policy and src's
    payload indexes, before any point is loaded. Fail closed unless dst then
    holds exactly src's point ids: a partial copy must never become a
    publish base. src is only read.

    Reads page by `ingest_scan_page_size`; each page is written in upsert
    requests sized by bytes (`ingest_copy_request_bytes`, issue #1379), since
    a page of 4096-dimension points can exceed the server's request limit."""
    if client.collection_exists(dst):
        raise RuntimeError(f"copy target {dst!r} already exists — refusing to copy into it.")
    info = client.get_collection(src)
    vectors = info.config.params.vectors
    dense = vectors.get("dense") if isinstance(vectors, dict) else None
    if dense is None:
        raise RuntimeError(f"copy source {src!r} has no named dense vector — refusing to copy it.")
    vectors_config, sparse_vectors_config = collection_vector_configs(dense.size)
    client.create_collection(
        dst,
        vectors_config=vectors_config,
        sparse_vectors_config=sparse_vectors_config,
        on_disk_payload=True,
        **settings.collection_distribution_kwargs(),
    )
    for field, index in sorted((getattr(info, "payload_schema", None) or {}).items()):
        client.create_payload_index(dst, field_name=field, field_schema=index.data_type)
    offset: int | str | UUID | None = None
    want: set[str] = set()
    # A final single-page/single-point wait=True only synchronizes the shard(s)
    # owning those points and skips write_consistency_factor error checks on
    # intermediate wait=False pages. Restrict wait=False pipelining to the
    # explicit 1/1/1 policy; every other target (e.g. production 6/3/2) flushes
    # every batch with wait=True while overlapping scroll(src) with one
    # in-flight synchronous upsert (#1298). An unset or partial policy is not
    # 1/1/1: the server default then creates dst, with one shard per peer on a
    # multi-peer cluster, and 1/1/1 is never inferred (deploy.md collection
    # policy).
    single_shard_unreplicated = (
        settings.qdrant_shard_number,
        settings.qdrant_replication_factor,
        settings.qdrant_write_consistency_factor,
    ) == (1, 1, 1)
    budget = settings.ingest_copy_request_bytes
    if single_shard_unreplicated:
        pending_async = False
        last_point: models.PointStruct | None = None
        while True:
            page, offset = client.scroll(
                src,
                limit=settings.ingest_scan_page_size,
                with_payload=True,
                with_vectors=True,
                offset=offset,
            )
            if page:
                batches = byte_bounded_batches(_records_to_points(page, want), budget)
                for index, batch in enumerate(batches):
                    is_last = offset is None and index == len(batches) - 1
                    client.upsert(dst, points=batch, wait=is_last)
                    pending_async = not is_last
                    last_point = batch[-1]
            if offset is None or not page:
                break
        if pending_async and last_point is not None:
            client.upsert(dst, points=[last_point], wait=True)
    else:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending: Future[Any] | None = None
            while True:
                page, offset = client.scroll(
                    src,
                    limit=settings.ingest_scan_page_size,
                    with_payload=True,
                    with_vectors=True,
                    offset=offset,
                )
                if pending is not None:
                    pending.result()
                    pending = None
                if page:
                    batches = byte_bounded_batches(_records_to_points(page, want), budget)
                    pending = pool.submit(_upsert_each, client, dst, batches)
                if offset is None or not page:
                    break
            if pending is not None:
                pending.result()
    got = _point_ids(client, settings, dst)
    if got != want:
        raise RuntimeError(
            f"copy {src!r} -> {dst!r} unverified: {len(got)} of {len(want)} point ids match "
            "the source — refusing to publish from it."
        )


def swap_alias_to(
    client: QdrantPoints, settings: Settings, new_physical: str, old_physical: str | None,
    *, build_id: str | None = None,
) -> dict[str, str | None]:
    """Point the `<collection>` alias at a verified generation (issue #359
    req 4/5). The delete+create pair rides one atomic alias call, so readers
    see the complete old or the complete new generation — a failed swap
    leaves the previous live generation serving. The superseded physical is
    KEPT (plus a safety snapshot): rollback and GC are operator actions.
    Callers refuse a physical collection squatting on the alias name before
    any write, so `old_physical` is always a published alias target or None.

    `old_physical` is also the predecessor the cutover was planned against
    (issue #1019): after the safety snapshot, immediately before the atomic
    call, the alias is re-read and must still resolve to it, or the swap
    refuses and sends nothing. A publisher overtaken during its catalog/seal
    phase (an unsupported second writer outside the target lock) can then
    never switch the alias back to an older candidate. Qdrant alias updates
    are not compare-and-swap, so the remaining window is this read-to-call
    gap; the target lock stays the primary control.
    Returns the publication summary for the run log."""
    alias = settings.qdrant_collection
    from mainframe_rag.ingest.build import build_aliases

    observed = {item.alias_name: item.collection_name for item in client.get_aliases().aliases}
    immutable = build_aliases(alias, build_id) if build_id is not None else ()
    if any(name in observed for name in immutable):
        raise RuntimeError("immutable build alias already exists; refusing to rebind it")
    safety_snapshot: str | None = None
    if old_physical is not None:
        safety_snapshot = snapshot_collection(client, old_physical)
    # Alias existence is checked here (not trusted from resolve time): a
    # dangling alias still occupies the name and needs the delete half.
    current = next(
        (d.collection_name for d in client.get_aliases().aliases if d.alias_name == alias), None
    )
    alias_exists = current is not None
    resolved, _ = live_collection_from(
        alias, current, client.collection_exists(current if current is not None else alias)
    )
    if current is None and resolved is not None:
        resolved = None  # a squatting physical is refused upstream; not a predecessor
    if resolved != old_physical:
        raise RuntimeError(
            f"live target moved during publication ({old_physical!r} -> {resolved!r}): "
            "another publisher cut over first — alias untouched, retry against the "
            "current live generation."
        )
    ops: list[models.CreateAliasOperation | models.DeleteAliasOperation] = []
    if alias_exists:
        ops.append(models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias)))
    ops.append(
        models.CreateAliasOperation(
            create_alias=models.CreateAlias(collection_name=new_physical, alias_name=alias)
        )
    )
    if immutable:
        for name, target in zip(immutable, (new_physical, new_physical + "__completions"), strict=True):
            ops.append(models.CreateAliasOperation(
                create_alias=models.CreateAlias(collection_name=target, alias_name=name)
            ))
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
    """Upsert chunk points in settings.batch_size batches (bounds owned by
    Settings and docs/ingest.md). Returns point count.

    Upserts are idempotent by construction — point ids are UUID5 of the chunk
    key — so a connection-level retry/replay of a batch is safe. There is no
    application-level retry loop; the client timeout bounds each call
    (issue #20 PR C). The contextual prefix (issue #78) is stored for
    observability when present; it is never filtered on, so it takes no
    payload index.

    Pair-length contract (issue #359): chunks and vectors must align exactly;
    a mismatch raises instead of silently truncating via zip."""
    # Deferred (#1318): the payload rule module imports ibm_pdf, hence PyMuPDF,
    # at runtime; only the ingest writer reaches this function.
    from mainframe_rag.ingest.payload import point_payload

    collection = settings.qdrant_collection
    rules_v = extraction_rules_version()
    if len(chunks) != len(vectors):
        raise ValueError(
            f"chunks/vectors length mismatch: {len(chunks)} chunks vs "
            f"{len(vectors)} vectors — refusing to truncate."
        )
    points: list[models.PointStruct] = []
    for chunk, (dense, (sparse_idx, sparse_val)) in zip(chunks, vectors, strict=True):
        context = contexts.get(chunk.chunk_id) if contexts else None
        payload = point_payload(parsed, chunk, rules_v, context)
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
