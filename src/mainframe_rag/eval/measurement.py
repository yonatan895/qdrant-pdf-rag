"""Measurement protocol helpers for the answer eval (issue #1195).

Two metric groups, reported separately (docs/metric-contract.md):

  hardware-independent  pass rate per class, truncation rate, prompt /
                        completion / reasoning tokens per answer. Decisions
                        on quality and budgets use this group only.
  hardware-dependent    time to first token, decode tokens per second,
                        end-to-end time. Valid for the measured host only.

Every value here is a count, a duration or an identifier. No question,
answer or reasoning text enters a field this module produces: the SSE reader
keeps only the terminal `final` object (which the caller already receives on
the JSON path) and drops token deltas after noting when the first arrived.

Sources, none of which changes the agent's response contract:
  - `/v1/answer` SSE `final`: `finish_reason`, `ttft_ms`, `usage`.
  - `/v1/answer` JSON `Server-Timing`: `llm` and, with `LLM_STREAM`, `ttft`.
  - the in-process answer log line (unchanged, in-process mode only).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

# One SSE answer is bounded by the agent's own response limits; this is only a
# guard against an endpoint that never terminates a frame.
MAX_STREAM_CHARS = 16 * 1024 * 1024

STREAM_FAILED = "stream error"
STREAM_INCOMPLETE = "stream ended without final"
STREAM_MALFORMED = "malformed answer stream"
STREAM_OVERSIZE = "answer stream exceeded the size bound"

_CODE_RE = re.compile(r"[a-z_]{1,64}")
_TIMING_RE = re.compile(r"^\s*([A-Za-z_]+)\s*;\s*dur=(\d{1,9})\s*$")

TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "reasoning_tokens")
# Scalars whose run-to-run spread a sweep reads (dotted paths into metrics).
SPREAD_PATHS = (
    "answer_pass_rate",
    "abstain_pass_rate",
    "measurement.hardware_independent.truncation.rate",
    "measurement.hardware_independent.generation_incomplete.rate",
    "measurement.hardware_independent.tokens.prompt_tokens.p50",
    "measurement.hardware_independent.tokens.prompt_tokens.p95",
    "measurement.hardware_independent.tokens.completion_tokens.p50",
    "measurement.hardware_independent.tokens.completion_tokens.p95",
    "measurement.hardware_independent.tokens.reasoning_tokens.p50",
    "measurement.hardware_independent.tokens.reasoning_tokens.p95",
    "measurement.hardware_dependent.ttft_ms.p50",
    "measurement.hardware_dependent.ttft_ms.p95",
    "measurement.hardware_dependent.client_first_token_ms.p50",
    "measurement.hardware_dependent.client_first_token_ms.p95",
    "measurement.hardware_dependent.elapsed_ms.p50",
    "measurement.hardware_dependent.elapsed_ms.p95",
    "measurement.hardware_dependent.decode_tps.p50",
    "measurement.hardware_dependent.decode_tps.p95",
)


def is_count(value: Any) -> bool:
    """A non-negative integer; bool is not a count."""
    return type(value) is int and value >= 0


def percentile(values: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile (no interpolation): the smallest observed
    value with at least p percent of the sample at or below it. None for an
    empty sample: a missing signal is never a zero."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def distribution(values: Iterable[float | None]) -> dict[str, Any]:
    """n, p50, p95, mean and max over the known values."""
    known = [v for v in values if v is not None]
    if not known:
        return {"n": 0, "p50": None, "p95": None, "mean": None, "max": None}
    return {
        "n": len(known),
        "p50": percentile(known, 50),
        "p95": percentile(known, 95),
        "mean": round(sum(known) / len(known), 3),
        "max": max(known),
    }


def parse_server_timing(header: Any) -> dict[str, int]:
    """`llm_ms` / `ttft_ms` from the agent's `Server-Timing` header. Anything
    unparseable is absent, never a zero."""
    found: dict[str, int] = {}
    if not isinstance(header, str):
        return found
    for part in header.split(","):
        match = _TIMING_RE.match(part)
        if match and match.group(1) in ("llm", "ttft"):
            found[match.group(1) + "_ms"] = int(match.group(2))
    return found


def decode_tps(
    completion_tokens: Any, reasoning_tokens: Any, span_ms: Any
) -> float | None:
    """Visible decode rate: tokens emitted after reasoning, over the time from
    the first visible token to the end of generation. None unless every input
    is known and the span is positive. When the upstream reports no reasoning
    split, thinking tokens stay in the numerator and the rate overstates;
    `reasoning_split_rows` in the summary says how many rows had the split."""
    if not is_count(completion_tokens) or not is_count(reasoning_tokens):
        return None
    if type(span_ms) is not int or span_ms <= 0:
        return None
    visible = completion_tokens - reasoning_tokens
    if visible <= 0:
        return None
    return round(visible * 1000 / span_ms, 2)


@dataclass(frozen=True)
class StreamOutcome:
    """One consumed `/v1/answer` stream. Exactly one of `final`, `failure`
    and `response` is set: the validated terminal object, a fixed failure
    label, or the non-200 response for the caller's envelope reader."""

    final: dict[str, Any] | None = None
    failure: str | None = None
    response: Any = None
    first_token_ms: int | None = None


def _final_counts(final: dict[str, Any]) -> bool:
    usage = final.get("usage")
    ttft = final.get("ttft_ms")
    finish = final.get("finish_reason")
    return (
        isinstance(finish, str) and bool(finish)
        and isinstance(usage, dict)
        and all(is_count(usage.get(key)) for key in (*TOKEN_FIELDS, "total_tokens"))
        # The key is required even when its value is null: the zero-hits final
        # legitimately carries `ttft_ms: null`, but a final without the key
        # would make `stream_signals` raise instead of failing the row closed.
        and "ttft_ms" in final
        and (ttft is None or is_count(ttft))
    )


def read_answer_stream(
    client: Any, body: dict[str, Any], clock: Callable[[], float] = time.monotonic
) -> StreamOutcome:
    """POST `/v1/answer` with `stream: true` and consume the SSE response.

    Token deltas are not kept: only the arrival time of the first one is
    recorded. The stream contract (docs/agent.md section 3) is enforced
    closed: an `error` event, end of stream without `final`, or a `final`
    without valid counts is a failure, never a judged answer."""
    started = clock()
    first_token_ms: int | None = None
    with client.stream("POST", "/v1/answer", json=body) as response:
        if response.status_code != 200:
            response.read()
            return StreamOutcome(response=response)
        event, data, seen = "", [], 0
        for line in response.iter_lines():
            seen += len(line) + 1
            if seen > MAX_STREAM_CHARS:
                return StreamOutcome(failure=STREAM_OVERSIZE, first_token_ms=first_token_ms)
            if line.startswith("event:"):
                event = line[6:].strip()
                continue
            if line.startswith("data:"):
                if event != "token":
                    data.append(line[5:].lstrip())
                continue
            if line:
                continue
            name, raw = event, "\n".join(data)
            event, data = "", []
            if name == "token":
                if first_token_ms is None:
                    first_token_ms = int((clock() - started) * 1000)
                continue
            if name not in ("final", "error"):
                continue
            try:
                payload = json.loads(raw)
            except ValueError:
                return StreamOutcome(failure=STREAM_MALFORMED, first_token_ms=first_token_ms)
            if not isinstance(payload, dict):
                return StreamOutcome(failure=STREAM_MALFORMED, first_token_ms=first_token_ms)
            if name == "error":
                code = payload.get("code")
                label = code if isinstance(code, str) and _CODE_RE.fullmatch(code) else "?"
                return StreamOutcome(
                    failure=f"{STREAM_FAILED} ({label})", first_token_ms=first_token_ms
                )
            if not _final_counts(payload):
                return StreamOutcome(failure=STREAM_MALFORMED, first_token_ms=first_token_ms)
            return StreamOutcome(final=payload, first_token_ms=first_token_ms)
    return StreamOutcome(failure=STREAM_INCOMPLETE, first_token_ms=first_token_ms)


def stream_signals(final: dict[str, Any]) -> dict[str, Any]:
    """Count-only row fields from a validated `final`."""
    usage = final["usage"]
    return {
        "finish_reason": final["finish_reason"],
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"],
        "reasoning_tokens": usage["reasoning_tokens"],
        "total_tokens": usage["total_tokens"],
        "ttft_ms": final["ttft_ms"],
    }


def _rate(count: int, total: int) -> float | None:
    return round(count / total, 4) if total else None


def measurement_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """The two metric groups over judged rows. Pure.

    Token and timing distributions cover rows that made a model call
    (`path == "llm"`): the zero-hits short-circuit has no generation, so its
    zero usage and near-zero latency would only dilute them. Each
    distribution carries its own `n`; a row without the signal is left out,
    never counted as zero."""
    judged = [r for r in results if r.get("verdict") in ("pass", "fail")]
    generated = [r for r in judged if r.get("path") == "llm"]
    by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in judged:
        by_class[str(row.get("query_class"))].append(row)
    finished = [r for r in generated if isinstance(r.get("finish_reason"), str)]
    truncated = sum(1 for r in finished if r["finish_reason"] != "stop")
    incomplete = sum(1 for r in judged if r.get("verification_state") == "generation_incomplete")
    with_state = sum(1 for r in judged if r.get("verification_state") is not None)
    return {
        "hardware_independent": {
            "pass_rate_by_class": {
                cls: {
                    "n": len(rows),
                    "pass": sum(1 for r in rows if r["verdict"] == "pass"),
                    "rate": _rate(sum(1 for r in rows if r["verdict"] == "pass"), len(rows)),
                }
                for cls, rows in sorted(by_class.items())
            },
            # Finish-reason basis: exact, needs the SSE final or the
            # in-process log join.
            "truncation": {
                "n": len(finished), "truncated": truncated,
                "rate": _rate(truncated, len(finished)),
            },
            # Verification-state basis: available on every transport, but an
            # upper bound (it also counts a finished answer with no usable
            # body, docs/agent.md verification states).
            "generation_incomplete": {
                "n": with_state, "count": incomplete, "rate": _rate(incomplete, with_state),
            },
            "tokens": {
                field: distribution(
                    r.get(field) if is_count(r.get(field)) else None for r in generated
                )
                for field in TOKEN_FIELDS
            },
            "reasoning_split_rows": sum(
                1 for r in generated if is_count(r.get("reasoning_tokens")) and r["reasoning_tokens"] > 0
            ),
        },
        "hardware_dependent": {
            field: distribution(
                r.get(field) if is_count(r.get(field)) else None for r in generated
            )
            for field in ("ttft_ms", "client_first_token_ms", "llm_ms", "elapsed_ms")
        } | {
            "decode_tps": distribution(
                r.get("decode_tps") if isinstance(r.get("decode_tps"), float) else None
                for r in generated
            ),
        },
    }


def _lookup(metrics: dict[str, Any], path: str) -> Any:
    value: Any = metrics
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def repeat_spread(
    per_repeat: list[dict[str, Any]], results: list[dict[str, Any]]
) -> dict[str, Any]:
    """Run-to-run spread over N repeats of one sample. Pure.

    Per metric: the per-repeat values, min, max, mean and range (max - min).
    A difference between two arms smaller than the range of either arm is
    inside the noise this sample can resolve. `verdict_flips` lists the entry
    ids judged differently across repeats (ids only)."""
    paths = list(SPREAD_PATHS)
    classes = sorted({
        cls for metrics in per_repeat
        for cls in (_lookup(metrics, "measurement.hardware_independent.pass_rate_by_class") or {})
    })
    spread: dict[str, Any] = {}
    for path in paths:
        spread[path] = _spread([_lookup(metrics, path) for metrics in per_repeat])
    for cls in classes:
        spread[f"pass_rate_by_class.{cls}"] = _spread([
            ((_lookup(metrics, "measurement.hardware_independent.pass_rate_by_class") or {})
             .get(cls) or {}).get("rate")
            for metrics in per_repeat
        ])
    verdicts: dict[str, set[str]] = defaultdict(set)
    for row in results:
        verdicts[str(row.get("id"))].add(str(row.get("verdict")))
    return {
        "n": len(per_repeat),
        "spread": spread,
        "verdict_flips": sorted(key for key, seen in verdicts.items() if len(seen) > 1),
    }


def _spread(values: list[Any]) -> dict[str, Any]:
    known = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not known:
        return {"values": values, "min": None, "max": None, "mean": None, "range": None}
    return {
        "values": values,
        "min": min(known),
        "max": max(known),
        "mean": round(sum(known) / len(known), 4),
        "range": round(max(known) - min(known), 4),
    }


def sample_identity(sample: list[dict[str, Any]]) -> dict[str, Any]:
    """Order-sensitive identity of the rows a run covers, so two arms of a
    sweep can prove they ran the same subset in the same order. Ids only."""
    ids = [str(entry["id"]) for entry in sample]
    return {
        "n": len(ids),
        "classes": len({str(entry["query_class"]) for entry in sample}),
        "ids_sha256": hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest(),
    }
