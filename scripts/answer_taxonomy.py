#!/usr/bin/env python3
"""Per-row answer failure taxonomy (issue #830): join answer-eval reports with
the packing diagnostic's per-row retrieval and packing attribution and give
every golden row exactly one primary class.

Offline and pure: it reads two kinds of JSON report and writes one. No Qdrant,
no model, no agent.

  * answer reports: ``python -m mainframe_rag.eval.answers --out ...``; pass
    several when the run is split across sessions. A row id seen in two
    reports, or a row outside the expected golden set, refuses the join (each
    row is run exactly once);
  * one packing report: ``scripts/packing_diagnostic.py --out ...`` over the
    same corpus and every row class of the answer run, for example
    ``--classes CMP,DIA,DOC,LEG,MSG,NEG,SYN,SYS,TBL,VER``. ``--profile`` picks
    the planner profile that matches the served one; it is required when the
    report holds more than one.

Primary class of a row, first match wins:

  pass                  the answer eval judged the row a pass
  timeout               client read timeout, or the agent's 504 deadline_exceeded
  error                 any other request, HTTP or malformed-response error
  unsafe_answer         abstain row: the trap was answered with citations
  retrieval_miss        answer row: the served path found no hits, or the
                        diagnostic never retrieved a gold target
  packing_loss          answer row: a gold target was retrieved but not packed
  unattributed          answer row failing without a diagnostic row to read
  refusal_gold_present  answer row: refused with the gold in the prompt
  generation_incomplete served state generation_incomplete (cut before stop)
  citation_format       empty body, no validated or only inferred citations,
                        or another state mismatch (on an abstain row: a decline
                        the agent did not recognize)
  content_miss          grounded and cited, but a gold substring or the
                        requested identifier check failed

Retrieval and packing come first because the model cannot be judged on
evidence it never saw; the model-behavior classes a row also shows are kept in
``also``. Abstain rows have no gold to retrieve, so the diagnostic is not read
for them. A golden row absent from every answer report is ``not_run`` and
never counts as a pass.

The report holds identifiers, classes, outcomes and redacted failure buckets
only: no query, answer, citation or chunk text.

    python scripts/answer_taxonomy.py \\
        --answers run1/answer-report.json --answers run2/answer-report.json \\
        --packing packing/report.json --profile target \\
        --out taxonomy.json --markdown taxonomy.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from mainframe_rag.eval.answers import failure_bucket

PASS = "pass"
TIMEOUT = "timeout"
ERROR = "error"
UNSAFE_ANSWER = "unsafe_answer"
RETRIEVAL_MISS = "retrieval_miss"
PACKING_LOSS = "packing_loss"
UNATTRIBUTED = "unattributed"
REFUSAL_GOLD_PRESENT = "refusal_gold_present"
GENERATION_INCOMPLETE = "generation_incomplete"
CITATION_FORMAT = "citation_format"
CONTENT_MISS = "content_miss"
NOT_RUN = "not_run"

CLASSES = (
    PASS,
    TIMEOUT,
    ERROR,
    UNSAFE_ANSWER,
    RETRIEVAL_MISS,
    PACKING_LOSS,
    UNATTRIBUTED,
    REFUSAL_GOLD_PRESENT,
    GENERATION_INCOMPLETE,
    CITATION_FORMAT,
    CONTENT_MISS,
    NOT_RUN,
)

# Model-behavior classes, most explanatory first: a refusal explains missing
# citations and substrings; a cut generation explains a missing cite block.
_MODEL_ORDER = (REFUSAL_GOLD_PRESENT, GENERATION_INCOMPLETE, CITATION_FORMAT, CONTENT_MISS)

# judge() failure templates (mainframe_rag.eval.answers), matched on the
# bucket so quoted details never matter. "trap answered" is read by classify:
# it decides the abstain-row class and is not a model-behavior class.
_FAILURE_CLASS = {
    "explicit refusal on an answer-tier query": REFUSAL_GOLD_PRESENT,
    "empty answer body": CITATION_FORMAT,
    "zero validated citations": CITATION_FORMAT,
    "only inferred citations …": CITATION_FORMAT,
    "missing required substring": CONTENT_MISS,
    "forbidden substring present": CONTENT_MISS,
    "identifier not cited or mentioned": CONTENT_MISS,
}
# A served state of None renders unquoted, so match the raw string.
_STATE_MISMATCH_RE = re.compile(r"^verification state .+ != expected ")

# Error rows: run_query records "request error: <exception class>" or
# "HTTP <status> (<code>)". Only a read timeout and the agent's own deadline
# mean the generation outlived its budget; a connect or pool timeout is an
# unreachable agent, an error.
_TIMEOUT_RE = re.compile(r"^(request error: ReadTimeout|HTTP 504 \(deadline_exceeded\))$")

# Packing diagnostic row categories (scripts/packing_diagnostic.py).
_PACKING_RETRIEVAL = {"not_retrieved", "partial_retrieval"}
_PACKING_LOSS = "packing_loss"
_PACKING_OK = "ok"


class JoinError(ValueError):
    """The inputs cannot be joined into one exactly-once taxonomy."""


@dataclass
class RowClass:
    id: str
    row_class: str
    expected_behavior: str
    verdict: str
    primary: str
    also: list[str] = field(default_factory=list)
    failure_buckets: list[str] = field(default_factory=list)
    served_path: str | None = None
    verification_state: str | None = None
    packing_category: str | None = None
    # kind:key=outcome for every diagnostic target that is not retrieved_packed
    gold_gaps: list[str] = field(default_factory=list)
    # The answer run's own sibling /v1/search pool disagrees with the
    # diagnostic's retrieval about gold presence (corpus or settings drift).
    pool_disagrees: bool = False
    source: str | None = None  # which answer report carried the row


def _model_classes(row: dict[str, Any]) -> list[str]:
    """Model-behavior classes a failing judged row shows, in _MODEL_ORDER."""
    found: set[str] = set()
    for failure in row.get("failures") or []:
        bucket = failure_bucket(str(failure))
        if _STATE_MISMATCH_RE.match(str(failure)):
            state = row.get("verification_state")
            if state == "generation_incomplete":
                found.add(GENERATION_INCOMPLETE)
            elif state == "insufficient_evidence" and row.get("expected_behavior") == "answer":
                found.add(REFUSAL_GOLD_PRESENT)
            else:
                found.add(CITATION_FORMAT)
            continue
        cls = _FAILURE_CLASS.get(bucket)
        if cls is not None:
            found.add(cls)
    return [c for c in _MODEL_ORDER if c in found]


def _gold_gaps(packing: dict[str, Any] | None) -> list[str]:
    if not packing:
        return []
    return [
        f"{t['kind']}:{t['key']}={t['outcome']}"
        for t in packing.get("targets") or []
        if t.get("outcome") != "retrieved_packed"
    ]


def classify(row: dict[str, Any], packing: dict[str, Any] | None) -> RowClass:
    """One answer-eval result row (plus its diagnostic row, if any) to its
    primary class. Pure."""
    row_id = str(row.get("id") or "?")
    behavior = str(row.get("expected_behavior") or "?")
    verdict = str(row.get("verdict") or "?")
    failures = [str(f) for f in row.get("failures") or []]
    out = RowClass(
        id=row_id,
        row_class=row_id.split("-")[0],
        expected_behavior=behavior,
        verdict=verdict,
        primary=PASS,
        failure_buckets=sorted({failure_bucket(f) for f in failures}),
        served_path=row.get("path"),
        verification_state=row.get("verification_state"),
    )
    if behavior == "answer" and packing is not None:
        out.packing_category = packing.get("category")
        out.gold_gaps = _gold_gaps(packing)
        # gold_retrieved is None when the row names no gold doc or the run
        # had no pool: a missing signal never disagrees.
        pool_gold = row.get("gold_retrieved")
        diag_gold = any(h.get("gold_doc") for h in packing.get("hits") or [])
        out.pool_disagrees = pool_gold is not None and bool(pool_gold) != diag_gold

    if verdict == "pass":
        return out
    if verdict != "fail":
        out.primary = TIMEOUT if any(_TIMEOUT_RE.match(f) for f in failures) else ERROR
        return out

    model = _model_classes(row)
    if behavior != "answer":
        trap = any(failure_bucket(f) == "trap answered" for f in failures)
        out.primary = UNSAFE_ANSWER if trap else (model[0] if model else UNATTRIBUTED)
        out.also = [c for c in model if c != out.primary]
        return out

    if row.get("path") == "zero_hits":
        out.primary = RETRIEVAL_MISS
    elif packing is None:
        out.primary = UNATTRIBUTED
    elif out.packing_category in _PACKING_RETRIEVAL:
        out.primary = RETRIEVAL_MISS
    elif out.packing_category == _PACKING_LOSS:
        out.primary = PACKING_LOSS
    elif out.packing_category == _PACKING_OK and model:
        out.primary = model[0]
    else:
        # A diagnostic category this join does not know, or a failure
        # template no class owns: say so rather than guess.
        out.primary = UNATTRIBUTED
    out.also = [c for c in model if c != out.primary]
    return out


def merge_answer_reports(reports: Sequence[tuple[str, dict[str, Any]]]) -> dict[str, tuple[str, dict[str, Any]]]:
    """Row id -> (source label, result row). A row in two reports refuses."""
    merged: dict[str, tuple[str, dict[str, Any]]] = {}
    for label, report in reports:
        results = report.get("results")
        if not isinstance(results, list):
            raise JoinError(f"{label}: no results list")
        for row in results:
            row_id = str(row.get("id") or "")
            if not row_id:
                raise JoinError(f"{label}: a result row has no id")
            if row_id in merged:
                raise JoinError(f"row {row_id} appears in {merged[row_id][0]} and {label}")
            merged[row_id] = (label, row)
    return merged


def packing_rows(report: dict[str, Any], profile: str | None) -> tuple[str, dict[str, dict[str, Any]]]:
    """(profile name, row id -> diagnostic row) for the chosen profile."""
    profiles = report.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise JoinError("packing report has no profiles")
    if profile is None:
        if len(profiles) != 1:
            raise JoinError(f"packing report has profiles {sorted(profiles)}; pass --profile")
        profile = next(iter(profiles))
    if profile not in profiles:
        raise JoinError(f"profile {profile!r} not in packing report {sorted(profiles)}")
    rows = {str(r["id"]): r for r in profiles[profile].get("rows") or []}
    return profile, rows


def build_taxonomy(
    expected_ids: Iterable[str],
    answers: dict[str, tuple[str, dict[str, Any]]],
    packing: dict[str, dict[str, Any]],
) -> list[RowClass]:
    """Every expected row exactly once, in id order. An answer row outside
    the expected set refuses: the run measured something else."""
    expected = sorted(set(expected_ids))
    unexpected = sorted(set(answers) - set(expected))
    if unexpected:
        raise JoinError(f"answer rows outside the expected golden set: {', '.join(unexpected)}")
    out: list[RowClass] = []
    for row_id in expected:
        if row_id not in answers:
            out.append(
                RowClass(
                    id=row_id,
                    row_class=row_id.split("-")[0],
                    expected_behavior="?",
                    verdict="not_run",
                    primary=NOT_RUN,
                )
            )
            continue
        source, row = answers[row_id]
        cls = classify(row, packing.get(row_id))
        cls.source = source
        out.append(cls)
    return out


def summarize(rows: Sequence[RowClass]) -> dict[str, Any]:
    """Primary-class counts overall and per row class, plus passes whose gold
    was not fully packed (the answer survived a retrieval or packing gap)."""
    per_class: dict[str, Counter[str]] = defaultdict(Counter)
    for r in rows:
        per_class[r.row_class][r.primary] += 1
    total = Counter(r.primary for r in rows)
    return {
        "rows": len(rows),
        "classes": {c: total[c] for c in CLASSES if total[c]},
        "by_row_class": {
            name: {c: counts[c] for c in CLASSES if counts[c]}
            for name, counts in sorted(per_class.items())
        },
        "passes_with_gold_gaps": sorted(r.id for r in rows if r.primary == PASS and r.gold_gaps),
        "pool_disagreements": sorted(r.id for r in rows if r.pool_disagrees),
    }


def render_markdown(rows: Sequence[RowClass], summary: dict[str, Any]) -> str:
    present = [c for c in CLASSES if c in summary["classes"]]
    lines = [
        "| Row class | " + " | ".join(present) + " |",
        "|---|" + "---:|" * len(present),
    ]
    for name, counts in summary["by_row_class"].items():
        lines.append(f"| {name} | " + " | ".join(str(counts.get(c, 0)) for c in present) + " |")
    lines.append("| all | " + " | ".join(str(summary["classes"][c]) for c in present) + " |")
    lines += [
        "",
        "| Row | Behavior | Primary | Also | Failures | Packing | Gold gaps | Pool disagrees |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if r.primary == PASS:
            continue
        lines.append(
            f"| {r.id} | {r.expected_behavior} | {r.primary} | {', '.join(r.also) or '-'} "
            f"| {'; '.join(r.failure_buckets) or '-'} | {r.packing_category or '-'} "
            f"| {'; '.join(r.gold_gaps) or '-'} | {'yes' if r.pool_disagrees else '-'} |"
        )
    for key, label in (
        ("passes_with_gold_gaps", "Passes with gold gaps"),
        ("pool_disagreements", "Pool disagreements"),
    ):
        lines += ["", f"{label}: {', '.join(summary[key]) or 'none'}"]
    return "\n".join(lines) + "\n"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise JoinError(f"{path}: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise JoinError(f"{path}: not a JSON object")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--answers", type=Path, action="append", required=True,
                        help="answer-eval JSON report (repeat for a split run)")
    parser.add_argument("--packing", type=Path, required=True, help="packing diagnostic JSON report")
    parser.add_argument("--profile", default=None, help="packing profile (required if several)")
    parser.add_argument("--golden", type=Path, action="append", default=None,
                        help="expected row set (repeatable; default evals/golden.jsonl)")
    parser.add_argument("--out", type=Path, default=None, help="write the JSON report here")
    parser.add_argument("--markdown", type=Path, default=None, help="write the tables here")
    args = parser.parse_args(argv)

    from mainframe_rag.eval.datasets import DatasetError, load_golden

    try:
        golden_paths = args.golden or [_REPO / "evals" / "golden.jsonl"]
        expected = [e.id or "" for p in golden_paths for e in load_golden(p)]
        if "" in expected or len(set(expected)) != len(expected):
            raise JoinError("expected golden set has a missing or duplicate id")
        answer_reports = [(str(p), _load_json(p)) for p in args.answers]
        packing_report = _load_json(args.packing)
        profile, diag = packing_rows(packing_report, args.profile)
        rows = build_taxonomy(expected, merge_answer_reports(answer_reports), diag)
    except (DatasetError, JoinError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    summary = summarize(rows)
    report = {
        "inputs": {
            "answers": [
                {"path": label, "params": rep.get("params"), "representation": rep.get("representation")}
                for label, rep in answer_reports
            ],
            "packing": {
                "path": str(args.packing),
                "profile": profile,
                "representation": packing_report.get("representation"),
                "embed_model": packing_report.get("embed_model"),
                "dense_dim": packing_report.get("dense_dim"),
                "errors": packing_report.get("errors"),
            },
            "golden": [str(p) for p in golden_paths],
        },
        "summary": summary,
        "rows": [asdict(r) for r in rows],
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    table = render_markdown(rows, summary)
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(table, encoding="utf-8")
    print(table)
    representation = (packing_report.get("representation") or {}).get("outcome")
    if representation not in (None, "compatible"):
        print(f"warn: packing corpus representation is {representation!r}, not current", file=sys.stderr)
    return 1 if summary["classes"].get(NOT_RUN) else 0


if __name__ == "__main__":
    sys.exit(main())
