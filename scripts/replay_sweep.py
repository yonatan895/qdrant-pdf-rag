#!/usr/bin/env python3
"""Sweep production ranking configs over recorded prefetch pools.

Record side: ``scripts/capture_pool.py`` (live, RC/gap). This is the offline
side: rebuild each recorded pool, run the production ranking chain
(per-leg ``rrf_fuse`` → split merge → optional recorded-CE rerank →
``diversify_hits``), and score doc-level recall/MRR against a golden set
for a grid of candidate configs. Hermetic: no GPU, no network.

Approximation (why adoption needs the live eval): pools record ids, ranks,
chunk_type, page labels, the physical first page (the live diversification
bucket), and CE scores only — no headings, message_ids, or text (log
contract). Scoring is therefore doc-level (``expected_doc_ids``
plus top-5 ``must_not_retrieve``) unless ``--headings-from`` joins the
captured ids to a local collection: chunk ids are UUID5 over
``source_rev|heading_path|page|ordinal``, so an id that resolves locally
carries the captured heading, and ``expected_heading`` then scores with
the shared ``is_relevant_hit``. The join fails closed when any captured id
is missing locally or its doc/page disagree (a different ingest). Page
expectations and trap message-id checks stay with ``eval_retrieval.py``
and the frozen holdout. A gain found here is a candidate, never a verdict.

Usage:
    .venv/bin/python scripts/replay_sweep.py \
        --pools bundles/pools-20260912.jsonl --golden evals/golden.jsonl

    # every candidate config; --json writes the full per-query rows
    .venv/bin/python scripts/replay_sweep.py --pools p.jsonl --golden g.jsonl --json out.json

    # section-level relevance via a local collection ingested from the same sources
    .venv/bin/python scripts/replay_sweep.py --pools p.jsonl \
        --golden evals/sections.jsonl --headings-from manuals_current
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from capture_pool import record_to_rows, replay_pool
from eval_retrieval import GoldenEntry, is_relevant_hit, load_golden

# Production prefetch depth for the non-rerank path (query.PREFETCH_LIMIT):
# pools may be captured deeper, so replays that mimic production must trim.
PREFETCH_LIMIT = 40
MUST_NOT_WINDOW = 5


@dataclass(frozen=True)
class SweepConfig:
    """One replay configuration. Defaults mirror production."""

    label: str
    fuse_limit: int | None = None  # None -> max(limit*3, 24) / rerank_candidates
    rerank: bool = False
    alpha: float = 1.0
    max_per_page: int = 1
    max_per_doc: int = 3
    limit: int = 8
    prefetch_limit: int | None = None  # None -> production depth for the path


class _RecordedReranker:
    """Replays recorded CE scores by rerank text (production protocol shape)."""

    def __init__(self, score_by_text: dict[str, float]) -> None:
        self._score_by_text = score_by_text

    def score(self, query: str, texts: list[str]) -> list[float]:
        return [self._score_by_text[text] for text in texts]


class HeadingJoinError(ValueError):
    """Captured chunk ids do not resolve to the same chunks locally."""


def _recorded_depth(record: dict[str, Any]) -> int:
    """Per-leg depth the pool was captured at. Legacy pools (no recorded
    depth) prove only their longest recorded leg: Qdrant returns exactly
    ``limit`` points unless the filtered corpus is smaller."""
    depth = (record.get("_meta") or {}).get("depth")
    if type(depth) is int:
        return depth
    return max(
        (len(leg.get(key) or []) for leg in record.get("legs") or [] for key in ("dense", "sparse")),
        default=0,
    )


def unsupported_reason(records: list[dict], config: SweepConfig) -> str | None:
    """Why a config cannot replay these pools (None when it can).

    An explicit ``prefetch_limit`` asks for a pool depth every pool must
    have recorded. Default configs use the production depth, which every
    capture records."""
    if config.prefetch_limit is None:
        return None
    for record in records:
        if "error" in record:
            continue
        depth = _recorded_depth(record)
        if depth < config.prefetch_limit:
            return f"pool depth {depth} < prefetch {config.prefetch_limit}"
    return None


def replay_record(
    record: dict[str, Any], config: SweepConfig, settings, headings: dict[str, str] | None = None
) -> list:
    """Run the production ranking chain over one recorded pool.

    Per-leg pools are trimmed to the production prefetch depth for the
    config's path (40 without rerank, ``rerank_candidates`` with it) so a
    deep capture cannot simulate a deeper production fetch. Legs merge by
    the recorded ``_meta.split_mode`` (single / comparative / diagnostic);
    CE-less or bypassed recordings stay RRF-only exactly like production.
    """
    from mainframe_rag.retrieve.query import (
        SPLIT_MERGE_DIAGNOSTIC,
        _type_boosts,
        diversify_hits,
        max_split_hits,
        merge_split_hits,
        rrf_fuse,
    )
    from mainframe_rag.retrieve.rerank import format_rerank_text, rerank_candidates

    meta = record.get("_meta") or {}
    mode = str(meta.get("split_mode") or "single")
    rerank_active = bool(config.rerank) and meta.get("ce_scored") is True
    prefetch = config.prefetch_limit
    if prefetch is None:
        prefetch = int(settings.rerank_candidates) if rerank_active else PREFETCH_LIMIT
    depth = _recorded_depth(record)
    if config.prefetch_limit is not None and depth < config.prefetch_limit:
        raise ValueError(
            f"record {record.get('query')!r}: prefetch {config.prefetch_limit} exceeds the "
            f"recorded pool depth {depth}"
        )
    fuse_limit = config.fuse_limit
    if fuse_limit is None:
        fuse_limit = int(settings.rerank_candidates) if rerank_active else max(config.limit * 3, 24)

    boosts = _type_boosts(settings)
    fused_lists = []
    for leg_idx in range(len(record["legs"])):
        rows = record_to_rows(record, leg=leg_idx, max_rank=prefetch, headings=headings)
        dense, sparse, _ce = replay_pool(rows)
        weights = _leg_weights(settings, str(record["legs"][leg_idx].get("effective_text") or ""))
        fused_lists.append(
            rrf_fuse(
                dense, sparse, weights, k=settings.rrf_k, limit=fuse_limit, type_boosts=boosts
            )
        )

    if len(fused_lists) == 1:
        fused = fused_lists[0]
    elif mode == "comparative":
        fused = max_split_hits(fused_lists, settings.rrf_k, fuse_limit)
    else:
        fused = merge_split_hits(fused_lists, SPLIT_MERGE_DIAGNOSTIC, settings.rrf_k, fuse_limit)

    if rerank_active:
        ce = record.get("ce") or {}
        missing = [hit.chunk_id for hit in fused if hit.chunk_id not in ce]
        if missing:
            raise ValueError(f"record {record.get('query')!r}: no CE score for {missing!r}")
        score_map = {format_rerank_text(hit): float(ce[hit.chunk_id]) for hit in fused}
        fused = rerank_candidates(
            str(record.get("query") or ""),
            fused,
            _RecordedReranker(score_map),
            alpha=config.alpha,
            top_k=config.limit,
        )

    return diversify_hits(
        fused, limit=config.limit, max_per_page=config.max_per_page, max_per_doc=config.max_per_doc
    )


def _leg_weights(settings, text: str) -> tuple[float, float]:
    from mainframe_rag.retrieve.filters import parse_query
    from mainframe_rag.retrieve.query import _ranking_params

    return _ranking_params(settings, parse_query(text).has_identifiers)[0]


def score_hits(hits: list, entry: GoldenEntry, *, section_level: bool = False) -> dict | None:
    """Relevance row for one replayed query (None for non-answer).

    Doc-level by default; ``section_level`` (headings joined) applies the
    shared ``is_relevant_hit``, so ``expected_heading`` must match too."""
    expected = set(entry.expected_doc_ids)
    if entry.expected_behavior != "answer" or not expected:
        return None

    def relevant(hit) -> bool:
        if section_level:
            return is_relevant_hit(hit.doc_id, hit.heading, entry)
        return hit.doc_id in expected

    row: dict = {"id": entry.id, "query_class": entry.query_class}
    row["recall@1"] = 1.0 if hits and relevant(hits[0]) else 0.0
    row["recall@5"] = 1.0 if any(relevant(h) for h in hits[:5]) else 0.0
    reciprocal = 0.0
    for rank, hit in enumerate(hits, 1):
        if relevant(hit):
            reciprocal = 1.0 / rank
            break
    row["mrr"] = reciprocal
    violations = [h.doc_id for h in hits[:MUST_NOT_WINDOW] if h.doc_id in set(entry.must_not_retrieve)]
    if violations:
        row["violations"] = violations
    return row


def summarize(rows: list[dict]) -> dict:
    def mean(sub: list[dict], key: str) -> float | None:
        values = [r[key] for r in sub if key in r]
        return round(sum(values) / len(values), 3) if values else None

    classes: dict[str, dict] = {}
    for cls in sorted({r["query_class"] for r in rows if r.get("query_class")}):
        sub = [r for r in rows if r.get("query_class") == cls]
        classes[cls] = {"n": len(sub)}
        for key in ("recall@1", "recall@5", "mrr"):
            classes[cls][key] = mean(sub, key)
    violations = [v for r in rows for v in r.get("violations", [])]
    return {
        "n": len(rows),
        "recall@1": mean(rows, "recall@1"),
        "recall@5": mean(rows, "recall@5"),
        "mrr": mean(rows, "mrr"),
        "violations": len(violations),
        "classes": classes,
    }


def run_config(
    records: list[dict],
    entries_by_query: dict[str, GoldenEntry],
    config: SweepConfig,
    settings,
    headings: dict[str, str] | None = None,
) -> dict:
    scoring = "section" if headings is not None else "doc"
    reason = unsupported_reason(records, config)
    if reason is not None:
        return {"label": config.label, "scoring": scoring, "skipped": reason}
    rows: list[dict] = []
    failures = 0
    unmatched = 0
    for record in records:
        if "error" in record:
            failures += 1
            continue
        entry = entries_by_query.get(str(record.get("query") or ""))
        if entry is None:
            unmatched += 1
            continue
        hits = replay_record(record, config, settings, headings)
        row = score_hits(hits, entry, section_level=headings is not None)
        if row is not None:
            rows.append(row)
    return {
        "label": config.label, "scoring": scoring, "failures": failures, "unmatched": unmatched,
        **summarize(rows), "rows": rows,
    }


def candidate_configs(settings) -> list[SweepConfig]:
    """The pre-registered grid: each axis against the production config."""
    base = SweepConfig(
        label="production",
        max_per_page=settings.retrieve_max_chunks_per_page,
        max_per_doc=settings.retrieve_max_chunks_per_doc,
    )
    configs = [
        base,
        replace(base, label="fuse32", fuse_limit=32),
        replace(base, label="fuse40", fuse_limit=40),
        replace(base, label="page2", max_per_page=2),
        replace(base, label="doc2", max_per_doc=2),
        replace(base, label="doc4", max_per_doc=4),
    ]
    for alpha in (1.0, 0.75, 0.5, 0.25, 0.0):
        configs.append(replace(base, label=f"alpha{alpha:g}", rerank=True, alpha=alpha))
    # The rerank_candidates ceiling: needs pools captured with --depth >= 100.
    configs.append(replace(base, label="rerank100", rerank=True, prefetch_limit=100, fuse_limit=100))
    return configs


def join_headings(records: list[dict], client, collection: str) -> dict[str, str]:
    """Map every captured chunk id to its local ``heading_path``.

    Fail-closed (HeadingJoinError): every id must resolve in ``collection``
    with the recorded doc_id and page label. Ids are UUID5 over the source
    revision and heading path, so a resolved id is the captured chunk; a
    missing id means the local ingest differs (other sources/rules)."""
    recorded: dict[str, dict] = {}
    for record in records:
        if "error" not in record:
            recorded.update(record.get("chunks") or {})
    ids = sorted(recorded)
    headings: dict[str, str] = {}
    mismatched = 0
    for start in range(0, len(ids), 256):
        points = client.retrieve(
            collection, ids=ids[start:start + 256],
            with_payload=["doc_id", "page_label", "page_start", "heading_path"], with_vectors=False,
        )
        for point in points:
            pid, payload = str(point.id), point.payload or {}
            want = recorded.get(pid)
            if want is None:
                continue
            local = (str(payload.get("doc_id") or ""), str(payload.get("page_label") or ""))
            moved = "page_start" in want and payload.get("page_start") != want["page_start"]
            if moved or local != (want.get("doc_id", ""), want.get("page", "")):
                mismatched += 1
                continue
            headings[pid] = str(payload.get("heading_path") or "")
    missing = len(ids) - len(headings) - mismatched
    if missing or mismatched:
        raise HeadingJoinError(
            f"{missing} captured chunk ids missing and {mismatched} with a different doc/page "
            f"in {collection!r} (of {len(ids)}); join only a collection ingested from the "
            "captured sources under the same extraction rules"
        )
    return headings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pools", type=Path, required=True, help="capture_pool.py output JSONL")
    parser.add_argument("--golden", type=Path, default=Path("evals/golden.jsonl"), help="golden JSONL path")
    parser.add_argument("--json", type=Path, default=None, help="write full per-query results here")
    parser.add_argument(
        "--headings-from", default=None, metavar="COLLECTION",
        help="join captured ids to this local collection's headings for section-level scoring",
    )
    args = parser.parse_args(argv)

    from mainframe_rag.eval.datasets import (
        DatasetError,
        require_rc_for_collection,
        require_rc_for_golden,
    )

    try:
        # Venue rule (issue #316): the frozen holdout is an RC-only
        # instrument, even for offline replays. Verify before parsing or scoring.
        require_rc_for_golden([args.golden])
        if args.headings_from is not None:
            require_rc_for_collection(args.headings_from)
        entries = load_golden(args.golden)
    except DatasetError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    from mainframe_rag.config import load_settings

    settings = load_settings()
    records = []
    for line in args.pools.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    entries_by_query = {entry.query: entry for entry in entries}

    headings = None
    if args.headings_from is not None:
        from qdrant_client import QdrantClient

        client = QdrantClient(
            url=settings.qdrant_url, api_key=settings.qdrant_api_key, timeout=settings.qdrant_timeout_s
        )
        try:
            headings = join_headings(records, client, args.headings_from)
        except HeadingJoinError as exc:
            print(f"FAIL: heading join refused: {exc}", file=sys.stderr)
            return 2

    results = [
        run_config(records, entries_by_query, cfg, settings, headings)
        for cfg in candidate_configs(settings)
    ]
    print(f"scoring: {'section-level (headings joined)' if headings is not None else 'doc-level'}")
    header = f"{'config':<12} {'n':>4} {'r@1':>6} {'r@5':>6} {'mrr':>6} {'viol':>4} {'fail':>4}"
    print(header)
    for result in results:
        if "skipped" in result:
            print(f"{result['label']:<12} skipped: {result['skipped']}")
            continue
        print(
            f"{result['label']:<12} {result['n']:>4} {result['recall@1']!s:>6} "
            f"{result['recall@5']!s:>6} {result['mrr']!s:>6} {result['violations']:>4} "
            f"{result['failures']:>4}"
        )
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
