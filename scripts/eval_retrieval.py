#!/usr/bin/env python3
"""Retrieval accuracy eval: recall@k and MRR against a golden set.

This is the ruler for "increasing accuracy": retrieval changes (embedder,
chunking, RRF constants, filters) must show their delta here before they
merge (AGENTS.md). It runs the real pipeline - build_embedder + the retrieve
module + a real Qdrant - directly, without the agent HTTP hop.

Golden set (evals/golden.jsonl = dev, evals/holdout.jsonl = frozen holdout),
one JSON object per line:
    {"id": "msg-iec130i-01", "query": "...",
     "query_class": "message_id|doc_number|syntax|diagnostic|comparative|version|negative",
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
file missing, or collection mismatch — a skip is not a pass, issue #159).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import httpx2
from pydantic import BaseModel, Field, ValidationError, model_validator

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mainframe_rag.config import load_settings
from mainframe_rag.manifest import write_run_manifest
from mainframe_rag.retrieve.query import SearchHit
from mainframe_rag.retrieve.query import search as retrieve_search

SEARCH_LIMIT = 8  # headroom for recall@5
MUST_NOT_WINDOW = 5  # must_not violations are gated inside the top-5

QUERY_CLASSES = (
    "message_id",
    "doc_number",
    "syntax",
    "diagnostic",
    "comparative",
    "version",
    "negative",
)


class GoldenEntry(BaseModel):
    query: str = Field(min_length=1)
    expected_doc_ids: list[str] = Field(default_factory=list)
    expected_heading: str | None = None
    expected_page: str | None = None
    must_not_retrieve: list[str] = Field(default_factory=list)
    must_not_message_ids: list[str] = Field(default_factory=list)
    expected_behavior: Literal["answer", "abstain"] = "answer"
    query_class: Literal[QUERY_CLASSES] | None = None  # type: ignore[valid-type]
    id: str | None = None
    source: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _behavior_matches_expectations(self) -> GoldenEntry:
        if self.expected_behavior == "abstain" and self.expected_doc_ids:
            raise ValueError(
                "abstain entries must not set expected_doc_ids; "
                "expectations are expressed via must_not_retrieve/must_not_message_ids"
            )
        if self.expected_behavior == "answer" and not self.expected_doc_ids:
            raise ValueError(
                "answer entries require expected_doc_ids; "
                "use expected_behavior='abstain' for negative/trap queries"
            )
        return self


def load_golden(path: Path) -> list[GoldenEntry]:
    entries: list[GoldenEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entry = GoldenEntry.model_validate_json(line)
        except (ValidationError, ValueError) as exc:
            raise SystemExit(f"invalid golden entry: {line[:120]} ({exc})")
        entries.append(entry)
    return entries


def default_baseline_path(embed_mode: str) -> Path:
    """Mode-keyed baselines: hash numbers gate CI/dev; vllm numbers gate
    release-candidate runs on the live stack. The two are not comparable."""
    return Path("evals/baseline-vllm.json") if embed_mode == "vllm" else Path("evals/baseline.json")


def is_sibling_exception(query_ids: set[str], doc_message_ids) -> bool:
    """Sibling-precision allowance, shared by must_not_violations (retrieval
    eval + harness L1) and verify_golden: a chunk/doc that carries a bait
    message id ALONGSIDE the query's own id is one page documenting adjacent
    messages (e.g. IOS207I and IOS208I share a page) — not a wrong-sibling
    answer. Only sibling-only payloads violate."""
    return bool(set(query_ids) & set(doc_message_ids or ()))


def is_relevant_hit(hit_doc_id: str, hit_heading: str, entry: GoldenEntry) -> bool:
    """Doc-level relevance, shared by score_entry and harness L1: doc_id in
    the expected set AND heading substring (if given)."""
    if hit_doc_id not in set(entry.expected_doc_ids):
        return False
    heading = (entry.expected_heading or "").lower()
    return not heading or heading in hit_heading.lower()


def must_not_violations(hits: list[SearchHit], entry: GoldenEntry) -> list[dict]:
    """Collect must_not violations inside the top-5 window.

    Sibling-precision allowance (same rule as the corpus builder's trap
    assertion): a chunk that carries a bait message id ALONGSIDE the query's
    own id is one page documenting adjacent messages (e.g. IOS207I and
    IOS208I share a page in both editions of System Messages Vol 8) — not a
    wrong-sibling answer. Only sibling-only chunks violate. Without this,
    the chunk-level gate is stricter than the trap's documented intent and
    every adjacent-message page in a multi-edition corpus trips it. Shared
    by the retrieval eval and the harness L1 so the allowance cannot
    diverge between the two gates."""
    from mainframe_rag.regexes import find_message_ids

    query_ids = set(find_message_ids(entry.query))
    violations: list[dict] = []
    for rank, hit in enumerate(hits[:MUST_NOT_WINDOW], 1):
        if hit.doc_id in set(entry.must_not_retrieve):
            violations.append({"type": "doc_id", "value": hit.doc_id, "rank": rank})
        hit_msgs = set(hit.message_ids or ()) & set(entry.must_not_message_ids)
        if hit_msgs and not is_sibling_exception(query_ids, hit.message_ids):
            violations.append(
                {"type": "message_id", "value": sorted(hit_msgs), "rank": rank, "doc_id": hit.doc_id}
            )
    return violations


def gain(hit_doc_id: str, hit_heading: str, hit_page: str, entry: GoldenEntry) -> int:
    """Graded gain for one hit: 1 for a doc hit, +1 heading, +1 page."""
    if hit_doc_id not in set(entry.expected_doc_ids):
        return 0
    g = 1
    heading = (entry.expected_heading or "").lower()
    if heading and heading in (hit_heading or "").lower():
        g += 1
    page = entry.expected_page or ""
    if page and hit_page == page:
        g += 1
    return g


def ndcg_at_k(hits: Sequence[Any], entry: GoldenEntry, k: int = SEARCH_LIMIT) -> float | None:
    """Doc-level nDCG@k against the entry's own ideal (every expected doc at
    max gain). The hit list is DEDUPLICATED per doc_id (best-ranked chunk
    wins) — otherwise N chunks of one expected doc each contribute gain and
    DCG can exceed IDCG (nDCG > 1), which is meaningless. Doc-level ranking
    matches the doc-level gold. None when the entry has no expected docs
    (abstain rows)."""
    expected_n = len(entry.expected_doc_ids)
    if expected_n == 0:
        return None
    max_gain = 1
    if entry.expected_heading:
        max_gain += 1
    if entry.expected_page:
        max_gain += 1
    seen: set[str] = set()
    dcg = 0.0
    rank = 0
    for hit in hits:
        doc_id = getattr(hit, "doc_id", "")
        heading = getattr(hit, "heading", "")
        page_label = getattr(hit, "page_label", "") or ""
        if doc_id in seen:
            continue
        seen.add(doc_id)
        rank += 1
        if rank > k:
            break
        g = gain(doc_id, heading, page_label, entry)
        dcg += g / math.log2(rank + 1)
    ideal_gains = [max_gain] + [1] * (expected_n - 1)
    idcg = sum(g / math.log2(i + 1) for i, g in enumerate(ideal_gains[:k], start=1))
    if idcg == 0.0:
        return None
    return dcg / idcg


def score_entry(hits: list[SearchHit], entry: GoldenEntry) -> dict:
    """Score one entry against retrieved hits.

    Relevance = doc_id in expected set AND heading substring (if given).
    Abstain entries produce no recall/MRR keys (excluded from the
    denominators) and record their top scores for calibration. must_not
    violations are collected inside the MUST_NOT_WINDOW window for both
    doc IDs and message-ID payloads, with the sibling-precision allowance:
    a chunk co-carrying the query's own message id is the same documented
    page, never a wrong-sibling answer. expected_page feeds the page_hit@5
    diagnostic (doc-restricted; never a hard gate)."""
    expected = set(entry.expected_doc_ids)
    abstain = entry.expected_behavior == "abstain"

    def relevant(hit: SearchHit) -> bool:
        return is_relevant_hit(hit.doc_id, hit.heading, entry)

    row: dict = {
        "query": entry.query,
        "id": entry.id,
        "query_class": entry.query_class,
        "expected_behavior": entry.expected_behavior,
        "hit_doc_ids": [h.doc_id for h in hits[:3]],
    }

    if abstain:
        row["top_scores"] = [round(h.score, 4) for h in hits[:5]]
    else:
        reciprocal_rank = 0.0
        for rank, hit in enumerate(hits, 1):
            if relevant(hit):
                reciprocal_rank = 1.0 / rank
                break
        row["recall@1"] = 1.0 if hits[:1] and relevant(hits[0]) else 0.0
        row["recall@3"] = 1.0 if any(relevant(h) for h in hits[:3]) else 0.0
        row["recall@5"] = 1.0 if any(relevant(h) for h in hits[:5]) else 0.0
        row["recall@8"] = 1.0 if any(relevant(h) for h in hits[:8]) else 0.0
        row["mrr"] = reciprocal_rank
        ndcg = ndcg_at_k(hits, entry, k=8)
        if ndcg is not None:
            row["ndcg@8"] = round(ndcg, 4)

    violations = must_not_violations(hits, entry)
    if violations:
        row["violations"] = violations

    if entry.expected_page and expected:
        page_hit = any(
            h.doc_id in expected and h.page_label == entry.expected_page for h in hits[:5]
        )
        row["page_hit@5"] = 1.0 if page_hit else 0.0

    return row


def evaluate(golden: list[GoldenEntry], settings) -> dict:
    from qdrant_client import QdrantClient

    from mainframe_rag.ingest.embed import build_embedder

    client = QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout=settings.qdrant_timeout_s,
    )
    embedder = build_embedder(settings)
    from mainframe_rag.retrieve.rerank import build_reranker

    reranker = build_reranker(settings)
    # HyDE/step-back A/B (issue #82): the eval drives the sync twin, so the
    # rewrite leg rides the same dedicated bounded-timeout client the agent
    # builds in lifespan. Absent unless a rewrite flag is on.
    rewrite_llm = None
    if settings.hyde_enabled or settings.stepback_enabled:
        from mainframe_rag.agent.answer import build_rewrite_llm

        rewrite_llm = build_rewrite_llm(settings)
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
                llm=rewrite_llm,
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


def summarize(
    rows: list[dict],
    *,
    failures: int,
    elapsed_s: float,
    embed_mode: str,
    collection: str,
) -> dict:
    """Pure aggregation over scored rows (unit-testable; no I/O).

    Answer rows carry recall/mrr keys; abstain rows carry top_scores and are
    excluded from the recall/MRR denominators by construction."""
    scored = [r for r in rows if "recall@1" in r]

    def mean_over(sub: list[dict], key: str) -> float | None:
        values = [r[key] for r in sub if key in r]
        return round(sum(values) / len(values), 3) if values else None

    def mean(key: str) -> float:
        value = mean_over(scored, key)
        return 0.0 if value is None else value

    def mean_by_kind(key: str, kind: str) -> float | None:
        return mean_over([r for r in scored if r.get("kind") == kind], key)

    classes: dict[str, dict] = {}
    for cls in sorted({r["query_class"] for r in rows if r.get("query_class")}):
        sub = [r for r in rows if r.get("query_class") == cls]
        classes[cls] = {"n": len(sub), "scored": sum(1 for r in sub if "recall@1" in r)}
        for key in ("recall@1", "recall@3", "recall@5", "recall@8", "mrr", "ndcg@8"):
            classes[cls][key] = mean_over(sub, key)

    abstain_rows = [r for r in rows if r.get("expected_behavior") == "abstain" and "top_scores" in r]
    top_scores = [r["top_scores"][0] for r in abstain_rows if r["top_scores"]]
    abstain_summary = {
        "n": len(abstain_rows),
        "top_score_mean": round(sum(top_scores) / len(top_scores), 4) if top_scores else None,
        "top_score_max": round(max(top_scores), 4) if top_scores else None,
    }

    # The zero gate applies corpus-wide: every scored row is checked.
    violations = [v for r in rows for v in r.get("violations", [])]
    must_not_summary = {
        "checked": len(rows),
        "violations": len(violations),
        "rate": round(len(violations) / len(rows), 4) if rows else 0.0,
    }

    page_rows = [r for r in rows if "page_hit@5" in r]
    page_summary = {
        "checked": len(page_rows),
        "hit@5": mean_over(page_rows, "page_hit@5"),
    }

    return {
        "n": len(rows),
        "failures": failures,
        "elapsed_s": elapsed_s,
        "embed_mode": embed_mode,
        "collection": collection,
        "recall@1": mean("recall@1"),
        "recall@3": mean("recall@3"),
        "recall@5": mean("recall@5"),
        "recall@8": mean("recall@8"),
        "mrr": mean("mrr"),
        "ndcg@8": mean("ndcg@8"),
        "identifier": {k: mean_by_kind(k, "identifier") for k in ("recall@1", "recall@5", "recall@8", "mrr", "ndcg@8")},
        "nl": {k: mean_by_kind(k, "nl") for k in ("recall@1", "recall@5", "recall@8", "mrr", "ndcg@8")},
        "classes": classes,
        "abstain": abstain_summary,
        "must_not": must_not_summary,
        "page": page_summary,
        "rows": rows,
    }


EVAL_GATED_METRICS = {
    # dotted path into the report -> minimum allowed ratio vs baseline (1.0 = no drop, 0.95 = 5% margin)
    "recall@1": 0.90,
    "recall@5": 0.95,
    "recall@8": 0.95,
    "mrr": 0.95,
    "ndcg@8": 0.95,
    "identifier.recall@1": 1.0,  # identifier queries must never drop
    "classes.message_id.recall@1": 1.0,  # safety-critical message ID lookups must never drop
}

# Absolute invariants: must be exactly zero whenever baseline checking runs,
# regardless of the recorded baseline (a wrong doc or sibling message ID
# surfacing in the top-5 is a failure even if it happened at baseline time).
EVAL_ZERO_GATED_METRICS = (
    "must_not.violations",
)


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
    for dotted in EVAL_ZERO_GATED_METRICS:
        current = _get(report, dotted)
        if current is None:
            print(f"warn: {dotted} not scored in this run; not gated", file=sys.stderr)
            continue
        if current > 0:
            regressions.append(f"{dotted}: {current} > 0 (absolute gate: must_not hits in the top-{MUST_NOT_WINDOW})")
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
        "recall@8": report.get("recall@8"),
        "mrr": report.get("mrr"),
        "ndcg@8": report.get("ndcg@8"),
        "identifier": report.get("identifier", {}),
        "nl": report.get("nl", {}),
        "classes": report.get("classes", {}),
        "abstain": report.get("abstain", {}),
        "page": report.get("page", {}),
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
    for key in ("recall@1", "recall@3", "recall@5", "recall@8", "mrr", "ndcg@8"):
        base_val = _get(baseline, key) if baseline else None
        min_ratio = EVAL_GATED_METRICS.get(key)
        gate = f">= {round(base_val * min_ratio, 3)}" if (base_val is not None and min_ratio is not None) else "n/a"
        lines.append(
            f"| {key} | {report[key]} | {report['identifier'].get(key)} | {report['nl'].get(key)} | {base_val} | {gate} |"
        )

    classes = report.get("classes") or {}
    if classes:
        lines += ["", "### Per golden query class", "", "| class | n | scored | r@1 | r@3 | r@5 | r@8 | mrr | ndcg@8 |", "|---|---|---|---|---|---|---|---|---|"]
        for cls, stats in classes.items():
            cells = " | ".join("-" if stats.get(k) is None else str(stats.get(k)) for k in ("recall@1", "recall@3", "recall@5", "recall@8", "mrr", "ndcg@8"))
            lines.append(f"| {cls} | {stats.get('n')} | {stats.get('scored')} | {cells} |")

    abstain = report.get("abstain") or {}
    if abstain.get("n"):
        lines += [
            "",
            (
                f"abstain entries: n={abstain['n']} "
                f"top_score_mean={abstain.get('top_score_mean')} top_score_max={abstain.get('top_score_max')} "
                "(recorded for score-floor calibration; excluded from recall/MRR)"
            ),
        ]
    must_not = report.get("must_not") or {}
    lines.append(
        f"must_not violations: {must_not.get('violations', 0)} (gate: 0 within top-{MUST_NOT_WINDOW}; checked {must_not.get('checked', 0)} rows)"
    )
    page = report.get("page") or {}
    if page.get("checked"):
        lines.append(f"page_hit@5 (diagnostic): {page.get('hit@5')} over {page['checked']} entries with expected_page")

    lines += ["", "| query | class | kind | r@1 | r@5 | mrr | viol | top hit doc_ids |", "|---|---|---|---|---|---|---|---|"]
    for row in report["rows"]:
        if "error" in row:
            lines.append(f"| {row['query'][:60]} | {row.get('query_class') or '-'} | error | - | - | - | - | {row['error'][:40]} |")
        else:
            cells = " | ".join(
                "-" if row.get(k) is None else f"{row[k]:.0f}" if k.startswith("recall") else f"{row[k]:.2f}"
                for k in ("recall@1", "recall@5", "mrr")
            )
            viol = len(row.get("violations", []))
            lines.append(
                f"| {row['query'][:60]} | {row.get('query_class') or '-'} | {row.get('kind', '')} | {cells} | {viol} "
                f"| {', '.join(row['hit_doc_ids'])[:60]} |"
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

    settings = load_settings()
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
                    "the numbers are not comparable",
                    file=sys.stderr,
                )
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

    try:
        # evaluate() returns a flat report; record the scored summary metrics
        # (everything except the per-query rows) so manifests are comparable.
        metrics = {
            k: report[k]
            for k in (
                "n", "failures", "recall@1", "recall@3", "recall@5", "mrr",
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

    if regressions:
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
