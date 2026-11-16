"""What the agent packed and retrieved for one answer, kept on the eval row
(issue #1191, graded eval PR 1).

The answer contract already carries the packed excerpts (`supplied_evidence`,
`evidence_omitted`; docs/agent.md) and, on the streamed routes, the final
retrieval hits. The graded eval needs them per row to say where an expected
fact was lost: not retrieved, not packed, trimmed, or packed but unused.

Rows keep a text-free projection: doc id, physical pages (1-based, the
`pdf_page_start` convention), the printed page label, the character range and
trim flags, scores, and a sha256 of each excerpt's text. Headings, titles,
citation strings and hit text are manual-derived and are never copied. The
excerpt text itself is kept only on request (`--keep-evidence-text`), in the
private report, for a later grounding check; the text-free export copies none
of these fields (`answers.export_row` is an allow-list).

Absent fields (an older agent, the buffered `/v1/answer` body, which has no
hits) and malformed ones are unknown (None), never an empty list: the capture
is observational and never changes a verdict."""

from __future__ import annotations

import hashlib
import math
from typing import Any

_EXCERPT_INTS = ("start_char", "end_char", "source_chars")
_EXCERPT_FLAGS = ("truncated_start", "truncated_end", "text_clipped", "duplicate_citation")


def _int(value: Any) -> bool:
    return type(value) is int and value >= 0


def _page(value: Any) -> bool:
    return value is None or (type(value) is int and value >= 1)


def _number(value: Any) -> bool:
    return value is None or (type(value) in (int, float) and math.isfinite(value))


def _excerpt_meta(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    index, doc_id, label, text = (item.get(k) for k in ("index", "doc_id", "page_label", "text"))
    start, end = item.get("pdf_page_start"), item.get("pdf_page_end")
    if (
        type(index) is not int or not isinstance(doc_id, str) or not doc_id
        or not isinstance(label, str) or not isinstance(text, str)
        or not _page(start) or not _page(end)
        or not all(_int(item.get(k)) for k in _EXCERPT_INTS)
        or not all(type(item.get(k, False)) is bool for k in _EXCERPT_FLAGS)
    ):
        return None
    return {
        "index": index, "doc_id": doc_id, "page_label": label,
        "pdf_page_start": start, "pdf_page_end": end,
        **{k: item[k] for k in _EXCERPT_INTS},
        **{k: item.get(k, False) for k in _EXCERPT_FLAGS},
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _hit_meta(item: Any) -> dict[str, Any] | None:
    """One retrieval hit; its 0-based page span (`SearchHit.page_start`)
    becomes the 1-based supplied-evidence convention."""
    if not isinstance(item, dict):
        return None
    chunk_id, doc_id, chunk_type, label = (
        item.get(k) for k in ("chunk_id", "doc_id", "chunk_type", "page_label"))
    start, end = item.get("page_start"), item.get("page_end")
    score, rerank = item.get("score"), item.get("rerank_score")
    if (
        not all(isinstance(v, str) for v in (chunk_id, doc_id, chunk_type, label))
        or not (start is None or _int(start)) or not (end is None or _int(end))
        or score is None or not _number(score) or not _number(rerank)
    ):
        return None
    return {
        "chunk_id": chunk_id, "doc_id": doc_id, "chunk_type": chunk_type, "page_label": label,
        "pdf_page_start": None if start is None else start + 1,
        "pdf_page_end": None if start is None else (start if end is None else end) + 1,
        "score": score, "rerank_score": rerank,
    }


def _all_or_none(items: Any, project: Any) -> list[dict[str, Any]] | None:
    if not isinstance(items, list):
        return None
    out = [project(item) for item in items]
    return None if any(o is None for o in out) else out


def evidence_capture(data: dict[str, Any], keep_text: bool = False) -> dict[str, Any]:
    """Row fields from one answer object (the JSON body, the SSE `final`,
    or the chat finish chunk): `supplied_evidence_meta`, `evidence_omitted`,
    `final_hits`, and with `keep_text` also `supplied_evidence_text`."""
    supplied = data.get("supplied_evidence")
    meta = _all_or_none(supplied, _excerpt_meta)
    omitted = data.get("evidence_omitted")
    fields: dict[str, Any] = {
        "supplied_evidence_meta": meta,
        "evidence_omitted": omitted if _int(omitted) else None,
        "final_hits": _all_or_none(data.get("hits"), _hit_meta),
    }
    if keep_text:
        # `meta` is not None only when `supplied` is a list of valid entries.
        fields["supplied_evidence_text"] = (
            None if meta is None or not isinstance(supplied, list)
            else [{"index": item["index"], "text": item["text"]} for item in supplied])
    return fields
