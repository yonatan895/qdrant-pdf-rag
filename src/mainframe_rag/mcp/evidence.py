"""Downstream knowledge adapter for the MCP bridge (issue #405 MCP1).

Thin translation over the shared evidence service (`retrieve/evidence.py`):
typed tool arguments in, MCP content envelopes out. No second retrieval
engine, no direct database access beyond the service's own calls, no LLM.

Trust model (same as the HTTP adapter): scope comes from the caller through
the transport boundary — these tools never mint authority, and references
are revalidated locators, never bearer tokens. Per-reference ACLs are
deferred to #373 (single-audience assumption, documented in the evidence
contract); denial here means the serving gate refuses the generation.

Hosting is deliberately unconfigured in this card: the bridge runs FTP-only
until G2 records compatibility/auth/retention and the deploy owner wires a
host process (`__main__` is untouched). The backend is injected, so the
adapter and its parity are fully testable without any live contact.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from mainframe_rag.agent.serving import SERVABLE_OUTCOMES
from mainframe_rag.config import Settings
from mainframe_rag.ports import AsyncQdrantPoints, Embedder, QdrantPoints, Reranker
from mainframe_rag.retrieve.evidence import (
    EVIDENCE_DEFAULT_CONTEXT_BUDGET,
    EVIDENCE_MAX_CONTEXT_BUDGET,
    EvidenceNotFound,
    EvidenceRetired,
    parse_reference,
    read_evidence,
    search_evidence,
)

EVIDENCE_TOOL_SCHEMAS: dict[str, dict] = {
    "evidence_search": {
        "description": "Search curated manuals with caller scope; returns opaque "
        "evidence references plus citation text. Scope filters are never dropped.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Operator question (non-empty)"},
                "product": {"type": "string"},
                "version": {"type": "string"},
                "source": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 40, "default": 8},
            },
            "required": ["query"],
        },
    },
    "evidence_read": {
        "description": "Read one referenced evidence chunk in its generation, "
        "or an explicit retired/not-found outcome — never substituted text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {"type": "string", "description": "Opaque ev_<genfp16>_<uuid> locator"},
                "context_budget": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": EVIDENCE_MAX_CONTEXT_BUDGET,
                    "default": EVIDENCE_DEFAULT_CONTEXT_BUDGET,
                },
            },
            "required": ["reference"],
        },
    },
}

# Stable tool-error codes mirror the HTTP envelopes (client-visible fixed
# text, never exception/upstream bodies).
REPRESENTATION_UNAVAILABLE = "the retrieval generation is not available"
EVIDENCE_NOT_FOUND = "evidence not found"
EVIDENCE_RETIRED = "evidence retired; refresh explicitly"
RETRIEVAL_FAILED = "retrieval failed"


@dataclass(frozen=True)
class ResolvedGeneration:
    """Host-resolved serving target. The serving gate stays the decision
    owner; this is its verdict carried across the sync MCP boundary."""

    physical: str | None
    outcome: str

    @property
    def servable(self) -> bool:
        return self.physical is not None and self.outcome in SERVABLE_OUTCOMES


@dataclass(frozen=True)
class EvidenceDeps:
    """Everything the knowledge tools need. The host injects these; the
    FTP sidecar leaves the backend absent until a host is accepted.

    `search_fn`/`read_fn` default to the shared service; doubles override
    them to record inputs (the same seam as the HTTP adapter's retrieve_fn).
    """

    client: QdrantPoints | AsyncQdrantPoints
    embedder: Embedder
    settings: Settings
    rules_v: str
    resolve: Callable[[], ResolvedGeneration]
    reranker: Reranker | None = None
    search_fn: Callable[..., Awaitable[Any]] | None = None
    read_fn: Callable[..., Awaitable[Any]] | None = None


def gate_resolver(gate: Any, client: Any, settings: Settings, rules_v: str) -> Callable[[], ResolvedGeneration]:
    """Production resolver over the real serving gate (host wiring helper)."""

    def resolve() -> ResolvedGeneration:
        generation = _run(gate.generation(client, settings, rules_v))
        return ResolvedGeneration(physical=generation.physical, outcome=generation.outcome)

    return resolve


def _run(coro: Awaitable[Any]) -> Any:
    """Drive one service coroutine from sync tool code.

    stdio transport has no running loop (fresh `asyncio.run`); the HTTP
    transport calls tools inside the server loop, so that case runs on a
    helper thread with its own loop instead of failing closed. Timeouts
    stay the legs' own Settings bounds — the same envelope as HTTP.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _ok_text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _err_text(code: str, message: str) -> dict:
    return {"content": [{"type": "text", "text": f"{code}: {message}"}], "isError": True}


#: Sentinel for present-but-malformed arguments. Helpers return (never raise)
#: so the only `raise ValueError` sites sit behind non-type conditions —
#: ValueError is the -32602 envelope `server._run_tool` maps, while scope
#: filters must still distinguish absent (open scope) from malformed
#: (client bug, never silent broadening — the D1 rule).
_INVALID: Any = object()


def _req_str(args: dict, name: str) -> Any:
    """Required non-empty string, or _INVALID."""
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        return _INVALID
    return value


def _opt_str(args: dict, name: str) -> Any:
    """Optional string: None when absent or blank, _INVALID when wrong-typed."""
    if name not in args:
        return None
    value = args[name]
    if not isinstance(value, str):
        return _INVALID
    return value or None


def _opt_int(args: dict, name: str, *, default: int, lo: int, hi: int) -> Any:
    """Optional bounded integer; _INVALID when wrong-typed or out of range."""
    if name not in args:
        return default
    value = args[name]
    if isinstance(value, bool) or not isinstance(value, int):
        return _INVALID
    if not lo <= value <= hi:
        return _INVALID
    return value


def _refuse_unservable(generation: ResolvedGeneration) -> dict | None:
    if generation.servable and generation.physical is not None:
        return None
    return _err_text("representation_unavailable", REPRESENTATION_UNAVAILABLE)


def evidence_search(deps: EvidenceDeps, args: dict) -> dict:
    """Scoped search over the resolved generation. `args` is a caller-
    validated object (`handle_request` rejects non-objects with -32602).
    Malformed fields raise ValueError (-32602); every other failure is an
    isError tool result, never a protocol error (framing must survive)."""
    query = _req_str(args, "query")
    product = _opt_str(args, "product")
    version = _opt_str(args, "version")
    source = _opt_str(args, "source")
    limit = _opt_int(args, "limit", default=8, lo=1, hi=40)
    if query is _INVALID:
        raise ValueError("query (required) must be a non-empty string")
    if _INVALID in (product, version, source):
        raise ValueError("product, version, and source must be strings")
    if limit is _INVALID:
        raise ValueError("limit must be an integer within 1..40")
    if len(query) > deps.settings.query_max_chars:
        raise ValueError("query exceeds the maximum length")
    generation = deps.resolve()
    if (refused := _refuse_unservable(generation)) is not None:
        return refused
    assert generation.physical is not None
    search = deps.search_fn or search_evidence
    try:
        hits, kind, _timings, genfp = _run(
            search(
                deps.client,
                deps.embedder,
                generation.physical,
                query,
                product=product,
                version=version,
                source=source,
                limit=limit,
                settings=deps.settings,
                reranker=deps.reranker,
                rules_v=deps.rules_v,
            )
        )
    except Exception:  # noqa: BLE001 — mapped to a stable code below
        return _err_text("upstream_error", RETRIEVAL_FAILED)
    body = {
        "generation": genfp,
        "query_kind": kind,
        "hits": [hit.model_dump(mode="json") for hit in hits],
    }
    return _ok_text(json.dumps(body))


def evidence_read(deps: EvidenceDeps, args: dict) -> dict:
    """Exact read of one reference. Malformed locators are -32602 (client
    bug, parity with HTTP 422); well-formed-but-unknown and retired refs
    are explicit isError outcomes, never substituted text."""
    reference = _req_str(args, "reference")
    budget = _opt_int(
        args, "context_budget",
        default=EVIDENCE_DEFAULT_CONTEXT_BUDGET, lo=1, hi=EVIDENCE_MAX_CONTEXT_BUDGET,
    )
    if reference is _INVALID:
        raise ValueError("reference (required) must be a non-empty string")
    if budget is _INVALID:
        raise ValueError("context_budget must be an integer within its bounds")
    try:
        parse_reference(reference)
    except ValueError as exc:
        raise ValueError(f"malformed evidence reference: {exc}") from exc
    generation = deps.resolve()
    if (refused := _refuse_unservable(generation)) is not None:
        return refused
    assert generation.physical is not None
    read = deps.read_fn or read_evidence
    try:
        record = _run(
            read(
                deps.client,
                generation.physical,
                reference,
                deps.settings,
                deps.rules_v,
                context_budget=budget,
            )
        )
    except EvidenceRetired:
        return _err_text("not_found", EVIDENCE_RETIRED)
    except EvidenceNotFound:
        return _err_text("not_found", EVIDENCE_NOT_FOUND)
    except Exception:  # noqa: BLE001 — mapped to a stable code below
        return _err_text("upstream_error", RETRIEVAL_FAILED)
    return _ok_text(record.model_dump_json())
