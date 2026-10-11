"""The stored point payload: the one place a chunk becomes payload fields.

This module is an extraction-rules module (`rules_version._RULE_MODULES`):
any change to which fields are stored, or how they are derived, changes the
extraction-rules version, so existing collections refuse a resumed ingest
until they are re-extracted instead of silently mixing payload shapes.
"""

from __future__ import annotations

from typing import Any

from mainframe_rag.ingest.chunk import Chunk
from mainframe_rag.ingest.ibm_pdf import ParsedDoc
from mainframe_rag.ingest.identity import source_rev_key


def normalize_context(text: str, max_chars: int) -> str:
    """The stored contextual prefix recipe (issue #78): collapse whitespace
    and enforce the deterministic char cap so the embed-window budget pin
    stays provable for any model output. It lives in this rule module so a
    change to the recipe changes the extraction-rules version (review of
    #701): ingest then refuses or re-extracts instead of mixing recipes."""
    collapsed = " ".join(text.split())
    return collapsed[:max_chars].rstrip()


def point_payload(
    parsed: ParsedDoc, chunk: Chunk, rules_v: str, context: str | None = None
) -> dict[str, Any]:
    """Payload for one chunk point. The contextual prefix (issue #78) is
    stored for observability when present; it is never filtered on, so it
    takes no payload index."""
    payload: dict[str, Any] = {
        "vendor": parsed.vendor,
        "product": parsed.product,
        "version": parsed.version,
        "doc_id": chunk.doc_id,
        # Source-revision key (issue #361): the destructive key since
        # the 361B migration — locks, deletes, completions, and chunk
        # ids all scope to it. Computed from the same labels the
        # planner gates on.
        "source_rev": source_rev_key(
            parsed.vendor, parsed.product, parsed.version, parsed.sha256
        ),
        "title": parsed.title,
        "heading_path": chunk.heading_path,
        "page_label": chunk.page_label,
        "page_start": chunk.page_start,
        # Physical span end, 0-based inclusive (issue #271): citations
        # fall back to "p. PDF n–m" when printed labels cannot locate
        # the whole span. Not indexed and not part of chunk identity.
        "page_end": chunk.page_end if chunk.page_end is not None else chunk.page_start,
        "chunk_type": chunk.chunk_type,
        "message_ids": chunk.message_ids,
        "members": chunk.members,
        # Completion-code entries (issue #591). Without this key the
        # keyword index exists but stays empty and every code query's
        # must-filter matches nothing, so the family would be inert.
        "system_codes": chunk.system_codes,
        "sha256": parsed.sha256,
        "rules_v": rules_v,
        "text": chunk.text,
    }
    # Atomic-unit spans (issue #368): stored only for structured chunks
    # (non-empty span list). Prose chunks omit the key, so their
    # payloads stay byte-identical to before; legacy points without the
    # key pack via the shared fallback detector. Never indexed.
    if chunk.units:
        payload["units"] = [[span.start, span.end, span.kind] for span in chunk.units]
    if context:
        payload["context"] = context
    return payload
