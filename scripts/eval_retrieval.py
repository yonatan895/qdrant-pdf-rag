#!/usr/bin/env python3
"""Retrieval accuracy eval: recall@k and MRR against a golden set.

This is the ruler for "increasing accuracy": retrieval changes (embedder,
chunking, RRF constants, filters) must show their delta here before they
merge (AGENTS.md). It runs the real pipeline - build_embedder + the retrieve
module + a real Qdrant - directly, without the agent HTTP hop.

Golden set (evals/golden.jsonl), one JSON object per line:
    {"query": "...", "expected_doc_ids": ["SA22-0000-00"],
     "expected_heading": "optional heading substring", "note": "why"}

Scoring is doc-level and never pins top-1 across potentially equal-text
chunks (AGENTS.md): a hit is relevant when its doc_id is in
expected_doc_ids AND (if given) the heading substring matches.

    EMBED_MODE=hash QDRANT_URL=http://127.0.0.1:6333 QDRANT_COLLECTION=local-corpus \
        python scripts/eval_retrieval.py --golden evals/golden.jsonl

    python scripts/eval_retrieval.py --label-draft --docs 40   # draft candidates
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mainframe_rag.config import load_settings
from mainframe_rag.retrieve.query import search as retrieve_search

SEARCH_LIMIT = 8  # headroom for recall@5


def load_golden(path: Path) -> list[dict]:
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entry = json.loads(line)
        if not entry.get("query") or not entry.get("expected_doc_ids"):
            raise SystemExit(f"golden entry needs query + expected_doc_ids: {line[:120]}")
        entries.append(entry)
    return entries


def score_entry(hits: list[dict], entry: dict) -> dict:
    """Relevance = doc_id in expected set AND heading substring (if given)."""
    expected = set(entry["expected_doc_ids"])
    heading = (entry.get("expected_heading") or "").lower()

    def relevant(hit: dict) -> bool:
        if hit["doc_id"] not in expected:
            return False
        return not heading or heading in hit["heading"].lower()

    reciprocal_rank = 0.0
    for rank, hit in enumerate(hits, 1):
        if relevant(hit):
            reciprocal_rank = 1.0 / rank
            break
    return {
        "query": entry["query"],
        "recall@1": 1.0 if hits[:1] and relevant(hits[0]) else 0.0,
        "recall@3": 1.0 if any(relevant(h) for h in hits[:3]) else 0.0,
        "recall@5": 1.0 if any(relevant(h) for h in hits[:5]) else 0.0,
        "mrr": reciprocal_rank,
        "hit_doc_ids": [h["doc_id"] for h in hits[:3]],
    }


def evaluate(golden: list[dict], settings) -> dict:
    from qdrant_client import QdrantClient

    from mainframe_rag.ingest.embed import build_embedder

    client = QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout=settings.qdrant_timeout_s,
    )
    embedder = build_embedder(settings)
    collection = settings.qdrant_collection

    rows, failures = [], 0
    started = time.perf_counter()
    for entry in golden:
        try:
            hits, kind, _timings = retrieve_search(
                client, embedder, collection, entry["query"], limit=SEARCH_LIMIT
            )
            rows.append(score_entry([asdict(h) for h in hits], entry))
            rows[-1]["kind"] = kind
        except (httpx.HTTPError, RuntimeError, OSError, ValueError) as exc:
            # One bad query must not kill the eval; counted as a failure.
            failures += 1
            rows.append({"query": entry["query"], "error": str(exc)[:200], "kind": "error"})

    def mean(key: str) -> float:
        scored = [r[key] for r in rows if key in r]
        return round(sum(scored) / len(scored), 3) if scored else 0.0

    def mean_by_kind(key: str, kind: str) -> float | None:
        scored = [r[key] for r in rows if key in r and r.get("kind") == kind]
        return round(sum(scored) / len(scored), 3) if scored else None

    return {
        "n": len(golden),
        "failures": failures,
        "elapsed_s": round(time.perf_counter() - started, 2),
        "embed_mode": settings.embed_mode,
        "collection": collection,
        "recall@1": mean("recall@1"),
        "recall@3": mean("recall@3"),
        "recall@5": mean("recall@5"),
        "mrr": mean("mrr"),
        "identifier": {k: mean_by_kind(k, "identifier") for k in ("recall@1", "recall@5", "mrr")},
        "nl": {k: mean_by_kind(k, "nl") for k in ("recall@1", "recall@5", "mrr")},
        "rows": rows,
    }


def summary_markdown(report: dict) -> str:
    lines = [
        "## Retrieval eval",
        "",
        (
            f"n={report['n']} failures={report['failures']} mode={report['embed_mode']} "
            f"collection={report['collection']} ({report['elapsed_s']}s)"
        ),
        "",
        "| metric | all | identifier | nl |",
        "|---|---|---|---|",
    ]
    for key in ("recall@1", "recall@3", "recall@5", "mrr"):
        lines.append(
            f"| {key} | {report[key]} | {report['identifier'].get(key)} | {report['nl'].get(key)} |"
        )
    lines += ["", "| query | kind | r@1 | r@5 | mrr | top hit doc_ids |", "|---|---|---|---|---|---|"]
    for row in report["rows"]:
        if "error" in row:
            lines.append(f"| {row['query'][:60]} | error | - | - | - | {row['error'][:40]} |")
        else:
            lines.append(
                f"| {row['query'][:60]} | {row.get('kind', '')} | {row['recall@1']:.0f} "
                f"| {row['recall@5']:.0f} | {row['mrr']:.2f} | {', '.join(row['hit_doc_ids'])[:60]} |"
            )
    return "\n".join(lines) + "\n"


def label_draft(collection: str, settings, docs: int) -> list[dict]:
    """Mechanically true draft entries from collection payload: identifier
    queries per doc (doc number, message ids) plus one heading-derived topic
    query per doc. Humans edit queries; expectations are payload facts."""
    from qdrant_client import QdrantClient

    client = QdrantClient(
        url=settings.qdrant_url, api_key=settings.qdrant_api_key,
        timeout=settings.qdrant_timeout_s,
    )
    points, _ = client.scroll(
        collection,
        limit=docs,
        with_payload=["doc_id", "title", "heading_path", "message_ids"],
    )
    drafts = []
    for point in points:
        payload = point.payload or {}
        doc_id = str(payload.get("doc_id") or "")
        title = str(payload.get("title") or "")
        heading = str(payload.get("heading_path") or "")
        if not doc_id:
            continue
        drafts.append({"query": doc_id, "expected_doc_ids": [doc_id], "note": f"title: {title[:60]}"})
        for msg in list(payload.get("message_ids") or [])[:1]:
            drafts.append({
                "query": str(msg), "expected_doc_ids": [doc_id],
                "note": f"message id in {title[:50]}",
            })
        leaf = heading.split(">")[-1].strip()
        if leaf:
            drafts.append({
                "query": leaf, "expected_doc_ids": [doc_id],
                "expected_heading": leaf.lower(),
                "note": f"heading of {title[:50]} - EDIT the query to read naturally",
            })
    # de-duplicate identical queries, keep first
    seen, unique = set(), []
    for d in drafts:
        if d["query"].lower() not in seen:
            seen.add(d["query"].lower())
            unique.append(d)
    return unique


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--golden", type=Path, default=None, help="golden JSONL path")
    parser.add_argument("--out", type=Path, default=None, help="write the JSON report here")
    parser.add_argument("--summary", type=Path, default=None, help="write a markdown table here")
    parser.add_argument(
        "--label-draft", action="store_true",
        help="draft golden entries from collection payload instead of scoring",
    )
    parser.add_argument("--docs", type=int, default=40, help="label-draft: docs to sample")
    args = parser.parse_args(argv)

    settings = load_settings()
    if args.label_draft:
        drafts = label_draft(settings.qdrant_collection, settings, args.docs)
        for d in drafts:
            print(json.dumps(d, ensure_ascii=False))
        return 0
    if args.golden is None:
        parser.error("--golden is required unless --label-draft is set")

    golden = load_golden(args.golden)
    report = evaluate(golden, settings)

    summary = summary_markdown(report)
    print(summary, file=sys.stderr)
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(summary)
    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload + "\n")
    else:
        print(payload)
    return 0 if report["failures"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
