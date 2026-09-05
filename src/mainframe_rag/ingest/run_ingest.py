"""Ingest CLI (air-gap Job).

    python -m mainframe_rag.ingest.run_ingest --src /corpus --progress /work/inventory.jsonl

Process pool, one PDF per worker (workers = CPU-1). Skip rules:
- inventory says this sha256 already upserted
- Qdrant already holds this doc_id with the same sha256
If Qdrant holds the doc_id with a different sha256, delete by doc_id then re-upsert.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import multiprocessing as mp
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pymupdf

from mainframe_rag.config import Settings, load_settings
from mainframe_rag.ingest.chrome import strip_chrome
from mainframe_rag.ingest.chunk import Chunk, make_chunks
from mainframe_rag.ingest.context import (
    ContextLLMClient,
    append_context_entries,
    generate_contexts,
    load_context_cache,
    resolve_cache_path,
)
from mainframe_rag.ingest.embed import build_embedder, embed_batch
from mainframe_rag.ingest.ibm_pdf import ParsedDoc, parse_pdf, sanitize_page_text, sha256_file
from mainframe_rag.ingest.inventory import (
    InventoryRecord,
    append_record,
    load_inventory,
    should_skip,
)
from mainframe_rag.ingest.qdrant_io import (
    delete_by_doc,
    doc_sha256,
    ensure_collection,
    set_bulk_indexing,
    stored_rules_version,
    upsert_chunks,
)
from mainframe_rag.ingest.rules_version import extraction_rules_version
from mainframe_rag.ingest.walk import detect_vendor, walk_pdfs
from mainframe_rag.logs import configure_logging
from mainframe_rag.ports import SparseVector

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
    """Per-doc_id locks for the upsert stage. Two files may legitimately
    claim one doc_id (shared form numbers); their check-delete-upsert
    sequence must not interleave across the parallel streams.

    Locks are retained in memory for the run: bounded by the unique doc_ids
    in the corpus (~hundreds of entries), so eviction is unnecessary."""
    _global: threading.Lock
    _locks: dict[str, threading.Lock]

    def __init__(self) -> None:
        self._global = threading.Lock()
        self._locks = {}

    def get(self, doc_id: str) -> threading.Lock:
        with self._global:
            return self._locks.setdefault(doc_id, threading.Lock())


def _upsert_one(
    parsed: ParsedDoc,
    chunks: list[Chunk],
    vectors: list[tuple[list[float], SparseVector]],
    settings: Settings,
    locks: _DocLocks,
    contexts: dict[str, str] | None = None,
    force_reingest: bool = False,
) -> tuple[str, float]:
    """Stage 2 (upsert stream): qdrant-level skip, delete-on-sha-mismatch,
    batched upsert. Vectors arrive precomputed from the parse worker. The
    doc_id lock keeps colliding docs from interleaving. Returns
    (status, seconds). `force_reingest` (--reingest, issue #124) turns the
    stored-sha-equal early return into delete+re-upsert: after a rules
    change the payload content is stale even though the file bytes are
    identical, so the sha check alone must never skip."""
    started = time.perf_counter()
    client = _get_qdrant(settings)
    with locks.get(parsed.doc_id):
        stored_sha = doc_sha256(client, settings, parsed.doc_id)
        if stored_sha == parsed.sha256 and not force_reingest:
            return "skipped", round(time.perf_counter() - started, 3)
        if stored_sha is not None:
            delete_by_doc(client, settings, parsed.doc_id)
        upserted = upsert_chunks(client, settings, parsed, chunks, vectors, contexts)
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
    settings = load_settings()
    workers = resolve_workers(workers, settings)
    rules_v = extraction_rules_version()
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
    if not dry_run:
        client = _get_qdrant(settings)
        ensure_collection(client, settings)
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
        if bulk:
            # Qdrant skill: HNSW builds must not compete with a bulk load.
            set_bulk_indexing(client, settings.qdrant_collection, bulk=True)
            bulk_active = True
    try:
        pdfs = walk_pdfs(src)
        if limit:
            pdfs = pdfs[:limit]
        inventory = load_inventory(progress)

        tasks: list[tuple[str, str | None, str | None, str | None, str, str, bool, str | None]] = []
        for path in pdfs:
            record = inventory.get(str(path))
            sha = sha256_file(path)
            if record and should_skip(record, sha, allow_dry=dry_run, rules_version=rules_v):
                files_ok += 1  # already ingested — an ok outcome
                log.info(json.dumps({"path": str(path), "sha256": record.sha256, "action": "skip"}))
                continue
            # sha passes through: the parent hashed for the skip check, so the
            # worker never re-reads the file for hashing. Embedding flag keeps
            # the --dry-run contract (parse + chunk only, no embeddings).
            # Cache path travels with the task because spawn workers share no
            # memory with the parent (None when contextual ingest is off).
            tasks.append(
                (
                    str(path), vendor or detect_vendor(path), product, version, str(src), sha,
                    not dry_run, str(cache_path) if cache_path is not None else None,
                )
            )

        log.info(
            json.dumps(
                {
                    "action": "start",
                    "pdfs": len(pdfs),
                    "todo": len(tasks),
                    "workers": workers,
                    "upsert_streams": settings.ingest_upsert_streams,
                    "bulk_load": bulk,
                }
            )
        )
        if not tasks:
            # Nothing to do (all skipped): still emit the run summary.
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
        upsert_pending: dict[concurrent.futures.Future, InventoryRecord] = {}

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
                        upsert_pending[
                            upsert_pool.submit(
                                _upsert_one, parsed, chunks, vectors, settings, locks,
                                contexts or None, force_reingest,
                            )
                        ] = record
                        refill_parse()
                    else:  # upsert stream result
                        record = upsert_pending.pop(future)
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
                        if record.status in ("upserted", "skipped"):
                            # "skipped" = Qdrant already holds doc_id at this
                            # sha256 — still an ok outcome.
                            files_ok += 1
                        if record.status == "upserted":
                            chunks_upserted += record.chunks
                        append_record(progress, record)
                        refill_parse()
    finally:
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

    _log_summary(
        started, files_ok, files_failed, chunks_upserted, failures,
        parse_seconds=parse_seconds, upsert_seconds=upsert_seconds,
        pages=pages_seen, bulk_load=bulk,
    )
    return 1 if failures else 0


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
