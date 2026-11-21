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
    single source of truth for citations: it returns validated cite strings
    plus a `citations_inferred` flag for cites it mapped from bare bracket
    markers with no explicit citation line (issue #269). Grounding is
    explicit-provenance only — an answer row whose only cites are inferred
    FAILS; the flag is surfaced in the JSON report so fabrication is visible
    rather than silently grounded. This eval never re-parses model text with
    its own citation regex — one rule per concept.
      - answer entry  -> FAIL on: empty body, abstention (the shared
                         marker+shape predicate, #135), zero validated
                         citations, or only inferred citations. A grounded
                         partial answer carrying a scope caveat is NOT an
                         abstention and passes the refusal check; gold
                         substrings still judge its completeness.
      - abstain entry -> FAIL only when the model cites excerpts AND does not
                         explicitly decline (the trap was answered). Zero
                         citations always passes; citing-but-declining is a
                         WARN (hedged abstention), recorded for diagnosis.
    Gold substrings (gold_must_contain / gold_must_not_contain) are applied
    as case-folded literal checks on the answer body for BOTH behaviors:
    the seed author's per-entry intent governs, so no normalization magic
    happens here. Dev-golden refusal rows no longer pin refusal wording
    (issue #596, option a): they carry expected_verification_state
    insufficient_evidence, which the agent serves only when its own refusal
    predicate (or the zero-hits path) recognizes the decline, so any
    recognized phrasing passes and an uncited non-refusal still fails on the
    state check. Answer rows may still FORBID "excerpts do not answer"; the
    frozen holdout's refusal rows keep the literal until a dedicated holdout
    decision. must_cite_identifier requires the
    identifier to appear in the body or in a validated citation string.
    These gold checks read the answer body plus the extracted `script`
    (maintainer decision 2026-11-17, #1285): a build that moves a code fence
    into `script` scores the same as one that keeps it in the answer. The
    structural checks (empty body, refusal, grounding) read the body only.
    Gold checks judge MODEL phrasing, so they are suppressed on the agent's
    zero-hits path (a canned message has no model text); the structural
    verdict still fires there.

Deliberate non-features
    - No finish_reason verdict: the JSON AnswerResponse does not expose it and
      judge() never reads it. The finish reason, token counts and time to
      first token are recorded per row where an existing surface carries them
      (in-process answer log, or the SSE `final` under --stream; issue #1195)
      and reported in the measurement groups, never as a verdict input.
    - No retries: /v1/answer is single-shot by contract (issue #20 PR C);
      an eval that retried would measure retry policy, not the product.
    - No baseline comparison yet: reasoning-model sampling is not
      bit-deterministic run to run. Structural FAILs (grounding honesty)
      are the gate; numeric rates are recorded in the run manifest for
      trend inspection. A tolerance-gated baseline can be added once run
      to run variance is known.

Execution shape
    In-process FastAPI TestClient against the live app by default, using
    production code and lifespan-built clients. --agent-url instead sends
    the same requests to an already deployed HTTP agent and checks its
    serving readiness before scoring. Requires the live stack (Qdrant +
    gateway-backed embedding and reasoning); run eval:answers explicitly,
    never as part of plain pytest.

    Sampling is deterministic stratified round-robin (sorted classes, sorted
    ids, no RNG): every query_class — including negative/abstain traps — is
    represented, and reports are reproducible. --max-queries bounds GPU cost
    (each query is one reasoning-model call); --all runs the full corpus.

Measurement protocol (issue #1195, docs/metric-contract.md)
    --stream (HTTP mode) reads the SSE `final`, so rows carry finish reason,
    token counts and time to first token without the in-process log join.
    --subset N is the same round-robin with a guard that no class is dropped
    and a recorded sample identity (ordered ids plus each resolved dataset's
    basename and sha256); a sweep refuses a resolved set that contains the
    frozen holdout and needs an explicit non-holdout --golden. --repeats R
    runs the sample R times in whole passes and reports the per-metric spread.
    Defaults are unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import sys
import time
from collections import Counter, defaultdict
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2

# The agent's fixed zero-hits response (agent/app.py) — quoted, not imported,
# to keep the eval importable without the fastapi/qdrant stack for its pure
# helpers' tests. If the production string changes, this must change with it.
ZERO_HITS_ANSWER = "No supporting manual excerpts were found for this question."

# Issue #132 named-code form: "No manual excerpts carry DSN90221I." (capped
# term list, so the shape is bounded).
_ZERO_HITS_NAMED_RE = re.compile(r"^No manual excerpts carry .{1,300}\.$")


def is_zero_hits_answer(answer: str) -> bool:
    """True for either canned zero-hits message. Single helper for the
    live-row path below (one rule per concept): model text parroting the
    exact shape is indistinguishable from canned by construction, the
    same premise the exact-match form already accepts."""
    body = answer.strip()
    return body == ZERO_HITS_ANSWER or _ZERO_HITS_NAMED_RE.match(body) is not None


class AnswerCapture(logging.Handler):
    """Buffer the app's per-request answer log lines (logger 'agent') so
    rows can carry the token budget facts the response contract
    deliberately omits (issue #298): query complexity, finish reason,
    and prompt/completion/reasoning/total usage, joined by request_id.
    Sibling to answer_tier._AlertCapture, which joins the non-stop alert
    subset; this one keeps the full answer line for attribution."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.signals: dict[str, dict[str, Any]] = {}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = json.loads(record.getMessage())
        except json.JSONDecodeError:
            return
        if payload.get("action") != "answer":
            return
        rid = str(payload.get("request_id") or "")
        if not rid:
            return
        self.signals[rid] = {
            key: payload.get(key)
            for key in (
                "query_complexity",
                "finish_reason",
                "verification_state",
                "budget_verified",
                "units_omitted",
                "prompt_tokens",
                "completion_tokens",
                "reasoning_tokens",
                "total_tokens",
                "inline_bracket_present",
                "citations_header_present",
                "cites_rejected_shape_bad",
                "cites_rejected_unmapped",
            )
        }


# Refusal interpretation is the agent's single helper (issue #135): the eval's
# abstain verdicts and the agent's zero-citation rule must never diverge.
# The trap branch keeps the marker test (any decline phrase = declined, even
# a long one); the answer branch needs the shape floor so a grounded partial
# answer's scope caveat is not scored as a refusal (#305).
from mainframe_rag.agent.answer import (
    VERIFICATION_STATES,
    has_answer_body,
    is_abstention,
    is_coding_request,
    is_refusal,
)
from mainframe_rag.config import Settings, load_settings
from mainframe_rag.eval.datasets import (
    DatasetError,
    VenueError,  # noqa: F401 — compatibility export
    load_golden_records_with_identity,
    require_rc_for_collection,
    require_tuning_golden,
    resolve_golden_paths,
)
from mainframe_rag.eval.measurement import (
    STREAM_INCOMPLETE,
    STREAM_MALFORMED,
    STREAM_OVERSIZE,
    decode_tps,
    distribution,
    is_count,
    measurement_summary,
    parse_server_timing,
    read_answer_stream,
    read_chat_stream,
    repeat_spread,
    sample_identity,
    stream_signals,
    value_spread,
)


def _agent_url(raw: str) -> str:
    """Explicit HTTP target; URL credentials must never enter reports."""
    try:
        parts = urlsplit(raw.strip())
        _ = parts.port
    except ValueError:
        raise argparse.ArgumentTypeError("agent URL must be an http(s) URL") from None
    if (
        parts.scheme not in ("http", "https") or not parts.hostname
        or parts.username is not None or parts.password is not None
        or parts.query or parts.fragment
    ):
        raise argparse.ArgumentTypeError("agent URL must be http(s), without credentials, query or fragment")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


# Upper bound of the per-request client timeout. It must exceed the longest
# answer a deliberate measurement waits for (xhigh with the maximum thinking
# reserve, issue #843); it is not a default and does not change any agent limit.
_MAX_REQUEST_TIMEOUT_S = 1800


def _request_timeout(raw: str) -> float:
    message = f"request timeout must be 1–{_MAX_REQUEST_TIMEOUT_S} seconds"
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(message) from None
    if not math.isfinite(value) or not 1 <= value <= _MAX_REQUEST_TIMEOUT_S:
        raise argparse.ArgumentTypeError(message)
    return value


def _http_health(client: httpx2.Client) -> tuple[dict[str, Any] | None, str | None]:
    """Read the actual agent verdict, retaining only its fixed health schema."""
    from mainframe_rag.agent.serving import SERVABLE_OUTCOMES

    try:
        response = client.get("/healthz", timeout=10.0)
    except httpx2.HTTPError:
        return None, "agent health request failed"
    http_error = None if response.status_code == 200 else f"agent health returned HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return None, http_error or "malformed agent health response"
    if (
        not isinstance(body, dict) or body.get("status") not in ("ok", "degraded")
        or type(body.get("qdrant")) is not bool
        or any(body.get(key) is not None and type(body.get(key)) is not bool
               for key in ("embed", "rerank"))
        or body.get("representation") not in (
            *SERVABLE_OUTCOMES, "empty", "reembed_required", "legacy", "pending", "unknown",
        )
    ):
        return None, http_error or "malformed agent health response"
    health = {key: body.get(key) for key in ("status", "qdrant", "embed", "representation", "rerank")}
    if http_error is not None:
        return health, http_error
    if (
        health["status"] != "ok" or not health["qdrant"]
        or health["embed"] is False or health["rerank"] is False
        or health["representation"] not in SERVABLE_OUTCOMES
    ):
        return health, "agent health is not servable"
    return health, None


def _write_http_manifest(report: dict[str, Any]) -> dict[str, Any]:
    """Remote trend evidence cannot inherit local Settings or Qdrant identity."""
    from mainframe_rag.manifest import get_git_sha

    manifest = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_type": "eval_answers", "execution_mode": "http",
        "git_sha": "unknown", "evaluator_git_sha": get_git_sha(),
        "settings_hash": None, "model_ids": None, "extraction_rules_version": None,
        "qdrant_version": "unknown", "collection_snapshot_id": None,
        "params": report["params"], "metrics": report["metrics"],
        "deployment": report["deployment"], "representation": report["representation"],
    }
    out = Path("evals/runs/eval_answers_runs.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(manifest, ensure_ascii=False) + "\n")
    return manifest


def judge(
    entry: dict[str, Any],
    answer: str,
    citations: list[str],
    judge_gold: bool = True,
    citations_inferred: bool = False,
    verification_state: str | None = None,
    script: str | None = None,
) -> tuple[str, list[str], list[str]]:
    """Judge one /v1/answer response against a golden entry.

    Returns (verdict, failures, warns); verdict is "pass" or "fail".
    Pure function — no I/O, no stack access — so hermetic tests can fire
    every branch.

    Grounding is explicit-provenance only (issue #269): `citations_inferred`
    marks citations the agent mapped from bare bracket markers with no
    explicit `Citations:` block, the cheapest fabrication path. Those rows
    are surfaced in the report and FAIL the answer verdict — a validated
    cite must come from an explicit citation line to count as grounded.

    Refusal on an answer row is judged with the agent's own abstention
    predicate (`is_abstention`, #135/#305/#630): a marker-bearing answer with
    any non-refusal clause is a grounded partial answer, not a refusal. Marker presence alone (the old verdict) failed
    substantive answers that scope what the excerpts do not cover — the
    #305 gap report measured that artifact as a refusal share. True refusals
    (every clause refuses) still FAIL. On a trap row the
    marker test stays (any decline phrase counts as declining; a grounded
    decline warns), because there a long answer means the trap was answered.

    judge_gold=False suppresses the gold-substring and must_cite_identifier
    checks: they judge MODEL phrasing, and the agent's fixed zero-hits
    message contains no model text (citing a canned string can never teach
    us anything about the model). Structural grounding verdicts always run.

    Verification-state check (issue #365, opt-in): entries carrying
    `expected_verification_state` fail when the served state differs. Golden
    files predate the field, so it never fires unless an entry opts in —
    no baseline churn from the new contract."""
    failures: list[str] = []
    warns: list[str] = []
    body = answer.strip()
    refuses = is_refusal(body)
    abstained = is_abstention(body)
    grounded = bool(citations)

    if entry["expected_behavior"] == "answer":
        if not has_answer_body(body, citations):
            failures.append("empty answer body")
        elif abstained:
            failures.append("explicit refusal on an answer-tier query")
        elif not grounded:
            failures.append("zero validated citations")
        elif citations_inferred:
            failures.append("only inferred citations (no explicit Citations: block)")
    else:  # abstain
        if grounded and not refuses:
            failures.append(f"trap answered: {len(citations)} validated citation(s)")
        elif grounded and refuses:
            warns.append("hedged abstention: cites excerpts but declines")
        elif not grounded and not refuses:
            warns.append("silent abstention: no citations and no explicit refusal phrase")

    expected_state = entry.get("expected_verification_state")
    if expected_state is not None and verification_state != expected_state:
        failures.append(
            f"verification state {verification_state!r} != expected {expected_state!r}"
        )

    if judge_gold:
        content = gold_text(body, script)
        failures.extend(missing_gold(entry, content))
        for s in entry.get("gold_must_not_contain") or []:
            if str(s).lower() in content.lower():
                failures.append(f"forbidden substring present: {s!r}")

        ident = entry.get("must_cite_identifier")
        if ident:
            hay = " ".join([content, *citations]).lower()
            if str(ident).lower() not in hay:
                failures.append(f"identifier not cited or mentioned: {ident!r}")

    return ("fail" if failures else "pass"), failures, warns


def gold_text(body: str, script: object) -> str:
    """What the gold-string checks read: the answer body plus the extracted
    script, if any (#1285), so where a build's parser puts a code fence does
    not change the verdict."""
    if isinstance(script, str) and script.strip():
        return f"{body}\n{script.strip()}"
    return body


def missing_gold(entry: dict[str, Any], body: str) -> list[str]:
    """The required-substring failures of one answer body: case-folded
    literal checks, one per missing `gold_must_contain` string."""
    return [f"missing required substring: {s!r}"
            for s in entry.get("gold_must_contain") or [] if str(s).lower() not in body.lower()]


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


def answer_completeness(judged: list[dict[str, Any]]) -> tuple[int, float | None]:
    """Answer-completeness gate (issue #305): (eligible, rate) over judged
    answer rows whose expected docs were in the fetched pool. Complete means
    the validated citations cover every expected doc (doc-level citation
    recall 1.0) — a refusal or partial answer over retrieved gold is
    incomplete. Rows without the pool join never enter the denominator: a
    missing signal must not fabricate a miss. Pure; shared by both
    summaries so the rate can never diverge between reports."""
    gold_rows = [
        r for r in judged
        if r.get("verdict") in ("pass", "fail")
        and r.get("expected_behavior") == "answer"
        and r.get("gold_retrieved")
    ]
    if not gold_rows:
        return 0, None
    complete = sum(1 for r in gold_rows if r.get("citation_recall") == 1.0)
    return len(gold_rows), round(complete / len(gold_rows), 4)


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
        "inferred_citations": sum(1 for r in judged if r.get("citations_inferred")),
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
    # Failure histogram (issue #299): template-bucketed so the WHY report
    # reads off this metric instead of grepping row strings.
    by_failure: Counter = Counter()
    for r in judged:
        for f in r.get("failures") or []:
            by_failure[failure_bucket(f)] += 1
    metrics["by_failure"] = dict(sorted(by_failure.items()))
    # Truncation attribution (issue #298): pass counts per served query
    # complexity. Unknown covers error rows and callers without the answer
    # log join — a missing signal, never a third complexity.
    by_complexity: Counter = Counter()
    by_complexity_pass: Counter = Counter()
    for r in judged:
        complexity = r.get("query_complexity") or "unknown"
        by_complexity[complexity] += 1
        if r["verdict"] == "pass":
            by_complexity_pass[complexity] += 1
    metrics["by_complexity"] = {
        complexity: {"n": by_complexity[complexity], "pass": by_complexity_pass[complexity]}
        for complexity in sorted(by_complexity)
    }
    # Citation WHY shares (issue #299): one dominant mode per row, plus the
    # inferred-path wrong-index counter the contract addition unlocked.
    metrics["by_why"] = dict(sorted(Counter(why_mode(r) for r in judged).items()))
    metrics["inferred_index_off_gold"] = sum(1 for r in judged if inferred_index_off_gold(r))
    # Answer completeness (issue #305): the gate the refusal track parks on.
    completeness_n, completeness_rate = answer_completeness(judged)
    metrics["answer_completeness"] = completeness_rate
    metrics["answer_completeness_n"] = completeness_n
    # Refusal/unsafe split (issue #365): false refusals are answer-tier rows
    # failing as explicit refusals; unsafe answers are abstain-tier rows
    # failing as answered traps. Separate rates so one cannot hide behind
    # the other's pass rate.
    false_refusals = sum(
        1 for r in answer_rows
        if r["verdict"] == "fail"
        and "explicit refusal on an answer-tier query" in (r.get("failures") or [])
    )
    unsafe_answers = sum(
        1 for r in abstain_rows
        if r["verdict"] == "fail"
    )
    metrics["false_refusals"] = false_refusals
    metrics["false_refusal_rate"] = (
        round(false_refusals / len(answer_rows), 4) if answer_rows else None
    )
    metrics["unsafe_answers"] = unsafe_answers
    metrics["unsafe_answer_rate"] = (
        round(unsafe_answers / len(abstain_rows), 4) if abstain_rows else None
    )
    # Verification-state histogram (issue #365): what the served contract
    # reported per row. Error rows stay out of `judged`, so `unknown` means a
    # judged row whose response carried no state (pre-#365 server) — a
    # missing signal, never a fabricated state.
    by_state: Counter = Counter()
    for r in judged:
        by_state[str(r.get("verification_state") or "unknown")] += 1
    metrics["by_verification_state"] = dict(sorted(by_state.items()))
    # Acceptance-state mismatches (issue #365, opt-in): rows whose entry
    # carries `expected_verification_state` and whose served state differs.
    # Zero on corpora without the opt-in field — no baseline churn.
    metrics["state_mismatches"] = sum(
        1
        for r in judged
        if r.get("expected_verification_state") is not None
        and r.get("verification_state") != r["expected_verification_state"]
    )
    # Packing attribution (issue #368): whole atomic units dropped by
    # packing, and remote-confirmed token budgets — report-only signals for
    # the completeness/truncation tradeoff, never verdict inputs, never a
    # default flip. Missing signals (error rows, old servers) stay out of
    # the denominator.
    metrics["units_omitted_total"] = sum(
        int(r.get("units_omitted") or 0) for r in judged
    )
    signaled = [r for r in judged if r.get("budget_verified") is not None]
    metrics["budget_verified_n"] = sum(
        1 for r in signaled if r.get("budget_verified") is True
    )
    metrics["budget_verified_rate"] = (
        round(metrics["budget_verified_n"] / len(signaled), 4) if signaled else None
    )
    # Measurement protocol (issue #1195): hardware-independent and
    # hardware-dependent groups, reported apart. Report-only.
    metrics["measurement"] = measurement_summary(results)
    # Coverage of the rates above (#1216): every rate divides by judged rows
    # only, so errors and rows with no pass/fail verdict are in no denominator
    # at all. Report them next to the rates instead of hiding them in the pass
    # rate. Appended after the #1195 measurement block so the key order of the
    # existing JSON fields does not move.
    total = len(results)
    # Rows outside pass/fail/error, counted so judged + errors + unjudged == total.
    metrics["total"] = total
    metrics["unjudged"] = total - len(judged) - metrics["errors"]
    metrics["error_rate"] = round(metrics["errors"] / total, 4) if total else None
    metrics["judged_rate"] = round(len(judged) / total, 4) if total else None
    # Coding requests (issue #1276), appended last so no key order moves.
    metrics["coding"] = coding_breakdown(results)
    return metrics


def coding_breakdown(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts for the rows whose question is a coding request (issue #1276).

    The class is derived on the evaluator side from each row's question with
    the agent's own deterministic classifier, never from a dataset field, so
    the old build, the candidate and a `coding-off` arm count the same rows.
    Text-free: counts, rates with their denominators and state labels only.
    The refusal rule is summarize()'s false-refusal rule."""
    rows = [r for r in results if is_coding_request(str(r.get("query") or ""))]
    judged = [r for r in rows if r.get("verdict") in ("pass", "fail")]
    answer_rows = [r for r in judged if r.get("expected_behavior") == "answer"]
    refusals = sum(1 for r in answer_rows
                   if "explicit refusal on an answer-tier query" in (r.get("failures") or []))
    passed = sum(1 for r in judged if r["verdict"] == "pass")
    return {
        "n": len(rows),
        "judged": len(judged),
        "pass": passed,
        "answer_n": len(answer_rows),
        "false_refusals": refusals,
        "false_refusal_rate": round(refusals / len(answer_rows), 4) if answer_rows else None,
        "zero_hits": sum(1 for r in judged if r.get("path") == "zero_hits"),
        "by_verification_state": dict(sorted(Counter(
            str(r.get("verification_state") or "unknown") for r in judged).items())),
    }


def _mean(values: list[Any]) -> float | None:
    known = [v for v in values if _number_or_none(v) is not None]
    return round(sum(known) / len(known), 4) if known else None


def _turn_block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Follow-up metrics for one turn (issue #1268), evidence first. Every
    rate states its denominator; a row without the signal stays out of it."""
    judged = [r for r in rows if r.get("verdict") in ("pass", "fail")]
    known = [r for r in judged if type(r.get("evidence_gold_hit")) is bool]
    hit = sum(1 for r in known if r["evidence_gold_hit"])
    recall = [r.get("evidence_citation_recall") for r in judged
              if r.get("evidence_citation_recall") is not None]
    # Precision has no value on a row without citations: its own denominator.
    precision = [r.get("evidence_citation_precision") for r in judged
                 if r.get("evidence_citation_precision") is not None]
    labels: Counter = Counter()
    for r in rows:
        labels.update(export_failure_label(str(f)) for f in r.get("failures") or [])
    passed = sum(1 for r in judged if r["verdict"] == "pass")
    return {
        "n": len(rows), "judged": len(judged),
        "errors": sum(1 for r in rows if r.get("verdict") == "error"),
        "evidence_n": len(known), "evidence_gold_hit": hit,
        "evidence_gold_hit_rate": round(hit / len(known), 4) if known else None,
        "zero_hits": sum(1 for r in judged if r.get("path") == "zero_hits"),
        # Same rule and name as summarize()'s false_refusals.
        "false_refusals": sum(1 for r in judged if "explicit refusal on an answer-tier query"
                              in (r.get("failures") or [])),
        "by_verification_state": dict(sorted(Counter(
            str(r.get("verification_state") or "unknown") for r in judged).items())),
        "citation_n": len(recall),
        "citation_precision_n": len(precision),
        "citation_precision_mean": _mean(precision),
        "citation_recall_mean": _mean(recall),
        "pass": passed,
        "pass_rate": round(passed / len(judged), 4) if judged else None,
        "by_failure": dict(sorted(labels.items())),
    }


def turn_breakdown(results: list[dict[str, Any]], repeats: int,
                   cut: int | None = None) -> dict[str, Any]:
    """Metrics by turn over the follow-up sessions (issue #1268). Turn 1 is
    the openers of the rows that got a follow-up, so both turns cover the
    same sessions; `vs_opener` pairs each follow-up with its own opener by
    (id, repeat) on the evidence gold hit. Pure."""
    def key(row: dict[str, Any]) -> tuple[str, int]:
        return str(row.get("id")), int(row.get("repeat") or 1)

    follow = [r for r in results if r.get("turn") == 2]
    sessions = {key(r) for r in follow}
    openers = [r for r in results if r.get("turn") == 1 and key(r) in sessions]
    first, second = _turn_block(openers), _turn_block(follow)
    chars = sorted(r["prior_answer_chars"] for r in follow
                   if is_count(r.get("prior_answer_chars")))
    if cut is None:
        cut = Settings.model_fields["chat_max_prior_turn_chars"].default
    second["prior_answer_chars"] = {
        "n": len(chars), "p50": chars[len(chars) // 2] if chars else None,
        "max": chars[-1] if chars else None, "cut_chars": cut,
        "over_cut": sum(1 for c in chars if c > cut),
    }
    opener_hit = {key(r): r.get("evidence_gold_hit") for r in openers}
    pairs: Counter = Counter()
    for r in follow:
        before, after = opener_hit.get(key(r)), r.get("evidence_gold_hit")
        if type(before) is not bool or type(after) is not bool:
            pairs["unknown"] += 1
        else:
            pairs[{(True, True): "kept", (True, False): "lost",
                   (False, True): "gained", (False, False): "neither"}[(before, after)]] += 1
    second["vs_opener"] = {name: pairs.get(name, 0)
                           for name in ("kept", "lost", "gained", "neither", "unknown")}
    if repeats > 1:
        for block, rows in ((first, openers), (second, follow)):
            per = [_turn_block([r for r in rows if int(r.get("repeat") or 1) == n])
                   for n in range(1, repeats + 1)]
            block["repeats"] = {
                name: value_spread([p[name] for p in per])
                for name in ("evidence_gold_hit_rate", "pass_rate", "citation_recall_mean")
            }
    return {"1": first, "2": second}


def _pair_state(before: Any, after: Any) -> str:
    if type(before) is not bool or type(after) is not bool:
        return "unknown"
    return {(True, True): "kept", (True, False): "lost",
            (False, True): "gained", (False, False): "neither"}[(before, after)]


_PAIR_STATES = ("kept", "lost", "gained", "neither", "unknown")


def switch_deltas(results: list[dict[str, Any]], switched: list[dict[str, Any]]) -> dict[str, Any]:
    """Each topic-switch follow-up against the same question asked as a
    turn-1 opener in the same repeat (issue #1268): pass, evidence gold hit
    and verification state, counted per pair. Pure."""
    single = {(str(r.get("id")), int(r.get("repeat") or 1)): r
              for r in results if r.get("turn") == 1}
    passes: Counter = Counter()
    evidence: Counter = Counter()
    states: Counter = Counter()
    moves: Counter = Counter()

    def verdict(row: dict[str, Any] | None) -> bool | None:
        if row is None or row.get("verdict") not in ("pass", "fail"):
            return None
        return bool(row["verdict"] == "pass")

    for r in switched:
        alone = single.get((str(r.get("switch_id")), int(r.get("repeat") or 1)))
        passes[_pair_state(verdict(alone), verdict(r))] += 1
        evidence[_pair_state((alone or {}).get("evidence_gold_hit"), r.get("evidence_gold_hit"))] += 1
        if verdict(alone) is None or verdict(r) is None:
            states["unknown"] += 1
            continue
        before = _state_or_none((alone or {}).get("verification_state")) or "unknown"
        after = _state_or_none(r.get("verification_state")) or "unknown"
        states["same" if before == after else "changed"] += 1
        if before != after:
            moves[f"{before}>{after}"] += 1
    return {
        "n": len(switched),
        "pass": {name: passes.get(name, 0) for name in _PAIR_STATES},
        "evidence_gold_hit": {name: evidence.get(name, 0) for name in _PAIR_STATES},
        "verification_state": {name: states.get(name, 0) for name in ("same", "changed", "unknown")},
        "verification_moves": dict(sorted(moves.items())),
    }


def exposure_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Shares of follow-ups whose history answer exceeds the declared cap,
    and where the appended citation list lies against it. Pure."""
    from mainframe_rag.eval.follow_up import CITATION_LIST_STATES

    known = [r for r in rows if type(r.get("history_over_cap")) is bool]
    over = sum(1 for r in known if r["history_over_cap"])
    lists = Counter(str(r.get("citation_list_cut")) for r in known)
    cut = lists.get("partial", 0) + lists.get("cut", 0)
    return {
        "n": len(known), "over_cap": over,
        "over_cap_share": round(over / len(known), 4) if known else None,
        "citation_list": {name: lists.get(name, 0) for name in sorted(CITATION_LIST_STATES)},
        "citation_list_cut_share": round(cut / len(known), 4) if known else None,
    }


def restate_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Class-B follow-ups (issue #1268): declines, and the opener's gold
    strings by where they lie against the declared cut, crossed with whether
    the follow-up's packed evidence holds an opener gold doc. The evidence
    side is a doc-level proxy: `supplied_evidence` carries doc ids, not
    excerpt text, so it says the doc was there, not the string. Pure."""
    from mainframe_rag.eval.follow_up import GOLD_POSITIONS

    judged = [r for r in rows if r.get("verdict") in ("pass", "fail")]
    split = {position: {side: {"n": 0, "found": 0}
                        for side in ("in_evidence", "not_in_evidence", "unknown")}
             for position in GOLD_POSITIONS}
    for r in judged:
        hit = r.get("evidence_gold_hit")
        side = "unknown" if type(hit) is not bool else "in_evidence" if hit else "not_in_evidence"
        for position, counts in (r.get("gold_split") or {}).items():
            if position in split:
                split[position][side]["n"] += _count_or_none(counts.get("n")) or 0
                split[position][side]["found"] += _count_or_none(counts.get("found")) or 0
    declines = sum(1 for r in judged if r.get("restate_declined") is True)
    return {
        "n": len(judged),
        "declines": declines,
        "decline_rate": round(declines / len(judged), 4) if judged else None,
        "gold_n": sum(_count_or_none(r.get("gold_n")) or 0 for r in judged),
        "gold_found_n": sum(_count_or_none(r.get("gold_found_n")) or 0 for r in judged),
        "gold_split": split,
    }


def _timing(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-arm cost of one turn and kind. The chat stream carries no
    server ttft and no usage: those stay None."""
    return {
        "elapsed_ms": distribution([r.get("elapsed_ms") for r in rows
                                    if r.get("verdict") in ("pass", "fail")]),
        "client_first_token_ms": distribution([r.get("client_first_token_ms") for r in rows
                                               if r.get("verdict") in ("pass", "fail")]),
        "ttft_ms": None, "total_tokens": None,
    }


def kind_breakdown(results: list[dict[str, Any]], repeats: int, plan: FollowUpPlan
                   ) -> dict[str, Any]:
    """`metrics.by_turn` under `--follow-up` (issue #1268): turn x kind.
    Kinds are never pooled (their verdicts mean different things). Turn 1
    of a kind is the openers that got that kind; `vs_opener` is dropped for
    `switch`, whose question and gold differ from its opener's, and replaced
    by `vs_single`, the same question asked as an opener. Pure."""
    from mainframe_rag.eval.follow_up import RESTATE_FOLLOW_UPS

    first: dict[str, Any] = {}
    second: dict[str, Any] = {}
    openers = [r for r in results if r.get("turn") == 1]
    for kind in plan.kinds:
        follow = [r for r in results if r.get("turn") == 2 and r.get("follow_up_kind") == kind]
        block = turn_breakdown([*openers, *follow], repeats, plan.prior_cap)
        sessions = {(str(r.get("id")), int(r.get("repeat") or 1)) for r in follow}
        block["1"]["timing"] = _timing([r for r in openers if (str(r.get("id")),
                                        int(r.get("repeat") or 1)) in sessions])
        block["2"]["timing"] = _timing(follow)
        block["2"]["exposure"] = exposure_summary(follow)
        block["2"]["skipped"] = dict(sorted(plan.skipped[kind].items()))
        if kind == "switch":
            block["2"].pop("vs_opener")
            block["2"]["vs_single"] = switch_deltas(results, follow)
        if kind in RESTATE_FOLLOW_UPS:
            block["2"]["restate"] = restate_summary(follow)
        first[kind], second[kind] = block["1"], block["2"]
    return {"1": first, "2": second}


def error_code(resp: Any) -> str:
    """Stable error code of a non-200 /v1/answer response (issue #307).

    The agent's fixed envelope is ``{"code", "message"}`` at the top level
    (``ErrorEnvelope``); the eval used to read a nested ``error.code`` that
    never exists, so every non-200 row recorded the code as ``?`` and a
    ``prompt_budget_exceeded`` packing loss was indistinguishable from a
    validation error. The nested shape stays accepted for old servers. Only
    the code is read: bodies and messages never reach the report."""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return "?"
    if not isinstance(body, dict):
        return "?"
    code = body.get("code")
    if not isinstance(code, str):
        nested = body.get("error")
        code = nested.get("code") if isinstance(nested, dict) else None
    return code if isinstance(code, str) and code else "?"


def apply_pool_join(
    row: dict[str, Any], entry: dict[str, Any], hits: list[dict[str, Any]]
) -> None:
    """Attach the doc-level citation recall to an answer row from the sibling
    /v1/search pool (issue #307), so `answer_completeness` is computed by the
    answer eval and not only by harness L2. Observational only: unlike L2 an
    unmapped citation never fails the row here (the verdict stays the
    structural judge's) and no gate reads these fields. Rows without gold or
    a judged verdict are left untouched; a missing pool never fabricates a
    miss (the field stays absent and the row stays out of the denominator)."""
    from mainframe_rag.eval.judging import cited_doc_ids, precision_recall

    gold = {str(d) for d in (entry.get("expected_doc_ids") or [])}
    if row.get("verdict") not in ("pass", "fail") or entry.get("expected_behavior") != "answer" or not gold:
        return
    cited, unmatched = cited_doc_ids(row.get("citations") or [], hits)
    precision, recall = precision_recall(cited, gold)
    row["cited_doc_ids"] = sorted(cited)
    row["unmatched_citations"] = unmatched
    row["citation_precision"] = precision
    row["citation_recall"] = recall


def apply_evidence(
    row: dict[str, Any], entry: dict[str, Any], supplied: list[dict[str, Any]]
) -> None:
    """Evidence fields from what the agent packed for this turn (issue
    #1268): the terminal chunk's `supplied_evidence`, already validated by
    `chat_answer_body`. Not a separate search: a follow-up searched with its
    own words would hide what condensation changes. An empty list (the
    zero-hits path) is a known miss; citations map to doc ids through the
    same entries the agent validated them against."""
    from mainframe_rag.eval.judging import precision_recall

    doc_ids = list(dict.fromkeys(str(item["doc_id"]) for item in supplied))
    by_citation = {str(item["citation"]): str(item["doc_id"]) for item in supplied}
    gold = {str(d) for d in (entry.get("expected_doc_ids") or [])}
    cited = {by_citation[c] for c in row.get("citations") or [] if c in by_citation}
    precision, recall = precision_recall(cited, gold)
    row.update(
        evidence_doc_ids=doc_ids,
        evidence_gold_hit=bool(gold & set(doc_ids)) if gold else None,
        evidence_cited_doc_ids=sorted(cited),
        evidence_citation_precision=precision,
        evidence_citation_recall=recall,
    )


# Turn-2 failures before any follow-up request is made (issue #1268).
OPENER_NOT_ANSWERED = "opener not answered"
HISTORY_FAILED = "follow-up history failed"
# A class-B follow-up the agent declined (issue #1268).
RESTATE_DECLINED = "follow-up declined"
# A `--sessions` row whose history the Pipe would not send (issue #1191).
SESSION_NOT_BUILT = "session request not built"


def has_follow_up(entry: dict[str, Any]) -> bool:
    """Only answer rows with gold docs get a second turn: a trap row has no
    topic to follow, and without gold docs the evidence hit is undefined."""
    return entry.get("expected_behavior") == "answer" and bool(entry.get("expected_doc_ids"))


def follow_up_entry(
    entry: dict[str, Any], kind: str = "anaphoric", partner: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The judged entry of the second turn, by follow-up kind.

    anaphoric: the opener's id, class and gold docs with the class follow-up
    as its question. The opener's gold substrings, identifier and expected
    state are written for the opener's question and are not carried: turn 2
    is judged structurally only.
    switch: the partner row itself (its question and all of its gold),
    under the opener's id: the session is the opener's.
    restate/summary: a class-B follow-up with the opener's gold docs and
    gold substrings (`judge_restate`)."""
    from mainframe_rag.eval.follow_up import RESTATE_FOLLOW_UPS, follow_up_for

    if kind == "switch":
        assert partner is not None
        return {**partner, "id": entry["id"]}
    follow = {
        "id": entry["id"], "query": follow_up_for(entry.get("query_class")),
        "query_class": entry["query_class"], "expected_behavior": "answer",
        "expected_doc_ids": list(entry.get("expected_doc_ids") or []),
        "trap_type": entry.get("trap_type"), "domain": entry.get("domain"),
    }
    if kind in RESTATE_FOLLOW_UPS:
        follow.update(query=RESTATE_FOLLOW_UPS[kind],
                      gold_must_contain=list(entry.get("gold_must_contain") or []))
    return follow


class FollowUpPlan:
    """What `--follow-up` asks after each opener (issue #1268): the kinds in
    session order, the switch partner entries by opener id, and the
    operator-declared prior-turn cap. Skips are counted per kind and reason,
    since a skipped follow-up has no row."""

    def __init__(self, kinds: tuple[str, ...], partners: dict[str, dict[str, Any]],
                 prior_cap: int) -> None:
        self.kinds = kinds
        self.partners = partners
        self.prior_cap = prior_cap
        self.skipped: dict[str, Counter] = {kind: Counter() for kind in kinds}


def _skip_reason(kind: str, entry: dict[str, Any], opener: dict[str, Any],
                 plan: FollowUpPlan) -> str | None:
    """Why this opener gets no follow-up of this kind, or None."""
    from mainframe_rag.eval.follow_up import RESTATE_FOLLOW_UPS

    if kind == "switch" and str(entry["id"]) not in plan.partners:
        return "no_partner"
    if kind in RESTATE_FOLLOW_UPS:
        # Restating needs something to restate: gold strings, found in turn 1.
        if not entry.get("gold_must_contain"):
            return "no_gold_strings"
        if opener.get("verdict") != "pass":
            return "opener_not_passed"
    return None


def judge_restate(row: dict[str, Any], entry: dict[str, Any]) -> None:
    """Class-B verdict: the opener's gold substrings in the follow-up answer
    (`missing_gold`, as `judge`), and no decline. The agent answers from
    fresh excerpts only, so an uncited restatement is the product rule, not
    a failure. A decline (the structured decline, a whole-response refusal
    or the zero-hits path) fails even when it names a gold string, and is
    counted apart. Unlike `judge`, the zero-hits path is judged too: the
    user got no restatement."""
    answer = str(row.get("answer") or "")
    missing = missing_gold(entry, gold_text(answer, row.get("script")))
    gold_n = len(entry.get("gold_must_contain") or [])
    declined = row.get("path") == "zero_hits" or is_abstention(answer, row.get("citations") or [])
    row.update(
        verdict="fail" if missing or declined else "pass",
        failures=[*missing, *([RESTATE_DECLINED] if declined else [])],
        warns=[w for w in row.get("warns") or [] if not str(w).startswith("zero-hits path")],
        # A decline restates nothing, even when it names a gold string.
        restate_declined=declined, gold_n=gold_n,
        gold_found_n=0 if declined else gold_n - len(missing),
    )


def _annotate_history(row: dict[str, Any], entry: dict[str, Any], kind: str,
                      request: dict[str, Any], cap: int) -> None:
    """Exposure of the history as sent, and for class B where each opener
    gold string lies against the cut and whether this answer has it."""
    from mainframe_rag.eval.follow_up import (
        GOLD_POSITIONS,
        RESTATE_FOLLOW_UPS,
        gold_positions,
        history_answer,
        history_exposure,
        history_view,
    )

    view = history_view(history_answer(request))
    over, citation_list = history_exposure(view, cap)
    row.update(history_over_cap=over, citation_list_cut=citation_list)
    if kind in RESTATE_FOLLOW_UPS and row.get("verdict") in ("pass", "fail"):
        gold = [str(s) for s in entry.get("gold_must_contain") or []]
        answer = str(row.get("answer") or "").lower()
        split = {name: {"n": 0, "found": 0} for name in GOLD_POSITIONS}
        for s, position in zip(gold, gold_positions(view, gold, cap), strict=True):
            split[position]["n"] += 1
            split[position]["found"] += int(s.lower() in answer and not row.get("restate_declined"))
        row["gold_split"] = split


def run_follow_ups(
    client: Any, entry: dict[str, Any], opener: dict[str, Any], frames: list[str],
    pipe_mod: Any, temperature: float | None, plan: FollowUpPlan | None = None,
    keep_evidence_text: bool = False,
) -> list[dict[str, Any]]:
    """Turn 2 of one session, once per follow-up kind: each with the history
    the Open WebUI Pipe would send after the opener (`eval.follow_up`). All
    kinds branch off the same opener and kept answer. Without a plan (no
    `--follow-up`): the anaphoric follow-up alone, rows as before."""
    from mainframe_rag.eval.follow_up import (
        RESTATE_FOLLOW_UPS,
        kept_answer,
        prior_answer_chars,
        second_turn_request,
    )

    jobs: list[tuple[str, dict[str, Any]]] = []
    for kind in (("anaphoric",) if plan is None else plan.kinds):
        reason = None if plan is None else _skip_reason(kind, entry, opener, plan)
        if reason is not None:
            assert plan is not None
            plan.skipped[kind][reason] += 1
            continue
        partner = None if plan is None else plan.partners.get(str(entry["id"]))
        jobs.append((kind, follow_up_entry(entry, kind, partner)))
    rows: list[dict[str, Any]] = []
    answered = opener.get("verdict") in ("pass", "fail")
    kept: str | None = None
    if answered and jobs:
        try:
            kept = kept_answer(pipe_mod, entry["query"], frames)
        except Exception:  # noqa: BLE001 — the Pipe is a boundary; a fixed label only
            kept = None
    for kind, follow in jobs:
        request = None
        if kept is not None:
            try:
                request = second_turn_request(pipe_mod, entry["query"], kept, follow["query"])
            except Exception:  # noqa: BLE001 — as above
                request = None
        if not answered or request is None:
            # No follow-up call: the opener errored, or the Pipe would make none.
            row = _base_row(follow, temperature)
            row.update(verdict="error", failures=[OPENER_NOT_ANSWERED if not answered
                                                  else HISTORY_FAILED],
                       elapsed_ms=0, turn=2)
        else:
            row = run_query(client, follow, None, None, temperature=temperature, stream=True,
                            endpoint="chat", chat_body=request, evidence=True,
                            keep_evidence_text=keep_evidence_text)
            row.update(turn=2, prior_answer_chars=prior_answer_chars(request))
            if plan is not None and kind in RESTATE_FOLLOW_UPS and row.get("verdict") in ("pass", "fail"):
                judge_restate(row, follow)
        if plan is not None:
            row.update(follow_up_kind=kind,
                       switch_id=(plan.partners[str(entry["id"])]["id"] if kind == "switch"
                                  else None))
            if request is not None:
                _annotate_history(row, follow, kind, request, plan.prior_cap)
        rows.append(row)
    return rows


def run_session(client: Any, entry: dict[str, Any], pipe_mod: Any,
                temperature: float | None, keep_evidence_text: bool = False) -> dict[str, Any]:
    """One fixed-history session (`--sessions`, issue #1191): its last user
    turn asked over `/v1/chat` with the whole authored history as the Open
    WebUI Pipe would send it, judged like a golden row, with the evidence
    fields read off the agent's packed list. No call when the Pipe would
    make none (a fixed error label)."""
    from mainframe_rag.eval.follow_up import history_request, prior_answer_chars

    turns = entry["turns"]
    try:
        request = history_request(pipe_mod, turns)
    except Exception:  # noqa: BLE001 — the Pipe is a boundary; a fixed label only
        request = None
    if request is None:
        row = _base_row(entry, temperature)
        row.update(verdict="error", failures=[SESSION_NOT_BUILT], elapsed_ms=0,
                   session_turns=len(turns), prior_answer_chars=None)
        return row
    row = run_query(client, entry, None, None, temperature=temperature, stream=True,
                    endpoint="chat", chat_body=request, evidence=True,
                    keep_evidence_text=keep_evidence_text)
    row.update(session_turns=len(turns), prior_answer_chars=prior_answer_chars(request))
    return row


def _valid_response_hits(raw: Any) -> list[dict[str, Any]] | None:
    """Non-empty `hits` list from a response payload (SSE `final` or `/v1/chat`
    finish chunk) carrying real hit shape (`doc_id` and `cite`), including any
    appended `NeighbourHit`s (#1314). None when absent, empty, or malformed."""
    if not isinstance(raw, list) or not raw:
        return None
    if any(
        not isinstance(h, dict)
        or not isinstance(h.get("doc_id"), str)
        or not isinstance(h.get("cite"), str)
        for h in raw
    ):
        return None
    return raw


def _valid_supplied_evidence(raw: Any) -> list[dict[str, Any]] | None:
    """Non-empty `supplied_evidence` list from an answer response (JSON or SSE),
    carrying valid `index`, `doc_id`, `citation`, and `text`. None when absent,
    empty, or malformed."""
    if not isinstance(raw, list) or not raw:
        return None
    if any(
        not isinstance(item, dict)
        or type(item.get("index")) is not int
        or not isinstance(item.get("doc_id"), str)
        or not isinstance(item.get("citation"), str)
        or not isinstance(item.get("text"), str)
        for item in raw
    ):
        return None
    return raw


def resolve_retrieval_hits(
    data: dict[str, Any], pool: list[dict[str, Any]] | None
) -> list[dict[str, Any]] | None:
    """Hit list behind `hit_doc_ids` and `gold_retrieved` (#1314).

    Streamed `/v1/answer` and `/v1/chat` carry `hits` (every retrieved hit plus
    appended `NeighbourHit`s). Buffered `/v1/answer` carries only
    `supplied_evidence`: use it when there is no sibling `/v1/search` pool,
    when more excerpts were packed than `/v1/search` returned (neighbours
    survived packing), or when a packed citation is absent from `pool`
    (cross-page neighbour or selected hit). Otherwise keep `pool` so
    `expand-off` with tail omission keeps all retrieved hits byte-identically.
    """
    resp_hits = _valid_response_hits(data.get("hits"))
    if resp_hits is not None:
        return resp_hits
    supplied = _valid_supplied_evidence(data.get("supplied_evidence"))
    if supplied is not None:
        if pool is None or len(supplied) > len(pool):
            return supplied
        pool_cites = {h.get("cite") for h in pool if isinstance(h.get("cite"), str)}
        if pool_cites and any(item["citation"] not in pool_cites for item in supplied):
            return supplied
    return pool


def resolve_citation_hits(
    data: dict[str, Any], pool: list[dict[str, Any]] | None
) -> list[dict[str, Any]] | None:
    """Hit/excerpt list behind citation P/R and judge evidence (#1314).

    Validated citations come exclusively from the final packed evidence
    manifest (`supplied_evidence`, issue #364), so prefer valid
    `supplied_evidence` first (omitted tail hits never leak to the judge, and
    both JSON and SSE surfaces carry packed neighbours with explicit prompt
    indices), then streamed `hits`, then the sibling `/v1/search` pool.
    """
    supplied = _valid_supplied_evidence(data.get("supplied_evidence"))
    if supplied is not None:
        return supplied
    resp_hits = _valid_response_hits(data.get("hits"))
    if resp_hits is not None:
        return resp_hits
    return pool


def _fetch_pool(client: Any, query: str, limit: int = 8) -> list[dict[str, Any]] | None:
    """Sibling /v1/search pool (the same call the answer retrieval makes, at
    the deployed `answer_hit_limit`, the agent default unless `--hit-limit` declares another,
    issue #1191). Used as fallback when a response carries no `hits` or
    `supplied_evidence`, for `--image-arm`, and on buffered JSON `expand-off`
    runs with tail omission (#1314). None on any failure: a missing pool is a
    missing signal, not an empty pool."""
    try:
        resp = client.post("/v1/search", json={"query": query, "limit": limit})
        if resp.status_code != 200:
            return None
        body = resp.json()
        hits = body.get("hits") if isinstance(body, dict) else None
        if not isinstance(hits, list) or any(not isinstance(hit, dict) for hit in hits):
            return None
        return hits
    except Exception:  # noqa: BLE001
        return None


def chat_answer_body(content: str, terminal: dict[str, Any]) -> str | None:
    """The judged answer body of a chat stream (see `chat_answer_parts`)."""
    parts = chat_answer_parts(content, terminal)
    return None if parts is None else parts[0]


def chat_answer_parts(content: str, terminal: dict[str, Any]) -> tuple[str, str | None] | None:
    """The answer body of one `/v1/chat` stream (issue #1191), or None when
    the terminal chunk's evidence fields are malformed.

    The chat stream carries the model's raw output as deltas, then the
    validated `**Citations:**` list as one more delta; it has no parsed
    answer field. The body `/v1/answer` returns is `parse_answer` over that
    raw output with the prompt's supplied evidence, and the terminal chunk
    carries that evidence (`supplied_evidence`: prompt label, citation, doc
    id). So the body here is the agent's own parser over the same inputs:
    one rule, not a second citation regex. Citations, provenance and the
    verification state still come from the terminal chunk, as the server
    computed them. Without supplied evidence (the zero-hits answer) the
    content is the agent's canned text and is used as it is."""
    from mainframe_rag.agent.answer import EvidenceEntry, PromptEvidence, parse_answer

    citations, supplied = terminal.get("citations"), terminal.get("supplied_evidence")
    if (
        not isinstance(citations, list) or any(not isinstance(c, str) for c in citations)
        or not isinstance(supplied, list)
    ):
        return None
    entries = []
    for item in supplied:
        index = item.get("index") if isinstance(item, dict) else None
        cite = item.get("citation") if isinstance(item, dict) else None
        doc_id = item.get("doc_id") if isinstance(item, dict) else None
        if type(index) is not int or not isinstance(cite, str) or not isinstance(doc_id, str):
            return None
        entries.append(EvidenceEntry(
            prompt_index=index, chunk_id="", doc_id=doc_id, cite=cite, truncated=False,
            included_chars=0, source_chars=0, est_tokens=0,
        ))
    if citations:
        listed = "\n\n**Citations:**\n" + "\n".join(f"- {c}" for c in citations)
        content = content.removesuffix(listed)
    if not entries:
        script = terminal.get("script")
        return content, script if isinstance(script, str) else None
    parsed = parse_answer(content, PromptEvidence(entries=tuple(entries)))
    return parsed.answer, parsed.script


def _chat_answer_data(
    terminal: dict[str, Any], answer: str, script: str | None = None
) -> dict[str, Any]:
    """The answer-shaped object `_judge_response` reads, from the chat
    terminal chunk. Fields the chunk lacks stay absent, never invented.
    `answer` and `script` are the evaluator's own parse of the streamed
    text (`chat_answer_parts`), so both builds are judged by one parser."""
    chunk_id = terminal.get("chunk_id")
    data: dict[str, Any] = {
        key: terminal[key]
        for key in ("citations", "citations_inferred", "inferred_indices",
                    "verification_state", "script", "script_review_required",
                    # Read by `capture.evidence_capture` only (issue #1191).
                    "supplied_evidence", "evidence_omitted", "hits")
        if key in terminal
    }
    data["answer"] = answer
    data["script"] = script
    if isinstance(chunk_id, str):
        data["request_id"] = chunk_id.removeprefix("chatcmpl-")
    return data


def run_query(
    client: Any,
    entry: dict[str, Any],
    answer_signals: dict[str, dict[str, Any]] | None = None,
    hits: list[dict[str, Any]] | None = None,
    temperature: float | None = 0.0,
    stream: bool = False,
    endpoint: str = "answer",
    chat_body: dict[str, Any] | None = None,
    frames: list[str] | None = None,
    evidence: bool = False,
    keep_evidence_text: bool = False,
    prefer_response_hits: bool | None = None,
    citation_hits_out: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One live /v1/answer call + verdict. Records everything the report and
    the manifest need, including the failure detail (the answer body is the
    model's own output — kept in the JSON report for debugging, never
    logged).

    Temperature pins deterministic sampling (issue #596): 0.0 by default so
    comparisons are stable; pass the production setting explicitly to measure
    it, or None for the server default. No retries — a failed request is an
    error row, never retried (deliberate non-feature).

    stream=True (issue #1195) sends the same request with `stream: true` and
    judges the SSE `final`, which carries the same answer fields plus the
    finish reason, token usage and `ttft_ms`. Token deltas are not kept.

    endpoint="chat" (issue #1191) sends the question as a single-turn
    streaming `/v1/chat` request, as the Open WebUI Pipe does, and judges the
    terminal chunk plus the body `chat_answer_body` derives. That stream has
    no `usage` and no `ttft_ms`: those row fields stay None (unknown).

    Two-turn mode (issue #1268): `chat_body` replaces the single-turn chat
    messages (the Pipe's follow-up request; `temperature` is still added),
    `frames` receives the stream's `data:` lines for the Pipe replay, and
    `evidence` adds the `evidence_*` fields read off the terminal chunk's
    `supplied_evidence`.

    Every judged row, on every transport, keeps the packed evidence and the
    final hits as text-free metadata (`eval.capture`, issue #1191);
    `keep_evidence_text` adds the excerpt text for the private report."""
    if citation_hits_out is not None:
        citation_hits_out.clear()
    t0 = time.monotonic()
    row = _base_row(entry, temperature)
    body: dict[str, Any] = {"query": entry["query"], "temperature": temperature}
    measured: dict[str, Any] = {}
    outcome = None
    try:
        if endpoint == "chat":
            request = (dict(chat_body) if chat_body is not None else {
                "messages": [{"role": "user", "content": entry["query"]}], "stream": True,
            })
            outcome = read_chat_stream(client, {**request, "temperature": temperature},
                                       frames=frames)
            resp = outcome.response
        elif stream:
            outcome = read_answer_stream(client, {**body, "stream": True})
            resp = outcome.response
        else:
            resp = client.post("/v1/answer", json=body)
    except Exception as exc:  # noqa: BLE001 — one bad request must not kill the run
        row.update(verdict="error", failures=[f"request error: {type(exc).__name__}"], elapsed_ms=int((time.monotonic() - t0) * 1000))
        return row
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    if outcome is not None and outcome.failure is not None:
        # Fixed labels only: an `error` event, a stream that ended without
        # `final`, or a `final` without valid counts is never a judged answer.
        row.update(verdict="error", failures=[outcome.failure], elapsed_ms=elapsed_ms)
        return row
    if endpoint == "chat" and outcome is not None and outcome.final is not None:
        parts = chat_answer_parts(outcome.content or "", outcome.final)
        if parts is None:
            row.update(verdict="error", failures=[STREAM_MALFORMED], elapsed_ms=elapsed_ms)
            return row
        answer, script = parts
        measured = {"finish_reason": outcome.final["finish_reason"],
                    "client_first_token_ms": outcome.first_token_ms}
        row = _judge_response(
            row, entry, _chat_answer_data(outcome.final, answer, script), elapsed_ms,
            answer_signals, hits, measured, keep_evidence_text,
            prefer_response_hits=prefer_response_hits,
            citation_hits_out=citation_hits_out,
        )
        if evidence and row.get("verdict") in ("pass", "fail"):
            apply_evidence(row, entry, outcome.final["supplied_evidence"])
        return row
    if outcome is not None and outcome.final is not None:
        measured = stream_signals(outcome.final)
        measured["client_first_token_ms"] = outcome.first_token_ms
        if outcome.first_token_ms is not None:
            measured["decode_tps"] = decode_tps(
                measured["completion_tokens"], measured["reasoning_tokens"],
                elapsed_ms - outcome.first_token_ms,
            )
        return _judge_response(
            row, entry, outcome.final, elapsed_ms, answer_signals, hits, measured,
            keep_evidence_text,
            prefer_response_hits=prefer_response_hits,
            citation_hits_out=citation_hits_out,
        )
    return _judge_http_response(
        row, entry, resp, elapsed_ms, answer_signals, hits, keep_evidence_text,
        prefer_response_hits=prefer_response_hits,
        citation_hits_out=citation_hits_out,
    )


def _base_row(entry: dict[str, Any], temperature: float | None) -> dict[str, Any]:
    """The request-independent row fields every outcome carries."""
    return {
        "id": entry["id"],
        "query": entry["query"],
        "query_class": entry["query_class"],
        "expected_behavior": entry["expected_behavior"],
        # The sampling temperature this row ran under (issue #596): report
        # and manifest readers need it to interpret before/after deltas.
        "temperature": temperature,
        # Opt-in acceptance state (issue #365): None on pre-state entries, so
        # the report can count mismatches without guessing from failures.
        "expected_verification_state": entry.get("expected_verification_state"),
        "trap_type": entry.get("trap_type"),
        "domain": entry.get("domain"),
    }


def _judge_http_response(
    row: dict[str, Any],
    entry: dict[str, Any],
    resp: Any,
    elapsed_ms: int,
    answer_signals: dict[str, dict[str, Any]] | None,
    hits: list[dict[str, Any]] | None,
    keep_evidence_text: bool = False,
    prefer_response_hits: bool | None = None,
    citation_hits_out: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A buffered /v1/answer response, or a non-200 stream response."""
    if resp.status_code != 200:
        # The error envelope is the agent's fixed client contract; str(exc)
        # and upstream bodies never reach the client, so do not expect them.
        code = error_code(resp)
        row.update(verdict="error", failures=[f"HTTP {resp.status_code} ({code})"], elapsed_ms=elapsed_ms)
        return row
    try:
        data = resp.json()
    except ValueError:
        row.update(verdict="error", failures=["malformed answer response"], elapsed_ms=elapsed_ms)
        return row
    if not isinstance(data, dict):
        row.update(verdict="error", failures=["malformed answer response"], elapsed_ms=elapsed_ms)
        return row
    # JSON path: the LLM-leg timings ride `Server-Timing` (`ttft` only when the
    # server measured it, i.e. with LLM_STREAM). Absent stays absent.
    headers = getattr(resp, "headers", None)
    measured = parse_server_timing(headers.get("Server-Timing") if headers is not None else None)
    return _judge_response(
        row, entry, data, elapsed_ms, answer_signals, hits, measured,
        keep_evidence_text,
        prefer_response_hits=prefer_response_hits,
        citation_hits_out=citation_hits_out,
    )


def _judge_response(
    row: dict[str, Any],
    entry: dict[str, Any],
    data: dict[str, Any],
    elapsed_ms: int,
    answer_signals: dict[str, dict[str, Any]] | None,
    hits: list[dict[str, Any]] | None,
    measured: dict[str, Any],
    keep_evidence_text: bool = False,
    prefer_response_hits: bool | None = None,
    citation_hits_out: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate and judge one answer object (the JSON body or the SSE
    `final`: same answer fields). `measured` holds the count/timing signals
    the transport itself carried; the in-process log join wins where both
    exist, and an absent signal stays None."""
    answer_value = data.get("answer")
    citation_values = data.get("citations")
    if (
        not isinstance(answer_value, str)
        or not isinstance(citation_values, list)
        or any(not isinstance(cite, str) for cite in citation_values)
    ):
        row.update(verdict="error", failures=["malformed answer response"], elapsed_ms=elapsed_ms)
        return row
    answer = answer_value
    citations = citation_values
    citations_inferred = bool(data.get("citations_inferred", False))
    # A malformed provenance field is a contract violation, not a citation
    # signal: fail the row closed like every other bad-response path instead
    # of raising through the runner (issue #299 review).
    try:
        inferred_indices = [int(i) for i in (data.get("inferred_indices") or [])]
    except (TypeError, ValueError, OverflowError):
        row.update(
            verdict="error",
            failures=["malformed inferred_indices in response"],
            elapsed_ms=elapsed_ms,
        )
        return row
    # Verification state (issue #365): unknown values are a contract
    # violation, not a quality signal — fail closed. Missing (pre-#365
    # server) reads as None: the row is judged without the state check,
    # never assumed accepted.
    verification_state = data.get("verification_state")
    if verification_state is not None and (
        not isinstance(verification_state, str) or verification_state not in VERIFICATION_STATES
    ):
        row.update(
            verdict="error",
            failures=["unknown verification_state in response"],
            elapsed_ms=elapsed_ms,
        )
        return row
    script_review_required = bool(data.get("script_review_required", False))
    zero_hits = is_zero_hits_answer(answer)
    # Zero-hits is the agent's canned message (no model text): gold-substring
    # checks would judge the canned string, not the model — suppress them and
    # record why. The structural verdict still fires (refusal on an answer
    # entry remains a FAIL: that is the retrieval gap showing through).
    verdict, failures, warns = judge(
        entry,
        answer,
        citations,
        judge_gold=not zero_hits,
        citations_inferred=citations_inferred,
        verification_state=verification_state,
        script=data.get("script") if isinstance(data.get("script"), str) else None,
    )
    if zero_hits:
        warns.append("zero-hits path: gold substrings not judged (canned agent message)")
    signals = (answer_signals or {}).get(str(data.get("request_id") or ""), {})

    def signal(key: str) -> Any:
        value = signals.get(key)
        return measured.get(key) if value is None else value

    llm_ms, ttft_ms = measured.get("llm_ms"), measured.get("ttft_ms")
    rate = measured.get("decode_tps")
    if rate is None and type(llm_ms) is int and type(ttft_ms) is int:
        # Buffered path with LLM_STREAM: both legs are server-measured.
        rate = decode_tps(signal("completion_tokens"), signal("reasoning_tokens"), llm_ms - ttft_ms)
    # A model call always has a positive prompt count. The agent turns a
    # missing upstream usage block into zeros, so an all-zero usage on the
    # model path means "not reported": unknown, never a known zero (#1195
    # review N1). The zero-hits path has no model call and keeps its zeros.
    prompt_tokens = signal("prompt_tokens")
    completion_tokens = signal("completion_tokens")
    reasoning_tokens = signal("reasoning_tokens")
    total_tokens = signal("total_tokens")
    if not zero_hits and is_count(prompt_tokens) and prompt_tokens == 0:
        # A positive prompt count with zero completion is a real upstream
        # count for a finished call; only the all-zero sentinel is unknown.
        prompt_tokens = completion_tokens = reasoning_tokens = total_tokens = None
        warns.append("zero prompt tokens: model usage not reported (unknown)")
    gold_ids = sorted({str(d) for d in (entry.get("expected_doc_ids") or [])})
    use_response = (hits is not None) if prefer_response_hits is None else prefer_response_hits
    retrieval_hits = resolve_retrieval_hits(data, hits) if use_response else hits
    if citation_hits_out is not None:
        resolved_citations = resolve_citation_hits(data, hits) if use_response else hits
        if resolved_citations is not None:
            citation_hits_out[:] = resolved_citations
    hit_ids = (
        None
        if retrieval_hits is None
        else [str(h.get("doc_id") or "") for h in retrieval_hits]
    )
    supplied_n, supplied_doc_n = _supplied_counts(data.get("supplied_evidence"))
    row.update(
        verdict=verdict,
        failures=failures,
        warns=warns,
        answer=answer,
        citations=citations,
        citations_inferred=citations_inferred,
        # Provenance detail (issue #299): 1-based prompt excerpt indices the
        # inferred citations came from, straight off the response contract.
        inferred_indices=inferred_indices,
        script=data.get("script"),
        # Verification state + script-review flag straight off the response
        # contract (issue #365): the row carries what the client saw.
        verification_state=verification_state,
        script_review_required=script_review_required,
        # joins server-side alert logs (finish_reason != stop) to this row —
        # the response contract deliberately does not expose finish_reason
        request_id=data.get("request_id"),
        path="zero_hits" if zero_hits else "llm",
        elapsed_ms=elapsed_ms,
        # Token-budget attribution (issue #298): present when the caller
        # joined the app's answer log line, else None (error rows, or a
        # caller without the capture handler — never fabricated).
        query_complexity=signals.get("query_complexity"),
        finish_reason=signal("finish_reason"),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        # Hardware-dependent timings (issue #1195). `ttft_ms` and `llm_ms`
        # are the server's model-leg clocks; `client_first_token_ms` is this
        # client's request start to the first SSE token (retrieval included).
        ttft_ms=ttft_ms,
        llm_ms=llm_ms,
        client_first_token_ms=measured.get("client_first_token_ms"),
        decode_tps=rate,
        # Whether the model emitted a Citations: header at all — separates
        # truncated-before-cites from chose-not-to-cite downstream. The
        # signal is parse-time (the returned body has the header stripped);
        # None when the caller has no capture handler.
        citations_header_present=signals.get("citations_header_present"),
        # WHY attribution (issue #299): gold + sibling-pool doc ids travel
        # with the row so P/R misses split into retriever blame (gold never
        # in the pool) vs reader blame (gold present, uncited) without
        # rejoining the golden set. None when the caller has no pool.
        expected_doc_ids=gold_ids,
        hit_doc_ids=hit_ids,
        gold_retrieved=(
            None
            if hit_ids is None or not gold_ids
            else bool(set(hit_ids) & set(gold_ids))
        ),
        # The server zeroes cites on abstentions via the same is_abstention
        # predicate (one helper, shared) — recomputed here so a cited-then-
        # zeroed row reads distinctly from a never-cited one.
        abstention_zeroed=bool(is_abstention(answer) and not citations),
        # Citation WHY telemetry (issue #299): parse-time attempt counters
        # joined from the answer log; None when the caller has no capture
        # handler — a missing signal, never fabricated.
        inline_bracket_present=signals.get("inline_bracket_present"),
        cites_rejected_shape_bad=signals.get("cites_rejected_shape_bad"),
        cites_rejected_unmapped=signals.get("cites_rejected_unmapped"),
        # Packing attribution (issue #368): remote-confirmed budget and
        # whole units dropped, joined from the answer log; None without the
        # capture handler. Report-only — judge() never reads these.
        budget_verified=signals.get("budget_verified"),
        units_omitted=signals.get("units_omitted"),
        # Retrieval depth (issue #1191): excerpts the agent supplied to the model
        # and their distinct manuals, off the response; counts only.
        supplied_n=supplied_n,
        supplied_doc_n=supplied_doc_n,
    )
    # Stage attribution (issue #1191): what this very answer packed and
    # retrieved, text-free unless the run asked for the excerpt text.
    # Report-only — judge() never reads these.
    from mainframe_rag.eval.capture import RowCitations, evidence_capture

    row.update(evidence_capture(
        data, keep_text=keep_evidence_text,
        citations=RowCitations(citations, citations_inferred, inferred_indices)))
    return row


def _supplied_counts(supplied: Any) -> tuple[int | None, int | None]:
    """(entries, distinct doc ids) of a response's `supplied_evidence`; both
    None when the field is missing or an entry has no doc id (unknown, never
    a count)."""
    if not isinstance(supplied, list) or any(
        not isinstance(entry, dict) or not isinstance(entry.get("doc_id"), str)
        for entry in supplied
    ):
        return None, None
    return len(supplied), len({entry["doc_id"] for entry in supplied})


_FAILURE_DETAIL_RE = re.compile(r"\([^()]*:[^()]*\)|\[[^\[\]]*\]|'[^']*'")
_FAILURE_LEAD_COUNT_RE = re.compile(r"^\d+ ")


def failure_bucket(failure: str) -> str:
    """Histogram key for a failure string (issue #299): parenthesized,
    bracketed, and quoted details are redacted and a leading count is
    normalized, so templates bucket stably ('trap answered: 3 …' and
    '…: 5 …' are one bucket; the '(no explicit Citations: block)'
    qualifier does not split the inferred bucket on its inner colon).
    Pure."""
    redacted = _FAILURE_DETAIL_RE.sub("…", failure)
    redacted = _FAILURE_LEAD_COUNT_RE.sub("N ", redacted)
    return redacted.split(":")[0].strip() or failure


def why_mode(row: dict[str, Any]) -> str:
    """Dominant citation-WHY mode for one row (issue #299), most specific
    first. Pure; shared by both summaries so the shares can never diverge.
    A truncated row that still cites (or whose Citations: header survived)
    is not 'truncated before cites' — the cut did not lose the cite block.
    A missing telemetry signal (no answer-log join) never asserts a negative:
    the row reads 'unknown', not 'truncated_before_cites' or 'absent'."""
    if row.get("verdict") == "error":
        return "error"
    if row.get("path") == "zero_hits":
        return "zero_hits"
    if row.get("abstention_zeroed"):
        return "abstention_zeroed"
    if row.get("citations"):
        return "cited_inferred" if row.get("citations_inferred") else "cited_explicit"
    header = row.get("citations_header_present")
    if row.get("truncated"):
        if header is False:
            return "truncated_before_cites"
        if header is None:
            return "unknown"
        # header True: the cut happened after the cite block; classify the
        # attempt shape below.
    if row.get("cites_rejected_unmapped"):
        return "fabricated_unmapped"
    if row.get("cites_rejected_shape_bad"):
        return "malformed_shape_bad"
    if row.get("inline_bracket_present"):
        return "bracket_unmatched"
    if (
        row.get("inline_bracket_present") is None
        or row.get("cites_rejected_shape_bad") is None
        or row.get("cites_rejected_unmapped") is None
    ):
        return "unknown"
    return "absent"


def inferred_index_off_gold(row: dict[str, Any]) -> bool:
    """True when every inferred citation's 1-based excerpt index points at a
    hit whose doc id is not in the expected set — the measurable slice of
    right-doc/wrong-index on the inferred path (issue #299). Uses the
    response's explicit prompt-index map (`supplied_evidence_meta`, #1314)
    when present, falling back to positional `hit_doc_ids` otherwise. Pure;
    False when the inputs are missing or uncoercible (never fabricated)."""
    indices = row.get("inferred_indices") or []
    expected = set(row.get("expected_doc_ids") or [])
    if not indices or not expected:
        return False
    supplied_meta = row.get("supplied_evidence_meta")
    by_index: dict[int, str] | None = None
    if isinstance(supplied_meta, list) and supplied_meta and all(
        isinstance(item, dict)
        and type(item.get("index")) is int
        and isinstance(item.get("doc_id"), str)
        for item in supplied_meta
    ):
        by_index = {item["index"]: item["doc_id"] for item in supplied_meta}
    hits = row.get("hit_doc_ids")
    if by_index is None and not hits:
        return False
    for i in indices:
        if isinstance(i, bool):
            return False
        try:
            idx = int(i)
        except (TypeError, ValueError):
            return False
        if by_index is not None:
            if by_index.get(idx) in expected:
                return False
        elif hits and 1 <= idx <= len(hits) and str(hits[idx - 1]) in expected:
            return False
    return True


# ------------------------------------------------ served coverage (#1191)
# `GET /v1/sources` lists the served build's sources (docs/agent.md section 1).
_SOURCES_PAGE_LIMIT = 200
# A guard against a cursor that never ends: 1,000 pages of 200 sources.
_SOURCES_MAX_PAGES = 1000
COVERAGE_STATES = ("all", "some", "none", "not_applicable", "unknown")


def _sources_page(client: Any, cursor: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """One validated `/v1/sources` page, or a fixed reason label. Only the
    build id, the doc ids, their titles and the cursor are read. A title is
    optional: an entry without a string title is kept by doc id alone."""
    params: dict[str, Any] = {"limit": _SOURCES_PAGE_LIMIT}
    if cursor is not None:
        params["cursor"] = cursor
    try:
        response = client.get("/v1/sources", params=params)
    except Exception:  # noqa: BLE001 — coverage is observational, never fatal
        return None, "request_failed"
    if response.status_code != 200:
        return None, f"http_{response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return None, "malformed"
    sources = body.get("sources") if isinstance(body, dict) else None
    if (
        not isinstance(body, dict) or not isinstance(body.get("build_id"), str)
        or not body["build_id"] or not isinstance(sources, list)
        or any(not isinstance(s, dict) or not isinstance(s.get("doc_id"), str) for s in sources)
        or not (body.get("next_cursor") is None or isinstance(body.get("next_cursor"), str))
    ):
        return None, "malformed"
    return {"build_id": body["build_id"], "doc_ids": [s["doc_id"] for s in sources],
            "titles": [(s["doc_id"], s["title"]) for s in sources if isinstance(s.get("title"), str)],
            "next_cursor": body.get("next_cursor")}, None


def fetch_coverage(client: Any) -> dict[str, Any]:
    """The doc ids the deployed build serves, from every `/v1/sources` page.

    `status` is "known" only after a complete walk of one build. A 503 (a
    build published without a catalog), an agent older than the endpoint, a
    transport error, a malformed page, a build change between pages or the
    page cap all give "unknown" with a fixed `reason`: coverage is never a
    failure and never touches a verdict or the exit code. `titles` holds
    (doc id, title) pairs for the corpus coverage check's matching
    (eval/coverage.py); no report carries it."""
    doc_ids: set[str] = set()
    titles: set[tuple[str, str]] = set()
    build_id: str | None = None
    cursor: str | None = None
    pages = 0
    while True:
        if pages >= _SOURCES_MAX_PAGES:
            return _coverage("unknown", "page_cap", build_id, pages)
        page, reason = _sources_page(client, cursor)
        if page is None:
            return _coverage("unknown", reason, build_id, pages)
        pages += 1
        if build_id is None:
            build_id = page["build_id"]
        elif page["build_id"] != build_id:
            return _coverage("unknown", "build_changed", build_id, pages)
        doc_ids.update(page["doc_ids"])
        titles.update(page["titles"])
        cursor = page["next_cursor"]
        if cursor is None:
            return _coverage("known", None, build_id, pages, frozenset(doc_ids), frozenset(titles))


def _coverage(status: str, reason: str | None, build_id: str | None, pages: int,
              doc_ids: frozenset[str] = frozenset(),
              titles: frozenset[tuple[str, str]] = frozenset()) -> dict[str, Any]:
    return {"status": status, "reason": reason, "build_id": build_id, "pages": pages,
            "doc_ids": doc_ids, "titles": titles}


def apply_coverage(row: dict[str, Any], entry: dict[str, Any], coverage: dict[str, Any]) -> None:
    """Mark whether the row's expected docs are served: all, some, none,
    not_applicable (the row names no expected doc) or unknown (no complete
    listing). The match is on the exact doc id, edition included, so content
    served under another edition reads as not served."""
    expected = {str(d) for d in (entry.get("expected_doc_ids") or [])}
    if not expected:
        row.update(coverage="not_applicable", expected_served=0)
        return
    if coverage["status"] != "known":
        row.update(coverage="unknown", expected_served=None)
        return
    served = len(expected & coverage["doc_ids"])
    state = "all" if served == len(expected) else "some" if served else "none"
    row.update(coverage=state, expected_served=served)


def coverage_summary(coverage: dict[str, Any], results: list[dict[str, Any]],
                     build_id_end: str | None) -> dict[str, Any]:
    """Counts per coverage state over the distinct rows (a repeated row
    counts once). The served doc-id list itself is not reported."""
    seen: dict[str, str] = {}
    for row in results:
        seen.setdefault(str(row.get("id")), str(row.get("coverage") or "unknown"))
    counts = Counter(seen.values())
    return {
        "status": coverage["status"], "reason": coverage["reason"],
        "build_id": coverage["build_id"], "build_id_end": build_id_end,
        "pages": coverage["pages"],
        "sources_n": len(coverage["doc_ids"]) if coverage["status"] == "known" else None,
        "rows": {state: counts.get(state, 0) for state in COVERAGE_STATES},
    }


def _served_build_id(client: Any) -> str | None:
    """The build id the first sources page names now (end-of-run check)."""
    page, _ = _sources_page(client, None)
    return page["build_id"] if page is not None else None


# --------------------------------------------------- text-free export (#1191)
# What may leave an air-gapped site (maintainer decision 2026-11-16): row ids,
# verdicts, verification states, fixed failure labels, token counts, timings,
# finish reasons and doc ids. Question, answer, citation, excerpt and failure
# text never do. Every value below is copied by an explicit rule; nothing is
# copied by default.
EXPORT_SCHEMA = "eval_answers_export/1"

# The agent's stable error codes (docs/agent.md section 2 and the stream
# contract). Any other code is reported as "?".
_ERROR_CODES = frozenset({
    "deadline_exceeded", "evidence_budget_exceeded", "http_error", "internal",
    "invalid_request", "method_not_allowed", "model_timeout", "not_configured", "not_found",
    "overloaded", "prompt_budget_exceeded", "qdrant_unready", "representation_unavailable",
    "unsupported_image", "unsupported_reasoning_effort", "upstream_error",
})
_FIXED_FAILURE_LABELS = frozenset({
    "empty answer body", "explicit refusal on an answer-tier query",
    "zero validated citations", "only inferred citations …", "trap answered",
    "missing required substring", "forbidden substring present",
    "identifier not cited or mentioned", "request error", "malformed answer response",
    "malformed inferred_indices in response", "unknown verification_state in response",
    STREAM_INCOMPLETE, STREAM_MALFORMED, STREAM_OVERSIZE,
    "agent health request failed", "malformed agent health response",
    "agent health is not servable",
    OPENER_NOT_ANSWERED, HISTORY_FAILED, RESTATE_DECLINED, SESSION_NOT_BUILT,
})
# OpenAI-compatible finish reasons; any other value is reported as "other".
_FINISH_REASONS = frozenset({"stop", "length", "content_filter", "tool_calls", "abort"})
_FIXED_WARN_LABELS = frozenset({
    "hedged abstention", "silent abstention", "zero-hits path", "zero prompt tokens",
})
_CODED_FAILURE_RE = re.compile(r"(HTTP \d{3}|stream error) \((.*)\)", re.DOTALL)
_STATE_FAILURE_RE = re.compile(r"verification state (?:…|None) != expected (?:…|None)")
_HEALTH_HTTP_RE = re.compile(r"agent health returned HTTP \d{3}")
_LABEL_RE = re.compile(r"[a-z_]{1,32}")
_BUILD_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def export_failure_label(failure: str) -> str:
    """A failure string as a label from a closed set; "other" otherwise.
    Coded failures keep their status and a known code only."""
    coded = _CODED_FAILURE_RE.fullmatch(failure)
    if coded is not None:
        code = coded.group(2)
        return f"{coded.group(1)} ({code if code in _ERROR_CODES else '?'})"
    label = failure_bucket(failure)
    if (
        label in _FIXED_FAILURE_LABELS or _STATE_FAILURE_RE.fullmatch(label)
        or _HEALTH_HTTP_RE.fullmatch(label)
    ):
        return label
    return "other"


def _export_warn_label(warn: str) -> str:
    label = failure_bucket(warn)
    return label if label in _FIXED_WARN_LABELS else "other"


def _count_or_none(value: Any) -> int | None:
    return value if is_count(value) else None


def _number_or_none(value: Any) -> float | int | None:
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        return value
    return None


def _bool_or_none(value: Any) -> bool | None:
    return value if type(value) is bool else None


def _doc_ids_or_none(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return [str(d) for d in value]


def _state_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value in VERIFICATION_STATES else None


def export_row(row: dict[str, Any]) -> dict[str, Any]:
    """One result row reduced to the exportable fields."""
    finish = row.get("finish_reason")
    citations = row.get("citations")
    indices = row.get("inferred_indices")
    exported = {
        "id": str(row.get("id")),
        "repeat": row.get("repeat") if is_count(row.get("repeat")) else 1,
        "query_class": str(row.get("query_class")),
        "expected_behavior": str(row.get("expected_behavior")),
        "verdict": row.get("verdict") if row.get("verdict") in ("pass", "fail", "error") else "error",
        "failures": [export_failure_label(str(f)) for f in row.get("failures") or []],
        "warns": [_export_warn_label(str(w)) for w in row.get("warns") or []],
        "verification_state": _state_or_none(row.get("verification_state")),
        "expected_verification_state": _state_or_none(row.get("expected_verification_state")),
        "finish_reason": (None if finish is None
                          else finish if finish in _FINISH_REASONS else "other"),
        "path": row.get("path") if row.get("path") in ("llm", "zero_hits") else None,
        "why": why_mode(row),
        "citations_n": len(citations) if isinstance(citations, list) else None,
        "citations_inferred": _bool_or_none(row.get("citations_inferred")),
        "inferred_n": len(indices) if isinstance(indices, list) else None,
        "script_review_required": _bool_or_none(row.get("script_review_required")),
        "abstention_zeroed": _bool_or_none(row.get("abstention_zeroed")),
        **{key: _count_or_none(row.get(key)) for key in (
            "prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens",
            "elapsed_ms", "ttft_ms", "llm_ms", "client_first_token_ms")},
        "decode_tps": _number_or_none(row.get("decode_tps")),
        "gold_retrieved": _bool_or_none(row.get("gold_retrieved")),
        "expected_doc_ids": _doc_ids_or_none(row.get("expected_doc_ids")),
        "hit_doc_ids": _doc_ids_or_none(row.get("hit_doc_ids")),
        "cited_doc_ids": _doc_ids_or_none(row.get("cited_doc_ids")),
        "citation_precision": _number_or_none(row.get("citation_precision")),
        "citation_recall": _number_or_none(row.get("citation_recall")),
        "coverage": row.get("coverage") if row.get("coverage") in COVERAGE_STATES else None,
        "expected_served": _count_or_none(row.get("expected_served")),
        "supplied_n": _count_or_none(row.get("supplied_n")),
        "supplied_doc_n": _count_or_none(row.get("supplied_doc_n")),
    }
    if "turn" in row or "session_turns" in row:
        # Two-turn rows (issue #1268) and session rows (#1191) only:
        # single-turn rows keep their keys.
        exported.update(
            **({"turn": 2 if row["turn"] == 2 else 1} if "turn" in row
               else {"session_turns": _count_or_none(row.get("session_turns"))}),
            prior_answer_chars=_count_or_none(row.get("prior_answer_chars")),
            evidence_doc_ids=_doc_ids_or_none(row.get("evidence_doc_ids")),
            evidence_gold_hit=_bool_or_none(row.get("evidence_gold_hit")),
            evidence_cited_doc_ids=_doc_ids_or_none(row.get("evidence_cited_doc_ids")),
            evidence_citation_precision=_number_or_none(row.get("evidence_citation_precision")),
            evidence_citation_recall=_number_or_none(row.get("evidence_citation_recall")),
        )
    if "image_arm" in row:
        # `--image-arm` runs only (issue #1287): sizes, counts and a fixed CER
        # label; never the image, its base64, the transcript or the drawn text.
        from mainframe_rag.eval.image_arm import IMAGE_ARMS, cer_bucket

        arm = row["image_arm"]
        exported.update(
            image_arm=arm if arm in IMAGE_ARMS else None,
            image_n=_count_or_none(row.get("image_n")),
            image_bytes=_count_or_none(row.get("image_bytes")),
            image_reads_n=_count_or_none(row.get("image_reads_n")),
            image_read_failed=_bool_or_none(row.get("image_read_failed")),
            image_cer_bucket=cer_bucket(_number_or_none(row.get("image_cer"))),
        )
    if "follow_up_kind" in row:
        # `--follow-up` runs only (issue #1268); turn-1 rows carry None.
        from mainframe_rag.eval.follow_up import (
            CITATION_LIST_STATES,
            FOLLOW_UP_KINDS,
            GOLD_POSITIONS,
        )

        kind, switch_id, split = row["follow_up_kind"], row.get("switch_id"), row.get("gold_split")
        cut = row.get("citation_list_cut")
        exported.update(
            follow_up_kind=kind if kind in FOLLOW_UP_KINDS else None,
            switch_id=None if switch_id is None else str(switch_id),
            history_over_cap=_bool_or_none(row.get("history_over_cap")),
            citation_list_cut=cut if cut in CITATION_LIST_STATES else None,
            restate_declined=_bool_or_none(row.get("restate_declined")),
            gold_n=_count_or_none(row.get("gold_n")),
            gold_found_n=_count_or_none(row.get("gold_found_n")),
            gold_split=None if not isinstance(split, dict) else {
                position: {name: _count_or_none((split.get(position) or {}).get(name))
                           for name in ("n", "found")}
                for position in GOLD_POSITIONS},
        )
    return exported


def build_export(report: dict[str, Any], evaluator_git_sha: str, timestamp: str) -> dict[str, Any]:
    """The whole export: the manifest's aggregate blocks plus text-free rows.

    `metrics` is the report's own block with two exceptions: `by_failure` is
    recounted from the exported labels (a bucket of free text becomes
    "other"), and `by_complexity` keys outside the fixed label shape become
    "other". The agent URL is left out (it may name a site host)."""
    results = report["results"]
    rows = [export_row(row) for row in results]
    metrics = json.loads(json.dumps(report["metrics"]))
    by_failure: Counter = Counter()
    for raw, exported in zip(results, rows, strict=True):
        # Two-turn runs: the top-level block covers turn 1; `by_turn` has its own.
        if raw.get("verdict") in ("pass", "fail") and raw.get("turn") != 2:
            by_failure.update(exported["failures"])
    metrics["by_failure"] = dict(sorted(by_failure.items()))
    complexity: dict[str, dict[str, int]] = {}
    for key, block in (metrics.get("by_complexity") or {}).items():
        target = complexity.setdefault(key if _LABEL_RE.fullmatch(key) else "other",
                                       {"n": 0, "pass": 0})
        target["n"] += block["n"]
        target["pass"] += block["pass"]
    metrics["by_complexity"] = complexity
    params = {key: value for key, value in report["params"].items() if key != "agent_url"}
    coverage = report.get("coverage")
    if coverage is not None:
        coverage = dict(coverage)
        for key in ("build_id", "build_id_end"):
            value = coverage.get(key)
            coverage[key] = value if isinstance(value, str) and _BUILD_ID_RE.fullmatch(value) else None
    return {
        "schema": EXPORT_SCHEMA,
        "timestamp": timestamp,
        "evaluator_git_sha": evaluator_git_sha,
        "params": params,
        "metrics": metrics,
        "healthz": (report.get("deployment") or {}).get("healthz"),
        "representation": report.get("representation"),
        "coverage": coverage,
        "rows": rows,
    }


def write_summary(path: Path, results: list[dict[str, Any]], metrics: dict[str, Any]) -> None:
    """Human-readable markdown summary: metrics table + every failure with
    its detail so the report is self-contained."""
    lines: list[str] = ["# Answer-tier eval summary", ""]
    lines.append(f"- queries: {metrics['queries']} (judged {metrics['judged']}, errors {metrics['errors']})")
    if "total" in metrics and "unjudged" in metrics:
        lines.append(
            f"- answer pass rate: {metrics['answer_pass_rate']} "
            f"(n={metrics['answer_n']}) ; judged {metrics['judged']}/{metrics['total']}, "
            f"errors {metrics['errors']}, unjudged {metrics['unjudged']}"
        )
    else:
        # Metrics from before issue #1216 (for example loaded from an older
        # report file) carry no coverage fields: keep the line in its old shape.
        lines.append(f"- answer pass rate: {metrics['answer_pass_rate']} (n={metrics['answer_n']})")
    lines.append(f"- abstain pass rate: {metrics['abstain_pass_rate']} (n={metrics['abstain_n']})")
    lines.append(f"- failures: {metrics['failures']}, warns: {metrics['warns']}, zero-hits paths: {metrics['zero_hits']}")
    lines.append(f"- inferred-citation rows (not grounded): {metrics['inferred_citations']}")
    lines.append(f"- citations per answer (mean): {metrics['citations_per_answer']}")
    lines.append(
        f"- false refusals: {metrics.get('false_refusals')} "
        f"(rate {metrics.get('false_refusal_rate')}, n={metrics.get('answer_n')})"
    )
    lines.append(
        f"- unsafe answers: {metrics.get('unsafe_answers')} "
        f"(rate {metrics.get('unsafe_answer_rate')}, n={metrics.get('abstain_n')})"
    )
    if metrics.get("by_verification_state"):
        lines.append(
            "- verification states: "
            + ", ".join(f"{k}={v}" for k, v in metrics["by_verification_state"].items())
        )
    lines.append(
        f"- answer completeness: {metrics.get('answer_completeness')} "
        f"(n={metrics.get('answer_completeness_n')}: expected docs retrieved; share whose "
        "validated citations cover all of them; None = caller had no pool join)"
    )
    coding = metrics.get("coding")
    if coding:
        lines.append(
            f"- coding requests (#1276): {coding['n']} rows, pass {coding['pass']}/{coding['judged']}, "
            f"false refusals {coding['false_refusals']} "
            f"(rate {coding['false_refusal_rate']}, n={coding['answer_n']})"
        )
    lines.append("")
    lines.append("| class | n | pass |")
    lines.append("|---|---|---|")
    for cls, m in metrics["by_class"].items():
        lines.append(f"| {cls} | {m['n']} | {m['pass']} |")
    if metrics.get("by_failure"):
        lines.append("")
        lines.append("| failure | count |")
        lines.append("|---|---|")
        for bucket, count in metrics["by_failure"].items():
            lines.append(f"| {bucket} | {count} |")
    if metrics.get("by_why"):
        lines.append("")
        lines.append("| citation WHY mode | count |")
        lines.append("|---|---|")
        for mode, count in metrics["by_why"].items():
            lines.append(f"| {mode} | {count} |")
        lines.append(f"- inferred indices off gold: {metrics.get('inferred_index_off_gold')}")
    lines.append("")
    lines.extend(_turn_lines(metrics))
    lines.extend(_measurement_lines(metrics))
    fails = [r for r in results if r.get("verdict") in ("fail", "error")]
    if fails:
        lines.append("## Failures")
        for r in fails:
            turn = f" turn {r['turn']}" if "turn" in r else ""
            if r.get("follow_up_kind"):
                turn += f" {r['follow_up_kind']}"
            lines.append(f"### {r['id']}{turn} ({r['verdict']}, {r['expected_behavior']})")
            lines.append(f"- query: {r['query']}")
            for f in r.get("failures") or []:
                lines.append(f"- FAIL: {f}")
            if r.get("citations") is not None:
                lines.append(f"- citations: {r['citations']}")
            body = (r.get("answer") or "")[:600]
            lines.append(f"- answer (truncated): {body}")
            lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _turn_lines(metrics: dict[str, Any]) -> list[str]:
    """The two-turn block (issue #1268): evidence first, verdicts last.
    Above this section every metric covers turn 1 only."""
    by_turn = metrics.get("by_turn")
    if not by_turn:
        return []
    if "n" not in by_turn["1"]:
        return _kind_lines(by_turn)
    first, second = by_turn["1"], by_turn["2"]
    lines = ["## By turn (follow-up sessions; the metrics above cover turn 1 only)", "",
             "| metric | turn 1 | turn 2 |", "|---|---|---|"]
    for name in ("n", "errors", "evidence_n", "evidence_gold_hit", "evidence_gold_hit_rate",
                 "zero_hits", "false_refusals", "citation_n", "citation_precision_n",
                 "citation_precision_mean",
                 "citation_recall_mean", "pass", "pass_rate"):
        lines.append(f"| {name} | {first[name]} | {second[name]} |")
    states = sorted({*first["by_verification_state"], *second["by_verification_state"]})
    for state in states:
        lines.append(f"| state {state} | {first['by_verification_state'].get(state, 0)} "
                     f"| {second['by_verification_state'].get(state, 0)} |")
    pairs, chars = second["vs_opener"], second["prior_answer_chars"]
    lines += ["", "- follow-up vs its opener (evidence gold hit): "
              + ", ".join(f"{k}={v}" for k, v in pairs.items()),
              (f"- prior answer chars: n={chars['n']} p50={chars['p50']} max={chars['max']}, "
               f"over the {chars['cut_chars']}-char prior-turn cut: {chars['over_cut']}"),
              ("- turn-2 pass is structural only (body, no refusal, explicit validated "
               "citations); the opener's gold substrings and expected state are not applied"),
              ""]
    for turn, block in (("1", first), ("2", second)):
        for name, spread in (block.get("repeats") or {}).items():
            lines.append(f"- turn {turn} {name} over repeats: min {spread['min']} "
                         f"max {spread['max']} range {spread['range']}")
    lines.append("")
    return lines


_KIND_ROWS = ("n", "errors", "evidence_n", "evidence_gold_hit", "evidence_gold_hit_rate",
              "zero_hits", "false_refusals", "citation_n", "citation_recall_mean",
              "pass", "pass_rate")
_KIND_READING = {
    "anaphoric": "structural pass (body, no refusal, explicit validated citations)",
    "switch": "the switched-to row's full gold verdict",
}
_CLASS_B_READING = "the opener's gold substrings and no decline, after a passed opener"


def _kind_lines(by_turn: dict[str, Any]) -> list[str]:
    """`--follow-up` runs: one table per kind, turn 1 (its openers) against
    turn 2, then the kind's own counts. Kinds are never pooled."""
    lines = ["## By turn and follow-up kind (the metrics above cover turn 1 only)", ""]
    for kind, second in by_turn["2"].items():
        first = by_turn["1"][kind]
        lines += [f"### {kind}: turn-2 pass is {_KIND_READING.get(kind, _CLASS_B_READING)}", "",
                  "| metric | turn 1 | turn 2 |", "|---|---|---|"]
        lines += [f"| {name} | {first[name]} | {second[name]} |" for name in _KIND_ROWS]
        for name in ("elapsed_ms", "client_first_token_ms"):
            lines.append(f"| {name} p50 | {first['timing'][name]['p50']} "
                         f"| {second['timing'][name]['p50']} |")
        exposure = second["exposure"]
        lines += ["", (f"- history over the declared {second['prior_answer_chars']['cut_chars']}"
                       f"-char cut: {exposure['over_cap']}/{exposure['n']}; citation list: "
                       + ", ".join(f"{k}={v}" for k, v in exposure["citation_list"].items()))]
        if second["skipped"]:
            lines.append("- skipped: " + ", ".join(f"{k}={v}" for k, v in second["skipped"].items()))
        if "vs_opener" in second:
            lines.append("- vs its opener (evidence gold hit): "
                         + ", ".join(f"{k}={v}" for k, v in second["vs_opener"].items()))
        if "vs_single" in second:
            single = second["vs_single"]
            for name in ("pass", "evidence_gold_hit", "verification_state"):
                lines.append(f"- vs the same question alone ({name}): "
                             + ", ".join(f"{k}={v}" for k, v in single[name].items()))
        if "restate" in second:
            restate = second["restate"]
            lines.append(f"- declines: {restate['declines']} (rate {restate['decline_rate']}); "
                         f"gold strings found {restate['gold_found_n']}/{restate['gold_n']}")
            for position, sides in restate["gold_split"].items():
                lines.append(f"- {position}: " + ", ".join(
                    f"{side} {c['found']}/{c['n']}" for side, c in sides.items()))
        for turn, block in (("1", first), ("2", second)):
            for name, spread in (block.get("repeats") or {}).items():
                lines.append(f"- turn {turn} {name} over repeats: min {spread['min']} "
                             f"max {spread['max']} range {spread['range']}")
        lines.append("")
    return lines


def _measurement_lines(metrics: dict[str, Any]) -> list[str]:
    """The two metric groups (issue #1195) as summary tables: counts and
    durations only. Decisions read the first table; the second describes the
    measured host."""
    groups = metrics.get("measurement")
    if not groups:
        return []
    independent, dependent = groups["hardware_independent"], groups["hardware_dependent"]
    truncation, incomplete = independent["truncation"], independent["generation_incomplete"]
    lines = [
        "## Hardware-independent (decide on these)",
        (f"- truncation rate: {truncation['rate']} "
         f"({truncation['truncated']}/{truncation['n']} model answers with a known finish reason)"),
        (f"- generation_incomplete rate: {incomplete['rate']} "
         f"({incomplete['count']}/{incomplete['n']}; upper bound, every transport)"),
        f"- rows with a reported reasoning split: {independent['reasoning_split_rows']}",
        "",
        "| class | n | pass | rate |",
        "|---|---|---|---|",
    ]
    for cls, block in independent["pass_rate_by_class"].items():
        lines.append(f"| {cls} | {block['n']} | {block['pass']} | {block['rate']} |")
    lines += ["", "| tokens per answer | n | p50 | p95 | mean | max |", "|---|---|---|---|---|---|"]
    for name, block in independent["tokens"].items():
        lines.append(f"| {name} | {block['n']} | {block['p50']} | {block['p95']} | {block['mean']} | {block['max']} |")
    lines += ["", "## Hardware-dependent (this host only)", "",
              "| timing | n | p50 | p95 | mean | max |", "|---|---|---|---|---|---|"]
    for name, block in dependent.items():
        lines.append(f"| {name} | {block['n']} | {block['p50']} | {block['p95']} | {block['mean']} | {block['max']} |")
    repeats = metrics.get("repeats")
    if repeats:
        lines += ["", f"## Spread over {repeats['n']} repeats", "",
                  f"- entries judged differently across repeats: {len(repeats['verdict_flips'])}",
                  "", "| metric | min | max | mean | range |", "|---|---|---|---|---|"]
        for name, block in repeats["spread"].items():
            if block["range"] is not None:
                lines.append(f"| {name} | {block['min']} | {block['max']} | {block['mean']} | {block['range']} |")
    lines.append("")
    return lines


_MAX_REPEATS = 10


def _repeats(raw: str) -> int:
    message = f"repeats must be an integer from 1 to {_MAX_REPEATS}"
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(message) from None
    if not 1 <= value <= _MAX_REPEATS:
        raise argparse.ArgumentTypeError(message)
    return value


def _subset(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("subset must be a positive integer") from None
    if value < 1:
        raise argparse.ArgumentTypeError("subset must be a positive integer")
    return value


_ARM_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")


def _arm(raw: str) -> str:
    """Operator-declared arm label (issue #1268). The agent does not report
    whether it condenses, so the export carries the operator's declaration;
    a fixed shape keeps it text-free."""
    if not _ARM_RE.fullmatch(raw):
        raise argparse.ArgumentTypeError(
            "arm must be 1-32 characters of a-z, 0-9, '.', '_' or '-', starting with a letter or digit")
    return raw


def _follow_up_kinds(raw: str) -> tuple[str, ...]:
    """`all` or a comma list of follow-up kinds, returned in session order."""
    from mainframe_rag.eval.follow_up import FOLLOW_UP_KINDS

    names = FOLLOW_UP_KINDS if raw == "all" else tuple(raw.split(","))
    if not names or any(name not in FOLLOW_UP_KINDS for name in names):
        raise argparse.ArgumentTypeError(
            f"follow-up must be 'all' or a comma list of {', '.join(FOLLOW_UP_KINDS)}")
    return tuple(kind for kind in FOLLOW_UP_KINDS if kind in names)


def _setting_bounded(setting: str, label: str, raw: str) -> int:
    """An operator-declared value of an agent setting, within that setting's
    own bounds."""
    field = Settings.model_fields[setting]
    low = next(m.ge for m in field.metadata if hasattr(m, "ge"))
    high = next(m.le for m in field.metadata if hasattr(m, "le"))
    message = f"{label} must be an integer from {low} to {high}"
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(message) from None
    if not low <= value <= high:
        raise argparse.ArgumentTypeError(message)
    return value


def _prior_cap(raw: str) -> int:
    return _setting_bounded("chat_max_prior_turn_chars", "prior cap", raw)


def _hit_limit(raw: str) -> int:
    return _setting_bounded("answer_hit_limit", "hit limit", raw)


def select_subset(entries: list[dict[str, Any]], size: int) -> list[dict[str, Any]]:
    """Sweep subset (issue #1195): the default sampler's class-interleaved
    round-robin, refused when the size cannot hold one row of every class.
    One sampling rule (`select_sample`); the guard is what makes a subset
    comparable to a full pass class by class."""
    classes = {entry["query_class"] for entry in entries}
    if size < len(classes):
        raise ValueError(
            f"subset of {size} would drop a class: {len(classes)} classes need at least one row each"
        )
    return select_sample(entries, size)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Answer-tier golden eval against the live stack")
    parser.add_argument("--golden", type=Path, action="append", default=None,
                        help="golden JSONL path (repeatable; default: dev golden, +holdout under VENUE=rc)")
    parser.add_argument("--max-queries", type=int, default=24,
                        help="deterministic stratified sample size (default 24)")
    parser.add_argument("--all", action="store_true", help="run every golden entry (slow: one reasoning call each)")
    parser.add_argument("--out", type=Path, default=None, help="JSON report path")
    parser.add_argument("--summary", type=Path, default=None, help="markdown summary path")
    parser.add_argument("--agent-url", type=_agent_url, default=None,
                        help="evaluate an already deployed HTTP agent instead of local TestClient")
    parser.add_argument("--request-timeout", type=_request_timeout, default=300.0,
                        help=f"HTTP-mode request timeout in seconds (1–{_MAX_REQUEST_TIMEOUT_S}, default 300)")
    parser.add_argument("--temperature", type=float, default=0.0,
                        help="sampling temperature sent per request (default 0 for stable "
                             "comparisons, issue #596; pass the production setting explicitly "
                             "to measure it)")
    parser.add_argument("--stream", action="store_true",
                        help="HTTP mode: request the SSE stream and record finish reason, "
                             "token counts and time to first token from its final event "
                             "(issue #1195; default off keeps the JSON request)")
    parser.add_argument("--subset", type=_subset, default=None,
                        help="sweep subset size: the class-interleaved round-robin, refused "
                             "if any class would be dropped; a resolved set that contains the "
                             "frozen holdout needs an explicit non-holdout --golden "
                             "(replaces --max-queries)")
    parser.add_argument("--repeats", type=_repeats, default=1,
                        help=f"run the sample this many whole passes (1–{_MAX_REPEATS}) and "
                             "report the per-metric spread; more than one pass is a sweep and "
                             "needs an explicit non-holdout --golden under VENUE=rc "
                             "(default 1)")
    parser.add_argument("--endpoint", choices=("answer", "chat"), default="answer",
                        help="answer: POST /v1/answer (default). chat: each question as a "
                             "single-turn POST /v1/chat, the route the Open WebUI Pipe calls; "
                             "needs --stream, because the Pipe streams (issue #1191)")
    parser.add_argument("--coverage", action="store_true",
                        help="page GET /v1/sources once, record its build_id and mark per row "
                             "whether all, some or none of expected_doc_ids are served; a 503 "
                             "or an older agent is 'unknown', never a failure")
    parser.add_argument("--export", type=Path, default=None,
                        help="text-free JSON for leaving an air-gapped site: params, metrics, "
                             "health, coverage and per-row ids, labels, counts and timings; "
                             "no question, answer, citation or failure text")
    parser.add_argument("--turns", type=int, choices=(1, 2), default=1,
                        help="2: after each answer row with gold docs, ask its class follow-up "
                             "with the history the Open WebUI Pipe would send, and score the "
                             "follow-up from the evidence the agent packed (issue #1268; needs "
                             "--endpoint chat --stream and --arm; default 1)")
    parser.add_argument("--arm", type=_arm, default=None,
                        help="with --turns 2: the operator's label for the deployed setting "
                             "under test, e.g. condense-off or condense-on (recorded in params)")
    parser.add_argument("--follow-up", type=_follow_up_kinds, default=None,
                        help="with --turns 2: which follow-ups each opener gets, 'all' or a comma "
                             "list of anaphoric (the class follow-up), switch (another sampled "
                             "row's question), restate and summary (about the previous answer); "
                             "rows and metrics then carry the kind (default: anaphoric only, "
                             "output as before)")
    parser.add_argument("--prior-cap", type=_prior_cap, default=None,
                        help="with --follow-up: the deployed CHAT_MAX_PRIOR_TURN_CHARS as the "
                             "operator declares it (the agent does not report it); default "
                             f"{Settings.model_fields['chat_max_prior_turn_chars'].default}, the "
                             "agent default (recorded in params)")
    parser.add_argument("--hit-limit", type=_hit_limit, default=None,
                        help="the deployed ANSWER_HIT_LIMIT as the operator declares it (the "
                             "agent does not report it): the sibling /v1/search pool asks for "
                             "that many hits; default "
                             f"{Settings.model_fields['answer_hit_limit'].default}, the agent default "
                             "(recorded in params, issue #1191)")
    parser.add_argument("--image-arm", choices=("text", "image", "image+ask"), default=None,
                        help="with --endpoint chat: text sends the question typed (today's "
                             "body); image sends it only as a synthetic 80-column terminal "
                             "image drawn at run time; image+ask sends that image with a short "
                             "fixed typed question. Rows add image size, read count and a "
                             "transcript CER bucket; refuses the frozen holdout (issue #1287)")
    parser.add_argument("--sessions", type=Path, default=None,
                        help="fixed-history /v1/chat sessions (issue #1191): a private dev JSONL "
                             "of rows whose `turns` alternate user and authored assistant text; "
                             "each last turn is asked with the history the Open WebUI Pipe would "
                             "send and judged like a golden row. Replaces --golden; needs "
                             "--endpoint chat --stream")
    parser.add_argument("--keep-evidence-text", action="store_true",
                        help="also keep the packed excerpt text per row in the JSON report "
                             "(private; for a grounding check). Default: text-free metadata "
                             "only. Never in --export")
    args = parser.parse_args(argv)
    if args.stream and args.agent_url is None:
        parser.error("--stream needs --agent-url: the in-process client buffers the response, "
                     "so its first-token time would be the total time")
    if args.endpoint == "chat" and not args.stream:
        parser.error("--endpoint chat streams like the Open WebUI Pipe: add --stream "
                     "(which needs --agent-url)")
    if args.subset is not None and args.all:
        parser.error("--subset and --all are exclusive")
    two_turn = args.turns == 2
    if two_turn and args.endpoint != "chat":
        parser.error("--turns 2 sends the follow-up over /v1/chat: add --endpoint chat --stream")
    if two_turn != (args.arm is not None):
        parser.error("--arm labels a --turns 2 run: pass both or neither")
    if args.follow_up is not None and not two_turn:
        parser.error("--follow-up chooses the second turns: add --turns 2")
    if args.prior_cap is not None and args.follow_up is None:
        parser.error("--prior-cap is read by --follow-up: add it")
    if args.image_arm is not None and (args.endpoint != "chat" or two_turn):
        parser.error("--image-arm sends single-turn /v1/chat requests: add --endpoint chat "
                     "--stream, without --turns 2")
    sessions_mode = args.sessions is not None
    if sessions_mode:
        if args.endpoint != "chat":
            parser.error("--sessions asks over /v1/chat: add --endpoint chat --stream")
        if args.golden is not None or args.all or args.subset is not None or two_turn:
            parser.error("--sessions is its own dataset: drop --golden, --all, --subset "
                         "and --turns 2")
    if args.image_arm is not None and sessions_mode:
        parser.error("--image-arm draws dev golden questions: drop --sessions")
    pipe_mod: Any = None
    pipe_sha256: str | None = None
    if two_turn or sessions_mode:
        from mainframe_rag.eval.follow_up import PipeUnavailable, load_pipe

        try:
            pipe_mod, pipe_sha256 = load_pipe()
        except PipeUnavailable as exc:
            print(f"FAIL: {exc} (integrations/openwebui/docs_pipe.py in the evaluator "
                  "checkout)", file=sys.stderr)
            return 2

    sessions_identity: dict[str, Any] | None = None
    try:
        if sessions_mode:
            # Dev-only rows, never the holdout or the release set (refused by
            # name before the read). Identity is basename + sha256 + count.
            from mainframe_rag.eval.datasets import read_dev_only_bytes
            from mainframe_rag.eval.follow_up import SessionError, parse_sessions

            raw = read_dev_only_bytes(args.sessions)
            try:
                entries = parse_sessions(raw)
            except (SessionError, UnicodeDecodeError) as exc:
                print(f"FAIL: {exc if isinstance(exc, SessionError) else 'session file is not UTF-8'}",
                      file=sys.stderr)
                return 2
            digest = hashlib.sha256(raw).hexdigest()
            datasets = [{"name": args.sessions.name, "sha256": digest}]
            sessions_identity = {"name": args.sessions.name, "n": len(entries), "sha256": digest}
        else:
            # Venue rule (issue #268): dev defaults to the golden set only; the
            # holdout and the real-corpus collection require VENUE=rc. A tuning
            # sweep additionally refuses a resolved set that contains the holdout
            # (review B2), before any read or model call: only an explicit
            # non-holdout --golden may feed --subset/--repeats.
            golden_paths = resolve_golden_paths(args.golden)
            if args.subset is not None or args.repeats > 1:
                operation = "--subset" if args.subset is not None else f"--repeats {args.repeats}"
                require_tuning_golden(golden_paths, operation)
            if args.image_arm is not None:
                # Images are drawn from dev golden questions only (issue #1287).
                require_tuning_golden(golden_paths, "--image-arm")
            if args.agent_url is None:
                require_rc_for_collection(load_settings().qdrant_collection)
            entries, datasets = load_golden_records_with_identity(golden_paths)
    except DatasetError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    if len({e["id"] for e in entries}) != len(entries):
        print("BUILD FAILED: duplicate entry ids across golden files", file=sys.stderr)
        return 1

    if args.subset is not None:
        try:
            sample = select_subset(entries, args.subset)
        except ValueError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 2
    else:
        # Sessions are authored rows: every one, in file order.
        sample = (entries if args.all or sessions_mode
                  else select_sample(entries, args.max_queries))
    print(f"[*] answer-tier eval: {len(sample)} of {len(entries)} entries (deterministic stratified sample)", file=sys.stderr)
    # Whole passes, not row-by-row repeats: drift during a session (cache
    # warmth, co-tenant load) lands on every row equally (issue #1195).
    passes = [(number, entry) for number in range(1, args.repeats + 1) for entry in sample]
    plan: FollowUpPlan | None = None
    if args.follow_up is not None:
        from mainframe_rag.eval.follow_up import switch_pairs

        # Pairs are drawn from the follow-up rows of this sample, so every
        # switched-to question is also asked as an opener in the same pass.
        eligible = [entry for entry in sample if has_follow_up(entry)]
        by_id = {str(entry["id"]): entry for entry in eligible}
        pairs = switch_pairs(eligible) if "switch" in args.follow_up else {}
        plan = FollowUpPlan(
            args.follow_up, {opener: by_id[partner] for opener, partner in pairs.items()},
            Settings.model_fields["chat_max_prior_turn_chars"].default
            if args.prior_cap is None else args.prior_cap)

    results: list[dict[str, Any]] = []
    deployment: dict[str, Any] | None = None
    health_error: str | None = None
    client: Any
    signals: dict[str, dict[str, Any]] | None
    with ExitStack() as stack:
        if args.agent_url is not None:
            client = stack.enter_context(httpx2.Client(
                base_url=args.agent_url, timeout=args.request_timeout,
                follow_redirects=False, trust_env=False,
            ))
            health, health_error = _http_health(client)
            deployment = {"agent_url": args.agent_url, "healthz": health,
                          "git_sha": None, "image_digest": None}
            signals = None
        else:
            # The default keeps the production app's lifespan-built clients.
            from fastapi.testclient import TestClient

            import mainframe_rag.agent.app as app_mod

            capture = AnswerCapture()
            logging.getLogger("agent").addHandler(capture)
            stack.callback(logging.getLogger("agent").removeHandler, capture)
            client = stack.enter_context(TestClient(app_mod.app))
            signals = capture.signals
        coverage: dict[str, Any] | None = None
        build_id_end: str | None = None
        if args.coverage:
            coverage = (fetch_coverage(client) if health_error is None
                        else _coverage("unknown", "not_run", None, 0))
            print(f"[*] coverage: {coverage['status']} ({coverage['reason'] or 'complete'}), "
                  f"{coverage['pages']} page(s), {len(coverage['doc_ids'])} served doc id(s)",
                  file=sys.stderr)
        if health_error is not None:
            print(f"FAIL: {health_error}", file=sys.stderr)
            results = [dict({k: v for k, v in entry.items() if k != "turns"},
                            temperature=args.temperature, verdict="error",
                            failures=[health_error], elapsed_ms=0) for _, entry in passes]
            if sessions_mode:
                for row, (_, entry) in zip(results, passes, strict=True):
                    row.update(session_turns=len(entry["turns"]), prior_answer_chars=None)
            if two_turn:
                for row in results:
                    row["turn"] = 1
                    if plan is not None:
                        row.update(follow_up_kind=None, switch_id=None)
            if args.image_arm is not None:
                from mainframe_rag.eval.image_arm import unsent_fields

                for row in results:
                    row.update(unsent_fields(args.image_arm))
            if coverage is not None:
                for row, (_, entry) in zip(results, passes, strict=True):
                    apply_coverage(row, entry, coverage)
        else:
            pool_limit = (Settings.model_fields["answer_hit_limit"].default
                          if args.hit_limit is None else args.hit_limit)
            for i, (number, entry) in enumerate(passes, 1):
                if sessions_mode:
                    # No sibling search: a last turn searched on its own words
                    # would hide what the history changes; evidence comes from
                    # the agent's own packed list (as for a two-turn follow-up).
                    row = run_session(client, entry, pipe_mod, args.temperature,
                                      args.keep_evidence_text)
                else:
                    pool = _fetch_pool(client, entry["query"], pool_limit)
                    image_turn = None
                    if args.image_arm is not None:
                        from mainframe_rag.eval.image_arm import build_turn

                        image_turn = build_turn(args.image_arm, entry["query"])
                    frames: list[str] | None = (
                        [] if two_turn or image_turn is not None else None)
                    join_hits: list[dict[str, Any]] = []
                    row = run_query(
                        client, entry, signals, pool, temperature=args.temperature,
                        stream=args.stream, endpoint=args.endpoint,
                        chat_body=None if image_turn is None else image_turn.body,
                        frames=frames, evidence=two_turn,
                        keep_evidence_text=args.keep_evidence_text,
                        prefer_response_hits=image_turn is None,
                        citation_hits_out=join_hits,
                    )
                    if image_turn is not None:
                        from mainframe_rag.eval.image_arm import apply_image_reads

                        apply_image_reads(row, image_turn, frames)
                    effective_join = join_hits if join_hits else pool
                    if effective_join is not None:
                        apply_pool_join(row, entry, effective_join)
                session = [row]
                if two_turn:
                    row["turn"] = 1
                    if plan is not None:
                        row.update(follow_up_kind=None, switch_id=None)
                    if has_follow_up(entry):
                        session.extend(run_follow_ups(client, entry, row, frames or [],
                                                      pipe_mod, args.temperature, plan,
                                                      args.keep_evidence_text))
                for turn_row in session:
                    if coverage is not None:
                        # A switch row is judged on its partner's docs.
                        judged_entry = (plan.partners[str(entry["id"])]
                                        if plan is not None
                                        and turn_row.get("follow_up_kind") == "switch"
                                        else entry)
                        apply_coverage(turn_row, judged_entry, coverage)
                    if args.repeats > 1:
                        turn_row["repeat"] = number
                    results.append(turn_row)
                    marker = turn_row.get("verdict", "?").upper()
                    turn = f" t{turn_row['turn']}" if "turn" in turn_row else ""
                    if turn_row.get("follow_up_kind"):
                        turn += f" {turn_row['follow_up_kind']}"
                    print(
                        f"[{i:>3}/{len(passes)}] {marker:5s} {turn_row['id']:8s}{turn} "
                        # Session turns are private dev text: the id only.
                        f"{'' if sessions_mode else turn_row['query'][:58]}",
                        file=sys.stderr,
                    )
                    for f in turn_row.get("failures") or []:
                        label = export_failure_label(str(f)) if sessions_mode else f
                        print(f"        FAIL {label}", file=sys.stderr)
            if coverage is not None and coverage["status"] == "known":
                # A build that moved during the run splits the rows between builds.
                build_id_end = _served_build_id(client)

    # Two-turn runs (issue #1268): the existing blocks cover turn 1, so they
    # read as a single-turn chat run over the same sample; turn 2 is `by_turn`.
    openers = [r for r in results if r.get("turn") != 2]
    metrics = summarize(openers)
    if args.repeats > 1 and health_error is None:
        # Pooled metrics above; per-pass metrics only feed the spread.
        metrics["repeats"] = repeat_spread(
            [summarize([r for r in openers if r.get("repeat") == number])
             for number in range(1, args.repeats + 1)],
            openers,
        )
    if plan is not None:
        metrics["by_turn"] = kind_breakdown(results, args.repeats, plan)
    elif two_turn:
        metrics["by_turn"] = turn_breakdown(results, args.repeats)
    params: dict[str, Any] = {
        "temperature": args.temperature,
        # Measurement identity (issue #1195): which rows, in which order, how
        # often and over which transport. Ids are hashed, never listed here.
        "transport": "sse" if args.stream else "json",
        # Issue #1191: which route answered, and whether coverage was read.
        "endpoint": args.endpoint,
        "coverage": args.coverage,
        "repeats": args.repeats,
        "sample": {
            "mode": ("sessions" if sessions_mode else "subset" if args.subset is not None
                     else "all" if args.all else "default"),
            **sample_identity(sample),
            # Dataset identity (review N2): basename + sha256 of the exact
            # resolved files, never a private path or question text.
            "datasets": datasets,
        },
    }
    if args.hit_limit is not None:
        params["hit_limit"] = args.hit_limit
    if args.image_arm is not None:
        from mainframe_rag.eval.image_arm import PANEL_COLUMNS, image_ask_sha256, image_metrics

        params.update(image_arm=args.image_arm, image_columns=PANEL_COLUMNS,
                      image_ask_sha256=image_ask_sha256())
        metrics["image"] = image_metrics(results, args.image_arm)
    if two_turn:
        from mainframe_rag.eval.follow_up import follow_ups_sha256

        params.update(turns=2, arm=args.arm, follow_ups_sha256=follow_ups_sha256(),
                      pipe_sha256=pipe_sha256)
    if sessions_mode:
        params.update(sessions=sessions_identity, pipe_sha256=pipe_sha256)
    # Issue #1191: whether rows hold packed excerpt text (private reports only).
    params["keep_evidence_text"] = args.keep_evidence_text
    if plan is not None:
        from mainframe_rag.eval.follow_up import restate_follow_ups_sha256, switch_pairs_sha256

        switching = "switch" in plan.kinds
        params.update(
            follow_up=list(plan.kinds), prior_cap=plan.prior_cap,
            restate_follow_ups_sha256=restate_follow_ups_sha256(),
            switch_pairs_sha256=(switch_pairs_sha256(
                [str(entry["id"]) for entry in sample],
                {opener: str(partner["id"]) for opener, partner in plan.partners.items()})
                if switching else None),
            switch_pairs_n=len(plan.partners) if switching else None,
        )
    report: dict[str, Any] = {"metrics": metrics, "results": results, "params": params}
    if deployment is not None:
        params.update(agent_url=args.agent_url, request_timeout=args.request_timeout)
        report["deployment"] = deployment
        report["representation"] = {
            "servable": health_error is None,
            "outcome": (deployment["healthz"] or {}).get("representation", "unknown"),
        }
    if coverage is not None:
        report["coverage"] = coverage_summary(coverage, results, build_id_end)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        write_summary(args.summary, results, metrics)
    if args.export:
        from mainframe_rag.manifest import get_git_sha

        export = build_export(
            report,
            # The evaluator's own checkout, wherever the working directory is.
            get_git_sha(Path(__file__).resolve().parents[3]),
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        args.export.parent.mkdir(parents=True, exist_ok=True)
        args.export.write_text(json.dumps(export, indent=1, ensure_ascii=True) + "\n",
                               encoding="utf-8")
        print(f"export written: {len(export['rows'])} row(s), no text fields", file=sys.stderr)

    # Trend manifests stay in gitignored evals/runs/. Only in-process runs
    # may use the shared local Settings/Qdrant identity helper.
    try:
        if deployment is not None:
            manifest = _write_http_manifest(report)
        else:
            from mainframe_rag.manifest import write_run_manifest

            manifest = write_run_manifest("eval_answers", load_settings(), metrics, params=params)
        print(f"run manifest appended ({manifest['git_sha'][:8]})", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — manifest is observability, never the gate
        print(f"warn: failed to append run manifest: {exc}", file=sys.stderr)

    # Fail closed: structural FAILs and stack errors both gate the exit code.
    if health_error is not None:
        return 2
    clean = metrics["failures"] == 0 and metrics["errors"] == 0
    if two_turn:
        clean = clean and not any(r.get("verdict") in ("fail", "error") for r in results)
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
