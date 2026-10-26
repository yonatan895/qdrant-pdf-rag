"""Hybrid retrieval: dense + BM25 prefetch with filters, fused with local RRF.

Why local RRF instead of Qdrant's FusionQuery.RRF: per-prefetch weights are
required ([1,3] when identifiers are present, else [1,1]) and k=2. Qdrant's
server-side RRF does not expose weights. Two filtered prefetch queries, fused
here, preserve the "filters in prefetch" contract. architecture.md 4.5.
"""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.trace import SpanKind
from pydantic import BaseModel, ConfigDict
from qdrant_client import models

if TYPE_CHECKING:
    from mainframe_rag.config import Settings

from mainframe_rag.ports import (
    AsyncQdrantReader,
    AsyncReaderAdapter,
    Embedder,
    QdrantReader,
    Reranker,
)
from mainframe_rag.retrieve.filters import (
    QueryIdentifiers,
    build_fallback_filter,
    build_filter,
    build_scope_filter,
    parse_query,
    query_kind,
)
from mainframe_rag.retrieve.rewrite import expand_query, should_rewrite
from mainframe_rag.retrieve.screen import screen_query
from mainframe_rag.retrieve.split import split_query
from mainframe_rag.tracing import bind_module_tracer, start_as_current_span

# Proxy tracer: no-op until a real provider is installed (issue #83 — the
# agent's lifespan installs one when OTEL_EXPORTER_OTLP_ENDPOINT is set).
tracer = trace.get_tracer("mainframe-rag.retrieve")
bind_module_tracer(__name__, "tracer", "mainframe-rag.retrieve")

PREFETCH_LIMIT = 40
RRF_K = 60  # equals Settings.rrf_k (#860); also the default k of the split merges
RRF_WEIGHTS_IDENTIFIER = (1.0, 3.0)  # (dense, bm25): identifiers favor exact terms
RRF_WEIGHTS_NL = (1.0, 1.0)

# Second-level fusion across split retrieval paths (issue #214): comparative
# peers merge by distinct document per side (interleave_split_hits, #825);
# diagnostic legs merge by rank sum favoring the symptom anchor (anchor
# trust, issue #117).
SPLIT_MERGE_DIAGNOSTIC: tuple[float, float] = (2.0, 1.0)


RETRIEVE_PAYLOAD_FIELDS: tuple[str, ...] = (
    "doc_id",
    "title",
    "heading_path",
    "page_label",
    "chunk_type",
    "product",
    "version",
    "message_ids",
    "text",
    # Atomic-unit spans (issue #368): additive fetch, never filtered or
    # ranked on — ranking/filter behavior is byte-identical with or without.
    "units",
    # Physical PDF page span, 0-based inclusive (issue #271): the citation
    # fallback when printed labels cannot locate the chunk, and the
    # diversification page key.
    "page_start",
    "page_end",
)


# Stored title / heading_path / page_label are untrusted PDF metadata (/Title,
# outline, /PageLabels; issue #998). Serving bounds and sanitizes them, so a
# stored corpus is protected without re-ingest, and a 60 KB title can never
# grow a header that starves the prompt (issue #1008). Real values sit far
# below these bounds.
CITE_TITLE_MAX_CHARS = 200
CITE_HEADING_MAX_CHARS = 400
CITE_LABEL_MAX_CHARS = 32
_CITE_ELLIPSIS = "\u2026"
# Control (incl. newline/tab) and line/paragraph separators read as a space;
# format characters (bidi overrides, zero-width) and lone surrogates vanish.
_CITE_SPACE_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})
_CITE_DROP_CATEGORIES = frozenset({"Cf", "Cs"})
# The cite's own field markers: an IBM doc number (CITATION_LINE_RE's form)
# and a `p.`/`pp.` page designation with its leading separator and page token.
_CITE_DOCNO_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2,4}\d{2}-\d{4}(?:-\d{2})?(?![0-9])")
_CITE_PAGE_MARK_RE = re.compile(r"[\s,\uff0c]*\b[Pp]{1,2}\.(?:\s*[^\s,\uff0c]+)?")


def clean_cite_field(value: str, limit: int) -> str:
    """One stored citation field as single-line display text: control and
    separator characters become spaces, format/bidi/zero-width characters
    are dropped, whitespace collapses, and the result is bounded to `limit`
    characters (an ellipsis marks the cut). Idempotent."""
    out: list[str] = []
    # Bound the work as well as the output: a pathological field is cut
    # before the per-character pass.
    for ch in value[: limit * 8]:
        category = unicodedata.category(ch)
        if category in _CITE_DROP_CATEGORIES:
            continue
        out.append(" " if category in _CITE_SPACE_CATEGORIES else ch)
    text = " ".join("".join(out).split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + _CITE_ELLIPSIS
    return text


def plain_page_label(value: str) -> str:
    """The stored printed label when it is one plain location, else "" (the
    cite then names the physical PDF page). A label that is over the bound
    or carries a comma, a page designation or a doc number could fabricate a
    second location in the cite, so it is never cited (issue #998)."""
    label = clean_cite_field(value, CITE_LABEL_MAX_CHARS + 1)
    if (
        len(label) > CITE_LABEL_MAX_CHARS
        or "," in label
        or "\uff0c" in label
        or _CITE_PAGE_MARK_RE.search(label)
        or _CITE_DOCNO_RE.search(label)
    ):
        return ""
    return label


def _cite_part(text: str, doc_id: str) -> str:
    """A title or heading as cite text: page designations and doc numbers
    other than this hit's own are removed, so stored metadata can never make
    the cite name a second page or a document that was not ingested."""
    own = {doc_id}
    if base := re.match(r"[A-Z]{2,4}\d{2}-\d{4}", doc_id):
        own.add(base.group(0))
    text = _CITE_PAGE_MARK_RE.sub("", text)
    text = _CITE_DOCNO_RE.sub(lambda m: m.group(0) if m.group(0) in own else "", text)
    text = re.sub(r"\s+(?=[,\uff0c])", "", text)
    text = re.sub(r"([,\uff0c])(?:\s*[,\uff0c])+", r"\1", text)
    return " ".join(text.split()).strip(" ,\uff0c")


def format_citation(
    doc_id: str,
    title: str,
    heading_path: str,
    page_label: str,
    page_start: int | None = None,
    page_end: int | None = None,
) -> str:
    """SA22-7592-05 z/OS MVS Init..., IEASYSxx > LFAREA, p. 1-17

    The citation shape contract; cites.is_citation_shaped validates this shape
    on LLM output. A printed label range wins when ingest stored one (every
    page in the span labeled). Otherwise the physical PDF span is cited as
    `p. PDF n` / `p. PDF n–m` (1-based, issue #271): a location an engineer
    can open in the retained PDF, never a guessed printed folio. Legacy
    points without a physical page keep the page-less form.

    Title, heading and label are sanitized and bounded here (issue #998), so
    every cite is one line with exactly one doc number and at most one page
    designation, whatever the stored metadata says."""
    title = _cite_part(clean_cite_field(title, CITE_TITLE_MAX_CHARS), doc_id)
    heading_path = _cite_part(clean_cite_field(heading_path, CITE_HEADING_MAX_CHARS), doc_id)
    page_label = plain_page_label(page_label)
    parts = [f"{doc_id} {title}".strip(), heading_path]
    cite = ", ".join(p for p in parts if p)
    if page_label:
        cite += f", p. {page_label}"
    elif page_start is not None:
        first = page_start + 1
        last = (page_end if page_end is not None and page_end >= page_start else page_start) + 1
        cite += f", p. PDF {first}" if first == last else f", p. PDF {first}\u2013{last}"
    return cite


class SearchHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str
    score: float
    cite: str
    heading: str
    text: str
    doc_id: str
    title: str
    page_label: str
    chunk_type: str
    message_ids: tuple[str, ...]
    product: str | None = None
    version: str | None = None
    rerank_score: float | None = None
    # Persisted unit spans, [start, end, kind] triples over the stripped
    # chunk text (issue #368). None = legacy point or uncapped block: the
    # pack stage redetects with the shared chunk detectors. () = known
    # prose: legacy character truncation applies.
    units: tuple[tuple[int, int, str], ...] | None = None
    # Physical PDF page span, 0-based inclusive (issue #271). None = not
    # projected (legacy point or a hit built outside _to_hit).
    page_start: int | None = None
    page_end: int | None = None


def _payload_page(raw: object) -> int | None:
    """Fail-closed page parse: only a non-negative int (never bool) counts."""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return None
    return raw


def _parse_unit_spans(raw: object) -> tuple[tuple[int, int, str], ...] | None:
    """Fail-closed span parse: any malformation yields None (pack falls back
    to shared redetection) rather than an invented boundary."""
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        return None
    spans: list[tuple[int, int, str]] = []
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) != 3:
            return None
        start, end, kind = entry
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(kind, str)
            or kind not in ("atomic", "prose")
            or not (0 <= start <= end)
        ):
            return None
        spans.append((start, end, kind))
    return tuple(spans)


def _to_hit(point: models.ScoredPoint, score: float) -> SearchHit:
    payload = point.payload or {}
    doc_id = str(payload.get("doc_id") or "")
    # Served fields are bounded, single-line display text (issue #998); the
    # cite additionally drops foreign doc numbers and page designations.
    title = clean_cite_field(str(payload.get("title") or ""), CITE_TITLE_MAX_CHARS)
    heading = clean_cite_field(str(payload.get("heading_path") or ""), CITE_HEADING_MAX_CHARS)
    page_label = plain_page_label(str(payload.get("page_label") or ""))
    page_start = _payload_page(payload.get("page_start"))
    page_end = _payload_page(payload.get("page_end"))
    if page_start is None or page_end is None or page_end < page_start:
        # Legacy point (no page_end) or malformed span: single page.
        page_end = page_start
    return SearchHit(
        chunk_id=str(point.id),
        score=score,
        cite=format_citation(doc_id, title, heading, page_label, page_start, page_end),
        heading=heading,
        text=str(payload.get("text") or ""),
        doc_id=doc_id,
        title=title,
        page_label=page_label,
        chunk_type=str(payload.get("chunk_type") or "narrative"),
        product=payload.get("product"),
        version=payload.get("version"),
        message_ids=tuple(payload.get("message_ids") or []),
        units=_parse_unit_spans(payload.get("units")),
        page_start=page_start,
        page_end=page_end,
    )


def entry_headed(text: str, ids: Sequence[str]) -> bool:
    """True when some line of `text` BEGINS with one of the requested message
    ids (issue #922): the chunk holds that id's entry heading. A running
    header or chapter title that only names the id inside a range or a title
    ("Chapter 6. IEE messages (IEE400I - IEE699I)") starts with other text, so
    it never counts. Same start-of-line rule as the prompt packer's
    `_requested_identifiers`; no payload field and no vendor format is read."""
    if not ids or not text:
        return False
    alts = "|".join(re.escape(i) for i in sorted(ids, key=len, reverse=True))
    return re.search(rf"^[^\w\n]*(?:{alts})(?!\w)", text, re.IGNORECASE | re.MULTILINE) is not None


def leading_title_section(payload: dict) -> bool:
    """True when the chunk is the leading section under the document title.

    The outline rules put every page before a document's first kept bookmark
    under the document title (#1001), and that section starts at the
    document's first page unless the pages before it are skipped by name. A
    document with no outline gives the same heading path to its body windows
    (`fallback_sections`), so the heading alone also matches body text: 2452
    of the 2891 stored title-headed chunks start past page 0. Requiring
    `page_start == 0` keeps those out (a leading section that begins after
    skipped front matter is conservatively not demoted). A missing or
    malformed page never counts, so legacy points keep their order."""
    page = payload.get("page_start")
    if type(page) is not int or page != 0:
        return False
    title = str(payload.get("title") or "").strip()
    return bool(title) and str(payload.get("heading_path") or "").strip() == title


def _prefer_section_on_ties(
    ranked: list[tuple[str, float]], by_id: dict[str, models.ScoredPoint]
) -> list[tuple[str, float]]:
    """Resolve an exact fusion tie between a document's leading title section
    and a section chunk of another document in favour of the section chunk
    (issue #1100).

    The legs can each rank a different chunk first; their RRF sums then tie
    exactly (one `weight/(k+1)` each) and insertion order — dense leg first —
    decided the order. That let the leading title section take rank 1 from the
    body chunk that answers the question. Only *leading* title sections move
    (`leading_title_section`: page 0 and heading path == title), so a
    no-outline document's body windows keep their rank. Only *cross-document*
    ties move: a tie inside one document keeps RRF order, and a strictly
    higher-scoring title chunk is never demoted. Every other score and the
    order inside each group are untouched."""
    out = list(ranked)
    start = 0
    while start < len(out):
        end = start + 1
        while end < len(out) and out[end][1] == out[start][1]:
            end += 1
        if end - start > 1:
            group = out[start:end]
            leading = {key: leading_title_section(by_id[key].payload or {}) for key, _ in group}
            doc = {key: str((by_id[key].payload or {}).get("doc_id") or "") for key, _ in group}
            demoted = {
                key
                for key, _ in group
                if leading[key]
                and any(
                    not leading[other] and doc[other] != doc[key]
                    for other, _ in group
                )
            }
            if demoted:
                out[start:end] = [kv for kv in group if kv[0] not in demoted] + [
                    kv for kv in group if kv[0] in demoted
                ]
        start = end
    return out


def rrf_fuse(
    dense: list[models.ScoredPoint],
    sparse: list[models.ScoredPoint],
    weights: tuple[float, float],
    k: int = RRF_K,
    limit: int = 8,
    type_boosts: dict[str, float] | None = None,
    entry_ids: Sequence[str] = (),
) -> list[SearchHit]:
    """Weighted RRF over the two legs. `entry_ids` (the query's message ids,
    issue #922): candidates whose text has an entry heading for one of them
    rank above candidates that only mention it, RRF order kept inside each
    group. A range endpoint repeated in every page's running header makes
    dozens of chunks match the exact-id filter; only the entry is the answer.
    Empty `entry_ids` leaves the entry-headed pass byte-identical. Exact
    cross-document ties keep a section chunk above a document's leading title
    section (`_prefer_section_on_ties`, issue #1100)."""
    by_id: dict[str, models.ScoredPoint] = {}
    scores: dict[str, float] = defaultdict(float)
    for weight, points, leg in zip(weights, (dense, sparse), ("dense", "bm25"), strict=True):
        for rank, point in enumerate(points):
            key = str(point.id)
            by_id[key] = point
            contrib = weight / (k + rank + 1)
            if leg == "bm25" and type_boosts:
                # Per-type sparse boost (issue #216): syntax/table chunks
                # earn their factor; unknown/missing types score 1.0, and a
                # None/empty mapping keeps legacy fusion byte-identical.
                payload = point.payload or {}
                ctype = payload.get("chunk_type")
                if isinstance(ctype, str):
                    contrib *= type_boosts.get(ctype, 1.0)
            scores[key] += contrib
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    ranked = _prefer_section_on_ties(ranked, by_id)
    if entry_ids:
        headed = {
            key: entry_headed(str((by_id[key].payload or {}).get("text") or ""), entry_ids)
            for key, _ in ranked
        }
        ranked = [kv for kv in ranked if headed[kv[0]]] + [kv for kv in ranked if not headed[kv[0]]]
    return [_to_hit(by_id[key], score) for key, score in ranked[:limit]]


def merge_split_hits(
    lists: list[list[SearchHit]],
    weights: tuple[float, float],
    k: int = RRF_K,
    limit: int = 8,
) -> list[SearchHit]:
    """Second-level RRF over already-fused per-path rankings (issue #214).

    Same 1/(k+rank+1) shape as rrf_fuse, applied to hit ranks instead of
    point ranks; weights favor the anchor leg (diagnostic symptom-first).
    First-seen hit wins the object; its score becomes the merged score so
    downstream legs (diversify backfill, rerank RRF fusion) see one scale.
    Stable sort: full ties keep leg-1-then-leg-2 order.
    """
    by_id: dict[str, SearchHit] = {}
    scores: dict[str, float] = defaultdict(float)
    for weight, hits in zip(weights, lists, strict=True):
        for rank, hit in enumerate(hits):
            key = hit.chunk_id
            if key not in by_id:
                by_id[key] = hit
            scores[key] += weight / (k + rank + 1)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [by_id[key].model_copy(update={"score": score}) for key, score in ranked]


def max_split_hits(
    lists: list[list[SearchHit]],
    k: int = RRF_K,
    limit: int = 8,
) -> list[SearchHit]:
    """Best-evidence fusion over comparative peer rankings (issue #214).

    Takes each doc's MAXIMUM 1/(k+rank+1) across paths instead of the sum:
    RRF-sum lets shared-context noise ranking mid in both legs outscore an
    entity-focused rank-1 (measured: IGW-messages 0.2+0.2=0.4 past a focused
    0.33 on holdout CMP-14). Peers are equals, so no weights — first-seen
    hit wins ties by stability (leg-1 order). Score becomes the max so
    downstream legs see one scale.
    """
    by_id: dict[str, SearchHit] = {}
    scores: dict[str, float] = {}
    for hits in lists:
        for rank, hit in enumerate(hits):
            key = hit.chunk_id
            if key not in by_id:
                by_id[key] = hit
            score = 1.0 / (k + rank + 1)
            if score > scores.get(key, 0.0):
                scores[key] = score
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [by_id[key].model_copy(update={"score": score}) for key, score in ranked]


def interleave_split_hits(
    lists: list[list[SearchHit]],
    k: int = RRF_K,
    limit: int = 8,
) -> list[SearchHit]:
    """Comparative merge by distinct document per side (issue #825).

    Each side's chunks collapse to its documents in rank order (a document's
    best chunk); the sides then take turns, each placing its next document
    not yet shown. The remaining chunks follow in best-evidence order
    (`max_split_hits`). Chunk-level max merging put a side's second-best
    document near rank 2r, behind extra chunks of documents already shown
    (dev CMP-06: rank 22, outside the top 8). Top-1 is unchanged: both orders
    start with the first side's rank-1 chunk. Scores become 1/(k+position+1)
    so downstream legs (diversify backfill, rerank fusion) see one scale.
    """
    per_side: list[list[SearchHit]] = []
    for hits in lists:
        docs: set[str] = set()
        best: list[SearchHit] = []
        for hit in hits:
            if hit.doc_id not in docs:
                docs.add(hit.doc_id)
                best.append(hit)
        per_side.append(best)
    head: list[SearchHit] = []
    shown: set[str] = set()
    cursors = [0] * len(per_side)
    placed = True
    while placed:
        placed = False
        for side, best in enumerate(per_side):
            while cursors[side] < len(best) and best[cursors[side]].doc_id in shown:
                cursors[side] += 1
            if cursors[side] < len(best):
                hit = best[cursors[side]]
                cursors[side] += 1
                shown.add(hit.doc_id)
                head.append(hit)
                placed = True
    head_ids = {hit.chunk_id for hit in head}
    tail = [
        hit for hit in max_split_hits(lists, k, sum(len(hits) for hits in lists))
        if hit.chunk_id not in head_ids
    ]
    return [
        hit.model_copy(update={"score": 1.0 / (k + position + 1)})
        for position, hit in enumerate((head + tail)[:limit])
    ]


def _page_key(h: SearchHit) -> tuple[str, str]:
    """Diversification page bucket (issue #271): the physical first page.

    Printed labels collapse distinct pages (every unlabeled page is '',
    repeated folios share one label), so the per-page cap degraded to
    per-document there. Hits without a projected physical page (legacy
    points, hand-built hits) keep the printed-label bucket."""
    if h.page_start is not None:
        return (h.doc_id, f"pdf:{h.page_start}")
    return (h.doc_id, h.page_label)


def diversify_hits(
    hits: list[SearchHit],
    limit: int = 8,
    max_per_page: int = 1,
    max_per_doc: int = 3,
) -> list[SearchHit]:
    """Ensures search results provide diverse coverage across documents and pages
    so near-duplicate consecutive chunks do not crowd out relevant companion
    manuals or distinct sections."""
    selected: list[SearchHit] = []
    seen_pages: dict[tuple[str, str], int] = {}
    seen_docs: dict[str, int] = {}
    remaining: list[SearchHit] = []

    # Phase 1: select candidates respecting both per-page and per-doc caps
    for h in hits:
        p_key = _page_key(h)
        d_key = h.doc_id
        if seen_pages.get(p_key, 0) < max_per_page and seen_docs.get(d_key, 0) < max_per_doc:
            seen_pages[p_key] = seen_pages.get(p_key, 0) + 1
            seen_docs[d_key] = seen_docs.get(d_key, 0) + 1
            selected.append(h)
        else:
            remaining.append(h)
        if len(selected) >= limit:
            return selected

    # Phase 2: backfill without violating max_per_page (relax per-doc cap first)
    still_remaining: list[SearchHit] = []
    for h in remaining:
        p_key = _page_key(h)
        if seen_pages.get(p_key, 0) < max_per_page:
            seen_pages[p_key] = seen_pages.get(p_key, 0) + 1
            selected.append(h)
            if len(selected) >= limit:
                return selected
        else:
            still_remaining.append(h)

    still_remaining.sort(
        key=lambda h: (
            seen_pages.get(_page_key(h), 0),
            -(h.rerank_score if h.rerank_score is not None else h.score),
        )
    )
    for h in still_remaining:
        p_key = _page_key(h)
        seen_pages[p_key] = seen_pages.get(p_key, 0) + 1
        selected.append(h)
        if len(selected) >= limit:
            break

    return selected


_memoized_reranker: tuple[tuple[object, ...], Reranker | None] | None = None


def _reranker_config_key(settings: Settings) -> tuple[object, ...]:
    """Value key over every Settings field build_reranker()/HttpReranker
    consume (issue #156). The memo used id(settings): ids can be recycled
    by the allocator after a garbage-collected Settings, silently reusing
    a reranker built for a previous configuration. Equal values are safe
    to reuse by construction, so the key is the values themselves."""
    return (
        settings.embed_mode,
        settings.rerank_base_url,
        settings.embed_base_url,
        settings.rerank_model,
        settings.rerank_batch_size,
        settings.rerank_timeout_s,
        settings.allow_hash_mode,
        settings.http_connect_retries,
    )


def _rerank_bypass_reason(query: str, has_identifiers: bool) -> str | None:
    """Shared bypass classification for both twins (one rule, one helper —
    the reason string lands on the trace and must never diverge between
    search() and async_search())."""
    if screen_query(query) == "trap":
        return "trap"
    if has_identifiers:
        return "identifier"
    return None


def _resolve_active_reranker(
    settings: Settings | None,
    reranker: Reranker | None,
    query: str,
    has_identifiers: bool,
) -> tuple[Reranker | None, bool, str | None]:
    """Reranker dispatch shared by both twins: explicit client wins, else the
    flag-built memoized one; trap/identifier queries bypass (RRF order
    stands). Returns (active_reranker_or_None, rerank_active, bypass_reason).

    Issue #113: trap-class (injection) queries bypass rerank — however
    the reranker arrived (flag-built, memoized, or explicitly passed
    by the lifespan client). RRF order stands, so the must_not
    hard-zero holds with RERANK_ENABLED=true. Prefetch also drops to
    the non-rerank limit: no 50-candidate fetch for a leg that will
    not run.
    Issue #117: identifier-kind queries bypass rerank on the same
    structural principle — rerank authority scales with anchor trust.
    The cross-encoder scores shape-compatibility, so on exact-code
    queries it prefers confident definitions of the WRONG message and
    buries the right context (DSN9022I live: CE rank 13+ over RRF
    rank 1 — no rank-fusion k can bridge that gap, k cancels out).
    RRF's lexical anchor is the trustworthy signal here; the CE
    keeps serving NL queries, where it earns its keep (PAR-10/19)."""
    active_reranker = reranker
    if active_reranker is None and settings and settings.rerank_enabled:
        global _memoized_reranker
        key = _reranker_config_key(settings)
        if _memoized_reranker is None or _memoized_reranker[0] != key:
            from mainframe_rag.retrieve.rerank import build_reranker

            _memoized_reranker = (key, build_reranker(settings))
        active_reranker = _memoized_reranker[1]
    bypass_reason = _rerank_bypass_reason(query, has_identifiers)
    if active_reranker is not None and bypass_reason is not None:
        active_reranker = None
    return active_reranker, active_reranker is not None, bypass_reason


def _effective_query(settings: Settings | None, query: str) -> str:
    """Deterministic acronym expansion (issue #82) feeds both retrieval legs
    (and rerank scoring below). Identifiers, filters, and the returned
    query_kind stay on the operator's original query."""
    if settings and settings.acronym_expansion_enabled and should_rewrite(query):
        return expand_query(query)
    return query


def _split_paths_for(settings: Settings | None, query: str) -> tuple[list[str], str]:
    """Split gate shared by both twins (one rule per concept): flags off or
    no settings → legacy single path. Otherwise split_query owns trap and
    identifier bypass, so the twins cannot diverge on gating."""
    if settings is None or (
        not settings.comparative_split_enabled and not settings.diagnostic_dualpath_enabled
    ):
        return ([query], "single")
    return split_query(
        query,
        comparative_enabled=settings.comparative_split_enabled,
        diagnostic_enabled=settings.diagnostic_dualpath_enabled,
    )


def _prefetch_limit_for(settings: Settings | None, rerank_active: bool) -> int:
    return settings.rerank_candidates if (settings and rerank_active) else PREFETCH_LIMIT


def _needs_filter_fallback(
    dense_points: list[models.ScoredPoint],
    sparse_points: list[models.ScoredPoint],
    flt: models.Filter | None,
    fallback_flt: models.Filter | None = None,
) -> bool:
    """One rule for the empty-filtered retry (both twins share it): only when
    a filter was applied, both legs came back empty, and the filter can be relaxed.
    Non-empty filtered results take the byte-identical legacy path — no second call."""
    if flt is not None and not dense_points and not sparse_points:
        return not (fallback_flt is not None and flt == fallback_flt)
    return False


def _retrieve_span_attrs(
    limit: int,
    rerank_active: bool,
    prefetch_limit: int,
    flt: models.Filter | None,
    bypass_reason: str | None,
    split_paths: int,
    split_mode: str,
) -> dict[str, str | bool | int | float]:
    # No raw query text (issue #529 OBS-1A): kind/counts/config only.
    attrs: dict[str, str | bool | int | float] = {
        "rag.limit": limit,
        "rag.rerank_active": rerank_active,
        "rag.prefetch_limit": prefetch_limit,
        "rag.filter_present": flt is not None,
        "rag.split_paths": split_paths,
        "rag.split_mode": split_mode,
    }
    if bypass_reason is not None:
        attrs["rag.rerank_bypass_reason"] = bypass_reason
    return attrs


def _dense_search_params(settings: Settings | None) -> models.SearchParams | None:
    """The pinned search-time params of the dense leg (issue #842): HNSW
    `ef` and the INT8 quantization rescore/oversampling. One owner for every
    dense query (first attempt, filter-fallback retry, every split leg, pool
    capture); an unset field is omitted so the server default stays in force
    for it, and no pinned field at all sends no params object."""
    if settings is None:
        return None
    quantization = None
    if settings.dense_quant_rescore is not None or settings.dense_quant_oversampling is not None:
        quantization = models.QuantizationSearchParams(
            rescore=settings.dense_quant_rescore,
            oversampling=settings.dense_quant_oversampling,
        )
    if settings.dense_hnsw_ef is None and quantization is None:
        return None
    return models.SearchParams(hnsw_ef=settings.dense_hnsw_ef, quantization=quantization)


def _build_prefetch_requests(
    dense_vec,
    sparse_idx,
    sparse_val,
    flt: models.Filter | None,
    prefetch_limit: int,
    dense_params: models.SearchParams | None = None,
) -> tuple[models.QueryRequest, models.QueryRequest]:
    dense_req = models.QueryRequest(
        query=dense_vec,
        using="dense",
        limit=prefetch_limit,
        filter=flt,
        params=dense_params,
        with_payload=list(RETRIEVE_PAYLOAD_FIELDS),
    )
    sparse_req = models.QueryRequest(
        query=models.SparseVector(indices=sparse_idx, values=sparse_val),
        using="bm25",
        limit=prefetch_limit,
        filter=flt,
        with_payload=list(RETRIEVE_PAYLOAD_FIELDS),
    )
    return dense_req, sparse_req


def _type_boosts(settings: Settings | None) -> dict[str, float] | None:
    """Sparse-leg per-type boosts for rrf_fuse (issue #216, one rule per
    concept — both twins share it): None when settings are absent or both
    factors are 1.0, so legacy fusion stays byte-identical."""
    if settings is None:
        return None
    boosts: dict[str, float] = {}
    if settings.rrf_sparse_boost_syntax != 1.0:
        boosts["syntax"] = settings.rrf_sparse_boost_syntax
    if settings.rrf_sparse_boost_table != 1.0:
        boosts["table"] = settings.rrf_sparse_boost_table
    return boosts or None


def _ranking_params(
    settings: Settings | None, has_identifiers: bool
) -> tuple[tuple[float, float], int, int, int]:
    """(weights, k, max_per_page, max_per_doc): Settings when present, else
    the module-constant fallback both twins shared before."""
    if settings:
        weights = (
            (settings.rrf_weight_dense_identifier, settings.rrf_weight_sparse_identifier)
            if has_identifiers
            else (settings.rrf_weight_dense_nl, settings.rrf_weight_sparse_nl)
        )
        k = settings.rrf_k
        max_per_page = settings.retrieve_max_chunks_per_page
        max_per_doc = settings.retrieve_max_chunks_per_doc
    else:
        weights = RRF_WEIGHTS_IDENTIFIER if has_identifiers else RRF_WEIGHTS_NL
        k = RRF_K
        max_per_page = 1
        max_per_doc = 3
    return weights, k, max_per_page, max_per_doc


def _rrf_span_attrs(
    weights: tuple[float, float],
    k: int,
    candidates_in: int,
    type_boosts: dict[str, float] | None = None,
) -> dict:
    attrs: dict = {
        "rag.rrf_k": k,
        "rag.rrf_weights": f"{weights[0]:g},{weights[1]:g}",
        "rag.candidates_in": candidates_in,
    }
    if type_boosts:
        attrs["rag.rrf_type_boosts"] = ",".join(f"{t}={v:g}" for t, v in sorted(type_boosts.items()))
    return attrs


def _fuse_with_span(
    dense_points: list[models.ScoredPoint],
    sparse_points: list[models.ScoredPoint],
    weights: tuple[float, float],
    k: int,
    limit: int,
    type_boosts: dict[str, float] | None = None,
    entry_ids: Sequence[str] = (),
) -> list[SearchHit]:
    with start_as_current_span(
        tracer,
        "retrieve.rrf",
        attributes=_rrf_span_attrs(weights, k, len(dense_points) + len(sparse_points), type_boosts),
    ):
        return rrf_fuse(
            dense_points, sparse_points, weights, k=k, limit=limit, type_boosts=type_boosts,
            entry_ids=entry_ids,
        )


def _diversify_with_span(
    fused: list[SearchHit], limit: int, max_per_page: int, max_per_doc: int
) -> list[SearchHit]:
    with start_as_current_span(tracer, "retrieve.diversify") as dv_span:
        hits = diversify_hits(fused, limit=limit, max_per_page=max_per_page, max_per_doc=max_per_doc)
        dv_span.set_attributes(
            {
                "rag.candidates_in": len(fused),
                "rag.candidates_out": len(hits),
            }
        )
    return hits


def search(
    client: QdrantReader,
    embedder: Embedder,
    collection: str,
    query: str,
    product: str | None = None,
    version: str | None = None,
    limit: int = 8,
    settings: Settings | None = None,
    reranker: Reranker | None = None,
) -> tuple[list[SearchHit], str, dict[str, int]]:
    """Sync entry point: runs the async core to completion.

    Returns (hits, query_kind, timing_ms). Filters applied inside prefetch.

    `async_search()` is the single implementation; this wrapper exists for
    sync callers only (evals, tooling, scripts — audited: no async-context
    caller). Fail-closed inside a running loop: nesting `asyncio.run`
    deadlocks, so async callers must await `async_search()` instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "search() cannot run inside a running event loop "
            "(asyncio.run would deadlock); await async_search() instead."
        )
    return asyncio.run(
        async_search(
            AsyncReaderAdapter(client),
            embedder,
            collection,
            query,
            product=product,
            version=version,
            limit=limit,
            settings=settings,
            reranker=reranker,
        )
    )


def _query_filters(
    identifiers: QueryIdentifiers, product: str | None, version: str | None
) -> tuple[models.Filter | None, models.Filter | None]:
    """The constraint filter for a query and its relaxed retry filter: the
    retry keeps the scope and excludes known-wrong codes (Invariant D1)."""
    flt = build_filter(identifiers, product=product, version=version)
    fallback_flt = build_scope_filter(product=product, version=version)
    fallback_flt = build_fallback_filter(identifiers, scope=fallback_flt)
    return flt, fallback_flt


async def _prefetch_leg_points(
    client: AsyncQdrantReader,
    collection: str,
    leg_vecs: list[tuple[list[float], list[int], list[float]]],
    flt: models.Filter | None,
    fallback_flt: models.Filter | None,
    prefetch_limit: int,
    dense_params: models.SearchParams | None = None,
) -> list[tuple[list[models.ScoredPoint], list[models.ScoredPoint], bool]]:
    """The production prefetch, one owner for search and pool capture: per
    leg, one batched dense + BM25 query under the ORIGINAL filter (splitting
    changes ranking text only, never the constraint allowlist, so a stripped
    cause leg cannot surface must_not docs the symptom filter excluded), and
    the empty-filtered retry under the relaxed filter. Returns (dense,
    sparse, retried) per leg. `dense_params` rides every dense request,
    the retry included."""
    legs = []
    for dense_vec, sparse_idx, sparse_val in leg_vecs:
        dense_req, sparse_req = _build_prefetch_requests(
            dense_vec, sparse_idx, sparse_val, flt, prefetch_limit, dense_params
        )
        responses = await client.query_batch_points(collection, requests=[dense_req, sparse_req])
        dense_points, sparse_points = responses[0].points, responses[1].points
        retried = _needs_filter_fallback(dense_points, sparse_points, flt, fallback_flt)
        if retried:
            dense_req, sparse_req = _build_prefetch_requests(
                dense_vec, sparse_idx, sparse_val, fallback_flt, prefetch_limit, dense_params
            )
            responses = await client.query_batch_points(
                collection, requests=[dense_req, sparse_req]
            )
            dense_points, sparse_points = responses[0].points, responses[1].points
        legs.append((dense_points, sparse_points, retried))
    return legs


async def async_search(
    client: AsyncQdrantReader,
    embedder: Embedder,
    collection: str,
    query: str,
    product: str | None = None,
    version: str | None = None,
    limit: int = 8,
    settings: Settings | None = None,
    reranker: Reranker | None = None,
) -> tuple[list[SearchHit], str, dict[str, int]]:
    """Async: returns (hits, query_kind, timing_ms). Filters applied inside prefetch.

    Dense and sparse prefetch queries execute concurrently in a single HTTP
    batch call via query_batch_points.
    When reranking is enabled, fused candidates (top-50) are scored by the cross-encoder."""
    identifiers = parse_query(query)
    flt, fallback_flt = _query_filters(identifiers, product, version)

    active_reranker, rerank_active, bypass_reason = _resolve_active_reranker(
        settings, reranker, query, identifiers.has_identifiers
    )
    # Split on the operator's words (expansion never adds markers or ids);
    # each leg is expanded independently below. Single path short-circuits
    # so flags-off stays byte-identical legacy (no double expansion).
    sub_queries, split_mode = _split_paths_for(settings, query)
    query = _effective_query(settings, query)
    eff_legs = (
        [_effective_query(settings, p) for p in sub_queries] if split_mode != "single" else [query]
    )

    prefetch_limit = _prefetch_limit_for(settings, rerank_active)

    timings: dict[str, int] = {}
    span_attrs = _retrieve_span_attrs(
        limit, rerank_active, prefetch_limit, flt, bypass_reason, len(eff_legs), split_mode
    )

    with start_as_current_span(tracer, "retrieve.search", attributes=span_attrs) as span:
        # dense_query is a sync HTTP POST to the embed server and sparse_query
        # is CPU-bound FastEmbed/BM25 (the query encoder, never the document
        # one: issue #836); both are sync by protocol. Offload to a worker
        # thread — running them on the event loop would block every in-flight
        # request for the duration of the embed call (review S1).
        with start_as_current_span(
            tracer,
            "retrieve.embed",
            kind=SpanKind.CLIENT,
            attributes={"rag.embedder": type(embedder).__name__},
        ):
            t0 = time.monotonic()
            # One embed per retrieval leg, sequential (parity over fan-out;
            # each leg stays off the event loop via to_thread like today).
            leg_vecs: list[tuple[list[float], list[int], list[float]]] = []
            for eq in eff_legs:
                dense_vec = (await asyncio.to_thread(embedder.dense_query, [eq]))[0]
                sparse_idx, sparse_val = (await asyncio.to_thread(embedder.sparse_query, [eq]))[0]
                leg_vecs.append((dense_vec, sparse_idx, sparse_val))
            timings["embed_ms"] = int((time.monotonic() - t0) * 1000)

        with start_as_current_span(
            tracer,
            "retrieve.prefetch",
            kind=SpanKind.CLIENT,
            attributes={
                # Always one batch per leg (the reader protocol requires it);
                # kept for the trace attribute contract.
                "rag.batch": True,
                "rag.prefetch_limit": prefetch_limit,
            },
        ):
            t0 = time.monotonic()
            prefetched = await _prefetch_leg_points(
                client,
                collection,
                leg_vecs,
                flt,
                fallback_flt,
                prefetch_limit,
                _dense_search_params(settings),
            )
            leg_dense_points = [dense for dense, _, _ in prefetched]
            leg_sparse_points = [sparse for _, sparse, _ in prefetched]
            filter_fallback = any(retried for _, _, retried in prefetched)
            timings["qdrant_ms"] = int((time.monotonic() - t0) * 1000)

        weights, k, max_per_page, max_per_doc = _ranking_params(settings, identifiers.has_identifiers)
        boosts = _type_boosts(settings)
        if split_mode == "single":
            leg_weights = [weights]
        else:
            # Per-leg ranking text, shared constraint filter: comparative
            # legs are NL; the diagnostic cause leg is identifier-stripped.
            leg_weights = [
                _ranking_params(settings, parse_query(sub).has_identifiers)[0]
                for sub in sub_queries
            ]
        # Entry-headed ranking (issue #922) follows each leg's own ranking
        # text, like the weights: a stripped cause leg ranks by score alone.
        leg_entry_ids = (
            [identifiers.message_ids]
            if split_mode == "single"
            else [parse_query(sub).message_ids for sub in sub_queries]
        )

        if rerank_active and active_reranker is not None:
            from mainframe_rag.retrieve.rerank import rerank_candidates

            rrf_limit = settings.rerank_candidates if settings else 50
            fused_lists = [
                _fuse_with_span(dp, sp, w, k, rrf_limit, type_boosts=boosts, entry_ids=eids)
                for (dp, sp), w, eids in zip(
                    zip(leg_dense_points, leg_sparse_points, strict=True),
                    leg_weights,
                    leg_entry_ids,
                    strict=True,
                )
            ]
            if split_mode == "single":
                fused = fused_lists[0]
            elif split_mode == "comparative":
                fused = interleave_split_hits(fused_lists, k, rrf_limit)
            else:
                fused = merge_split_hits(fused_lists, SPLIT_MERGE_DIAGNOSTIC, k, rrf_limit)
            t_rr = time.monotonic()
            # Cross-encoder scoring is sync HTTP (batches of settings.rerank_batch_size);
            # offload like the embed leg above (review S1).
            with start_as_current_span(
                tracer,
                "retrieve.rerank",
                kind=SpanKind.CLIENT,
                attributes={"rag.candidates": len(fused)},
            ) as rr_span:
                fusion_alpha = settings.rerank_fusion_alpha if settings else 1.0
                reranked = await asyncio.to_thread(
                    rerank_candidates, query, fused, active_reranker, alpha=fusion_alpha
                )
                rr_span.set_attributes(
                    {
                        "rag.rerank_scores": ",".join(f"{h.score:.3f}" for h in reranked[:5]),
                        "rag.rerank_alpha": fusion_alpha,
                    }
                )
            timings["rerank_ms"] = int((time.monotonic() - t_rr) * 1000)
            fused = reranked
        else:
            fuse_limit = max(limit * 3, 24)
            fused_lists = [
                _fuse_with_span(dp, sp, w, k, fuse_limit, type_boosts=boosts, entry_ids=eids)
                for (dp, sp), w, eids in zip(
                    zip(leg_dense_points, leg_sparse_points, strict=True),
                    leg_weights,
                    leg_entry_ids,
                    strict=True,
                )
            ]
            if split_mode == "single":
                fused = fused_lists[0]
            elif split_mode == "comparative":
                fused = interleave_split_hits(fused_lists, k, fuse_limit)
            else:
                fused = merge_split_hits(fused_lists, SPLIT_MERGE_DIAGNOSTIC, k, fuse_limit)

        hits = _diversify_with_span(fused, limit, max_per_page, max_per_doc)
        span.set_attributes(
            {
                "rag.query_kind": query_kind(identifiers),
                "rag.hits": len(hits),
                "rag.filter_fallback": filter_fallback,
            }
        )

    return hits, query_kind(identifiers), timings
