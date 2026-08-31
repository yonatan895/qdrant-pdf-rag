#!/usr/bin/env python3
"""Micro-benchmarking and profiling tool for the mainframe RAG pipeline.

Isolates and measures latency and throughput of each pipeline stage:
1. PDF parsing and text/label extraction (PyMuPDF)
2. Running header/footer chrome stripping
3. Outline parsing and chunk creation
4. Embedding generation (Dense, FastEmbed BM25 sparse, and concurrent batch)
5. Model output parsing and citation validation

Usage:
    python scripts/profile_pipeline.py --docs 10
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from make_synthetic_pdf import build as build_synthetic_pdf

from mainframe_rag.agent.answer import parse_answer
from mainframe_rag.ingest.chrome import strip_chrome
from mainframe_rag.ingest.chunk import make_chunks
from mainframe_rag.ingest.embed import HashEmbedder, build_embed_text, embed_batch
from mainframe_rag.ingest.ibm_pdf import parse_pdf


def profile_pdf_parsing_and_chunking(corpus_dir: Path, num_docs: int) -> dict:
    """Measure single-doc parsing, text/label extraction, chrome stripping, and chunking."""
    pdf_paths: list[Path] = []
    for i in range(num_docs):
        p = corpus_dir / f"SA22-7{i:03d}-01.pdf"
        build_synthetic_pdf(
            p,
            doc_id=f"SA22-7{i:03d}-01",
            title=f"Synthetic Reference Manual Volume {i}",
            message_id=f"IEA5{i:02d}I",
        )
        pdf_paths.append(p)

    import pymupdf

    t0 = time.perf_counter()
    total_pages = 0
    parsed_docs = []
    for p in pdf_paths:
        parsed = parse_pdf(p, corpus_root=corpus_dir)
        doc = pymupdf.open(p)
        try:
            page_texts: list[str] = []
            page_labels: list[str | None] = []
            for i in range(doc.page_count):
                page = doc[i]
                page_texts.append(page.get_text())
                page_labels.append(page.get_label())
        finally:
            doc.close()
        total_pages += parsed.page_count
        stripped = strip_chrome(page_texts)
        chunks = make_chunks(parsed, stripped, page_labels)
        parsed_docs.append((parsed, chunks))
    elapsed = time.perf_counter() - t0

    total_chunks = sum(len(chunks) for _, chunks in parsed_docs)
    return {
        "docs": num_docs,
        "total_pages": total_pages,
        "total_chunks": total_chunks,
        "elapsed_s": round(elapsed, 4),
        "docs_per_s": round(num_docs / elapsed, 2) if elapsed > 0 else 0,
        "pages_per_s": round(total_pages / elapsed, 2) if elapsed > 0 else 0,
        "chunks_per_s": round(total_chunks / elapsed, 2) if elapsed > 0 else 0,
        "sample_parsed_docs": parsed_docs,
    }


def profile_embedding(chunks_data: list) -> dict:
    """Measure embedding throughput (HashEmbedder in-memory simulation)."""
    embedder = HashEmbedder()
    all_chunks = []
    for parsed, chunks in chunks_data:
        all_chunks.extend([(c, parsed.product, parsed.version, parsed.title) for c in chunks])

    texts = [build_embed_text(prod, ver, c.doc_id, title, c.heading_path, c.text) for c, prod, ver, title in all_chunks]

    t0 = time.perf_counter()
    _ = embedder.dense(texts)
    dense_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    _ = embedder.sparse(texts)
    sparse_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    for parsed, chunks in chunks_data:
        _ = embed_batch(chunks, parsed.product, parsed.version, parsed.title, embedder)
    batch_s = time.perf_counter() - t0

    return {
        "total_chunks": len(texts),
        "dense_s": round(dense_s, 4),
        "sparse_s": round(sparse_s, 4),
        "batch_total_s": round(batch_s, 4),
        "dense_chunks_per_s": round(len(texts) / dense_s, 2) if dense_s > 0 else 0,
        "sparse_chunks_per_s": round(len(texts) / sparse_s, 2) if sparse_s > 0 else 0,
    }


def profile_answer_parsing(iterations: int = 1000) -> dict:
    """Measure citation extraction, code fence extraction, and answer parsing."""
    sample_content = (
        "Based on the excerpt, command IEA500I indicates initialization wait.\n\n"
        "```jcl\n//JOB1 JOB ...\n//STEP1 EXEC PGM=IEFBR14\n```\n\n"
        "Citations:\n"
        "SA22-0000-00 Synthetic Reference Volume 0 > Chapter 1 > System parameters, p. 1-3\n"
    )
    allowed = {"SA22-0000-00 Synthetic Reference Volume 0 > Chapter 1 > System parameters, p. 1-3"}
    ordered = list(allowed)

    t0 = time.perf_counter()
    for _ in range(iterations):
        parse_answer(sample_content, allowed, ordered_cites=ordered)
    elapsed = time.perf_counter() - t0

    return {
        "iterations": iterations,
        "elapsed_s": round(elapsed, 4),
        "parses_per_s": round(iterations / elapsed, 2) if elapsed > 0 else 0,
        "latency_us_per_parse": round((elapsed / iterations) * 1_000_000, 2) if iterations > 0 else 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Profile RAG pipeline micro-benchmarks")
    parser.add_argument("--docs", type=int, default=10, help="Number of synthetic documents to profile")
    args = parser.parse_args()

    print("=" * 60)
    print(" PIPELINE MICRO-BENCHMARK PROFILER")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp_dir:
        corpus_dir = Path(tmp_dir)
        print(f"\n[1] Profiling PDF parsing, chrome stripping, and chunking ({args.docs} docs)...")
        parse_results = profile_pdf_parsing_and_chunking(corpus_dir, args.docs)
        print(f"    - Processed {parse_results['docs']} docs / {parse_results['total_pages']} pages in {parse_results['elapsed_s']}s")
        print(f"    - Throughput: {parse_results['docs_per_s']} docs/s ({parse_results['pages_per_s']} pages/s, {parse_results['chunks_per_s']} chunks/s)")

        print("\n[2] Profiling embedding vector generation...")
        embed_results = profile_embedding(parse_results["sample_parsed_docs"])
        print(f"    - Dense: {embed_results['dense_chunks_per_s']} chunks/s")
        print(f"    - Sparse: {embed_results['sparse_chunks_per_s']} chunks/s")
        print(f"    - Total batch embedding time: {embed_results['batch_total_s']}s")

        print("\n[3] Profiling answer parsing & citation validation (1000 ops)...")
        answer_results = profile_answer_parsing(1000)
        print(f"    - {answer_results['parses_per_s']} parses/s ({answer_results['latency_us_per_parse']} µs per parse)")

    print("\n" + "=" * 60)
    print(" Profiling completed successfully.")
    print("=" * 60)


if __name__ == "__main__":
    main()
