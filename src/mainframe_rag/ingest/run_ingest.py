"""Ingest CLI (air-gap Job).

    python -m mainframe_rag.ingest.run_ingest --src /corpus --progress /work/inventory.jsonl

Process pool, one PDF per worker (workers = CPU-1). Skip rules (issue #359):
- inventory says this sha256 already upserted AND a valid completion record
  tied to the actual target generation verifies in Qdrant
- Qdrant completion + point verification passes for this doc generation
Neither a single sampled point nor an unbound inventory line proves
completeness. If Qdrant holds the doc_id with a different sha256/rules
generation, invalidate its completion, delete by doc_id, then re-upsert and
verify before writing the new completion.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import multiprocessing as mp
import os
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from qdrant_client.http.exceptions import UnexpectedResponse

if TYPE_CHECKING:
    import pymupdf

from mainframe_rag.config import Settings, load_settings
from mainframe_rag.ingest.chrome import strip_chrome
from mainframe_rag.ingest.chunk import Chunk, make_chunks
from mainframe_rag.ingest.completion import (
    acquire_run_lock,
    completion_collection_for,
    completion_collection_name,
    delete_completion,
    doc_generation_id,
    ensure_completion_collection,
    expected_digests,
    is_doc_complete,
    plan_refresh_deletes,
    release_run_lock,
    source_labels,
    stale_completion_markers,
    verify_doc_points,
    write_completion,
)
from mainframe_rag.ingest.context import (
    ContextLLMClient,
    append_context_entries,
    generate_contexts,
    load_context_cache,
    resolve_cache_path,
)
from mainframe_rag.ingest.embed import build_embedder, embed_batch
from mainframe_rag.ingest.ibm_pdf import ParsedDoc, parse_pdf, sanitize_page_text, sha256_file
from mainframe_rag.ingest.identity import (
    RevisionCollisionError,
    find_collisions,
    plan_duplicates,
    prescan_doc_ids,
    source_rev_key,
)
from mainframe_rag.ingest.inventory import (
    InventoryRecord,
    append_record,
    load_inventory,
    should_skip,
)
from mainframe_rag.ingest.publish import (
    PublishTarget,
    corpus_fingerprint,
    ensure_staging,
    generation_fingerprint,
    resolve_staging_name,
    staging_name_for,
    sweep_unmarked_residue,
    verify_all_complete,
)
from mainframe_rag.ingest.qdrant_io import (
    delete_by_doc,
    delete_by_revision,
    ensure_collection,
    resolve_live_collection,
    set_bulk_indexing,
    snapshot_collection,
    stored_rules_version,
    swap_alias_to,
    upsert_chunks,
)
from mainframe_rag.ingest.representation import (
    STATE_PENDING,
    begin_manifest,
    check_ingest_compatible,
    commit_manifest,
    manifest_digest,
    refuse_limited_migration,
    require_attested_revision,
    require_in_place_reconverge,
)
from mainframe_rag.ingest.rules_version import extraction_rules_version
from mainframe_rag.ingest.walk import detect_vendor, walk_pdfs
from mainframe_rag.logs import configure_logging
from mainframe_rag.ports import SparseVector
from mainframe_rag.tracing import setup_tracing, shutdown_tracing

log = logging.getLogger("ingest")

_worker_qdrant = None
_worker_embedder = None
_worker_settings: Settings | None = None
_worker_context_client: ContextLLMClient | None = None
_worker_context_cache: dict[str, str] | None = None
_worker_context_cache_path: str | None = None


def _extract_page_texts(doc: pymupdf.Document) -> tuple[list[str], list[str | None]]:
    """Page texts sanitized at extraction plus page labels, in page order.

    Split out of _parse_one so the sanitize wiring is unit-testable with a
    stub document (no PyMuPDF needed): control/bidi/zero-width characters
    are dropped by sanitize_page_text (issue #87) before chrome detection
    sees the text, since those characters would also fracture chrome
    line-matching. Labels pass through untouched.
    """
    page_texts: list[str] = []
    page_labels: list[str | None] = []
    for i in range(doc.page_count):
        page = doc[i]
        page_texts.append(sanitize_page_text(page.get_text()))
        page_labels.append(page.get_label())
    return page_texts, page_labels


def _parse_one(
    args: tuple[str, str | None, str | None, str | None, str, str, bool, str | None],
) -> tuple[
    InventoryRecord, ParsedDoc, list[Chunk], list[tuple[list[float], SparseVector]], dict[str, str]
]:
    """Stage 1 (parse worker): parse, chunk, and embed. Embedding lives in
    the worker because hash embed is Python/GIL-bound — in a thread pool it
    would serialize; in a process pool it scales with the parse pool.
    Dry runs embed nothing (the --dry-run contract: parse + chunk only).
    The trailing contexts dict maps chunk_id -> situating prefix for the doc
    (empty when contextual ingest is off or on any error path); the parent
    merges it into the sidecar cache and the upsert payload."""
    import pymupdf

    path_str, vendor, product, version, corpus_root, sha, embed, cache_path = args
    started = time.monotonic()
    parsed: ParsedDoc | None = None
    try:
        path = Path(path_str)
        parsed = parse_pdf(
            path,
            vendor=vendor,
            product=product,
            version=version,
            corpus_root=Path(corpus_root) if corpus_root else None,
            sha256=sha,
        )
        doc = pymupdf.open(path)
        try:
            page_texts, page_labels = _extract_page_texts(doc)
        finally:
            doc.close()
        stripped = strip_chrome(page_texts)
        chunks = make_chunks(parsed, stripped, page_labels)
        settings = _load_worker_settings()
        batch = settings.batch_size
        embedder = _get_embedder(settings) if embed else None
        vectors: list[tuple[list[float], SparseVector]] = []
        contexts: dict[str, str] = {}
        if embed and embedder is not None:
            if settings.contextual_embed_enabled:
                # Defense in depth: the parent validates before spawning the
                # pool, but a worker must never silently embed header-only
                # vectors when the flag asked for contexts.
                if settings.embed_mode == "hash":
                    raise RuntimeError(
                        "CONTEXTUAL_EMBED_ENABLED=true requires embed_mode=vllm."
                    )
                settings.require_context_llm()
                if cache_path is None:
                    raise RuntimeError("contextual ingest requires a context cache path.")
                cache = _get_context_cache(cache_path)
                client = _get_context_client(settings)
                contexts, _ = generate_contexts(
                    chunks,
                    doc_sha256=sha,
                    product=parsed.product,
                    version=parsed.version,
                    title=parsed.title,
                    client=client,
                    cache=cache,
                    max_chars=settings.context_max_chars,
                )
            for i in range(0, len(chunks), batch):
                vectors.extend(
                    embed_batch(
                        chunks[i : i + batch],
                        parsed.product,
                        parsed.version,
                        parsed.title,
                        embedder,
                        contexts or None,
                    )
                )
        record = InventoryRecord(
            path=path_str,
            sha256=sha,
            doc_id=parsed.doc_id,
            pages=parsed.page_count,
            chunks=len(chunks),
            seconds=round(time.monotonic() - started, 3),
            rules_version=extraction_rules_version(),
            # Representation contract provenance (issue #362): computed in
            # the worker from its own settings — zero plumbing, and the
            # stamp provably equals the parent's manifest view. Additive.
            manifest_digest=manifest_digest(settings, extraction_rules_version()),
            # Source-revision provenance (issue #361): stamped now so the
            # 361B selector migration can map every committed doc without
            # re-reading the corpus. Additive — older readers ignore it.
            source_rev=source_rev_key(parsed.vendor, parsed.product, parsed.version, sha),
        )
        return record, parsed, chunks, vectors, contexts
    except Exception as exc:  # noqa: BLE001 — isolate worker crash from main pool
        record = InventoryRecord(
            path=path_str,
            sha256=sha,
            doc_id=parsed.doc_id if parsed is not None else Path(path_str).stem,
            pages=parsed.page_count if parsed is not None else 0,
            chunks=0,
            status="error",
            seconds=round(time.monotonic() - started, 3),
            error=str(exc)[:500],
            error_type=type(exc).__name__,
        )
        dummy_parsed = parsed if parsed is not None else ParsedDoc(
            path=Path(path_str),
            sha256=sha,
            doc_id=Path(path_str).stem,
            title="",
            product=product or "",
            version=version or "",
            vendor=vendor or "",
            toc=(),
            page_count=0,
        )
        return record, dummy_parsed, [], [], {}


def resolve_workers(requested: int | None, settings: Settings) -> int:
    """INGEST_WORKERS (or the CLI override) is a cap, never 'spawn unbounded':
    clamp into [1, 2*CPU]. The pool is bounded either way; this keeps a bad
    env value from fanning out beyond the box."""
    cap = max(1, 2 * (mp.cpu_count() or 2))
    base = settings.ingest_workers if requested is None else requested
    return max(1, min(int(base), cap))


def _load_worker_settings() -> Settings:
    """Spawn workers start with fresh module state; env is inherited, so a
    per-worker cached Settings is correct (and built once per worker)."""
    global _worker_settings
    if _worker_settings is None:
        _worker_settings = load_settings()
    return _worker_settings


def _get_embedder(settings: Settings):
    global _worker_embedder
    if _worker_embedder is None:
        _worker_embedder = build_embedder(settings)
    return _worker_embedder


def _get_context_client(settings: Settings) -> ContextLLMClient:
    global _worker_context_client
    if _worker_context_client is None:
        _worker_context_client = ContextLLMClient(settings)
    return _worker_context_client


def _get_context_cache(cache_path: str) -> dict[str, str]:
    """Per-worker snapshot of the sidecar cache. A worker only ever needs
    entries for the doc it is currently processing (chunk ids embed the
    doc id, and one file is processed by exactly one worker), so a snapshot
    taken at first use plus its own misses is complete — sibling workers'
    mid-run appends are for other docs and safely invisible."""
    global _worker_context_cache, _worker_context_cache_path
    if _worker_context_cache is None or _worker_context_cache_path != cache_path:
        _worker_context_cache = load_context_cache(Path(cache_path))
        _worker_context_cache_path = cache_path
    return _worker_context_cache


def _get_qdrant(settings: Settings):
    from qdrant_client import QdrantClient

    global _worker_qdrant
    if _worker_qdrant is None:
        _worker_qdrant = QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key,
            timeout=settings.qdrant_ingest_timeout_s,
        )
    return _worker_qdrant


class _DocLocks:
    """Per-revision locks for the upsert stage (issue #361). Two files may
    resolve to one printed doc_id (shared form numbers) while carrying
    different bytes; the planning gate (_gate_planned_entries) aborts such
    corpora before any delete/upsert, and coexisting revisions that passed
    planning take different locks so their check-delete-upsert sequences
    never serialize against each other by accident. A lock collision at
    this stage is a same-revision rerun, never a cross-revision overwrite:
    every holder passes its source_rev_key (vendor|product|version|sha256).

    Locks are retained in memory for the run: bounded by the unique source
    revisions in the corpus (~hundreds of entries), so eviction is unnecessary."""
    _global: threading.Lock
    _locks: dict[str, threading.Lock]

    def __init__(self) -> None:
        self._global = threading.Lock()
        self._locks = {}

    def get(self, key: str) -> threading.Lock:
        with self._global:
            return self._locks.setdefault(key, threading.Lock())


def _upsert_one(
    parsed: ParsedDoc,
    chunks: list[Chunk],
    vectors: list[tuple[list[float], SparseVector]],
    settings: Settings,
    locks: _DocLocks,
    contexts: dict[str, str] | None = None,
    force_reingest: bool = False,
    *,
    src_labels: str,
    lineage_rev: str | None = None,
) -> tuple[str, float]:
    """Stage 2 (upsert stream): verified-completion skip, invalidate-on-change,
    batched upsert, verify-before-mark. Vectors arrive precomputed from the
    parse worker. The revision lock keeps colliding docs from interleaving.
    Returns (status, seconds) with status in upserted | skipped | empty.
    `force_reingest` (--reingest, issue #124) bypasses the completion skip
    and re-extracts every doc. Empty (zero-chunk) docs are an explicit
    policy outcome (issue #359 req 7): nothing is deleted, upserted, or
    marked complete — the caller records `empty` and fails the run.
    src_labels binds the CLI vendor/product/version triple: overrides change
    payloads and embed headers, so a generation certified under one triple
    never satisfies a run under another. lineage_rev is the inventory
    lineage for this path (the previous source revision, when the progress
    file records one): the only key allowed to replace another revision's
    points. Without lineage, committed coexisting revisions are left alone
    (replacement would be the overwrite bug) and only residue is swept.
    """
    started = time.perf_counter()
    client = _get_qdrant(settings)
    rules_v = extraction_rules_version()
    revision = source_rev_key(parsed.vendor, parsed.product, parsed.version, parsed.sha256)
    if len(chunks) == 0:
        # Explicit policy, not accidental success: no completion, no skip.
        return "empty", round(time.perf_counter() - started, 3)
    if len(chunks) != len(vectors):
        raise ValueError(
            f"chunks/vectors length mismatch for {parsed.doc_id}: "
            f"{len(chunks)} chunks vs {len(vectors)} vectors."
        )
    with locks.get(revision):
        if not force_reingest and is_doc_complete(
            client, settings, parsed.doc_id,
            sha256=parsed.sha256, rules_v=rules_v, source_labels=src_labels,
            source_rev=revision,
        ):
            return "skipped", round(time.perf_counter() - started, 3)
        # Revision delete plan (issue #361): computed BEFORE any delete —
        # raises on unattributable residue instead of guessing.
        rev_deletes, legacy_delete = plan_refresh_deletes(
            client, settings, parsed.doc_id, revision, lineage_rev
        )
        # Invalidate this revision's markers (plus the lineage revision's
        # when it differs — a refresh retires the old generation's markers
        # with its points) before touching points: a failed refresh must
        # leave NO valid completion (safe retry), never a stale marker over
        # partial data. Only a 404 (collection dropped between the
        # exists-check and the delete) is tolerated — real Qdrant failures
        # propagate so the doc errors instead of publishing alongside a
        # stale marker.
        scopes = {revision}
        if lineage_rev is not None and lineage_rev != revision:
            scopes.add(lineage_rev)
        try:
            for scope in sorted(scopes):
                delete_completion(
                    client, settings, parsed.doc_id,
                    source_rev=scope, include_legacy=legacy_delete,
                )
        except UnexpectedResponse as exc:
            if exc.status_code != 404:
                raise
        for stale_rev in sorted(rev_deletes):
            delete_by_revision(client, settings, stale_rev)
        if legacy_delete:
            delete_by_doc(client, settings, parsed.doc_id)
        upserted = upsert_chunks(client, settings, parsed, chunks, vectors, contexts)
        expected, ids_digest, content_digest = expected_digests(chunks)
        if upserted != expected:
            raise RuntimeError(
                f"upserted {upserted} points for {parsed.doc_id}, expected "
                f"{expected} — completion withheld, retry recovers."
            )
        if not verify_doc_points(
            client,
            settings,
            parsed.doc_id,
            sha256=parsed.sha256,
            rules_v=rules_v,
            source_rev=revision,
            expected_chunks=expected,
            chunk_ids_digest=ids_digest,
            content_digest=content_digest,
        ):
            raise RuntimeError(
                f"post-upsert verification failed for {parsed.doc_id}: "
                f"expected {expected} chunks — completion withheld, retry recovers."
            )
        write_completion(
            client,
            settings,
            doc_id=parsed.doc_id,
            sha256=parsed.sha256,
            rules_v=rules_v,
            source_labels=src_labels,
            source_rev=revision,
            expected_chunks=expected,
            chunk_ids_digest=ids_digest,
            content_digest=content_digest,
        )
    log.info(
        json.dumps(
            {
                "doc_id": parsed.doc_id,
                "pages": parsed.page_count,
                "chunks": upserted,
                "seconds": round(time.perf_counter() - started, 3),
                "action": "upsert",
            }
        )
    )
    return "upserted", round(time.perf_counter() - started, 3)


def run(
    src: Path,
    progress: Path,
    workers: int | None,
    limit: int | None,
    dry_run: bool,
    vendor: str | None = None,
    product: str | None = None,
    version: str | None = None,
    force_reingest: bool = False,
) -> int:
    """Public entry: OTel tracing around the ingest body (issue #83).

    Parent-process spans only: parse workers are spawn processes that return
    records to the parent (they never inherit the log handler either), so
    per-document detail lives in the inventory/log stream. Tracing is
    default-off — with no OTEL_EXPORTER_OTLP_ENDPOINT the tracer is a no-op.
    """
    settings = load_settings()
    tracer = setup_tracing(
        settings.otel_exporter_otlp_endpoint,
        sample_ratio=settings.otel_sample_ratio,
        export_queue_size=settings.otel_export_queue_size,
        export_timeout_ms=settings.otel_export_timeout_ms,
        service_name=os.environ.get("OTEL_SERVICE_NAME") or "mainframe-rag-ingest",
    )
    root = tracer.start_span("ingest.run")
    token = otel_context.attach(trace.set_span_in_context(root))
    try:
        return _run_impl(
            src, progress, workers, limit, dry_run, settings, tracer, root,
            vendor=vendor, product=product, version=version, force_reingest=force_reingest,
        )
    except Exception as exc:
        root.set_attribute("ingest.error_type", type(exc).__name__)
        root.set_status(Status(StatusCode.ERROR, type(exc).__name__))
        raise
    finally:
        otel_context.detach(token)
        root.end()
        shutdown_tracing()


def _gate_planned_entries(
    src: Path, walk_entries: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """Identity planning gate (issue #361 req 3/4), shared by the in-place
    plan span and the alias-publish prewalk: byte-identical copies collapse
    onto the deterministic winner (logged, never ingested twice), and
    distinct revisions claiming one doc_id abort fail-closed before any
    parse worker spawns, delete runs, or point upserts. Unreadable files
    never join the collision map — the parse worker owns that error."""
    kept, duplicates = plan_duplicates(walk_entries, src)
    for dup in duplicates:
        log.info(
            json.dumps(
                {"path": dup.loser_rel, "winner": dup.winner_rel, "action": "duplicate"}
            )
        )
    resolved = prescan_doc_ids([path_str for path_str, _ in kept])
    collisions = find_collisions(
        [(path_str, sha, resolved[path_str]) for path_str, sha in kept], src
    )
    if collisions:
        raise RevisionCollisionError(collisions)
    return kept


def _log_record_drift(settings: Settings, record_drift: list[str]) -> None:
    """One rule for the record-only drift note (in-place preflight and the
    publish read-only check share it): warn loudly, proceed — vectors are
    unaffected, re-evaluation is owed."""
    if record_drift:
        log.warning(
            json.dumps(
                {
                    "action": "representation_drift",
                    "collection": settings.qdrant_collection,
                    "fields": record_drift,
                    "result": "record_only",
                    "note": "re-evaluation owed, never a re-ingest",
                }
            )
        )


def _run_impl(
    src: Path,
    progress: Path,
    workers: int | None,
    limit: int | None,
    dry_run: bool,
    settings: Settings,
    tracer: trace.Tracer,
    root: trace.Span,
    vendor: str | None = None,
    product: str | None = None,
    version: str | None = None,
    force_reingest: bool = False,
    prewalked: list[tuple[str, str]] | None = None,
    _publish_target: PublishTarget | None = None,
) -> int:
    workers = resolve_workers(workers, settings)
    rules_v = extraction_rules_version()
    src_labels = source_labels(vendor, product, version)
    started = time.monotonic()
    if settings.contextual_embed_enabled and not dry_run:
        # Fail the whole run before spawning the pool: a misconfigured flag
        # must never degrade into header-only vectors doc by doc. Dry runs
        # embed nothing, so they need no context endpoint.
        if settings.embed_mode == "hash":
            raise RuntimeError(
                "CONTEXTUAL_EMBED_ENABLED=true requires embed_mode=vllm; "
                "hash mode cannot call an LLM."
            )
        settings.require_context_llm()
    if settings.ingest_alias_publish and not dry_run and _publish_target is None:
        return _run_publish(
            src, progress, workers, limit, settings, tracer, root,
            vendor=vendor, product=product, version=version,
            force_reingest=force_reingest,
        )
    cache_path = resolve_cache_path(settings, progress) if settings.contextual_embed_enabled else None
    # Progress counters (issue #20 PR D): files ok / failed / chunks upserted,
    # logged once per run. Logs carry ids and counts, never PDF text.
    files_ok = 0
    files_failed = 0
    chunks_upserted = 0
    parse_seconds = 0.0
    upsert_seconds = 0.0
    pages_seen = 0
    bulk = settings.ingest_bulk_load and not dry_run
    bulk_active = False
    client = None
    run_lock = None
    manifest_mode: str | None = None
    if not dry_run:
        # Single-writer guard (issue #359 req 6): a second concurrent run
        # sharing the progress directory fails closed before any stage runs.
        run_lock = acquire_run_lock(progress)
        client = _get_qdrant(settings)
        ensure_collection(client, settings)
        ensure_completion_collection(client, settings)
        # Extraction-rules gate (issue #124): a non-empty collection whose
        # payloads were extracted under different rules must never be
        # appended to or skipped against — identifier regexes, chunking, or
        # classify changes would silently mix rule generations in one
        # collection and desync the message_ids prefetch filter (the #120
        # failure mode). Fail closed with the remediation; --reingest is
        # the deliberate override that re-extracts every doc. Empty
        # collection (None) needs no gate; legacy points (empty string)
        # are a mismatch like any other version.
        stored_v = stored_rules_version(client, settings)
        if stored_v is not None and stored_v != rules_v and not force_reingest:
            if stored_v == "":
                raise RuntimeError(
                    f"collection {settings.qdrant_collection!r} predates extraction-rules "
                    f"versioning (no rules_v on its points; this tree computes {rules_v!r}). "
                    "Re-ingest required: re-run with --reingest to stamp every doc "
                    "(never serve mixed-rule payloads)."
                )
            raise RuntimeError(
                f"extraction-rules mismatch: collection {settings.qdrant_collection!r} holds "
                f"payloads extracted under rules {stored_v!r}, this tree computes {rules_v!r}. "
                "Re-ingest required: re-run with --reingest to re-extract every doc "
                "(never serve mixed-rule payloads)."
            )
        # Identity attestation is unconditional (issue #391 F2): --reingest
        # bypasses rejection of stored data, never the requirement to name
        # the representation it writes.
        require_attested_revision(settings)
        if not force_reingest:
            # Representation preflight (issue #362): the rules gate proves
            # extraction identity; this proves embedding identity — same
            # dimension under a different model/revision must never be
            # skipped against. Runs before any parse worker spawns (req 4:
            # reject before expensive work, not in an offline report).
            # Record-only drift (query prefix) proceeds with a warning;
            # --reingest bypasses like the rules gate and re-embeds
            # everything downstream, so a bypassed run cannot mix.
            _, record_drift = check_ingest_compatible(
                client, settings, completion_collection_name(settings), rules_v
            )
            _log_record_drift(settings, record_drift)
        if limit:
            # A partial walk cannot certify a collection-wide contract
            # (issue #391 F2); read-only, refused before any mutation.
            # `--limit 0` truncates nothing (same falsy rule as the walk
            # above), so it is not a partial walk.
            refuse_limited_migration(
                client, settings, completion_collection_name(settings), rules_v
            )
        if bulk:
            # Qdrant skill: HNSW builds must not compete with a bulk load.
            set_bulk_indexing(client, settings.qdrant_collection, bulk=True)
            bulk_active = True
    try:
        with tracer.start_as_current_span("ingest.plan") as plan_span:
            if prewalked is None:
                pdfs = walk_pdfs(src)
                if limit:
                    pdfs = pdfs[:limit]
                walk_entries = [(str(p), sha256_file(p)) for p in pdfs]
            else:
                if limit is not None:
                    raise RuntimeError("pre-hashed walk and --limit are mutually exclusive.")
                walk_entries = prewalked
            # Identity gate (issue #361): dedup byte-identical copies and
            # abort on cross-revision doc_id collisions before any parse,
            # delete, or upsert. Deterministic for identical inputs.
            walk_entries = _gate_planned_entries(src, walk_entries)
            inventory = load_inventory(progress)
            # Lineage map (issue #361): the previous source revision per
            # path, for precise refresh replacement in the upsert stage. A
            # path whose record predates revision stamps carries None —
            # the refresh plan then treats stored history by the
            # sole-lineage/residue rules instead of guessing.
            lineage_by_path = {path: rec.source_rev for path, rec in inventory.items()}

            tasks: list[tuple[str, str | None, str | None, str | None, str, str, bool, str | None]] = []
            for path_str, sha in walk_entries:
                record = inventory.get(path_str)
                if record and should_skip(
                    record, sha, allow_dry=dry_run, rules_version=rules_v,
                    force_reingest=force_reingest,
                ):
                    if dry_run:
                        files_ok += 1  # already ingested — an ok outcome
                        log.info(json.dumps({"path": path_str, "sha256": record.sha256, "action": "skip"}))
                        continue
                    # Bound skip (issue #359 req 3): an inventory line alone
                    # never proves the target holds the generation. Require
                    # a valid completion + verified points; otherwise
                    # re-queue for parse+upsert+verify. Legacy records
                    # without a doc_id re-ingest explicitly.
                    assert client is not None
                    bound_doc = record.doc_id
                    bound_rev = record.source_rev
                    # Bound skip (issue #359 req 3, revision-scoped by #361):
                    # an inventory line alone never proves the target holds
                    # the generation. Require a valid completion for THIS
                    # revision plus verified points; otherwise re-queue for
                    # parse+upsert+verify. Legacy records without a doc_id
                    # or revision re-ingest explicitly (one lazy-migration
                    # cycle, never a wrong skip).
                    if bound_doc and bound_rev and is_doc_complete(
                        client, settings, bound_doc,
                        sha256=sha, rules_v=rules_v, source_labels=src_labels,
                        source_rev=bound_rev,
                    ):
                        files_ok += 1
                        log.info(json.dumps({"path": path_str, "sha256": record.sha256, "action": "skip"}))
                        continue
                    log.info(
                        json.dumps(
                            {
                                "path": path_str,
                                "sha256": sha[:16],
                                "action": "requeue",
                                "reason": "no_valid_completion",
                            }
                        )
                    )
                # sha passes through: the parent hashed for the skip check, so the
                # worker never re-reads the file for hashing. Embedding flag keeps
                # the --dry-run contract (parse + chunk only, no embeddings).
                # Cache path travels with the task because spawn workers share no
                # memory with the parent (None when contextual ingest is off).
                tasks.append(
                    (
                        path_str, vendor or detect_vendor(Path(path_str)), product, version, str(src), sha,
                        not dry_run, str(cache_path) if cache_path is not None else None,
                    )
                )
            plan_span.set_attribute("ingest.pdfs", len(walk_entries))
            plan_span.set_attribute("ingest.todo", len(tasks))
            root.set_attribute("ingest.workers", workers)
            root.set_attribute("ingest.pdfs", len(walk_entries))
            root.set_attribute("ingest.todo", len(tasks))

        if not dry_run:
            # Representation contract, opened AFTER the identity gate: a
            # colliding corpus aborts in planning with zero Qdrant writes,
            # and steady-state reruns stay zero-write. A re-embed-required
            # change is declared `pending` BEFORE any document is deleted or
            # re-embedded (issue #391 F2) and is flipped to `committed` only
            # on the success path below, after the residue proof — an
            # interrupted migration can never certify old vectors as the new
            # representation.
            assert client is not None
            manifest_d, manifest_mode = begin_manifest(
                client, completion_collection_name(settings), settings, rules_v
            )
            log.info(
                json.dumps(
                    {
                        "action": "representation",
                        "collection": settings.qdrant_collection,
                        "manifest_digest": manifest_d,
                        "result": manifest_mode,
                        "model_revision_attested": bool(settings.embed_model_revision),
                    }
                )
            )

        log.info(
            json.dumps(
                {
                    "action": "start",
                    "pdfs": len(walk_entries),
                    "todo": len(tasks),
                    "workers": workers,
                    "upsert_streams": settings.ingest_upsert_streams,
                    "bulk_load": bulk,
                }
            )
        )
        if not tasks:
            # Nothing to do (all skipped): still emit the run summary. A
            # pending migration with zero tasks commits only if the residue
            # proof passes (an empty corpus must not certify stale vectors).
            if manifest_mode == STATE_PENDING:
                assert client is not None
                _commit_migration_representation(client, settings, rules_v)
            root.set_attribute("ingest.files_ok", files_ok)
            root.set_attribute("ingest.files_failed", 0)
            root.set_attribute("ingest.chunks_upserted", chunks_upserted)
            _log_summary(
                started, files_ok, files_failed, chunks_upserted, failures=0,
                parse_seconds=parse_seconds, upsert_seconds=upsert_seconds,
                pages=pages_seen, bulk_load=bulk,
            )
            return 0

        ctx = mp.get_context("spawn")
        failures = 0
        # Combined in-flight budget: parse_pending + upsert_pending is capped
        # at window so slow upserts never let the parent hold unbounded
        # parsed/embedded docs and vectors in RAM.
        window = max(2, workers * 2)
        task_iter = iter(tasks)
        locks = _DocLocks()
        # Stage 2: dedicated upsert streams (Qdrant skill: 2-4 parallel
        # upload streams). Embedding is done in stage-1 workers; these
        # threads are I/O-bound against Qdrant. Skipped during dry runs.
        parse_pending: dict[concurrent.futures.Future, str] = {}
        # Upsert futures carry their inventory record plus the precomputed
        # generation binding (parent has chunks before submitting; the
        # worker thread must not recompute digests divergently).
        upsert_pending: dict[
            concurrent.futures.Future,
            tuple[InventoryRecord, str | None, str | None, str | None],
        ] = {}

        def submit_parse(task: tuple[str, str | None, str | None, str | None, str, str, bool, str | None]) -> None:
            parse_pending[pool.submit(_parse_one, task)] = task[0]

        def refill_parse() -> None:
            while len(parse_pending) + len(upsert_pending) < window:
                next_task = next(task_iter, None)
                if next_task is None:
                    break
                submit_parse(next_task)

        upsert_ctx = (
            ThreadPoolExecutor(max_workers=settings.ingest_upsert_streams)
            if not dry_run
            else nullcontext()
        )

        with (
            ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool,
            upsert_ctx as upsert_pool,
        ):
            refill_parse()

            while parse_pending or upsert_pending:
                done, _ = concurrent.futures.wait(
                    set(parse_pending) | set(upsert_pending),
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    if future in parse_pending:
                        path_str = parse_pending.pop(future)
                        try:
                            record, parsed, chunks, vectors, contexts = future.result()
                        except Exception as exc:  # noqa: BLE001 — one bad PDF must not kill the run
                            failures += 1
                            files_failed += 1
                            append_record(
                                progress,
                                InventoryRecord(
                                    path=path_str,
                                    sha256="",
                                    status="error",
                                    error=str(exc)[:500],
                                    error_type=type(exc).__name__,
                                ),
                            )
                            log.error(
                                json.dumps(
                                    {
                                        "path": path_str,
                                        "action": "error",
                                        "error_type": type(exc).__name__,
                                        "error": str(exc)[:500],
                                    }
                                )
                            )
                            refill_parse()
                            continue
                        if record.status == "error":
                            failures += 1
                            files_failed += 1
                            append_record(progress, record)
                            log.error(
                                json.dumps(
                                    {
                                        "path": path_str,
                                        "action": "error",
                                        "error_type": record.error_type,
                                        "error": record.error,
                                    }
                                )
                            )
                            refill_parse()
                            continue
                        pages_seen += record.pages
                        parse_seconds += record.seconds

                        if dry_run:
                            record.status = "dry"
                            append_record(progress, record)
                            files_ok += 1
                            log.info(
                                json.dumps(
                                    {
                                        "path": path_str,
                                        "doc_id": record.doc_id,
                                        "chunks": record.chunks,
                                        "action": "dry",
                                    }
                                )
                            )
                            refill_parse()
                            continue

                        assert upsert_pool is not None
                        if contexts:
                            # Cache-first: preserve the expensive LLM work even
                            # if the upsert below fails. Non-empty contexts
                            # imply cache_path was resolved (workers only
                            # generate when the parent validated + passed it).
                            assert cache_path is not None
                            append_context_entries(cache_path, parsed.sha256, contexts)
                        if len(chunks) == 0:
                            binding: tuple[str | None, str | None, str | None] = (None, None, None)
                        else:
                            n, ids_d, content_d = expected_digests(chunks)
                            if n != len(chunks) or n != record.chunks:
                                raise RuntimeError(
                                    f"binding digest mismatch for {record.path}: "
                                    f"{n} digested vs {len(chunks)} chunks vs "
                                    f"{record.chunks} recorded — refusing to bind."
                                )
                            binding = (
                                doc_generation_id(settings, parsed.sha256, rules_v, src_labels),
                                ids_d,
                                content_d,
                            )
                        upsert_pending[
                            upsert_pool.submit(
                                _upsert_one, parsed, chunks, vectors, settings, locks,
                                contexts or None, force_reingest, src_labels=src_labels,
                                lineage_rev=lineage_by_path.get(path_str),
                            )
                        ] = (record, *binding)
                        refill_parse()
                    else:  # upsert stream result
                        record, generation_id, ids_digest, content_digest = upsert_pending.pop(future)
                        try:
                            status, seconds = future.result()
                        except Exception as exc:  # noqa: BLE001 — one bad PDF must not kill the run
                            failures += 1
                            files_failed += 1
                            record.status = "error"
                            record.error = str(exc)[:500]
                            record.error_type = type(exc).__name__
                            log.error(
                                json.dumps(
                                    {
                                        "path": record.path,
                                        "doc_id": record.doc_id,
                                        "action": "error",
                                        "error_type": record.error_type,
                                        "error": record.error,
                                    }
                                )
                            )
                        else:
                            upsert_seconds += seconds
                            record.status = status
                            if status in ("upserted", "skipped"):
                                # Bind the inventory line to the committed
                                # generation (req 3); legacy lines without a
                                # binding never skip on their own.
                                record.generation_id = generation_id
                                record.chunk_ids_digest = ids_digest
                                record.content_digest = content_digest
                            elif status == "empty":
                                failures += 1
                                files_failed += 1
                                record.error = "document produced zero chunks; nothing published"
                                record.error_type = "EmptyDocument"
                                log.error(
                                    json.dumps(
                                        {
                                            "path": record.path,
                                            "doc_id": record.doc_id,
                                            "action": "empty",
                                            "error_type": record.error_type,
                                        }
                                    )
                                )
                        if record.status in ("upserted", "skipped"):
                            # "skipped" = verified completion for this
                            # generation already committed — still ok.
                            files_ok += 1
                        elif record.status == "empty":
                            pass  # already counted as failed above
                        if record.status == "upserted":
                            chunks_upserted += record.chunks
                        append_record(progress, record)
                        refill_parse()
        # Commit the pending migration contract inside the bulk window (it
        # is part of the load) and only when no document failed — the scope
        # proof runs here, before any summary claims success (issue #391 F2).
        if failures == 0 and manifest_mode == STATE_PENDING:
            assert client is not None
            _commit_migration_representation(client, settings, rules_v)
    finally:
        if run_lock is not None:
            release_run_lock(run_lock)
        if bulk_active and client is not None:
            # Restore the default indexing threshold; the optimizer rebuilds
            # HNSW in the background after the run (status yellow -> green).
            try:
                set_bulk_indexing(client, settings.qdrant_collection, bulk=False)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    json.dumps(
                        {"action": "restore_bulk_indexing_failed", "error": str(exc)[:200]}
                    )
                )

    root.set_attribute("ingest.files_ok", files_ok)
    root.set_attribute("ingest.files_failed", files_failed)
    root.set_attribute("ingest.chunks_upserted", chunks_upserted)
    root.set_attribute("ingest.pages", pages_seen)
    if failures:
        root.set_status(Status(StatusCode.ERROR, "document failures"))
        if manifest_mode == STATE_PENDING:
            # Scope proof needs every walked document re-embedded; failures
            # leave the contract pending (never certified over partial work).
            log.warning(
                json.dumps(
                    {
                        "action": "representation",
                        "collection": settings.qdrant_collection,
                        "result": STATE_PENDING,
                        "note": "document failures blocked the commit; resume with --reingest",
                    }
                )
            )
    _log_summary(
        started, files_ok, files_failed, chunks_upserted, failures,
        parse_seconds=parse_seconds, upsert_seconds=upsert_seconds,
        pages=pages_seen, bulk_load=bulk,
    )
    return 1 if failures else 0


def _commit_migration_representation(
    client, settings: Settings, rules_v: str
) -> None:
    """Success-path commit of a pending migration contract (issue #391 F2):
    prove no completion marker under another contract remains, then flip
    pending -> committed. Raises — leaving the contract pending — when the
    migration scope is incomplete, so serving/skips stay blocked."""
    digest = manifest_digest(settings, rules_v)
    count, labels = stale_completion_markers(client, settings, digest)
    if count:
        raise RuntimeError(
            f"representation migration incomplete: {count} completion marker(s) remain "
            f"under another contract (e.g. {', '.join(labels)}) — their vectors were not "
            "re-embedded by this run and may still be searchable. Ingest the complete "
            "corpus (or remove the stale generation deliberately) before committing the "
            "new contract; it stays pending."
        )
    commit_manifest(client, completion_collection_name(settings), settings, rules_v)
    log.info(
        json.dumps(
            {
                "action": "representation",
                "collection": settings.qdrant_collection,
                "manifest_digest": digest,
                "result": "committed",
            }
        )
    )


def _run_publish(
    src: Path,
    progress: Path,
    workers: int | None,
    limit: int | None,
    settings: Settings,
    tracer: trace.Tracer,
    root: trace.Span,
    vendor: str | None = None,
    product: str | None = None,
    version: str | None = None,
    force_reingest: bool = False,
) -> int:
    """Alias-publication orchestration (issue #359 req 4/5): converge a    versioned staging generation, then atomically swap the alias to it.

    Refuses `--limit` subsets and empty corpora fail-closed (publication
    certifies the whole corpus). The inner pipeline runs unmodified against
    staging settings; only a fully verified staging swaps, and the
    superseded physical is kept for operator rollback/GC."""
    rules_v = extraction_rules_version()
    labels = source_labels(vendor, product, version)
    if limit is not None:
        raise RuntimeError(
            "INGEST_ALIAS_PUBLISH refuses --limit: publication certifies the "
            "whole corpus; rerun without --limit."
        )
    pdfs = walk_pdfs(src)
    if not pdfs:
        raise RuntimeError(
            "INGEST_ALIAS_PUBLISH refuses an empty corpus: publishing nothing "
            "would leave the alias serving an empty generation."
        )
    prewalked = [(str(p), sha256_file(p)) for p in pdfs]
    # Identity gate (issue #361) before the corpus fingerprint and staging:
    # publication certifies the gated corpus — a colliding or duplicated
    # walk must fail here, never after a staging generation was cloned.
    prewalked = _gate_planned_entries(src, prewalked)
    client = _get_qdrant(settings)
    live, legacy = resolve_live_collection(client, settings)
    gen_fp = generation_fingerprint(settings, rules_v, labels)
    corp_fp = corpus_fingerprint(prewalked)
    staging = resolve_staging_name(
        client,
        settings.qdrant_collection,
        gen_fp,
        corp_fp,
        live,
        force_reingest=force_reingest,
    )
    staging_settings = settings.model_copy(update={"qdrant_collection": staging})
    if live == staging and not force_reingest:
        # Steady state: the derived generation is already live. Re-verify it
        # read-only instead of cloning onto itself. The representation
        # read-only check rides along: it is the one place record-only drift
        # (a new dense query prefix keeps the same staging name — and is
        # never a re-embed trigger) is acknowledged, and it raises on a
        # pending contract (interrupted run of the same representation).
        _, record_drift = check_ingest_compatible(
            client,
            staging_settings,
            completion_collection_name(staging_settings),
            rules_v,
        )
        _log_record_drift(staging_settings, record_drift)
        problems = verify_all_complete(
            client, staging_settings, prewalked, load_inventory(progress), rules_v, labels,
        )
        if problems:
            raise RuntimeError(
                f"live generation {live!r} fails verification for {len(problems)} "
                f"path(s) (e.g. {problems[0]!r}) — operator intervention required."
            )
        log.info(
            json.dumps(
                {
                    "action": "publish",
                    "alias": settings.qdrant_collection,
                    "physical": live,
                    "result": "already_live",
                    "docs": len(prewalked),
                }
            )
        )
        return 0
    base = staging_name_for(settings.qdrant_collection, gen_fp, corp_fp)
    if force_reingest and live is not None and (live == base or live.startswith(f"{base}_")):
        require_in_place_reconverge(
            client, staging_settings, completion_collection_for(live), rules_v
        )
    mode = ensure_staging(client, settings, staging_settings, live)
    log.info(
        json.dumps(
            {
                "action": "publish_stage",
                "alias": settings.qdrant_collection,
                "staging": staging,
                "live": live,
                "mode": mode,
            }
        )
    )
    target = PublishTarget(alias=settings.qdrant_collection, staging=staging, live=live, legacy=legacy)
    rc = _run_impl(
        src, progress, workers, None, False, staging_settings, tracer, root,
        vendor=vendor, product=product, version=version,
        force_reingest=force_reingest, prewalked=prewalked, _publish_target=target,
    )
    if rc != 0:
        return rc
    inv = load_inventory(progress)
    swept = sweep_unmarked_residue(client, staging_settings, prewalked, inv)
    if swept:
        log.info(json.dumps({"action": "publish_sweep", "staging": staging, "swept_points": swept}))
    problems = verify_all_complete(
        client, staging_settings, prewalked, inv, rules_v, labels,
    )
    if problems:
        raise RuntimeError(
            f"staging {staging!r} incomplete for {len(problems)} path(s) "
            f"(e.g. {problems[0]!r}) — alias untouched, {live!r} still live."
        )
    if staging == live:
        raise RuntimeError(
            f"Invariant D4 violation: publication attempted on serving collection {live!r} in place."
        )
    previous = live
    migrated = None
    if legacy and live is not None:
        # A legacy physical squats on the alias name: preserve it, then clear
        # the name (brief maintenance window, documented in docs/ingest.md).
        snap = snapshot_collection(client, live)
        log.info(json.dumps({"action": "publish_migrate", "legacy": live, "snapshot": snap}))
        client.delete_collection(live)
        migrated = live
        previous = None
    summary = swap_alias_to(client, settings, staging, previous)
    summary["docs"] = str(len(prewalked))
    summary["staging_mode"] = mode
    if migrated is not None:
        summary["migrated_legacy"] = migrated
    log.info(json.dumps({"action": "publish", **{k: str(v) for k, v in summary.items()}}))
    return 0


def _log_summary(
    started: float,
    files_ok: int,
    files_failed: int,
    chunks_upserted: int,
    failures: int,
    parse_seconds: float,
    upsert_seconds: float,
    pages: int,
    bulk_load: bool,
) -> None:
    """One 'done' summary per run (issue #20 PR D): files ok / failed /
    chunks upserted / phase seconds / pages_per_s / elapsed_ms. Warning
    level when anything failed."""
    wall = time.monotonic() - started
    payload = {
        "action": "done",
        "files_ok": files_ok,
        "files_failed": files_failed,
        "chunks_upserted": chunks_upserted,
        "parse_s": round(parse_seconds, 1),
        "upsert_s": round(upsert_seconds, 1),
        "pages_per_s": round(pages / wall, 1) if wall > 0 and pages else 0.0,
        "bulk_load": bulk_load,
        "elapsed_ms": int(wall * 1000),
    }
    if failures:
        log.warning(json.dumps(payload))
    else:
        log.info(json.dumps(payload))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PDF ingest into Qdrant")
    parser.add_argument("--src", required=True, type=Path, help="Corpus root (read-only)")
    parser.add_argument("--progress", required=True, type=Path, help="Inventory JSONL path")
    parser.add_argument("--workers", type=int, default=None, help="Default CPU-1")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N PDFs")
    parser.add_argument("--vendor", default=None)
    parser.add_argument("--product", default=None)
    parser.add_argument("--version", default=None)
    parser.add_argument(
        "--reingest",
        action="store_true",
        help="Re-extract every doc (bypass inventory and Qdrant sha skips); "
        "required after an extraction-rules change so payloads match the "
        "current rules (issue #124)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Parse + chunk only; no Qdrant, no embeddings"
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    configure_logging(args.log_level)
    return run(
        args.src,
        args.progress,
        args.workers or None,  # --workers 0 means "default", not "1 worker"
        args.limit,
        args.dry_run,
        vendor=args.vendor,
        product=args.product,
        version=args.version,
        force_reingest=args.reingest,
    )


if __name__ == "__main__":
    sys.exit(main())
