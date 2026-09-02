#!/usr/bin/env python3
"""Answer-tier golden eval: run /v1/answer against the live stack and judge
grounding honesty, not fluency.

Why this tier exists
    The retrieval eval (scripts/eval_retrieval.py) scores ranking. It cannot
    see whether the reasoning model grounds its claims in the retrieved
    excerpts or honestly refuses when they do not answer. The golden corpus
    carries two behaviors (answer / abstain) precisely so the agent tier can
    be gated on honesty: a hallucinated answer to an absent message id
    (IEA500I, CSQJ001I, ...) is a product failure that retrieval metrics
    never observe.

Judging contract (structural first)
    The agent's citation validator (agent/cites.py via parse_answer) is the
    single source of truth for grounding: "grounded" means >=1 citation that
    survived validation against the retrieved hit set. This eval never
    re-parses model text with its own citation regex — one rule per concept.
      - answer entry  -> FAIL on: empty body, explicit refusal, or zero
                         validated citations.
      - abstain entry -> FAIL only when the model cites excerpts AND does not
                         explicitly decline (the trap was answered). Zero
                         citations always passes; citing-but-declining is a
                         WARN (hedged abstention), recorded for diagnosis.
    Gold substrings (gold_must_contain / gold_must_not_contain) are applied
    as case-folded literal checks on the answer body for BOTH behaviors:
    the seed author's per-entry intent governs (some abstains REQUIRE the
    literal phrase "excerpts do not answer", others FORBID it), so no
    normalization magic happens here. must_cite_identifier requires the
    identifier to appear in the body or in a validated citation string.
    Gold checks judge MODEL phrasing, so they are suppressed on the agent's
    zero-hits path (a canned message has no model text); the structural
    verdict still fires there.

Deliberate non-features
    - No finish_reason check: AnswerResponse does not expose it; the app
      alerts finish_reason != stop per request in its logs. Changing the
      production response contract for an eval is out of scope.
    - No retries: /v1/answer is single-shot by contract (issue #20 PR C);
      an eval that retried would measure retry policy, not the product.
    - No baseline comparison yet: reasoning-model sampling is not
      bit-deterministic run to run. Structural FAILs (grounding honesty)
      are the gate; numeric rates are recorded in the run manifest for
      trend inspection. A tolerance-gated baseline can be added once run
      to run variance is known.

Execution shape
    In-process FastAPI TestClient against the live app (precedent:
    scripts/test_local_e2e_vllm.py) — same app code path as production,
    lifespan-built clients, no second deployment to drift. Requires the
    live stack (Qdrant + embedding vLLM + reasoning vLLM); it is a make
    target, never part of plain pytest.

    Sampling is deterministic stratified round-robin (sorted classes, sorted
    ids, no RNG): every query_class — including negative/abstain traps — is
    represented, and reports are reproducible. --max-queries bounds GPU cost
    (each query is one reasoning-model call); --all runs the full corpus.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# The agent's fixed zero-hits response (agent/app.py) — quoted, not imported,
# to keep the eval importable without the fastapi/qdrant stack for its pure
# helpers' tests. If the production string changes, this must change with it.
ZERO_HITS_ANSWER = "No supporting manual excerpts were found for this question."

# Explicit-refusal markers for the LLM path. The system prompt's rule 4
# instructs the model to "say so explicitly" when excerpts do not answer;
# these are the phrasings the prompt and the seed's gold fields actually use.
REFUSAL_MARKERS = (
    "no supporting manual excerpts",
    "excerpts do not answer",
    "excerpts do not contain",
    "excerpts provided do not",
    "not documented in the excerpts",
    "excerpts do not cover",
)


def is_explicit_refusal(answer_body: str) -> bool:
    """True when the answer body explicitly declines to answer from the
    excerpts. Single helper for both verdict branches (one rule per
    concept); case-folded substring semantics, matching the gold fields."""
    low = answer_body.lower()
    return any(m in low for m in REFUSAL_MARKERS)


def judge(
    entry: dict[str, Any],
    answer: str,
    citations: list[str],
    judge_gold: bool = True,
) -> tuple[str, list[str], list[str]]:
    """Judge one /v1/answer response against a golden entry.

    Returns (verdict, failures, warns); verdict is "pass" or "fail".
    Pure function — no I/O, no stack access — so hermetic tests can fire
    every branch.

    judge_gold=False suppresses the gold-substring and must_cite_identifier
    checks: they judge MODEL phrasing, and the agent's fixed zero-hits
    message contains no model text (citing a canned string can never teach
    us anything about the model). Structural grounding verdicts always run."""
    failures: list[str] = []
    warns: list[str] = []
    body = answer.strip()
    refusal = is_explicit_refusal(body)
    grounded = bool(citations)

    if entry["expected_behavior"] == "answer":
        if not body:
            failures.append("empty answer body")
        elif refusal:
            failures.append("explicit refusal on an answer-tier query")
        if not grounded and not refusal and body:
            failures.append("zero validated citations")
    else:  # abstain
        if grounded and not refusal:
            failures.append(f"trap answered: {len(citations)} validated citation(s)")
        elif grounded and refusal:
            warns.append("hedged abstention: cites excerpts but declines")
        elif not grounded and not refusal:
            warns.append("silent abstention: no citations and no explicit refusal phrase")

    if judge_gold:
        for s in entry.get("gold_must_contain") or []:
            if str(s).lower() not in body.lower():
                failures.append(f"missing required substring: {s!r}")
        for s in entry.get("gold_must_not_contain") or []:
            if str(s).lower() in body.lower():
                failures.append(f"forbidden substring present: {s!r}")

        ident = entry.get("must_cite_identifier")
        if ident:
            hay = " ".join([body, *citations]).lower()
            if str(ident).lower() not in hay:
                failures.append(f"identifier not cited or mentioned: {ident!r}")

    return ("fail" if failures else "pass"), failures, warns


def select_sample(entries: list[dict[str, Any]], max_queries: int) -> list[dict[str, Any]]:
    """Deterministic stratified round-robin over query classes.

    Sorted classes, sorted ids, no RNG: run-to-run reproducible, every class
    (incl. negative/abstain traps) represented, GPU cost bounded by
    max_queries. Classes with fewer entries are revisited first so a class
    of 6 still contributes its full depth against classes of 40."""
    by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in sorted(entries, key=lambda x: (x["query_class"], x["id"])):
        by_class[e["query_class"]].append(e)
    classes = sorted(by_class)
    picked: list[dict[str, Any]] = []
    depth = 0
    while len(picked) < max_queries:
        progressed = False
        for cls in classes:
            rows = by_class[cls]
            if depth < len(rows):
                picked.append(rows[depth])
                progressed = True
                if len(picked) >= max_queries:
                    return picked
        if not progressed:
            break
        depth += 1
    return picked


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-entry results into report metrics. Pure function."""
    judged = [r for r in results if r.get("verdict") in ("pass", "fail")]
    answer_rows = [r for r in judged if r["expected_behavior"] == "answer"]
    abstain_rows = [r for r in judged if r["expected_behavior"] == "abstain"]

    def rate(rows: list[dict[str, Any]]) -> float | None:
        return round(sum(1 for r in rows if r["verdict"] == "pass") / len(rows), 4) if rows else None

    metrics: dict[str, Any] = {
        "queries": len(results),
        "judged": len(judged),
        "errors": sum(1 for r in results if r.get("verdict") == "error"),
        "answer_n": len(answer_rows),
        "abstain_n": len(abstain_rows),
        "answer_pass_rate": rate(answer_rows),
        "abstain_pass_rate": rate(abstain_rows),
        "failures": sum(1 for r in judged if r["verdict"] == "fail"),
        "warns": sum(len(r.get("warns") or []) for r in judged),
        "zero_hits": sum(1 for r in results if r.get("path") == "zero_hits"),
        "citations_per_answer": (
            round(sum(len(r.get("citations") or []) for r in answer_rows) / len(answer_rows), 3)
            if answer_rows
            else None
        ),
    }
    by_class: Counter = Counter()
    by_class_pass: Counter = Counter()
    for r in judged:
        by_class[r["query_class"]] += 1
        if r["verdict"] == "pass":
            by_class_pass[r["query_class"]] += 1
    metrics["by_class"] = {
        cls: {"n": by_class[cls], "pass": by_class_pass[cls]} for cls in sorted(by_class)
    }
    return metrics


def run_query(client: Any, entry: dict[str, Any]) -> dict[str, Any]:
    """One live /v1/answer call + verdict. Records everything the report and
    the manifest need, including the failure detail (the answer body is the
    model's own output — kept in the JSON report for debugging, never
    logged)."""
    t0 = time.monotonic()
    row: dict[str, Any] = {
        "id": entry["id"],
        "query": entry["query"],
        "query_class": entry["query_class"],
        "expected_behavior": entry["expected_behavior"],
        "trap_type": entry.get("trap_type"),
        "domain": entry.get("domain"),
    }
    try:
        resp = client.post("/v1/answer", json={"query": entry["query"]})
    except Exception as exc:  # noqa: BLE001 — one bad request must not kill the run
        row.update(verdict="error", failures=[f"request error: {type(exc).__name__}"], elapsed_ms=int((time.monotonic() - t0) * 1000))
        return row
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    if resp.status_code != 200:
        # The error envelope is the agent's fixed client contract; str(exc)
        # and upstream bodies never reach the client, so do not expect them.
        try:
            code = resp.json().get("error", {}).get("code", "?")
        except Exception:  # noqa: BLE001
            code = "?"
        row.update(verdict="error", failures=[f"HTTP {resp.status_code} ({code})"], elapsed_ms=elapsed_ms)
        return row
    data = resp.json()
    answer = str(data.get("answer") or "")
    citations = list(data.get("citations") or [])
    zero_hits = answer.strip() == ZERO_HITS_ANSWER
    # Zero-hits is the agent's canned message (no model text): gold-substring
    # checks would judge the canned string, not the model — suppress them and
    # record why. The structural verdict still fires (refusal on an answer
    # entry remains a FAIL: that is the retrieval gap showing through).
    verdict, failures, warns = judge(entry, answer, citations, judge_gold=not zero_hits)
    if zero_hits:
        warns.append("zero-hits path: gold substrings not judged (canned agent message)")
    row.update(
        verdict=verdict,
        failures=failures,
        warns=warns,
        answer=answer,
        citations=citations,
        script=data.get("script"),
        path="zero_hits" if zero_hits else "llm",
        elapsed_ms=elapsed_ms,
    )
    return row


def write_summary(path: Path, results: list[dict[str, Any]], metrics: dict[str, Any]) -> None:
    """Human-readable markdown summary: metrics table + every failure with
    its detail so the report is self-contained."""
    lines: list[str] = ["# Answer-tier eval summary", ""]
    lines.append(f"- queries: {metrics['queries']} (judged {metrics['judged']}, errors {metrics['errors']})")
    lines.append(f"- answer pass rate: {metrics['answer_pass_rate']} (n={metrics['answer_n']})")
    lines.append(f"- abstain pass rate: {metrics['abstain_pass_rate']} (n={metrics['abstain_n']})")
    lines.append(f"- failures: {metrics['failures']}, warns: {metrics['warns']}, zero-hits paths: {metrics['zero_hits']}")
    lines.append(f"- citations per answer (mean): {metrics['citations_per_answer']}")
    lines.append("")
    lines.append("| class | n | pass |")
    lines.append("|---|---|---|")
    for cls, m in metrics["by_class"].items():
        lines.append(f"| {cls} | {m['n']} | {m['pass']} |")
    lines.append("")
    fails = [r for r in results if r.get("verdict") in ("fail", "error")]
    if fails:
        lines.append("## Failures")
        for r in fails:
            lines.append(f"### {r['id']} ({r['verdict']}, {r['expected_behavior']})")
            lines.append(f"- query: {r['query']}")
            for f in r.get("failures") or []:
                lines.append(f"- FAIL: {f}")
            if r.get("citations") is not None:
                lines.append(f"- citations: {r['citations']}")
            body = (r.get("answer") or "")[:600]
            lines.append(f"- answer (truncated): {body}")
            lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Answer-tier golden eval against the live stack")
    parser.add_argument("--golden", type=Path, action="append", default=None,
                        help="golden JSONL path (repeatable; default: evals/golden.jsonl + evals/holdout.jsonl)")
    parser.add_argument("--max-queries", type=int, default=24,
                        help="deterministic stratified sample size (default 24)")
    parser.add_argument("--all", action="store_true", help="run every golden entry (slow: one reasoning call each)")
    parser.add_argument("--out", type=Path, default=None, help="JSON report path")
    parser.add_argument("--summary", type=Path, default=None, help="markdown summary path")
    args = parser.parse_args(argv)

    golden_paths = args.golden or [REPO / "evals" / "golden.jsonl", REPO / "evals" / "holdout.jsonl"]
    entries: list[dict[str, Any]] = []
    for p in golden_paths:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                entries.append(json.loads(line))
    if len({e["id"] for e in entries}) != len(entries):
        print("BUILD FAILED: duplicate entry ids across golden files", file=sys.stderr)
        return 1

    sample = entries if args.all else select_sample(entries, args.max_queries)
    print(f"[*] answer-tier eval: {len(sample)} of {len(entries)} entries (deterministic stratified sample)", file=sys.stderr)

    # Live stack only from here on: env must be set before the app imports it
    # (same pattern as scripts/test_local_e2e_vllm.py — load_settings reads the
    # environment at import time).
    from fastapi.testclient import TestClient

    import mainframe_rag.agent.app as app_mod

    results: list[dict[str, Any]] = []
    with TestClient(app_mod.app) as client:
        for i, entry in enumerate(sample, 1):
            row = run_query(client, entry)
            results.append(row)
            marker = row.get("verdict", "?").upper()
            print(
                f"[{i:>3}/{len(sample)}] {marker:5s} {row['id']:8s} "
                f"{row['query'][:58]}",
                file=sys.stderr,
            )
            for f in row.get("failures") or []:
                print(f"        FAIL {f}", file=sys.stderr)

    metrics = summarize(results)
    report = {"metrics": metrics, "results": results}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        write_summary(args.summary, results, metrics)

    # Trend manifest (gitignored evals/runs/) — same helper as the retrieval
    # eval so dashboards see one shape. run_type "eval_answers".
    try:
        from mainframe_rag.config import load_settings
        from mainframe_rag.manifest import write_run_manifest

        manifest = write_run_manifest("eval_answers", load_settings(), metrics)
        print(f"run manifest appended ({manifest['git_sha'][:8]})", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — manifest is observability, never the gate
        print(f"warn: failed to append run manifest: {exc}", file=sys.stderr)

    # Fail closed: structural FAILs and stack errors both gate the exit code.
    return 0 if metrics["failures"] == 0 and metrics["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
