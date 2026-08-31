"""Identifier-aware filters. Filters go in prefetch, never after ANN.

Extracted identifiers (doc numbers, message IDs, members) plus product/version
from agent context become must-clauses on both dense and bm25 prefetches.
architecture.md section 4.5.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from qdrant_client import models

from mainframe_rag.regexes import find_docnos, find_members, find_message_ids


class QueryIdentifiers(BaseModel):
    doc_ids: list[str] = Field(default_factory=list)
    message_ids: list[str] = Field(default_factory=list)
    members: list[str] = Field(default_factory=list)

    @property
    def has_identifiers(self) -> bool:
        return bool(self.doc_ids or self.message_ids or self.members)


def parse_query(query: str) -> QueryIdentifiers:
    return QueryIdentifiers(
        doc_ids=find_docnos(query),
        message_ids=find_message_ids(query),
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
