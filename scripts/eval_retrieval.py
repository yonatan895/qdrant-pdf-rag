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

import httpx2

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
        except (httpx2.HTTPError, RuntimeError, OSError, ValueError) as exc:
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


EVAL_GATED_METRICS = {
    # dotted path into the report -> minimum allowed ratio vs baseline (1.0 = no drop, 0.95 = 5% margin)
    "recall@1": 0.90,
    "recall@5": 0.95,
    "mrr": 0.95,
    "identifier.recall@1": 1.0,  # identifier queries must never drop
}


def _get(result: dict, dotted: str):
    node: object = result
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set(target: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    node = target
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def check_baseline(report: dict, baseline: dict | None) -> list[str]:
    if baseline is None:
        return []
    regressions: list[str] = []
    if report.get("failures", 0) > 0:
        regressions.append(f"failures: {report['failures']} > 0 query errors occurred during evaluation")
    for dotted, min_ratio in EVAL_GATED_METRICS.items():
        current = _get(report, dotted)
        if current is None:
            print(f"warn: {dotted} not scored in this run; not gated", file=sys.stderr)
            continue
        base_val = _get(baseline, dotted)
        if base_val is None:
            print(f"warn: baseline has no {dotted}; not gated", file=sys.stderr)
            continue
        # For accuracy, current must be >= base_val * min_ratio
        threshold = round(base_val * min_ratio, 3)
        if current < threshold:
            regressions.append(
                f"{dotted}: {current} < baseline {base_val} (min allowed {threshold} with ratio {min_ratio})"
            )
    return regressions


def update_baseline(report: dict, baseline_path: Path) -> None:
    payload: dict = {
        "_meta": {
            "note": "Re-baseline via `make eval-baseline`; dedicated PR (AGENTS.md). Tolerances in scripts/eval_retrieval.py.",
            "n": report.get("n", 0),
            "collection": report.get("collection", "local-corpus"),
            "embed_mode": report.get("embed_mode", "hash"),
            "updated": time.strftime("%Y-%m-%d"),
        },
        "recall@1": report.get("recall@1"),
        "recall@3": report.get("recall@3"),
        "recall@5": report.get("recall@5"),
        "mrr": report.get("mrr"),
        "identifier": report.get("identifier", {}),
        "nl": report.get("nl", {}),
    }
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def summary_markdown(report: dict, baseline: dict | None = None) -> str:
    lines = [
        "## Retrieval eval",
        "",
        (
            f"n={report['n']} failures={report['failures']} mode={report['embed_mode']} "
            f"collection={report['collection']} ({report['elapsed_s']}s)"
        ),
        "",
        "| metric | all | identifier | nl | baseline | gate |",
        "|---|---|---|---|---|---|",
    ]
    for key in ("recall@1", "recall@3", "recall@5", "mrr"):
        base_val = _get(baseline, key) if baseline else None
        min_ratio = EVAL_GATED_METRICS.get(key)
        gate = f">= {round(base_val * min_ratio, 3)}" if (base_val is not None and min_ratio is not None) else "n/a"
        lines.append(
            f"| {key} | {report[key]} | {report['identifier'].get(key)} | {report['nl'].get(key)} | {base_val} | {gate} |"
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
    parser.add_argument("--check", type=Path, default=None, help="fail on accuracy regressions vs this baseline")
    parser.add_argument("--update-baseline", type=Path, default=None, help="record a new baseline here")
    parser.add_argument(
        "--label-draft", action="store_true",
        help="draft golden entries from collection payload instead of scoring",
    )
    parser.add_argument("--docs", type=int, default=40, help="label-draft: docs to sample")
    args = parser.parse_args(argv)
    if args.check and args.update_baseline:
        parser.error("--check and --update-baseline are mutually exclusive")

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

    baseline = None
    regressions: list[str] = []
    if args.check:
        if not args.check.exists():
            print(f"warn: baseline {args.check} missing; nothing gated", file=sys.stderr)
        else:
            baseline = json.loads(args.check.read_text(encoding="utf-8"))
            regressions = check_baseline(report, baseline)
    if args.update_baseline:
        update_baseline(report, args.update_baseline)
        print(f"baseline written to {args.update_baseline}", file=sys.stderr)

    summary = summary_markdown(report, baseline)
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

    if regressions:
        print("REGRESSIONS:", file=sys.stderr)
        for r in regressions:
            print(f"  {r}", file=sys.stderr)
        return 1
    return 0 if report["failures"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
