#!/usr/bin/env python3
"""Harness L2 — answer tier: citation precision/recall, faithfulness judge,
truncation rate, syntax-shape compliance.

Where it sits
    L1 (scripts/harness_l1.py) gates retrieval deterministically against a
    snapshot-pinned index. L2 scores the ANSWER tier on the live GPU stack
    (Qdrant + embedding vLLM + reasoning vLLM): the deterministic retrieval
    metrics cannot see whether the reasoning model cites the right books,
    stays faithful to the excerpts it cites, finishes its output, or codes
    the construct the query asked for.

Judging contract (inherits the answer-tier eval's rules)
    The agent's citation validator (agent/cites.py via parse_answer) is the
    single source of truth for grounding; /v1/answer only ever returns
    validated citations, so this harness NEVER re-parses model text for
    citations.     Structural verdicts (refusal on answer rows, trap answered,
    zero validated citations on the LLM path, gold substrings, inferred-only
    citations) come from scripts/eval_answers.py's runner — one judging path,
    not two. Grounding is explicit-provenance only (issue #269): the
    `citations_inferred` flag never counts toward `grounded_rate`.

    L2 adds four measurements, all pure additions to those rows:

    citation precision/recall (doc-level, vs expected_doc_ids)
        Which cited docs are in the entry's gold set (precision) and how
        much of the gold set was cited (recall). Citation strings are
        `[n] {hit.cite}`; because /v1/answer retrieves with the same
        limit-8 retrieve_search call /v1/search makes (same query, no
        filters, deterministic index), each citation maps back to its hit
        by exact cite-string match — no doc-number regex guessing. L2
        fetches that pool with a sibling /v1/search request rather than
        seeing the answer request's internal retrieval (the response
        contract deliberately does not expose it), so it must run on the
        L1-pinned collection; any citation that does not map back to the
        fetched pool FAILS the row — P/R computed over a silently-wrong
        join would be worse than no measurement.
    faithfulness (temp-0 NLI judge)
        The same local reasoning model judges, per grounded answer, whether
        the ANSWER is entailed by / contradicted by / unsupported-by the
        cited excerpts' text (claim-vs-excerpt prompting, temperature 0).
        The judge never sees the citation markers — only the evidence text
        and the answer body. Unparseable judge output is a structural FAIL
        (judge infrastructure must never silently pass a row); the label
        distribution is trend data, not a gate.
    relevance (L4 only, `relevance_enabled=True`)
        A second temp-0 judge labels the answer relevant/partial/irrelevant
        to the question — without excerpts, so it says nothing about
        grounding. L2 itself keeps its four measurements; harness_l4.py
        requests this leg and owns its thresholds. Unparseable output is a
        structural FAIL like faithfulness.
    truncation rate
        The app alerts finish_reason != stop per request in its logs (the
        response contract deliberately does not expose it). L2 runs the app
        in-process, captures those alert lines, and joins them to rows by
        request_id. Rate is trend data.
    syntax-shape compliance
        Syntax answer entries carry a `syntax_pattern` (authored in
        scripts/build_golden_corpus.py): the produced answer/script must
        match it. A miss is a structural FAIL — the query asked to code a
        construct and the answer must name it. Keyword-presence patterns by
        design; tighten per entry only after observing real outputs.

Gate vs trend
    Structural FAILs (grounding honesty + syntax misses + judge errors +
    request errors) gate the exit code, exactly like the answer-tier eval.
    Numeric rates (P/R, faithfulness, truncation) are recorded in the JSON
    report and the run manifest for trend inspection — reasoning-model
    sampling is not run-deterministic, so tolerance-gating them would gate
    noise. No retries: /v1/answer is single-shot by contract.

Execution shape
    In-process FastAPI TestClient against the live app (precedent:
    scripts/test_local_e2e_vllm.py, scripts/eval_answers.py). Requires the
    live GPU stack; it is a make target (harness-l2), never part of plain
    pytest and never a PR gate. Sampling is the same deterministic
    stratified round-robin as the answer-tier eval.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "scripts") not in sys.path:
    # script-path imports (eval_answers) must resolve both when run as
    # `python scripts/harness_l2.py` and when imported as scripts.harness_l2
    sys.path.insert(0, str(REPO / "scripts"))

from eval_answers import _AnswerCapture, failure_bucket, run_query, select_sample
from venue import VenueError, require_rc_for_collection, resolve_golden_paths

# Shared citation-index shape ([n] / [n, m]); see the inference rule in
# agent/answer.py — parentheses are IBM-manual noise, never markers.
_INLINE_CITE_RE = re.compile(r"\[\s*\d+(?:\s*,\s*\d+)*\s*\]")

from mainframe_rag.ports import ChatMessage

# Evidence bound for the judge prompt: 8 chunk-capped excerpts can exceed
# 25k chars; the judge needs the claims' context, not the whole pool.
JUDGE_MAX_EVIDENCE_CHARS = 6000

CITE_PREFIX_RE = re.compile(r"^\s*\[\d+\]\s*")
JUDGE_LABELS = ("entailed", "neutral", "contradiction")
RELEVANCE_LABELS = ("relevant", "partial", "irrelevant")
JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


class JudgeError(RuntimeError):
    """Judge output could not be parsed into a label — structural FAIL."""


def citation_to_hit(citation: str, hits: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Map a validated citation string (`[n] {hit.cite}`) back to its hit by
    exact cite match. None when the hit is not in the fetched search rows —
    the caller records it rather than guessing a doc id with a regex."""
    cite = CITE_PREFIX_RE.sub("", citation)
    for h in hits:
        if h.get("cite") == cite:
            return h
    return None


def cited_doc_ids(citations: list[str], hits: list[dict[str, Any]]) -> tuple[set[str], list[str]]:
    """Doc ids behind validated citations, plus any citation that could not
    be mapped (recorded for diagnosis; never guessed)."""
    docs: set[str] = set()
    unmatched: list[str] = []
    for c in citations:
        hit = citation_to_hit(c, hits)
        if hit is None:
            unmatched.append(c)
        else:
            docs.add(str(hit.get("doc_id") or ""))
    return docs, unmatched


def precision_recall(cited: set[str], gold: set[str]) -> tuple[float | None, float | None]:
    """Doc-level citation precision/recall. None denominators (no citations
    / no gold) stay None so abstain rows and zero-hit paths never dilute
    the averages."""
    if not gold:
        return None, None
    if not cited:
        return None, 0.0
    inter = len(cited & gold)
    return inter / len(cited), inter / len(gold)


def judge_messages(answer: str, evidence: str) -> list[ChatMessage]:
    """NLI-style claim-vs-excerpt prompt. The judge sees evidence text and
    the answer body — never citation markers (the validator owns those).
    Returns ChatMessage rows so the production HttpxLLMClient can send them
    unmodified (one client shape, no second serializer)."""
    return [
        ChatMessage(
            role="system",
            content=(
                "You are a strict grounding judge for a retrieval-augmented "
                "answer system. You are given manual EXCERPTS and an ANSWER "
                'produced from them. Reply with exactly one JSON object: '
                '{"label": "entailed"} when every factual claim in the ANSWER '
                'is supported by the EXCERPTS, {"label": "contradiction"} '
                'when the EXCERPTS contradict a claim, {"label": "neutral"} '
                "when the ANSWER makes claims the EXCERPTS neither support "
                "nor contradict. No other text."
            ),
        ),
        ChatMessage(role="user", content=f"EXCERPTS:\n{evidence}\n\nANSWER:\n{answer}"),
    ]


def relevance_messages(question: str, answer: str) -> list[ChatMessage]:
    """Answer-relevance prompt: does the ANSWER address the QUESTION.

    Separate from faithfulness by design (one label per prompt): relevance
    needs no excerpts, so the judge never sees manual text or citation
    markers here. Returns ChatMessage rows for the production client.
    """
    return [
        ChatMessage(
            role="system",
            content=(
                "You are a strict answer-relevance judge for a "
                "retrieval-augmented question answering system. Given a "
                "QUESTION and an ANSWER, reply with exactly one JSON object: "
                '{"label": "relevant"} when the ANSWER directly addresses the '
                'QUESTION, {"label": "partial"} when it addresses only part '
                'of the QUESTION or drifts off-topic, {"label": "irrelevant"} '
                "when it does not address the QUESTION. No other text."
            ),
        ),
        ChatMessage(role="user", content=f"QUESTION:\n{question}\n\nANSWER:\n{answer}"),
    ]


def _parse_label(text: str, labels: tuple[str, ...]) -> str:
    """Extract one label from the judge's JSON reply (tolerates fences/prose
    around the object; anything else fails closed)."""
    m = JSON_BLOCK_RE.search(text)
    if m:
        try:
            label = json.loads(m.group(0)).get("label")
        except json.JSONDecodeError:
            label = None
        if label in labels:
            return label
    raise JudgeError(f"unparseable judge reply: {text[:120]!r}")


def parse_judge_label(text: str) -> str:
    """Faithfulness label (entailed/neutral/contradiction)."""
    return _parse_label(text, JUDGE_LABELS)


def parse_relevance_label(text: str) -> str:
    """Relevance label (relevant/partial/irrelevant)."""
    return _parse_label(text, RELEVANCE_LABELS)


def evidence_for_citations(citations: list[str], hits: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Excerpt text behind the cited hits (deterministic retrieval makes the
    /v1/search rows the same pool the agent answered from), bounded for the
    judge prompt. Returns (evidence, unmapped citations)."""
    texts: list[str] = []
    unmapped: list[str] = []
    for c in citations:
        hit = citation_to_hit(c, hits)
        if hit is None:
            unmapped.append(c)
        else:
            texts.append(str(hit.get("text") or ""))
    evidence = "\n\n---\n\n".join(texts)
    if len(evidence) > JUDGE_MAX_EVIDENCE_CHARS:
        evidence = evidence[:JUDGE_MAX_EVIDENCE_CHARS] + "\n…[truncated for the judge]"
    return evidence, unmapped


class _AlertCapture(logging.Handler):
    """Buffer the app's per-request JSON log lines (logger 'agent') so L2 can
    join finish_reason != stop alerts to the row that produced them. The
    response contract deliberately does not expose finish_reason; the log
    line is the documented signal (agent/app.py)."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.alerts: dict[str, str] = {}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = json.loads(record.getMessage())
        except json.JSONDecodeError:
            return
        if payload.get("action") == "answer_alert" and payload.get("alert") == "finish_reason_non_stop":
            rid = str(payload.get("request_id") or "")
            if rid:
                self.alerts[rid] = str(payload.get("finish_reason") or "non-stop")


def syntax_check(pattern: str, body: str, script: str | None) -> bool:
    """True when the produced answer (or its fenced script) matches the
    entry's syntax-shape gold. Compile errors fail closed via JudgeError —
    a broken pattern must gate loudly, never silently pass."""
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise JudgeError(f"invalid syntax_pattern {pattern!r}: {exc}") from exc
    hay = f"{body}\n{script or ''}"
    return rx.search(hay) is not None


def summarize_l2(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate L2 rows: rates are trend data, structural fails gate."""
    judged = [r for r in results if r.get("verdict") in ("pass", "fail")]
    answer_llm = [
        r for r in judged
        if r["expected_behavior"] == "answer" and r.get("path") == "llm"
    ]
    syntax_rows = [r for r in answer_llm if r.get("query_class") == "syntax"]

    def rate(num: int, den: int) -> float | None:
        return round(num / den, 4) if den else None

    precs = [r["citation_precision"] for r in judged if r.get("citation_precision") is not None]
    recs = [r["citation_recall"] for r in judged if r.get("citation_recall") is not None]
    labels = [r["judge_label"] for r in judged if r.get("judge_label") in JUDGE_LABELS]
    judge_errs = sum(1 for r in judged if r.get("judge_error"))
    relevances = [r["relevance_label"] for r in judged if r.get("relevance_label") in RELEVANCE_LABELS]
    relevance_errs = sum(1 for r in judged if r.get("relevance_error"))
    n_answer = len(answer_llm)
    metrics: dict[str, Any] = {
        "queries": len(results),
        "judged": len(judged),
        "errors": sum(1 for r in results if r.get("verdict") == "error"),
        "structural_fails": sum(1 for r in judged if r["verdict"] == "fail"),
        "answer_llm_n": n_answer,
        "grounded_rate": rate(
            sum(1 for r in answer_llm if r.get("citations") and not r.get("citations_inferred")),
            n_answer,
        ),
        "inferred_citations": sum(1 for r in judged if r.get("citations_inferred")),
        "citation_precision": round(sum(precs) / len(precs), 4) if precs else None,
        "citation_recall": round(sum(recs) / len(recs), 4) if recs else None,
        "truncation_rate": rate(sum(1 for r in answer_llm if r.get("truncated")), n_answer),
        "syntax_compliance": (
            round(sum(1 for r in syntax_rows if r.get("syntax_ok")) / len(syntax_rows), 4)
            if syntax_rows else None
        ),
        "syntax_n": len(syntax_rows),
        "faithfulness": {
            "judged": len(labels),
            "judge_errors": judge_errs,
            **{lbl: rate(sum(1 for l in labels if l == lbl), len(labels)) for lbl in JUDGE_LABELS},
        },
        "relevance": {
            "judged": len(relevances),
            "judge_errors": relevance_errs,
            **{
                lbl: rate(sum(1 for l in relevances if l == lbl), len(relevances))
                for lbl in RELEVANCE_LABELS
            },
        },
        "unmapped_citations": sum(len(r.get("unmatched_citations") or []) for r in judged),
        # Truncation attribution (issue #298): truncated share per served
        # query complexity. Unknown covers rows without the answer log join
        # (error rows, zero-hits path) — a missing signal, never a verdict.
        "by_complexity": _by_complexity_truncation(answer_llm, rate),
        # WHY slices (issue #299): P/R means per query class plus the
        # template-bucketed failure histogram, so the report reads off
        # which mode dominates instead of grepping row strings.
        "by_class": _by_class_pr(judged),
        "by_failure": _by_failure_histogram(judged),
    }
    return metrics


def _by_class_pr(judged: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Mean P/R per query class over rows that have the metric. Pure."""
    precs: dict[str, list[float]] = {}
    recs: dict[str, list[float]] = {}
    ns: dict[str, int] = {}
    for r in judged:
        cls = str(r.get("query_class") or "unknown")
        ns[cls] = ns.get(cls, 0) + 1
        if r.get("citation_precision") is not None:
            precs.setdefault(cls, []).append(r["citation_precision"])
        if r.get("citation_recall") is not None:
            recs.setdefault(cls, []).append(r["citation_recall"])
    out: dict[str, dict[str, Any]] = {}
    for cls in sorted(ns):
        out[cls] = {
            "n": ns[cls],
            "citation_precision": (
                round(sum(precs[cls]) / len(precs[cls]), 4) if cls in precs else None
            ),
            "citation_recall": (
                round(sum(recs[cls]) / len(recs[cls]), 4) if cls in recs else None
            ),
        }
    return out


def _by_failure_histogram(judged: list[dict[str, Any]]) -> dict[str, int]:
    """Template-bucketed failure counts (shared bucketing with eval_answers).
    Pure."""
    hist: dict[str, int] = {}
    for r in judged:
        for f in r.get("failures") or []:
            bucket = failure_bucket(f)
            hist[bucket] = hist.get(bucket, 0) + 1
    return dict(sorted(hist.items()))


def _by_complexity_truncation(
    answer_llm: list[dict[str, Any]], rate: Any
) -> dict[str, dict[str, int | float | None]]:
    """Truncated share per served query complexity (issue #298). Pure."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in answer_llm:
        groups.setdefault(row.get("query_complexity") or "unknown", []).append(row)
    return {
        complexity: {
            "n": len(crows),
            "truncated": sum(1 for r in crows if r.get("truncated")),
            "truncation_rate": rate(sum(1 for r in crows if r.get("truncated")), len(crows)),
        }
        for complexity, crows in sorted(groups.items())
    }


def gate_l2(metrics: dict[str, Any]) -> tuple[str, list[str]]:
    """Structural gate: L2 rates are trend data; only fails and errors hold
    the verdict (mirrors harness.py's gate shape)."""
    reasons: list[str] = []
    if metrics["structural_fails"]:
        reasons.append(f"{metrics['structural_fails']} structural failure(s)")
    if metrics["errors"]:
        reasons.append(f"{metrics['errors']} request error(s)")
    if metrics["faithfulness"]["judge_errors"]:
        reasons.append(f"{metrics['faithfulness']['judge_errors']} faithfulness judge error(s)")
    if metrics["relevance"]["judge_errors"]:
        reasons.append(f"{metrics['relevance']['judge_errors']} relevance judge error(s)")
    return ("hold" if reasons else "pass"), reasons


def write_summary(path: Path, results: list[dict[str, Any]], metrics: dict[str, Any]) -> None:
    f = metrics["faithfulness"]
    lines: list[str] = [
        "# Harness L2 — answer tier",
        "",
        f"- queries: {metrics['queries']} (judged {metrics['judged']}, errors {metrics['errors']})",
        f"- grounded rate: {metrics['grounded_rate']} (n={metrics['answer_llm_n']} LLM-path answers)",
        (
            "- citation precision/recall (doc-level): "
            f"{metrics['citation_precision']} / {metrics['citation_recall']}  "
            "(precision: mean over rows with ≥1 citation; recall: over all answer rows "
            "with gold, zero cites = 0 — recall is the stricter half)"
        ),
        f"- truncation rate: {metrics['truncation_rate']} (non-stop finishes, from the app's alert logs)",
        (
            "- by complexity (served query complexity from the answer log line; "
            "unknown = rows without the join): "
            + ", ".join(
                f"{complexity} {block['truncated']}/{block['n']} ({block['truncation_rate']})"
                for complexity, block in (metrics.get("by_complexity") or {}).items()
            )
        ),
        (
            f"- syntax compliance: {metrics['syntax_compliance']} (n={metrics['syntax_n']})  "
            "(keyword-presence gold: the construct was NAMED, not that valid syntax was produced)"
        ),
        (
            f"- faithfulness: entailed {f['entailed']}, neutral {f['neutral']}, "
            f"contradiction {f['contradiction']} (judged {f['judged']}, judge errors {f['judge_errors']})"
        ),
        f"- structural fails: {metrics['structural_fails']} (gates), unmapped citations: {metrics['unmapped_citations']} (each fails its row)",
        "- by class P/R (means over rows carrying the metric): "
        + ", ".join(
            f"{cls} n={b['n']} P/R {b['citation_precision']}/{b['citation_recall']}"
            for cls, b in (metrics.get("by_class") or {}).items()
        ),
        "- by failure: "
        + ", ".join(
            f"{bucket} x{count}" for bucket, count in (metrics.get("by_failure") or {}).items()
        ),
        "",
    ]
    fails = [r for r in results if r.get("verdict") in ("fail", "error")]
    if fails:
        lines.append("## Failures")
        for r in fails:
            lines.append(f"### {r['id']} ({r['verdict']}, {r['expected_behavior']})")
            lines.append(f"- query: {r['query']}")
            for failure in r.get("failures") or []:
                lines.append(f"- FAIL: {failure}")
            if r.get("judge_error"):
                lines.append(f"- judge error: {r['judge_error']}")
            if r.get("syntax_ok") is False:
                lines.append(f"- syntax pattern missed: {r.get('syntax_pattern')}")
            if r.get("citations") is not None:
                lines.append(f"- citations: {r['citations']}")
            lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def apply_l2_measurements(
    row: dict[str, Any],
    entry: dict[str, Any],
    hits: list[dict[str, Any]],
    alerts: dict[str, str],
) -> None:
    """Attach the L2 measurements to one answer-tier row and fail closed on
    anything that would make a rate silently wrong. Pure function (no stack
    access) so hermetic tests fire every branch; the faithfulness judge
    stays in run_l2 because it needs the live client.

    Fail-closed rules (all structural, all gating):
      - a validated citation that does not map back to a hit — the join
        behind citation P/R is broken, so P/R must not present itself as an
        honest trend (the /v1/search pool can diverge from the agent's
        retrieval; run L2 on the L1-pinned collection and this fires only
        on real drift);
      - a missing request_id on an LLM-path row — truncation would read
        false by construction instead of being measured.
    """
    rid = row.get("request_id")
    if row.get("path") == "llm":
        if rid is None:
            row["truncated"] = None
            row.setdefault("failures", []).append("truncation unverifiable: response had no request_id")
            row["verdict"] = "fail"
        else:
            row["truncated"] = str(rid) in alerts

    cited, unmatched = cited_doc_ids(row.get("citations") or [], hits)
    row["cited_doc_ids"] = sorted(cited)
    row["unmatched_citations"] = unmatched
    gold = set(entry.get("expected_doc_ids") or [])
    if row["expected_behavior"] == "answer" and gold:
        prec, rec = precision_recall(cited, gold)
        row["citation_precision"] = prec
        row["citation_recall"] = rec
    if unmatched and row.get("path") == "llm":
        row.setdefault("failures", []).append(
            f"{len(unmatched)} validated citation(s) not in the fetched hit set: {unmatched}"
        )
        row["verdict"] = "fail"

    # Answer-content floor (issue #192): validated citations over a vacuous
    # body are decoration, not grounding — a weak model emitting only a
    # copied Citations: section photographs healthy P/R over zero
    # substance. Inline [n] / [n, m] markers are the shared citation shape
    # (parentheses are manual noise, never stripped). Script answers are
    # exempt (code carries the substance); honest abstentions never reach
    # here (no citations) and stay judged by the refusal verdict.
    if (
        row.get("path") == "llm"
        and row.get("expected_behavior") == "answer"
        and (row.get("citations") or [])
        and not row.get("script")
        and not _INLINE_CITE_RE.sub("", row.get("answer") or "").strip()
    ):
        row.setdefault("failures", []).append(
            "answer body has no substantive prose outside citations: "
            "validated citations over a vacuous body"
        )
        row["verdict"] = "fail"

    # Syntax-shape gold: LLM-path rows with real model text only — a
    # zero-hits canned message is already a structural fail via the
    # answer-tier verdict.
    pattern = entry.get("syntax_pattern")
    if pattern and row.get("path") == "llm" and not row.get("failures"):
        row["syntax_pattern"] = pattern
        try:
            row["syntax_ok"] = syntax_check(pattern, row.get("answer") or "", row.get("script"))
            if not row["syntax_ok"]:
                row.setdefault("failures", []).append(f"syntax pattern missed: {pattern}")
                row["verdict"] = "fail"
        except JudgeError as exc:
            row["syntax_ok"] = None
            row.setdefault("failures", []).append(str(exc))
            row["verdict"] = "fail"


def run_l2(
    entries: list[dict[str, Any]],
    max_queries: int | None,
    judge_enabled: bool = True,
    relevance_enabled: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Live run: search rows (deterministic, same pool the agent answers
    from), /v1/answer per entry, then the L2 measurements. Returns (rows,
    metrics). ``relevance_enabled`` adds the answer-relevance judge (L4);
    L2 itself runs faithfulness only."""
    from fastapi.testclient import TestClient

    import mainframe_rag.agent.app as app_mod
    from mainframe_rag.agent.answer import HttpxLLMClient
    from mainframe_rag.config import load_settings

    settings = load_settings()
    sample = entries if max_queries is None else select_sample(entries, max_queries)
    print(f"[*] harness L2: {len(sample)} of {len(entries)} entries", file=sys.stderr)

    capture = _AlertCapture()
    logging.getLogger("agent").addHandler(capture)
    answers = _AnswerCapture()
    logging.getLogger("agent").addHandler(answers)

    judge_client: Any = None
    if judge_enabled:
        judge_client = HttpxLLMClient(settings)

    results: list[dict[str, Any]] = []
    try:
        with TestClient(app_mod.app) as client:
            search_cache: dict[str, list[dict[str, Any]]] = {}

            def hits_for(query: str) -> list[dict[str, Any]]:
                if query not in search_cache:
                    resp = client.post("/v1/search", json={"query": query, "limit": 8})
                    # TestClient returns JSON — hits are already plain dicts
                    search_cache[query] = (
                        list(resp.json().get("hits", [])) if resp.status_code == 200 else []
                    )
                return search_cache[query]

            for i, entry in enumerate(sample, 1):
                hits = hits_for(entry["query"])
                # WHY join (issue #299): the same sibling pool the
                # measurements below use travels with the row, so
                # retriever blame (gold never pooled) splits from reader
                # blame (gold pooled, uncited) without rejoining gold.
                row = run_query(client, entry, answers.signals, hits)
                apply_l2_measurements(row, entry, hits, capture.alerts)

                # Faithfulness judge: grounded, non-refused model text.
                if (
                    judge_client is not None
                    and row.get("path") == "llm"
                    and row["expected_behavior"] == "answer"
                    and row.get("citations")
                    and not row.get("failures")
                ):
                    evidence, _unmapped = evidence_for_citations(row["citations"], hits)
                    try:
                        chat = judge_client.chat(
                            judge_messages(row["answer"], evidence),
                            temperature=0.0,
                        )
                        row["judge_label"] = parse_judge_label(chat.content)
                    except Exception as exc:  # noqa: BLE001 — judge infra fails closed
                        row["judge_error"] = f"{type(exc).__name__}: {exc}"
                        row.setdefault("failures", []).append(f"faithfulness judge failed: {row['judge_error']}")
                        row["verdict"] = "fail"

                # Relevance judge (L4 only): same grounded, non-failed rows;
                # it sees only question + answer, never excerpts or markers.
                if (
                    relevance_enabled
                    and judge_client is not None
                    and row.get("path") == "llm"
                    and row["expected_behavior"] == "answer"
                    and row.get("citations")
                    and not row.get("failures")
                ):
                    try:
                        chat = judge_client.chat(
                            relevance_messages(entry["query"], row["answer"]),
                            temperature=0.0,
                        )
                        row["relevance_label"] = parse_relevance_label(chat.content)
                    except Exception as exc:  # noqa: BLE001 — judge infra fails closed
                        row["relevance_error"] = f"{type(exc).__name__}: {exc}"
                        row.setdefault("failures", []).append(f"relevance judge failed: {row['relevance_error']}")
                        row["verdict"] = "fail"

                results.append(row)
                marker = row.get("verdict", "?").upper()
                extra = ""
                if row.get("judge_label"):
                    extra = f" judge={row['judge_label'][:4]}"
                if row.get("relevance_label"):
                    extra += f" rel={row['relevance_label'][:3]}"
                print(
                    f"[{i:>3}/{len(sample)}] {marker:5s} {row['id']:8s} {row['query'][:52]}{extra}",
                    file=sys.stderr,
                )
                for failure in row.get("failures") or []:
                    print(f"        FAIL {failure}", file=sys.stderr)
    finally:
        logging.getLogger("agent").removeHandler(capture)
        logging.getLogger("agent").removeHandler(answers)
        if judge_client is not None:
            judge_client.close()

    return results, summarize_l2(results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Harness L2: answer tier on the live GPU stack")
    parser.add_argument("--golden", type=Path, action="append", default=None,
                        help="golden JSONL path (repeatable; default: dev golden, +holdout under VENUE=rc)")
    parser.add_argument("--max-queries", type=int, default=24,
                        help="deterministic stratified sample size (default 24)")
    parser.add_argument("--all", action="store_true", help="run every golden entry (slow: one reasoning call each)")
    parser.add_argument("--no-judge", action="store_true",
                        help="skip the faithfulness judge (its infra errors gate; use only to isolate failures)")
    parser.add_argument("--out", type=Path, default=None, help="JSON report path")
    parser.add_argument("--summary", type=Path, default=None, help="markdown summary path")
    args = parser.parse_args(argv)

    from mainframe_rag.config import load_settings

    try:
        golden_paths = resolve_golden_paths(args.golden)
        require_rc_for_collection(load_settings().qdrant_collection)
    except VenueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    entries: list[dict[str, Any]] = []
    for p in golden_paths:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                entries.append(json.loads(line))
    if len({e["id"] for e in entries}) != len(entries):
        print("L2 FAILED: duplicate entry ids across golden files", file=sys.stderr)
        return 1

    t0 = time.monotonic()
    results, metrics = run_l2(entries, None if args.all else args.max_queries, judge_enabled=not args.no_judge)
    metrics["wall_s"] = round(time.monotonic() - t0, 1)

    report = {"metrics": metrics, "results": results}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        write_summary(args.summary, results, metrics)

    try:
        from mainframe_rag.config import load_settings
        from mainframe_rag.manifest import write_run_manifest

        manifest = write_run_manifest("harness_l2", load_settings(), metrics)
        print(f"run manifest appended ({manifest['git_sha'][:8]})", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — manifest is observability, never the gate
        print(f"warn: failed to append run manifest: {exc}", file=sys.stderr)

    verdict, reasons = gate_l2(metrics)
    print(f"[*] L2 VERDICT: {verdict}", file=sys.stderr)
    for r in reasons:
        print(f"    - {r}", file=sys.stderr)
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
