"""Identifier-aware filters. Filters go in prefetch, never after ANN.

Extracted identifiers (doc numbers, message IDs, members) plus product/version
from agent context become must-clauses on both dense and bm25 prefetches.
retrieval.md section 2.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from qdrant_client import models

from mainframe_rag.regexes import DOCNO_RE, find_members, find_message_ids


class QueryIdentifiers(BaseModel):
    doc_ids: list[str] = Field(default_factory=list)
    message_ids: list[str] = Field(default_factory=list)
    members: list[str] = Field(default_factory=list)

    @property
    def has_identifiers(self) -> bool:
        return bool(self.doc_ids or self.message_ids or self.members)


def _find_exact_docnos(text: str) -> set[str]:
    """Doc numbers usable as exact `doc_id` filters. A match immediately
    followed by `-` is a truncated edition suffix — a wildcard
    (`SC23-6858-xx`), a partial edition (`SC23-6858-0`), or a bare stem
    (`SC23-6858`) — and matches no `doc_id` exactly. Emitting it would
    force an exact filter that can only fail into the unfiltered
    fallback under identifier weights; dropping it keeps the raw text
    for BM25/dense and classifies honestly as NL. `DOCNO_RE` itself is
    untouched (shared with ingest: changing it would churn
    `rules_version` and force full re-ingest)."""
    out: set[str] = set()
    for m in DOCNO_RE.finditer(text):
        end = m.end()
        if end < len(text) and text[end] == "-":
            continue
        out.add(m.group(1))
    return out


def parse_query(query: str) -> QueryIdentifiers:
    # Issue #130: operators type lowercase. Message codes and doc numbers
    # are canonical-uppercase on both sides (ingest source text is
    # uppercase), so also extract from an uppercased copy and union.
    # Case change preserves word-char class, so upper-casing only ADDS
    # matches: pure-uppercase queries behave exactly as before.
    # Members stay case-sensitive: MEMBER_RE's lowercase xx convention
    # (IEASYSxx) matches payload case, and uppercasing would break it.
    # Match spans are case-stable, so the truncation check below sees the
    # same `-` boundary in both variants.
    upper = query.upper()
    return QueryIdentifiers(
        doc_ids=sorted(_find_exact_docnos(query) | _find_exact_docnos(upper)),
        message_ids=sorted(set(find_message_ids(query)) | set(find_message_ids(upper))),
        members=find_members(query),
    )


def query_kind(identifiers: QueryIdentifiers) -> str:
    return "identifier" if identifiers.has_identifiers else "nl"


def build_filter(
    identifiers: QueryIdentifiers,
    product: str | None = None,
    version: str | None = None,
) -> models.Filter | None:
    must: list[models.Condition] = []
    if product:
        must.append(models.FieldCondition(key="product", match=models.MatchValue(value=product)))
    if version:
        must.append(models.FieldCondition(key="version", match=models.MatchValue(value=version)))
    if identifiers.doc_ids:
        must.append(
            models.FieldCondition(key="doc_id", match=models.MatchAny(any=identifiers.doc_ids))
        )
    if identifiers.message_ids:
        must.append(
            models.FieldCondition(
                key="message_ids", match=models.MatchAny(any=identifiers.message_ids)
            )
        )
    if identifiers.members:
        must.append(
            models.FieldCondition(key="members", match=models.MatchAny(any=identifiers.members))
        )
    return models.Filter(must=must) if must else None
