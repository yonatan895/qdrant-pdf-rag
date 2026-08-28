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
import time
from concurrent.futures import ProcessPoolExecutor
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
    upsert_chunks,
)
from mainframe_rag.ingest.walk import detect_vendor, walk_pdfs
from mainframe_rag.logs import configure_logging

log = logging.getLogger("ingest")

_worker_settings: Settings | None = None
_worker_qdrant = None
_worker_embedder = None


def _parse_one(
    args: tuple[str, str | None, str | None, str | None, str],
) -> tuple[InventoryRecord, ParsedDoc, list[Chunk]]:
    import pymupdf

    path_str, vendor, product, version, corpus_root = args
    path = Path(path_str)
    started = time.monotonic()
    sha = sha256_file(path)
    parsed = parse_pdf(
        path,
        vendor=vendor,
        product=product,
        version=version,
        corpus_root=Path(corpus_root) if corpus_root else None,
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
    record = InventoryRecord(
        path=path_str,
        sha256=sha,
        doc_id=parsed.doc_id,
        pages=parsed.page_count,
        chunks=len(chunks),
        seconds=round(time.monotonic() - started, 3),
    )
    return record, parsed, chunks


def resolve_workers(requested: int | None, settings: Settings) -> int:
    """INGEST_WORKERS (or the CLI override) is a cap, never 'spawn unbounded':
    clamp into [1, 2*CPU]. The pool is bounded either way; this keeps a bad
    env value from fanning out beyond the box."""
    cap = max(1, 2 * (mp.cpu_count() or 2))
    base = settings.ingest_workers if requested is None else requested
    return max(1, min(int(base), cap))


def _init_worker() -> None:
    global _worker_settings, _worker_qdrant
    _worker_settings = load_settings()
    _worker_qdrant = None


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


def _upsert_one(parsed: ParsedDoc, chunks: list[Chunk], settings: Settings) -> str:
    client = _get_qdrant(settings)
    stored_sha = doc_sha256(client, settings, parsed.doc_id)
    if stored_sha == parsed.sha256:
        return "skipped"
    if stored_sha is not None:
        delete_by_doc(client, settings, parsed.doc_id)

    started = time.monotonic()
    upserted = 0
    batch = settings.batch_size
    for i in range(0, len(chunks), batch):
        vecs = embed_batch(
            chunks[i : i + batch],
            parsed.product,
            parsed.version,
            parsed.title,
            _get_embedder(settings),
        )
        upserted += upsert_chunks(client, settings, parsed, chunks[i : i + batch], vecs)
    log.info(
        json.dumps(
            {
                "doc_id": parsed.doc_id,
                "pages": parsed.page_count,
                "chunks": upserted,
                "seconds": round(time.monotonic() - started, 3),
                "action": "upsert",
            }
        )
    )
    return "upserted"


def run(
    src: Path,
    progress: Path,
    workers: int,
    limit: int | None,
    dry_run: bool,
    vendor: str | None = None,
    product: str | None = None,
    version: str | None = None,
) -> int:
    settings = load_settings()
    started = time.monotonic()
    # Progress counters (issue #20 PR D): files ok / failed / chunks upserted,
    # logged once per run. Logs carry ids and counts, never PDF text.
    files_ok = 0
    files_failed = 0
    chunks_upserted = 0
    if not dry_run:
        ensure_collection(_get_qdrant(settings), settings)
    pdfs = walk_pdfs(src)
    if limit:
        pdfs = pdfs[:limit]
    inventory = load_inventory(progress)

    tasks: list[tuple[str, str | None, str | None, str | None, str]] = []
    for path in pdfs:
        record = inventory.get(str(path))
        if record and should_skip(record, sha256_file(path), allow_dry=dry_run):
            files_ok += 1  # already ingested — an ok outcome
            log.info(json.dumps({"path": str(path), "sha256": record.sha256, "action": "skip"}))
            continue
        tasks.append((str(path), vendor or detect_vendor(path), product, version, str(src)))

    log.info(
        json.dumps({"action": "start", "pdfs": len(pdfs), "todo": len(tasks), "workers": workers})
    )
    if not tasks:
        # Nothing to do (all skipped): still emit the run summary.
        _log_summary(started, files_ok, files_failed, chunks_upserted, failures=0)
        return 0

    workers = resolve_workers(workers, settings)
    ctx = mp.get_context("spawn")
    failures = 0
    # Backpressure: keep at most 2 results per worker in flight. Parsing a
    # large PDF holds its whole chunk list in memory; submitting everything up
    # front could hold every PDF's parsed result in memory at once.
    window = max(2, workers * 2)
    task_iter = iter(tasks)
    pending: dict[concurrent.futures.Future, str] = {}

    def submit(task: tuple[str, str | None, str | None, str | None, str]) -> None:
        pending[pool.submit(_parse_one, task)] = task[0]

    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx, initializer=_init_worker) as pool:
        for task in itertools.islice(task_iter, window):
            submit(task)

        while pending:
            done, _ = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                path_str = pending.pop(future)
                # Refill one-for-one so the window never grows.
                next_task = next(task_iter, None)
                if next_task is not None:
                    submit(next_task)
                try:
                    record, parsed, chunks = future.result()
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

                try:
                    record.status = _upsert_one(parsed, chunks, settings)
                except Exception as exc:  # noqa: BLE001 — one bad PDF must not kill the run
                    failures += 1
                    files_failed += 1
                    record.status = "error"
                    record.error = str(exc)[:500]
                    record.error_type = type(exc).__name__
                    log.error(
                        json.dumps(
                            {
                                "path": path_str,
                                "doc_id": record.doc_id,
                                "action": "error",
                                "error_type": record.error_type,
                                "error": record.error,
                            }
                        )
                    )
                if record.status in ("upserted", "skipped"):
                    # "skipped" = Qdrant already holds doc_id at this sha256
                    # (fresh inventory, warm Qdrant) — still an ok outcome.
                    files_ok += 1
                if record.status == "upserted":
                    chunks_upserted += record.chunks
                append_record(progress, record)

    _log_summary(started, files_ok, files_failed, chunks_upserted, failures)
    return 1 if failures else 0


def _log_summary(
    started: float, files_ok: int, files_failed: int, chunks_upserted: int, failures: int
) -> None:
    """One 'done' summary per run (issue #20 PR D): files ok / failed /
    chunks upserted / elapsed_ms. Warning level when anything failed."""
    payload = {
        "action": "done",
        "files_ok": files_ok,
        "files_failed": files_failed,
        "chunks_upserted": chunks_upserted,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
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
    workers = args.workers or load_settings().ingest_workers
    return run(
        args.src,
        args.progress,
        workers,
        args.limit,
        args.dry_run,
        vendor=args.vendor,
        product=args.product,
        version=args.version,
    )


if __name__ == "__main__":
    sys.exit(main())
