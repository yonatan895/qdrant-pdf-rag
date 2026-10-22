"""Offline feedback analysis report (issue #805 PR 2, ADR-0005, decision D5).

`python -m mainframe_rag.feedback.report --in FILE [--out FILE|-] [--format md|json]`
reads one export file (canonical JSONL v1) and prints counts and bounded labels.
It never opens the live store and calls no model. The input goes through the
same parser as `exchange import` (`exchange.parse_export`), so it refuses
exactly what import refuses, with the same fixed codes.

Only allow-listed telemetry keys are read. Every label is bucketed against a
closed vocabulary (anything else is `other`, an absent value `unknown`), so a
new telemetry key, a hostile value, or any question, answer, comment, script or
citation text can never reach the output. The only per-record values printed
are the 12-hex request ids of `bad` ratings (the trace join key, ADR-0005).

This is a monitoring view, not an evaluation: feedback rows have seen system
output, so no figure here is a pass rate, a gate or a release input (#806 D6).
Output is deterministic for a given file except the two flags that compare the
export (and the oldest queue entry) with the clock at run time.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mainframe_rag.config import REASONING_EFFORTS
from mainframe_rag.feedback import exchange
from mainframe_rag.feedback.exchange import EXIT_REFUSED, Refused
from mainframe_rag.feedback.store import ENDPOINTS, RATINGS, REASONS

REPORT_VERSION = 1
LOW_N = 5  # a cell with fewer rated answers is flagged, never trusted
TOP_N = 5
QUEUE_LIMIT = 200
TRACE_WINDOW_DAYS = 14  # observability.md: traces expire after this
MAX_MODEL_LABELS = 8
_MIN_DATE = datetime(2000, 1, 1, tzinfo=UTC).timestamp()

# Closed vocabularies for telemetry labels. Pinned to their owners by tests.
VERIFICATION_STATES = ("insufficient_evidence", "unverified_draft", "generation_incomplete",
                       "accepted")
QUERY_KINDS = ("identifier", "nl")
COMPLEXITIES = ("simple", "complex")
FINISH_REASONS = ("stop", "length", "content_filter", "tool_calls", "function_call")
# Identity labels (model, release, rules version) are operator configuration,
# not a vocabulary: accept only identifier-shaped tokens (no spaces, no
# slashes), keep the most frequent few, bucket the rest.
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_HEX_RE = re.compile(r"[0-9a-f]{6,40}")
_UNKNOWN = "\0unknown"
_NUMERIC_KEYS = ("elapsed_ms", "ttft_ms", "prompt_tokens", "completion_tokens", "total_tokens")


def _label(value: Any, vocab: Iterable[str]) -> str:
    if value is None:
        return "unknown"
    return value if isinstance(value, str) and value in vocab else "other"


def _tri(value: Any) -> str:
    return "unknown" if not isinstance(value, bool) else ("true" if value else "false")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _date(ts: float) -> str:
    try:
        if ts < _MIN_DATE:
            return "other"
        return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "other"


def _share(bad: int, n: int) -> float:
    return round(bad / n, 4) if n else 0.0


def _sorted(counter: Counter[str]) -> dict[str, int]:
    return {k: counter[k] for k in sorted(counter)}


def _identity_buckets(raw: Counter[str], pattern: re.Pattern[str]) -> dict[str, int]:
    """Counts per identity token: well-formed tokens ranked by count then name,
    beyond MAX_MODEL_LABELS or malformed -> `other`; absent -> `unknown`."""
    unknown = raw.get(_UNKNOWN, 0)
    wellformed = {k: c for k, c in raw.items() if k != _UNKNOWN and pattern.fullmatch(k)}
    keep = sorted(wellformed, key=lambda k: (-wellformed[k], k))[:MAX_MODEL_LABELS]
    out = {k: wellformed[k] for k in keep}
    other = sum(raw.values()) - unknown - sum(out.values())
    for label, n in (("other", other), ("unknown", unknown)):
        if n:
            out[label] = n
    return {k: out[k] for k in sorted(out)}


def _identity_raw(value: Any) -> str:
    if value is None:
        return _UNKNOWN
    return value if isinstance(value, str) else ""


def _cell(rows: list[bool]) -> dict[str, Any]:
    n, bad = len(rows), sum(rows)
    return {"n": n, "bad": bad, "bad_share": _share(bad, n), "low_n": n < LOW_N}


def _cells(groups: dict[str, list[bool]]) -> dict[str, Any]:
    return {k: _cell(groups[k]) for k in sorted(groups)}


def _quantiles(values: list[float]) -> dict[str, Any]:
    v = sorted(values)
    n = len(v)
    if not n:
        return {"n": 0, "median": None, "p95": None, "low_n": True}
    mid = v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2
    return {"n": n, "median": round(mid, 1), "p95": round(v[max(math.ceil(0.95 * n) - 1, 0)], 1),
            "low_n": n < LOW_N}


def build_report(answers: list[dict[str, Any]], ratings: list[dict[str, Any]], *,
                 exported_at: float, retention_days: int, now: float) -> dict[str, Any]:
    by_id = {a["request_id"]: a for a in answers}
    joined = [(r, by_id[r["request_id"]]) for r in ratings if r["request_id"] in by_id]
    tele = [(r, a["telemetry"]) for r, a in joined]

    def count(fn: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], str]) -> dict[str, int]:
        return _sorted(Counter(fn(r, a, a["telemetry"]) for r, a in joined))

    reasons: Counter[str] = Counter()
    for r, _ in joined:
        reasons.update(_label(x, REASONS) for x in r["reasons"])
    models = {k: Counter(_identity_raw(t.get(k)) for _, t in tele)
              for k in ("llm_model", "embed_model")}
    ratings_by: dict[str, Any] = {
        "rating": count(lambda r, a, t: _label(r["rating"], RATINGS)),
        "reason": _sorted(reasons),
        "endpoint": count(lambda r, a, t: _label(a["endpoint"], ENDPOINTS)),
        "verification_state": count(lambda r, a, t: _label(t.get("verification_state"),
                                                           VERIFICATION_STATES)),
        "finish_reason": count(lambda r, a, t: _label(t.get("finish_reason"), FINISH_REASONS)),
        "query_kind": count(lambda r, a, t: _label(t.get("query_kind"), QUERY_KINDS)),
        "query_complexity": count(lambda r, a, t: _label(t.get("query_complexity"), COMPLEXITIES)),
        "reasoning_effort": count(lambda r, a, t: _label(t.get("reasoning_effort"),
                                                         REASONING_EFFORTS)),
        "model_profile": {
            "llm_model": _identity_buckets(models["llm_model"], _MODEL_RE),
            "embed_model": _identity_buckets(models["embed_model"], _MODEL_RE),
            "release": _identity_buckets(Counter(_identity_raw(t.get("release")) for _, t in tele),
                                         _HEX_RE),
            "extraction_rules_version": _identity_buckets(
                Counter(_identity_raw(t.get("extraction_rules_version")) for _, t in tele),
                _HEX_RE),
        },
        "date": _sorted(Counter(_date(r["created_at"]) for r, _ in joined)),
    }

    def groups(fn: Callable[[dict[str, Any]], str]) -> dict[str, list[bool]]:
        out: dict[str, list[bool]] = {}
        for r, a in joined:
            out.setdefault(fn(a["telemetry"]), []).append(r["rating"] == "bad")
        return out

    def omitted(t: dict[str, Any]) -> str:
        n = _number(t.get("units_omitted"))
        return "unknown" if n is None else ("gt0" if n > 0 else "0")

    def evidence(t: dict[str, Any]) -> str:
        n = _number(t.get("evidence"))
        return "unknown" if n is None else ("zero" if n <= 0 else "nonzero")

    clusters = {
        "query_kind_x_verification_state": _cells(groups(
            lambda t: f"{_label(t.get('query_kind'), QUERY_KINDS)}/"
                      f"{_label(t.get('verification_state'), VERIFICATION_STATES)}")),
        "reasoning_effort": _cells(groups(lambda t: _label(t.get("reasoning_effort"),
                                                           REASONING_EFFORTS))),
        "units_omitted": _cells(groups(omitted)),
        "budget_verified": _cells(groups(lambda t: _tri(t.get("budget_verified")))),
        "evidence": _cells(groups(evidence)),
        "has_script": _cells(groups(lambda t: _tri(t.get("has_script")))),
    }
    bad = [(r, a) for r, a in joined if r["rating"] == "bad"]
    top_codes: dict[str, Any] = {
        "reason": [[k, v] for k, v in sorted(Counter(_label(x, REASONS) for r, _ in bad
                   for x in r["reasons"]).items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_N]],
        "verification_state": [[k, v] for k, v in sorted(Counter(_label(
            a["telemetry"].get("verification_state"), VERIFICATION_STATES) for _, a in bad
        ).items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_N]],
    }
    numeric: dict[str, Any] = {}
    for key in _NUMERIC_KEYS:
        numeric[key] = {
            side: _quantiles([v for r, a in joined if r["rating"] == side
                              and (v := _number(a["telemetry"].get(key))) is not None])
            for side in ("bad", "good")}
    queue_rows = sorted(bad, key=lambda ra: (-ra[0]["created_at"], ra[0]["request_id"]))
    queue = [{"request_id": r["request_id"], "date": _date(r["created_at"]),
              "reasons": sorted(_label(x, REASONS) for x in r["reasons"]),
              "endpoint": _label(a["endpoint"], ENDPOINTS),
              "verification_state": _label(a["telemetry"].get("verification_state"),
                                           VERIFICATION_STATES),
              "trace_expected": now - r["created_at"] <= TRACE_WINDOW_DAYS * 86400}
             for r, a in queue_rows[:QUEUE_LIMIT]]
    stamps = [a["created_at"] for a in answers]
    rated_ids = {r["request_id"] for r, _ in joined}
    return {
        "report_version": REPORT_VERSION,
        "note": "Counts and bounded labels only. A monitoring view of user feedback, not an "
                "evaluation: no pass rate, no gate, never a release or eval input (#806 D6).",
        "export": {"exported_date": _date(exported_at), "retention_days": retention_days,
                   "stale": now - exported_at > retention_days * 86400},
        "coverage": {
            "answers": len(answers), "ratings": len(ratings),
            "rated_answers": len(rated_ids),
            "rated_share": _share(len(rated_ids), len(answers)),
            "first_date": _date(min(stamps)) if stamps else None,
            "last_date": _date(max(stamps)) if stamps else None,
            "low_n": len(joined) < LOW_N},
        "ratings_by": ratings_by,
        "failure_clusters": clusters,
        "top_failure_codes": top_codes,
        "numeric_bad_vs_good": numeric,
        "review_queue": {"total_bad": len(bad), "listed": len(queue),
                         "truncated": len(bad) - len(queue), "items": queue},
    }


def _md_counts(title: str, d: dict[str, Any]) -> list[str]:
    rows = [f"| {k} | {v} |" for k, v in d.items()] or ["| (none) | 0 |"]
    return [f"### {title}", "", "| label | n |", "|---|---|", *rows, ""]


def render_markdown(rep: dict[str, Any]) -> str:
    cov, exp = rep["coverage"], rep["export"]
    out = ["# Feedback report", "", f"_{rep['note']}_", "",
           f"Export: {exp['exported_date']} (retention {exp['retention_days']} d)"
           + (" - STALE: older than its retention window, delete it" if exp["stale"] else ""),
           "", "## Coverage", "",
           f"- answers: {cov['answers']}", f"- ratings: {cov['ratings']}",
           f"- rated answers: {cov['rated_answers']} (share {cov['rated_share']})",
           f"- first / last answer date: {cov['first_date']} / {cov['last_date']}",
           f"- low n: {str(cov['low_n']).lower()}", "", "## Ratings by label", ""]
    for name, d in rep["ratings_by"].items():
        if name == "model_profile":
            for sub, dd in d.items():
                out += _md_counts(f"model_profile.{sub}", dd)
        else:
            out += _md_counts(name, d)
    out += [f"## Failure clusters (bad share per cell; low_n marks n < {LOW_N}; no claim)", ""]
    for name, cells in rep["failure_clusters"].items():
        out += [f"### {name}", "", "| cell | n | bad | bad_share | low_n |", "|---|---|---|---|---|",
                *[f"| {k} | {c['n']} | {c['bad']} | {c['bad_share']} | {str(c['low_n']).lower()} |"
                  for k, c in cells.items()], ""]
    out += ["## Top failure codes (bad ratings)", ""]
    for name, pairs in rep["top_failure_codes"].items():
        out += [f"- {name}: " + (", ".join(f"{k} ({v})" for k, v in pairs) or "(none)")]
    out += ["", "## Numeric telemetry, bad vs good", "",
            "| key | side | n | median | p95 | low_n |", "|---|---|---|---|---|---|"]
    for key, sides in rep["numeric_bad_vs_good"].items():
        out += [f"| {key} | {s} | {q['n']} | {q['median']} | {q['p95']} | "
                f"{str(q['low_n']).lower()} |" for s, q in sides.items()]
    q = rep["review_queue"]
    out += ["", "## Review queue (bad ratings, newest first)", "",
            f"bad: {q['total_bad']}, listed: {q['listed']}, not listed: {q['truncated']}", "",
            "| request_id | date | reasons | endpoint | verification_state | trace_expected |",
            "|---|---|---|---|---|---|"]
    out += [f"| {i['request_id']} | {i['date']} | {','.join(i['reasons'])} | {i['endpoint']} | "
            f"{i['verification_state']} | {str(i['trace_expected']).lower()} |"
            for i in q["items"]]
    return "\n".join(out) + "\n"


def render(rep: dict[str, Any], fmt: str) -> bytes:
    if fmt == "json":
        return (json.dumps(rep, sort_keys=True, indent=2, ensure_ascii=True,
                           allow_nan=False) + "\n").encode("ascii")
    return render_markdown(rep).encode("ascii")


def run_report(src: str, out: str, fmt: str, *, clock: Callable[[], float]) -> None:
    if out != "-" and exchange._inside_git_worktree(Path(out)):
        raise Refused("output_in_git_worktree")
    data = exchange._read_input(src)
    now = clock()
    answers, ratings = exchange.parse_export(data, now=now)
    known = {a["request_id"] for a in answers}
    if any(r["request_id"] not in known for r in ratings):
        # import into an empty store refuses this too; a report has no target to resolve it
        raise Refused("dangling_rating")
    header = json.loads(data.split(b"\n", 1)[0])  # validated by parse_export
    rep = build_report(answers, ratings, exported_at=header["exported_at"],
                       retention_days=header["retention_days"], now=now)
    payload = render(rep, fmt)
    if out == "-":
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
    else:
        exchange._write_private(Path(out), payload)


def main(argv: list[str] | None = None, *, clock: Callable[[], float] = time.time) -> int:
    parser = argparse.ArgumentParser(prog="python -m mainframe_rag.feedback.report",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--in", dest="src", required=True, help="export file or - for stdin")
    parser.add_argument("--out", default="-", help="new file (mode 0600) or - for stdout")
    parser.add_argument("--format", choices=("md", "json"), default="md")
    args = parser.parse_args(argv)
    try:
        run_report(args.src, args.out, args.format, clock=clock)
    except Refused as e:
        print(f"feedback-report: refused: {e.code}", file=sys.stderr)
        return EXIT_REFUSED
    return 0


if __name__ == "__main__":
    sys.exit(main())
