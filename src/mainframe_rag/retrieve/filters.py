"""Identifier-aware filters. Filters go in prefetch, never after ANN.

Extracted identifiers (doc numbers, message IDs, members) plus product/version
from agent context become must-clauses on both dense and bm25 prefetches.
retrieval.md section 2.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field
from qdrant_client import models

from mainframe_rag.regexes import DOCNO_RE, find_members, find_message_ids

# Query-side member pattern (issue #133): case-insensitive twin of the
# shared MEMBER_RE, defined HERE — not in regexes.py — so ingest
# extraction (and extraction_rules_version) is untouched. Ingest must
# stay precise (payload pollution is permanent until re-ingest); the
# query side may over-match (a filter miss falls back to unfiltered,
# never to a wrong answer). Matches are folded to payload-canonical
# case by _fold_member_case before filtering.
MEMBER_QUERY_RE = re.compile(r"\b([A-Za-z]{3,8}(?:[xX]{2}|\d{2}))\b")


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
def _fold_member_case(token: str) -> str:
    """Map a query-typed member to payload-canonical case. Ingest extracts
    with case-sensitive MEMBER_RE, so payloads only ever hold UPPERCASE
    stems with a lowercase `xx` suffix (`IEASYSxx`) or digit endings
    (`EYUPLX01`) — verified zero case variance over 435k real-corpus
    points / 16,686 distinct values (issue #133 scan), hence no ingest
    normalization and no re-ingest. Operators type lowercase, so fold:
    uppercase the token, then restore the lowercase `xx` suffix."""
    up = token.upper()
    if up.endswith("XX"):
        up = up[:-2] + "xx"
    return up


def find_members_folded(query: str) -> list[str]:
    """Exact members plus case-folded ones (issue #133). MEMBER_QUERY_RE
    matches operator-typed case (`ieasysxx`, `IEASYSXX`); every match is
    folded to payload-canonical case, so this only ADDS recall like the
    #130 union. Both the exact and the folded form are emitted."""
    exact = set(find_members(query))
    folded = {_fold_member_case(m) for m in MEMBER_QUERY_RE.findall(query)}
    return sorted(exact | folded)


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
    # Members fold to payload-canonical case via find_members_folded
    # (issue #133): the corpus scan proved payloads hold a single case
    # form, so both the exact and the folded form are emitted.
    upper = query.upper()
    return QueryIdentifiers(
        doc_ids=sorted(_find_exact_docnos(query) | _find_exact_docnos(upper)),
        message_ids=sorted(set(find_message_ids(query)) | set(find_message_ids(upper))),
        members=find_members_folded(query),
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
