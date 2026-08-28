"""Ingest CLI (air-gap Job).

    python -m mainframe_rag.ingest.run_ingest --src /corpus --progress /work/inventory.jsonl

Process pool, one PDF per worker (workers = CPU-1). Skip rules:
- inventory says this sha256 already upserted
- Qdrant already holds this doc_id with the same sha256
If Qdrant holds the doc_id with a different sha256 (revised edition), delete
by doc_id then re-upsert. OCR stays off for born-digital pages.
"""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing as mp
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from mainframe_rag.config import Settings, load_settings
from mainframe_rag.ingest.chrome import strip_chrome
from mainframe_rag.ingest.chunk import Chunk, make_chunks
from mainframe_rag.ingest.embed import embed_batch
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

log = logging.getLogger("ingest")

_worker_settings: Settings | None = None
_worker_qdrant = None


def _parse_one(args: tuple[str, str]) -> tuple[InventoryRecord, ParsedDoc, list[Chunk]]:
    """Parse + chrome strip + chunk. Runs in the worker process."""
    import pymupdf

    path_str, vendor = args
    path = Path(path_str)
    started = time.monotonic()
    sha = sha256_file(path)
    parsed = parse_pdf(path, vendor)
    doc = pymupdf.open(path)
    try:
        page_texts = [page.get_text() for page in doc]
        page_labels = [page.get_label() for page in doc]
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


def _init_worker() -> None:
    global _worker_settings, _worker_qdrant
    _worker_settings = load_settings()
    _worker_qdrant = None  # created lazily; dry-run workers never need it


def _get_qdrant(settings: Settings):
    from qdrant_client import QdrantClient

    global _worker_qdrant
    if _worker_qdrant is None:
        _worker_qdrant = QdrantClient(
            url=settings.qdrant_url, api_key=settings.qdrant_api_key, timeout=120
        )
    return _worker_qdrant


def _upsert_one(parsed: ParsedDoc, chunks: list[Chunk], settings: Settings) -> str:
    """Embed + upsert one document in the current process. Returns final status."""
    if not parsed.doc_id:
        return "error-no-doc-id"
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
            chunks[i : i + batch], parsed.product, parsed.version, parsed.title, settings
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


def run(src: Path, progress: Path, workers: int, limit: int | None, dry_run: bool) -> int:
    settings = load_settings()
    if not dry_run:
        ensure_collection(_get_qdrant(settings), settings)  # fail fast on DENSE_DIM
    pdfs = walk_pdfs(src)
    if limit:
        pdfs = pdfs[:limit]
    inventory = load_inventory(progress)

    tasks: list[tuple[str, str]] = []
    for path in pdfs:
        record = inventory.get(str(path))
        if record and should_skip(record, sha256_file(path), allow_dry=dry_run):
            log.info(json.dumps({"path": str(path), "sha256": record.sha256, "action": "skip"}))
            continue
        tasks.append((str(path), detect_vendor(path)))

    log.info(
        json.dumps({"action": "start", "pdfs": len(pdfs), "todo": len(tasks), "workers": workers})
    )
    if not tasks:
        return 0

    ctx = mp.get_context("spawn")
    failures = 0
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx, initializer=_init_worker) as pool:
        futures = {pool.submit(_parse_one, t): t[0] for t in tasks}
        for future in as_completed(futures):
            path_str = futures[future]
            try:
                record, parsed, chunks = future.result()
            except Exception as exc:  # noqa: BLE001 — per-file failure must not kill the run
                failures += 1
                append_record(
                    progress,
                    InventoryRecord(path=path_str, sha256="", status="error", error=str(exc)[:500]),
                )
                log.error(json.dumps({"path": path_str, "action": "error", "error": str(exc)[:500]}))
                continue

            if dry_run:
                record.status = "dry"
                append_record(progress, record)
                log.info(
                    json.dumps(
                        {"path": path_str, "doc_id": record.doc_id, "chunks": record.chunks, "action": "dry"}
                    )
                )
                continue

            try:
                record.status = _upsert_one(parsed, chunks, settings)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                record.status = "error"
                record.error = str(exc)[:500]
                log.error(
                    json.dumps(
                        {"path": path_str, "doc_id": record.doc_id, "action": "error", "error": record.error}
                    )
                )
            append_record(progress, record)

    if failures:
        log.warning(json.dumps({"action": "done", "failures": failures}))
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mainframe manuals ingest")
    parser.add_argument("--src", required=True, type=Path, help="Corpus root (read-only)")
    parser.add_argument("--progress", required=True, type=Path, help="Inventory JSONL path")
    parser.add_argument("--workers", type=int, default=None, help="Default CPU-1")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N PDFs (pilot)")
    parser.add_argument(
        "--dry-run", action="store_true", help="Parse + chunk only; no Qdrant, no embeddings"
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), format="%(message)s")
    workers = args.workers or load_settings().ingest_workers
    return run(args.src, args.progress, workers, args.limit, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
