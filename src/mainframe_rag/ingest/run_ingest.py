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
import itertools
import json
import logging
import multiprocessing as mp
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

from mainframe_rag.config import Settings, load_settings
from mainframe_rag.ingest.chrome import strip_chrome
from mainframe_rag.ingest.chunk import Chunk, make_chunks
from mainframe_rag.ingest.embed import build_embedder, embed_batch
from mainframe_rag.ingest.ibm_pdf import ParsedDoc, parse_pdf, sha256_file
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
    upsert_chunks,
)
from mainframe_rag.ingest.walk import detect_vendor, walk_pdfs
from mainframe_rag.logs import configure_logging
from mainframe_rag.ports import SparseVector

log = logging.getLogger("ingest")

_worker_qdrant = None
_worker_embedder = None
_worker_settings: Settings | None = None


def _parse_one(
    args: tuple[str, str | None, str | None, str | None, str, str, bool],
) -> tuple[InventoryRecord, ParsedDoc, list[Chunk], list[tuple[list[float], SparseVector]]]:
    """Stage 1 (parse worker): parse, chunk, and embed. Embedding lives in
    the worker because hash embed is Python/GIL-bound — in a thread pool it
    would serialize; in a process pool it scales with the parse pool.
    Dry runs embed nothing (the --dry-run contract: parse + chunk only)."""
    import pymupdf

    path_str, vendor, product, version, corpus_root, sha, embed = args
    path = Path(path_str)
    started = time.monotonic()
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
        # pymupdf 1.28 no longer types Document as iterable; index explicitly.
        page_texts = [doc[i].get_text() for i in range(doc.page_count)]
        page_labels = [doc[i].get_label() for i in range(doc.page_count)]
    finally:
        doc.close()
    stripped = strip_chrome(page_texts)
    chunks = make_chunks(parsed, stripped, page_labels)
    vectors = (
        embed_batch(
            chunks, parsed.product, parsed.version, parsed.title, _get_embedder(_load_worker_settings())
        )
        if embed
        else []
    )
    record = InventoryRecord(
        path=path_str,
        sha256=sha,
        doc_id=parsed.doc_id,
        pages=parsed.page_count,
        chunks=len(chunks),
        seconds=round(time.monotonic() - started, 3),
    )
    return record, parsed, chunks, vectors


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
    sequence must not interleave across the parallel streams."""
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
) -> tuple[str, float]:
    """Stage 2 (upsert stream): qdrant-level skip, delete-on-sha-mismatch,
    batched upsert. Vectors arrive precomputed from the parse worker. The
    doc_id lock keeps colliding docs from interleaving. Returns
    (status, seconds)."""
    started = time.perf_counter()
    client = _get_qdrant(settings)
    with locks.get(parsed.doc_id):
        stored_sha = doc_sha256(client, settings, parsed.doc_id)
        if stored_sha == parsed.sha256:
            return "skipped", round(time.perf_counter() - started, 3)
        if stored_sha is not None:
            delete_by_doc(client, settings, parsed.doc_id)
        upserted = upsert_chunks(client, settings, parsed, chunks, vectors)
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
) -> int:
    settings = load_settings()
    workers = resolve_workers(workers, settings)
    started = time.monotonic()
    # Progress counters (issue #20 PR D): files ok / failed / chunks upserted,
    # logged once per run. Logs carry ids and counts, never PDF text.
    files_ok = 0
    files_failed = 0
    chunks_upserted = 0
    parse_seconds = 0.0
    upsert_seconds = 0.0
    pages_seen = 0
    bulk = settings.ingest_bulk_load and not dry_run
    client = None
    if not dry_run:
        client = _get_qdrant(settings)
        ensure_collection(client, settings)
        if bulk:
            # Qdrant skill: HNSW builds must not compete with a bulk load.
            set_bulk_indexing(client, settings.qdrant_collection, bulk=True)
    try:
        pdfs = walk_pdfs(src)
        if limit:
            pdfs = pdfs[:limit]
        inventory = load_inventory(progress)

        tasks: list[tuple[str, str | None, str | None, str | None, str, str, bool]] = []
        for path in pdfs:
            record = inventory.get(str(path))
            sha = sha256_file(path)
            if record and should_skip(record, sha, allow_dry=dry_run):
                files_ok += 1  # already ingested — an ok outcome
                log.info(json.dumps({"path": str(path), "sha256": record.sha256, "action": "skip"}))
                continue
            # sha passes through: the parent hashed for the skip check, so the
            # worker never re-reads the file for hashing. Embedding flag keeps
            # the --dry-run contract (parse + chunk only, no embeddings).
            tasks.append(
                (str(path), vendor or detect_vendor(path), product, version, str(src), sha, not dry_run)
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
        # Backpressure: keep at most 2 results per worker in flight. Parsing a
        # large PDF holds its whole chunk list in memory; submitting everything
        # up front could hold every PDF's parsed result in memory at once.
        window = max(2, workers * 2)
        task_iter = iter(tasks)
        locks = _DocLocks()
        # Stage 2: dedicated upsert streams (Qdrant skill: 2-4 parallel
        # upload streams). Embedding is done in stage-1 workers; these
        # threads are I/O-bound against Qdrant.
        parse_pending: dict[concurrent.futures.Future, str] = {}
        upsert_pending: dict[concurrent.futures.Future, InventoryRecord] = {}

        def submit_parse(task: tuple[str, str | None, str | None, str | None, str, str, bool]) -> None:
            parse_pending[pool.submit(_parse_one, task)] = task[0]

        with (
            ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool,
            ThreadPoolExecutor(max_workers=settings.ingest_upsert_streams) as upsert_pool,
        ):
            for task in itertools.islice(task_iter, window):
                submit_parse(task)

            while parse_pending or upsert_pending:
                done, _ = concurrent.futures.wait(
                    set(parse_pending) | set(upsert_pending),
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    if future in parse_pending:
                        path_str = parse_pending.pop(future)
                        # Refill one-for-one so the window never grows.
                        next_task = next(task_iter, None)
                        if next_task is not None:
                            submit_parse(next_task)
                        try:
                            record, parsed, chunks, vectors = future.result()
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
                            continue

                        upsert_pending[
                            upsert_pool.submit(_upsert_one, parsed, chunks, vectors, settings, locks)
                        ] = record
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
    finally:
        if bulk and client is not None:
            # Restore the default indexing threshold; the optimizer rebuilds
            # HNSW in the background after the run (status yellow -> green).
            set_bulk_indexing(client, settings.qdrant_collection, bulk=False)

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
    )


if __name__ == "__main__":
    sys.exit(main())
