#!/usr/bin/env python3
"""Retrieval accuracy eval: recall@k and MRR against a golden set.

This is the ruler for "increasing accuracy": retrieval changes (embedder,
chunking, RRF constants, filters) must show their delta here before they
merge (AGENTS.md). It runs the real pipeline - build_embedder + the retrieve
module + a real Qdrant - directly, without the agent HTTP hop.

Golden set (evals/golden.jsonl = dev, evals/holdout.jsonl = frozen holdout),
one JSON object per line:
    {"id": "msg-iec130i-01", "query": "...",
     "query_class": "message_id|doc_number|syntax|diagnostic|comparative|version|negative|table",
     "expected_behavior": "answer|abstain",
     "expected_doc_ids": ["SA22-0000-00"],
     "expected_heading": "optional heading substring",
     "expected_page": "optional page_label (diagnostic page_hit@5)",
     "must_not_retrieve": ["doc IDs that must not appear in top-5"],
     "must_not_message_ids": ["sibling IDs that must not appear in top-5 payloads"],
     "source": "operator-history|payload-draft", "note": "why"}

Scoring is doc-level and never pins top-1 across potentially equal-text
chunks (AGENTS.md): a hit is relevant when its doc_id is in
expected_doc_ids AND (if given) the heading substring matches.
Abstain entries carry no expected_doc_ids: they are excluded from the
recall/MRR denominators, their top scores are recorded for calibration, and
must_not violations are gated to zero within the top-5 window.

    EMBED_MODE=hash QDRANT_URL=http://127.0.0.1:6333 QDRANT_COLLECTION=local-corpus \
        python scripts/eval_retrieval.py --golden evals/golden.jsonl

    python scripts/eval_retrieval.py --label-draft --docs 40   # draft candidates

Exit codes: 0 green (or no gate requested); 1 regressions or query
failures; 2 an explicitly requested gate could not be applied (baseline
file missing, or collection/embed-mode mismatch — a skip is not a pass, issue #159).

Compatibility delegate (issue #508 C2): pure dataset/scoring owners live in
:mod:`mainframe_rag.eval.datasets` and :mod:`mainframe_rag.eval.retrieval`.
This module re-exports the same classes/functions (not a copy) and keeps the
live ``evaluate`` / ``label_draft`` / ``main`` entry points so documented
``python scripts/eval_retrieval.py ...`` invocations and Task bridges keep
working during migration.

Retirement condition: all known callers import the package directly, the
successor is documented and qualified, and the maintainer approves removing
this shim. Unknown external usage is not deletion authority.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import NotRequired, TypedDict

import httpx2

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from mainframe_rag.config import load_settings
from mainframe_rag.eval.datasets import (
    QUERY_CLASSES,
    GoldenEntry,
    VenueError,
    default_baseline_path,
    load_golden,
    require_rc_for_collection,
    require_rc_for_golden,
)
from mainframe_rag.eval.retrieval import (
    EVAL_ABSOLUTE_GATED_METRICS,
    EVAL_GATED_METRICS,
    EVAL_ZERO_GATED_METRICS,
    MUST_NOT_WINDOW,
    SEARCH_LIMIT,
    _absolute_floors_apply,
    _finite_number,
    _get,
    _set,
    check_baseline,
    gain,
    is_relevant_hit,
    is_sibling_exception,
    must_not_violations,
    ndcg_at_k,
    score_entry,
    summarize,
    summary_markdown,
    update_baseline,
)
from mainframe_rag.manifest import write_run_manifest
from mainframe_rag.retrieve.query import search as retrieve_search

__all__ = [
    "EVAL_ABSOLUTE_GATED_METRICS",
    "EVAL_GATED_METRICS",
    "EVAL_ZERO_GATED_METRICS",
    "MUST_NOT_WINDOW",
    "QUERY_CLASSES",
    "SEARCH_LIMIT",
    "GoldenEntry",
    "_absolute_floors_apply",
    "_finite_number",
    "_get",
    "_set",
    "check_baseline",
    "default_baseline_path",
    "evaluate",
    "gain",
    "is_relevant_hit",
    "is_sibling_exception",
    "label_draft",
    "load_golden",
    "main",
    "must_not_violations",
    "ndcg_at_k",
    "score_entry",
    "summarize",
    "summary_markdown",
    "update_baseline",
]


def evaluate(golden: list[GoldenEntry], settings) -> dict:
    from qdrant_client import QdrantClient

    from mainframe_rag.ingest.embed import build_embedder

    client = QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout=settings.qdrant_timeout_s,
    )
    embedder = build_embedder(settings)
    # Extraction-rules desync warning (issue #124): the eval is the
    # instrument that caught the #120 silent-recall loss — payload
    # message_ids extracted under older regex rules made the prefetch
    # filter match nothing. Warning only: the run still produces numbers
    # (trend data), but they are not comparable to a same-rules baseline.
    from mainframe_rag.ingest.qdrant_io import stored_rules_version
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    stored_v = stored_rules_version(client, settings)
    if stored_v is not None and stored_v != extraction_rules_version():
        print(
            f"warn: collection {settings.qdrant_collection!r} payloads were extracted under rules "
            f"{stored_v!r}; this tree computes {extraction_rules_version()!r} — "
            "re-ingest required for numbers comparable to a same-rules baseline",
            file=sys.stderr,
        )
    from mainframe_rag.retrieve.rerank import build_reranker

    reranker = build_reranker(settings)
    collection = settings.qdrant_collection

    rows, failures = [], 0
    started = time.perf_counter()
    for entry in golden:
        try:
            hits, kind, _timings = retrieve_search(
                client,
                embedder,
                collection,
                entry.query,
                limit=SEARCH_LIMIT,
                settings=settings,
                reranker=reranker,
            )
            rows.append(score_entry(hits, entry))
            rows[-1]["kind"] = kind
        except (httpx2.HTTPError, RuntimeError, OSError, ValueError) as exc:
            # One bad query must not kill the eval; counted as a failure.
            failures += 1
            rows.append({"query": entry.query, "error": str(exc)[:200], "kind": "error"})

    return summarize(
        rows,
        failures=failures,
        elapsed_s=round(time.perf_counter() - started, 2),
        embed_mode=settings.embed_mode,
        collection=collection,
    )


class _LabelDraft(TypedDict):
    query: str
    expected_doc_ids: list[str]
    note: str
    expected_heading: NotRequired[str]


def label_draft(collection: str, settings, docs: int) -> list[_LabelDraft]:
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
    drafts: list[_LabelDraft] = []
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
    parser.add_argument("--golden", type=Path, default=Path("evals/golden.jsonl"), help="golden JSONL path (default: dev set)")
    parser.add_argument("--out", type=Path, default=None, help="write the JSON report here")
    parser.add_argument("--summary", type=Path, default=None, help="write a markdown table here")
    parser.add_argument("--check", type=Path, default=None, help="fail on accuracy regressions vs this baseline (default when unset: mode-keyed baseline if it exists)")
    parser.add_argument("--no-check", action="store_true", help="disable baseline gating entirely")
    parser.add_argument("--update-baseline", type=Path, default=None, help="record a new baseline here")
    parser.add_argument(
        "--label-draft", action="store_true",
        help="draft golden entries from collection payload instead of scoring",
    )
    parser.add_argument("--docs", type=int, default=40, help="label-draft: docs to sample")
    parser.add_argument("--rerank", action="store_true", help="enable cross-encoder reranking")
    args = parser.parse_args(argv)
    if args.check and args.update_baseline:
        parser.error("--check and --update-baseline are mutually exclusive")
    if args.check and (args.no_check or args.label_draft):
        parser.error("--check cannot be combined with --no-check or --label-draft")

    settings = load_settings()
    try:
        # Venue rule (issue #268): the frozen holdout and the real-corpus
        # collection are RC-only instruments.
        require_rc_for_golden([args.golden])
        require_rc_for_collection(settings.qdrant_collection)
    except VenueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    if args.rerank:
        settings = settings.model_copy(update={"rerank_enabled": True})
    if args.label_draft:
        drafts = label_draft(settings.qdrant_collection, settings, args.docs)
        for d in drafts:
            print(json.dumps(d, ensure_ascii=False))
        return 0

    report = evaluate(load_golden(args.golden), settings)

    baseline = None
    regressions: list[str] = []
    check_path = args.check
    if check_path is None and not args.no_check and not args.update_baseline:
        # Mode-keyed default: gate against the baseline for this embed mode
        # when it has been recorded; a missing baseline warns, never gates.
        candidate = default_baseline_path(settings.embed_mode)
        if candidate.exists():
            check_path = candidate
        else:
            print(f"warn: no baseline for embed_mode={settings.embed_mode} ({candidate}); nothing gated", file=sys.stderr)
    gate_skipped = False
    if check_path is not None:
        if not check_path.exists():
            print(
                f"warn: baseline {check_path} missing; the requested gate cannot be applied — "
                "exit 2 so a missing gate cannot read as pass",
                file=sys.stderr,
            )
            gate_skipped = True
        else:
            baseline = json.loads(check_path.read_text(encoding="utf-8"))
            meta = baseline.get("_meta") or {}
            if meta.get("embed_mode") and meta["embed_mode"] != settings.embed_mode:
                print(
                    f"warn: baseline embed_mode={meta['embed_mode']} but this run is {settings.embed_mode}; "
                    "the numbers are not comparable; skipping gate — exit 2",
                    file=sys.stderr,
                )
                baseline = None
                gate_skipped = True
            if meta.get("collection") and meta["collection"] != settings.qdrant_collection:
                print(
                    f"warn: baseline collection {meta['collection']!r} != run collection "
                    f"{settings.qdrant_collection!r}; skipping gate (different corpora) — "
                    "exit 2 so a skip cannot read as pass (issue #159)",
                    file=sys.stderr,
                )
                baseline = None
                gate_skipped = True
            else:
                regressions = check_baseline(report, baseline)
                if baseline is not None and not regressions and not any(
                    _finite_number(_get(baseline, key))
                    for key in (*EVAL_GATED_METRICS, *EVAL_ABSOLUTE_GATED_METRICS)
                ):
                    regressions.append("baseline has no applicable comparison rule; requested gate unavailable")
                    gate_skipped = True
    if args.update_baseline:
        update_baseline(report, args.update_baseline)
        print(f"baseline written to {args.update_baseline}", file=sys.stderr)

    if check_path is None:
        gate_status = "not_requested"
    elif gate_skipped:
        gate_status = "skipped"
    elif regressions or report["failures"] > 0:
        gate_status = "failed"
    else:
        gate_status = "passed"
    report["gate"] = {"status": gate_status, "problems": regressions}
    summary = summary_markdown(report, baseline)
    summary += f"\n\nGate: {gate_status} (ungated diagnostics are not acceptance).\n"
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

    try:
        # evaluate() returns a flat report; record the scored summary metrics
        # (everything except the per-query rows) so manifests are comparable.
        metrics = {
            k: report[k]
            for k in (
                "n", "scored", "gate", "failures", "recall@1", "recall@3", "recall@5", "mrr",
                "identifier", "nl", "classes", "abstain", "must_not", "page",
            )
            if k in report
        }
        manifest = write_run_manifest("eval", settings, metrics)
        print(
            f"run manifest appended to evals/runs/eval_runs.jsonl (sha={manifest['git_sha'][:8]})",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"warn: failed to append run manifest: {exc}", file=sys.stderr)

    if regressions and not gate_skipped:
        print("REGRESSIONS:", file=sys.stderr)
        for r in regressions:
            print(f"  {r}", file=sys.stderr)
        return 1
    if report["failures"] > 0:
        return 1
    if gate_skipped:
        # An explicitly requested gate that could not be applied is not a
        # pass: a skipped verdict must be distinguishable from green (issue
        # #159). Query failures above already exit 1.
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
