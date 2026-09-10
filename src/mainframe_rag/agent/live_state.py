"""Deterministic live-state routing + fetch orchestration (ADR-0002, phase 2).

classify_live_need routes manual/live/hybrid with deterministic signals;
trap queries always route manual (injection must not steer mainframe
reads). fetch_live executes at most 2 allowlisted tool calls with byte
caps, audit-logs ids/counts/bytes (never content), and degrades to a
manuals-only marker on any failure. Phase 2 stops here: nothing calls
fetch_live from an endpoint yet (prompt wiring is phase 3).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from opentelemetry import trace

from mainframe_rag.agent.zowe_mcp import ZoweMCPError
from mainframe_rag.ports import ZoweMCP
from mainframe_rag.retrieve.screen import screen_query

if TYPE_CHECKING:
    from mainframe_rag.config import Settings

log = logging.getLogger("agent")

# Proxy tracer: upgrades via the global provider like every other module.
tracer = trace.get_tracer("mainframe-rag.agent")

_JOB_ID_RE = re.compile(r"\bJOB\d{5}\b", re.IGNORECASE)
_JES_WORD_RE = re.compile(r"\bJES\b", re.IGNORECASE)
_DATASET_RE = re.compile(r"'([A-Z0-9.@$#()]{2,60})'", re.IGNORECASE)
# Bare multi-dot uppercase tokens (USER.TEST.DATA): quoted names are the
# strong signal, but operators also type them bare. Two dots minimum —
# single-dot tokens ("V2R5", "1.2") are version noise, not datasets.
_BARE_DATASET_RE = re.compile(r"\b[A-Z$#@][A-Z0-9$#@]*\.[A-Z0-9$#@.]+\.[A-Z0-9$#@.]+\b")
_USS_PATH_RE = re.compile(r"(?<![\w/])(/[\w.][\w./-]{1,120})")
_SPOOL_ID_RE = re.compile(r"\bspool(?:\s+file)?\s+(\d{1,3})\b|\bfile\s+(\d{1,3})\b", re.IGNORECASE)
_LIVE_NOUNS = (
    "spool", "abend", "job failed", "job ended", "job failure",
    "last night", "this morning", "still running", "return code was",
    "what happened to",
)
_HYBRID_VERBS = ("failed", "fails", "abend", "dump", "ended", "rejected", "hang")

# Hard ceiling per fetch plan (ADR-0002: bounded calls).
MAX_TOOL_CALLS = 2


def classify_live_need(query: str) -> str:
    """Route manual/live/hybrid. Conservative by design: unknown phrasing
    stays manual (an MCP call costs mainframe I/O + latency), and traps
    never leave the manuals path. Identifiers alone do not imply live —
    only job/dataset/USS signals or past-tense failure language do."""
    if screen_query(query) == "trap":
        return "manual"
    lowered = query.lower()
    has_job = bool(_JOB_ID_RE.search(query))
    has_dataset = bool(_DATASET_RE.search(query) or _BARE_DATASET_RE.search(query))
    has_uss = bool(_USS_PATH_RE.search(query))
    has_spool_word = "spool" in lowered or bool(_JES_WORD_RE.search(query))
    live_signal = (
        has_job
        or has_dataset
        or has_uss
        or has_spool_word
        or any(noun in lowered for noun in _LIVE_NOUNS)
    )
    if not live_signal:
        return "manual"
    has_failure_verb = any(verb in lowered for verb in _HYBRID_VERBS)
    has_manual_anchor = has_failure_verb or "what does" in lowered or "mean" in lowered
    if has_manual_anchor and (has_job or has_spool_word or has_dataset):
        return "hybrid"
    return "live"


@dataclass(frozen=True, slots=True)
class LiveResult:
    """One fetch plan outcome. texts are capped excerpts (untrusted data —
    screening happens at the phase-3 call site, never here). degraded names
    the fallback path (dry_run/timeout/tool_error/...) or None when live."""

    route: str
    texts: tuple[str, ...] = ()
    truncated: bool = False
    degraded: str | None = None
    tools_used: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


def _truncate(text: str, max_bytes: int) -> tuple[str, bool]:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text, False
    cut = raw[:max_bytes].decode("utf-8", errors="ignore")
    return cut + "\n[... truncated: live byte cap ...]", True


def _plan_calls(query: str) -> list[tuple[str, dict[str, str]]]:
    """Deterministic tool plan (max 2, precedence: status → spool →
    dataset → USS). A job failure without an explicit spool id assumes
    spool file 2 (job log, highest single-file value) and says so in the
    result notes — never silently."""
    plan: list[tuple[str, dict[str, str]]] = []
    job_match = _JOB_ID_RE.search(query)
    dataset_match = _DATASET_RE.search(query)
    if dataset_match is not None:
        dataset_name: str | None = dataset_match.group(1).upper()
    else:
        bare_match = _BARE_DATASET_RE.search(query)
        dataset_name = bare_match.group(0).upper() if bare_match is not None else None
    uss_match = _USS_PATH_RE.search(query)
    lowered = query.lower()
    wants_spool = "spool" in lowered or bool(job_match)

    if job_match or "jes" in lowered:
        args: dict[str, str] = {}
        if job_match:
            args["job_id"] = job_match.group(0).upper()
        plan.append(("job_status", args))
    if wants_spool and len(plan) < MAX_TOOL_CALLS:
        spool_match = _SPOOL_ID_RE.search(query)
        spool_id = spool_match.group(1) or spool_match.group(2) if spool_match else None
        if job_match:
            plan.append(("jes_spool_read", {"job_id": job_match.group(0).upper(), "spool_id": spool_id or "2"}))
    if dataset_name is not None and len(plan) < MAX_TOOL_CALLS:
        plan.append(("dataset_read", {"dataset": dataset_name}))
    if uss_match and len(plan) < MAX_TOOL_CALLS:
        plan.append(("uss_read", {"path": uss_match.group(1)}))
    return plan[:MAX_TOOL_CALLS]


def _audit(request_id: str, route: str, tools: list[str], byte_count: int, degraded: str | None) -> None:
    log.info(
        json.dumps(
            {
                "request_id": request_id,
                "action": "live_fetch",
                "route": route,
                "tools": tools,
                "bytes": byte_count,
                "degraded": degraded,
            }
        )
    )


def fetch_live(
    settings: Settings,
    zowe_mcp: ZoweMCP | None,
    request_id: str,
    query: str,
    route: str,
) -> LiveResult:
    """Execute the fetch plan for a live/hybrid query. Every exit path —
    success, dry-run, tool error, transport error, misconfiguration —
    returns a LiveResult and audit-logs it; nothing here raises."""
    if route == "manual":
        _audit(request_id, route, [], 0, None)
        return LiveResult(route=route)
    plan = _plan_calls(query)
    planned_names = [name for name, _ in plan]
    if settings.zowe_mcp_dry_run:
        _audit(request_id, route, planned_names, 0, "dry_run")
        return LiveResult(route=route, degraded="dry_run", tools_used=tuple(planned_names))
    if zowe_mcp is None:
        _audit(request_id, route, planned_names, 0, "not_configured")
        return LiveResult(route=route, degraded="not_configured", tools_used=tuple(planned_names))

    texts: list[str] = []
    used: list[str] = []
    truncated_any = False
    notes: list[str] = []
    degraded: str | None = None
    max_bytes = settings.zowe_mcp_max_bytes
    with tracer.start_as_current_span(
        "live.fetch", attributes={"zowe.route": route, "zowe.planned": len(plan)}
    ) as span:
        t0 = time.monotonic()
        for name, args in plan:
            if name == "jes_spool_read" and "spool" not in query.lower() and not _SPOOL_ID_RE.search(query):
                notes.append("assumed spool file 2 (job log); explicit spool id wins when present")
            try:
                result = zowe_mcp.call_tool(name, args)
            except ZoweMCPError as exc:
                degraded = exc.code
                log.warning(
                    json.dumps(
                        {"request_id": request_id, "action": "live_fetch_error", "tool": name}
                    )
                )
                break
            except Exception:  # noqa: BLE001 — degrade, never raise (ADR-0002 fallback)
                degraded = "upstream_error"
                log.warning(
                    json.dumps(
                        {"request_id": request_id, "action": "live_fetch_error", "tool": name}
                    )
                )
                break
            used.append(name)
            for item in result.get("content", []):
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    cut, was_cut = _truncate(item["text"], max_bytes)
                    texts.append(cut)
                    truncated_any = truncated_any or was_cut
        byte_count = sum(len(t.encode("utf-8", errors="replace")) for t in texts)
        span.set_attributes(
            {
                "zowe.tools": ",".join(used),
                "zowe.bytes_out": byte_count,
                "zowe.truncated": truncated_any,
                "zowe.elapsed_ms": int((time.monotonic() - t0) * 1000),
            }
        )
        if degraded is not None:
            span.set_attribute("zowe.degraded", degraded)
    _audit(request_id, route, used, byte_count, degraded)
    return LiveResult(
        route=route,
        texts=tuple(texts),
        truncated=truncated_any,
        degraded=degraded,
        tools_used=tuple(used),
        notes=tuple(notes),
    )
