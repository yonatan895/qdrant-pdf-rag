"""Shared evidence service (issue #405 E1): scoped search plus exact reads.

Thin over `retrieve/query.async_search` and the `ports` Qdrant surface, behind
the caller-owned serving-gate binding. This module never calls an LLM —
search and fetch are pure retrieval legs; reasoning happens in `agent/`.

References are opaque versioned locators, `ev_<genfp16>_<chunk_uuid>`, where
`genfp16` is the 16-hex representation fingerprint (the same policy tuple
`compare_manifests` uses, so record-only drift keeps refs valid while any
re-embed-required change retires them). Raw point ids and collection names
are never accepted from clients: a bare UUID fails validation, and a ref
minted under another generation reports `retired` instead of resolving
against current data — a republish never silently substitutes new text for
an old reference.
"""

from __future__ import annotations

import inspect
import uuid

from pydantic import BaseModel, ConfigDict

from mainframe_rag.config import Settings
from mainframe_rag.ingest.completion import representation_fingerprint
from mainframe_rag.ports import AsyncQdrantPoints, Embedder, QdrantPoints, Reranker
from mainframe_rag.retrieve.query import (
    RETRIEVE_PAYLOAD_FIELDS,
    SearchHit,
    async_search,
    format_citation,
)

REFERENCE_PREFIX = "ev_"

# Exact-read projection: the search fields plus revision/source attribution.
# Additive fetch for this path only — search ranking/filter behavior is
# untouched (the fields are never filtered or ranked on here either).
EXACT_PAYLOAD_FIELDS: tuple[str, ...] = tuple(
    list(RETRIEVE_PAYLOAD_FIELDS) + ["source", "source_rev", "members"]
)

# Default bound for one exact-read excerpt. Chunks cap at 3500 chars, so the
# default never truncates real data; callers needing smaller windows get an
# explicit truncated flag, never a silent cut.
EVIDENCE_DEFAULT_CONTEXT_BUDGET = 8000
EVIDENCE_MAX_CONTEXT_BUDGET = 20000

_EVIDENCE_TRUNCATION_SUFFIX = "\n... [truncated]"


class EvidenceNotFound(LookupError):
    """No point for this reference in the bound generation."""


class EvidenceRetired(LookupError):
    """Reference minted under a superseded generation. Refresh explicitly —
    never resolve it against current data."""


class EvidenceRecord(BaseModel):
    """One exact evidence read. Frozen: the record describes the referenced
    generation, not whatever is current when the client gets around to it."""

    model_config = ConfigDict(frozen=True)

    reference: str
    generation: str
    cite: str
    doc_id: str
    title: str
    heading: str
    page_label: str
    text: str
    chunk_type: str = "narrative"
    message_ids: tuple[str, ...] = ()
    members: tuple[str, ...] = ()
    product: str | None = None
    version: str | None = None
    source: str | None = None
    source_rev: str | None = None
    truncated: bool = False


def current_genfp_hex(settings: Settings, rules_v: str) -> str:
    """16-hex generation fingerprint for ref minting and validation.

    Derived from the versioned representation fingerprint, so ref validity
    tracks exactly the re-embed-required policy — no second field list.
    """
    fingerprint = representation_fingerprint(settings, rules_v)
    hexpart = fingerprint.rsplit(":", 1)[-1]
    if len(hexpart) != 16 or any(c not in "0123456789abcdef" for c in hexpart):
        raise RuntimeError("evidence service refuses an unparseable generation fingerprint")
    return hexpart


def mint_reference(genfp_hex: str, chunk_id: str) -> str:
    """Mint the opaque locator for a hit of the bound generation."""
    return f"{REFERENCE_PREFIX}{genfp_hex}_{chunk_id}"


def parse_reference(reference: object) -> tuple[str, str]:
    """Split an opaque locator into (genfp_hex, chunk_id).

    Anything else — bare UUIDs, paths, URLs, collection names — raises
    ValueError. Callers map that to their fixed invalid-envelope.
    """
    if not isinstance(reference, str) or not reference.startswith(REFERENCE_PREFIX):
        raise ValueError("malformed evidence reference")
    rest = reference[len(REFERENCE_PREFIX) :]
    genfp_hex, sep, chunk_id = rest.partition("_")
    if not sep or len(genfp_hex) != 16 or any(c not in "0123456789abcdef" for c in genfp_hex):
        raise ValueError("malformed evidence reference")
    try:
        uuid.UUID(chunk_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("malformed evidence reference") from exc
    return genfp_hex, chunk_id


def attach_references(hits: list[SearchHit], genfp_hex: str) -> list[SearchHit]:
    """Bind minted references to a search result list (pure, no IO)."""
    return [
        hit.model_copy(update={"reference": mint_reference(genfp_hex, hit.chunk_id)})
        for hit in hits
    ]


async def search_evidence(
    client: AsyncQdrantPoints | QdrantPoints,
    embedder: Embedder,
    collection: str,
    query: str,
    *,
    product: str | None = None,
    version: str | None = None,
    source: str | None = None,
    limit: int = 8,
    settings: Settings,
    reranker: Reranker | None = None,
    rules_v: str,
    retrieve_fn=None,
) -> tuple[list[SearchHit], str, dict[str, int], str]:
    """Shared scoped search: (hits with references, query_kind, timings, generation).

    `retrieve_fn` defaults to `async_search`; the HTTP adapter passes its
    module-global alias so existing doubles keep intercepting one symbol.
    Scope (product/version/source) flows into the prefetch filter with the
    D1 scope-preserving fallback — this wrapper changes ranking never.
    """
    fn = retrieve_fn if retrieve_fn is not None else async_search
    res = fn(
        client,
        embedder,
        collection,
        query,
        product=product,
        version=version,
        source=source,
        limit=limit,
        settings=settings,
        reranker=reranker,
    )
    hits, kind, timings = await res if inspect.isawaitable(res) else res
    genfp_hex = current_genfp_hex(settings, rules_v)
    return attach_references(list(hits), genfp_hex), kind, timings, genfp_hex


async def read_evidence(
    client: AsyncQdrantPoints | QdrantPoints,
    collection: str,
    reference: str,
    settings: Settings,
    rules_v: str,
    *,
    context_budget: int = EVIDENCE_DEFAULT_CONTEXT_BUDGET,
) -> EvidenceRecord:
    """Exact read of one referenced chunk in the bound generation.

    Raises ValueError on malformed references, EvidenceRetired when the ref
    belongs to a superseded generation, EvidenceNotFound when the point is
    absent. Store faults propagate for the caller to map to upstream_error.
    """
    genfp_hex, chunk_id = parse_reference(reference)
    current = current_genfp_hex(settings, rules_v)
    if genfp_hex != current:
        raise EvidenceRetired(reference)
    try:
        in_range = 1 <= context_budget <= EVIDENCE_MAX_CONTEXT_BUDGET
    except TypeError as exc:
        raise ValueError("malformed context budget") from exc
    if not in_range:
        raise ValueError("malformed context budget")
    res = client.retrieve(
        collection,
        ids=[chunk_id],
        with_payload=list(EXACT_PAYLOAD_FIELDS),
        with_vectors=False,
    )
    records = await res if inspect.isawaitable(res) else res
    if not records:
        raise EvidenceNotFound(reference)
    payload = records[0].payload or {}
    doc_id = str(payload.get("doc_id") or "")
    title = str(payload.get("title") or "")
    heading = str(payload.get("heading_path") or "")
    page_label = str(payload.get("page_label") or "")
    text = str(payload.get("text") or "")
    truncated = False
    if len(text) > context_budget:
        keep = max(context_budget - len(_EVIDENCE_TRUNCATION_SUFFIX), 0)
        text = text[:keep].rstrip() + _EVIDENCE_TRUNCATION_SUFFIX
        truncated = True
    return EvidenceRecord(
        reference=reference,
        generation=current,
        cite=format_citation(doc_id, title, heading, page_label),
        doc_id=doc_id,
        title=title,
        heading=heading,
        page_label=page_label,
        text=text,
        chunk_type=str(payload.get("chunk_type") or "narrative"),
        message_ids=tuple(payload.get("message_ids") or []),
        members=tuple(payload.get("members") or []),
        product=payload.get("product"),
        version=payload.get("version"),
        source=payload.get("source"),
        source_rev=payload.get("source_rev"),
        truncated=truncated,
    )
