#!/usr/bin/env python3
"""Multi-turn session A/B for follow-up query condensation (ADR-0004 / M5).

Why this exists
    The chat routes condense a follow-up turn into a standalone search query
    only when CHAT_CONDENSE_ENABLED is on (default off). The retrieval eval
    (scripts/eval_retrieval.py) scores single queries and cannot see the
    follow-up case: "How do I resolve this?" retrieves nothing on its own,
    while the same turn after an identifier question should retrieve the same
    manual section the first turn did. This harness measures both arms
    against the dev golden set:

      literal   - retrieve the follow-up text as-is (the no-condense product
                  behavior while the flag is off)
      condensed - rewrite the follow-up with the agent's own condense prompt
                  through the live reasoning model, then retrieve

    Per-arm recall@k / MRR come from eval_retrieval.score_entry so the
    relevance rule cannot diverge between the two instruments. Condense and
    retrieval latencies are reported; the report is the evidence base for a
    future, separate decision to flip CHAT_CONDENSE_ENABLED (default flips
    are not feature-PR work, AGENTS.md).

Execution shape
    Live stack only (Qdrant + embedding model + reasoning model), golden dev
    set only (the frozen holdout is never iterated against). Exit 0 unless a
    query fails; no baseline gate yet. Run via `make eval-chat`.

    LLM_BASE_URL + LLM_MODEL_REASONING select the reasoning model (direct
    vLLM locally, or the platform gateway on RC); QDRANT_* / EMBED_* come
    from Settings like every other eval.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
if str(_REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO / "scripts"))

from eval_retrieval import (
    SEARCH_LIMIT,
    GoldenEntry,
    load_golden,
    score_entry,
)
from venue import VenueError, require_rc_for_collection, require_rc_for_golden

from mainframe_rag.agent.answer import HttpxLLMClient, condense_query
from mainframe_rag.config import load_settings
from mainframe_rag.manifest import write_run_manifest
from mainframe_rag.ports import ChatMessage
from mainframe_rag.retrieve.query import search as retrieve_search

FOLLOW_UPS: dict[str, str] = {
    "message_id": "What does it mean and how do I resolve it?",
    "syntax": "What are the parameters for this?",
    "diagnostic": "How do I recover from it?",
    "doc_number": "What does this document cover?",
    "comparative": "How do the options compare?",
    "version": "What is the difference for my release?",
    "table": "Where is this laid out?",
}
DEFAULT_FOLLOW_UP = "Tell me more about this."
ASSISTANT_PLACEHOLDER = "The relevant manual section was retrieved for your question."

ARM_LITERAL = "literal"
ARM_CONDENSED = "condensed"


def follow_up_query(entry: GoldenEntry) -> str:
    return FOLLOW_UPS.get(entry.query_class or "", DEFAULT_FOLLOW_UP)


def session_messages(entry: GoldenEntry) -> list[ChatMessage]:
    """First turn with the identifiers, a canned assistant turn, then the
    coreference follow-up — the shape condense_query is designed for."""
    return [
        ChatMessage(role="user", content=entry.query),
        ChatMessage(role="assistant", content=ASSISTANT_PLACEHOLDER),
        ChatMessage(role="user", content=follow_up_query(entry)),
    ]


def arm_entry(entry: GoldenEntry, query: str) -> GoldenEntry:
    """The follow-up is scored against the same relevant docs as its session
    opener: condensation wins only when the topic (and its manual) survives
    the rewrite."""
    return GoldenEntry(
        id=f"{entry.id or entry.query}::{query}",
        query=query,
        expected_doc_ids=entry.expected_doc_ids,
        expected_heading=entry.expected_heading,
        expected_page=entry.expected_page,
        must_not_retrieve=entry.must_not_retrieve,
        must_not_message_ids=entry.must_not_message_ids,
    )


def summarize_arms(rows: list[dict]) -> dict:
    """Pure aggregation: per-arm means plus the condensed-minus-literal
    deltas and the condense-call latency (arm B only)."""
    summary: dict = {}
    for arm in (ARM_LITERAL, ARM_CONDENSED):
        arm_rows = [r for r in rows if r["arm"] == arm and "recall@1" in r]
        entry: dict = {"n": len(arm_rows)}
        for key in ("recall@1", "recall@3", "recall@5", "recall@8", "mrr"):
            values = [r[key] for r in arm_rows if key in r]
            entry[key] = round(sum(values) / len(values), 3) if values else None
        retrieval_ms = [r["retrieval_ms"] for r in arm_rows]
        entry["retrieval_p50_ms"] = (
            round(sorted(retrieval_ms)[len(retrieval_ms) // 2], 1) if retrieval_ms else None
        )
        summary[arm] = entry
    condensed_rows = [
        r for r in rows if r["arm"] == ARM_CONDENSED and r.get("condense_ms") is not None
    ]
    summary["condense_p50_ms"] = (
        round(sorted(r["condense_ms"] for r in condensed_rows)[len(condensed_rows) // 2], 1)
        if condensed_rows
        else None
    )
    for key in ("recall@1", "recall@5", "mrr"):
        lit = summary[ARM_LITERAL].get(key)
        con = summary[ARM_CONDENSED].get(key)
        summary[f"delta_{key}"] = (
            round(con - lit, 3) if lit is not None and con is not None else None
        )
    return summary


def evaluate_sessions(entries: list[GoldenEntry], settings) -> dict:
    from qdrant_client import QdrantClient

    from mainframe_rag.ingest.embed import build_embedder
    from mainframe_rag.retrieve.rerank import build_reranker

    client = QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout=settings.qdrant_timeout_s,
    )
    embedder = build_embedder(settings)
    reranker = build_reranker(settings)
    llm = HttpxLLMClient(settings)

    rows: list[dict] = []
    failures = 0
    for entry in entries:
        messages = session_messages(entry)
        follow_up = messages[-1].content
        literal_row = _retrieve_arm(
            client, embedder, reranker, settings, entry, follow_up, ARM_LITERAL
        )
        rows.append(literal_row)

        condense_ms = None
        try:
            started = time.perf_counter()
            condensed_query = asyncio.run(condense_query(llm, messages, settings))
            condense_ms = int((time.perf_counter() - started) * 1000)
            condensed_query = condensed_query or follow_up
        except Exception as exc:  # noqa: BLE001 — a condense failure is a row failure, not an aborted run
            failures += 1
            rows.append(
                {
                    "arm": ARM_CONDENSED,
                    "session": entry.id,
                    "follow_up": follow_up,
                    "error": str(exc)[:200],
                }
            )
            continue
        condensed_row = _retrieve_arm(
            client, embedder, reranker, settings, entry, condensed_query, ARM_CONDENSED
        )
        condensed_row["condense_ms"] = condense_ms
        rows.append(condensed_row)

    llm.close()
    client.close()
    return {
        "rows": rows,
        "failures": failures,
        "summary": summarize_arms(rows),
        "settings": {
            "collection": settings.qdrant_collection,
            "embed_mode": settings.embed_mode,
            "llm_model": settings.llm_model_reasoning,
        },
    }


def _retrieve_arm(
    client, embedder, reranker, settings, entry: GoldenEntry, query: str, arm: str
) -> dict:
    started = time.perf_counter()
    try:
        hits, kind, timings = retrieve_search(
            client,
            embedder,
            settings.qdrant_collection,
            query,
            limit=SEARCH_LIMIT,
            settings=settings,
            reranker=reranker,
        )
    except Exception as exc:  # noqa: BLE001 — one bad query is a row failure
        return {
            "arm": arm,
            "session": entry.id,
            "follow_up": query,
            "retrieval_ms": int((time.perf_counter() - started) * 1000),
            "error": str(exc)[:200],
        }
    row = score_entry(hits, arm_entry(entry, query))
    row["arm"] = arm
    row["session"] = entry.id
    row["follow_up"] = query
    row["kind"] = kind
    row["retrieval_ms"] = int((time.perf_counter() - started) * 1000)
    row["timings"] = timings
    return row


def select_entries(golden: list[GoldenEntry], limit: int) -> list[GoldenEntry]:
    """Answer entries whose opener carries identifiers; deterministic order
    (first N by id) so runs are comparable."""
    eligible = [e for e in golden if e.expected_behavior == "answer" and e.expected_doc_ids]
    eligible.sort(key=lambda e: e.id or e.query)
    return eligible[:limit]


def summary_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# Multi-turn condensation A/B (CHAT_CONDENSE_ENABLED)",
        "",
        f"- collection: `{report['settings']['collection']}` | embed: `{report['settings']['embed_mode']}` | llm: `{report['settings']['llm_model']}`",
        f"- queries/direction: literal n={summary[ARM_LITERAL]['n']}, condensed n={summary[ARM_CONDENSED]['n']}, failures={report['failures']}",
        "",
        "| arm | recall@1 | recall@3 | recall@5 | recall@8 | MRR | retrieval p50 ms |",
        "|---|---|---|---|---|---|---|",
    ]
    for arm in (ARM_LITERAL, ARM_CONDENSED):
        entry = summary[arm]
        lines.append(
            f"| {arm} | {entry['recall@1']} | {entry['recall@3']} | {entry['recall@5']} | "
            f"{entry['recall@8']} | {entry['mrr']} | {entry['retrieval_p50_ms']} |"
        )
    lines += [
        "",
        f"- condense call p50: {summary['condense_p50_ms']} ms (one extra LLM call per follow-up)",
        f"- delta condensed-literal: recall@1 {summary['delta_recall@1']}, recall@5 {summary['delta_recall@5']}, MRR {summary['delta_mrr']}",
        "",
        "Enabling the flag is a separate, dedicated decision (AGENTS.md default-flip rule).",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--golden", type=Path, default=Path("evals/golden.jsonl"))
    parser.add_argument("--limit", type=int, default=12, help="sessions to run (default 12)")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--summary", type=Path, default=None)
    args = parser.parse_args(argv)

    settings = load_settings()
    try:
        require_rc_for_golden([args.golden])
        require_rc_for_collection(settings.qdrant_collection)
    except VenueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    entries = select_entries(load_golden(args.golden), args.limit)
    if not entries:
        print("no eligible sessions in the golden set", file=sys.stderr)
        return 2

    print(
        f"==> {len(entries)} sessions against {settings.qdrant_collection} ({settings.embed_mode})"
    )
    report = evaluate_sessions(entries, settings)
    write_run_manifest(
        "eval_chat",
        settings,
        {"n": len(entries), "failures": report["failures"], "summary": report["summary"]},
    )

    print(summary_markdown(report))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(summary_markdown(report), encoding="utf-8")
        print(f"wrote {args.summary}")
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
