"""Explicit size bounds for embedding input and ingested documents (issue #374).

Every bound is opt-in (Settings default 0 = unbounded, the pre-#374
behaviour) and refuses; none ever truncates. A refused embed input or
document is a typed error with a fixed message carrying counts only — never
manual text, paths or upstream text — so it is safe in the inventory record,
logs and client responses.

Not extraction rules: nothing here changes what a chunk contains, so this
module is outside `rules_version`. The decision to *include* an
oversized statement stays with the chunker (whole-statement preservation);
the bound only decides whether the embedding endpoint is called for it.
"""

from __future__ import annotations

from collections.abc import Iterable


class EmbedInputTooLarge(ValueError):
    """An embedding input exceeds `embed_max_input_chars`; no model was called."""

    def __init__(self, limit: int, largest: int, count: int) -> None:
        super().__init__(
            f"embed input exceeds bound: {count} input(s) over {limit} chars (largest {largest})"
        )
        self.limit = limit
        self.largest = largest
        self.count = count


class DocumentTooLarge(ValueError):
    """A document exceeds a per-document ingest bound; it is not ingested."""

    def __init__(self, dimension: str, limit: int, actual: int) -> None:
        super().__init__(f"document exceeds ingest bound: {dimension} {actual} > {limit}")
        self.dimension = dimension
        self.limit = limit
        self.actual = actual


def require_embed_inputs_within(texts: Iterable[str], limit: int, *, extra: int = 0) -> None:
    """Refuse the whole batch when any text (plus `extra` characters the
    caller will still prepend/append, e.g. a context allowance) exceeds
    `limit`. `limit <= 0` is the unbounded legacy behaviour."""
    if limit <= 0:
        return
    largest = 0
    over = 0
    for text in texts:
        size = len(text) + extra
        largest = max(largest, size)
        if size > limit:
            over += 1
    if over:
        raise EmbedInputTooLarge(limit, largest, over)


def require_document_within(dimension: str, actual: int, limit: int) -> None:
    """Refuse a document whose `dimension` (pdf_bytes/pages/chunks) exceeds
    `limit`. `limit <= 0` is the unbounded legacy behaviour."""
    if limit > 0 and actual > limit:
        raise DocumentTooLarge(dimension, limit, actual)
