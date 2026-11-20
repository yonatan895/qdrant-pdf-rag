# mypy: disallow_untyped_defs=True, disallow_untyped_calls=True, disallow_any_generics=True, warn_return_any=True, no_implicit_reexport=True, strict_equality=True, warn_unused_ignores=True
"""Neighbour expansion on the answer path (issue #1314).

After retrieval (and candidate selection), each hit's reading-order
neighbours within PAGE_WINDOW pages are appended as separate hits. Nothing
is stored for this: a chunk's ordinal is recovered by matching its point id
against the chunk-key contract (ingest.chunk.make_chunk_id), and reading
order is claimed only where the stored fields prove it. Everything else
stops the walk in that direction (fail closed), and any lookup failure
leaves the hits exactly as they were.

This module owns the lookup, the order rules and the budget rule;
answer_core.retrieve_for_answer owns when it runs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from mainframe_rag.agent.core_ports import StoredChunkReader
from mainframe_rag.config import Settings
from mainframe_rag.ingest.chunk import make_chunk_id
from mainframe_rag.logs import error_type
from mainframe_rag.retrieve.filters import build_scope_filter
from mainframe_rag.retrieve.query import NeighbourHit, SearchHit, hit_from_payload

if TYPE_CHECKING:
    from qdrant_client import models

log = logging.getLogger(__name__)

# A neighbour starts at most this many pages before or after its hit.
PAGE_WINDOW = 2
# One window scroll's limit. A window holding more is not read further: the
# scroll reports it as truncated and that hit is not expanded.
WINDOW_SCROLL_LIMIT = 65
# Ordinals tried when matching a section's point ids; a section whose chunks
# in the window lie past it is not orderable.
ORDINAL_SCAN_MAX = 1024
# The identity read carries no text.
IDENTITY_FIELDS = ["source_rev", "heading_path", "page_start", "page_end"]
_SCOPE_FIELDS = ("product", "version")

ExpandMode = Literal["expanded", "skipped", "fallback"]
ExpandReason = Literal["no_hits", "no_budget", "no_reader", "timeout", "lookup_error"]


@dataclass(frozen=True)
class ExpandOutcome:
    """What the stage did on one turn, text-free: fixed labels and counts.
    'added' neighbours were appended, to 'expanded' of the hits; 'stopped'
    directions ended on a fail-closed rule rather than at the window's edge
    or the per-hit cap; 'budget_cut' neighbours were found and left out
    because the budget was full."""

    mode: ExpandMode
    added: int = 0
    expanded: int = 0
    stopped: int = 0
    budget_cut: int = 0
    reason: ExpandReason | None = None

    def span_attributes(self) -> dict[str, str | int]:
        attributes: dict[str, str | int] = {
            "rag.expand_mode": self.mode,
            "rag.expand_added": self.added,
            "rag.expand_hits": self.expanded,
            "rag.expand_stopped": self.stopped,
            "rag.expand_budget_cut": self.budget_cut,
        }
        if self.reason is not None:
            attributes["rag.expand_reason"] = self.reason
        return attributes


def _page(raw: object) -> int | None:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else None


@dataclass(frozen=True)
class _Stored:
    chunk_id: str
    heading: str  # the raw stored heading_path: the chunk key's segment
    page_start: int
    page_end: int | None
    payload: dict[str, Any]


class _Window:
    """Every stored chunk of one source revision starting within PAGE_WINDOW
    pages of a hit's page, and the reading order those chunks prove."""

    def __init__(
        self, source_rev: str, last_page: int, records: Sequence[models.Record], truncated: bool,
    ) -> None:
        self.source_rev = source_rev
        self.last_page = last_page
        # A truncated scroll, or a point without the identity fields, means
        # the window is not known whole: nothing is ordered from it.
        self.whole = not truncated
        self.by_id: dict[str, _Stored] = {}
        self._pages: dict[int, list[_Stored]] = {}
        self._sections: dict[str, list[_Stored]] = {}
        self._ordinals: dict[str, dict[str, int] | None] = {}
        self._orders: dict[int, list[_Stored] | None] = {}
        for record in records:
            payload = record.payload or {}
            heading, start = payload.get("heading_path"), _page(payload.get("page_start"))
            if not isinstance(heading, str) or start is None or payload.get("source_rev") != source_rev:
                self.whole = False
                continue
            chunk = _Stored(str(record.id), heading, start, _page(payload.get("page_end")), payload)
            self.by_id[chunk.chunk_id] = chunk
            self._pages.setdefault(start, []).append(chunk)
            self._sections.setdefault(heading, []).append(chunk)
        self.pages = sorted(self._pages)

    def ordinals(self, heading: str) -> dict[str, int] | None:
        """Each of the section's chunks in the window to its ordinal, by
        matching point ids against the chunk key; None when a chunk matches
        no ordinal or two chunks share one (a heading in several runs)."""
        if heading not in self._ordinals:
            self._ordinals[heading] = self._match_ordinals(heading)
        return self._ordinals[heading]

    def _match_ordinals(self, heading: str) -> dict[str, int] | None:
        chunks = self._sections[heading]
        wanted = {chunk.chunk_id for chunk in chunks}
        starts = sorted({chunk.page_start for chunk in chunks})
        found: dict[str, int] = {}
        for ordinal in range(ORDINAL_SCAN_MAX):
            matched = [
                chunk_id for start in starts
                if (chunk_id := make_chunk_id(self.source_rev, heading, start, ordinal)) in wanted
                and self.by_id[chunk_id].page_start == start
            ]
            if len(matched) > 1:
                return None
            for chunk_id in matched:
                found[chunk_id] = ordinal
            if len(found) == len(wanted):
                return found
        return None

    def ordinal(self, chunk: _Stored) -> int | None:
        ordinals = self.ordinals(chunk.heading)
        return None if ordinals is None else ordinals[chunk.chunk_id]

    def page_order(self, page: int) -> list[_Stored] | None:
        """The chunks starting on 'page' in reading order, or None when the
        stored fields do not prove one."""
        if page not in self._orders:
            self._orders[page] = self._order_page(page)
        return self._orders[page]

    def _order_page(self, page: int) -> list[_Stored] | None:
        """Within a section, ordinal order. Across the sections of one page:
        at most one continuing from an earlier page (first), at most one
        starting and ending on this page (next), at most one starting here
        and running on (last). A section starts here when its ordinal-0
        chunk does; it runs on when a chunk of it ends or starts on a later
        page; it is confined only when a later page of the window shows it
        has no more chunks. Anything else is not orderable."""
        by_section: dict[str, list[_Stored]] = {}
        for chunk in self._pages[page]:
            by_section.setdefault(chunk.heading, []).append(chunk)
        continuing: list[str] = []
        confined: list[str] = []
        running_on: list[str] = []
        for heading, chunks in by_section.items():
            ordinals = self.ordinals(heading)
            if ordinals is None:
                return None
            chunks.sort(key=lambda chunk: ordinals[chunk.chunk_id])
            if ordinals[chunks[0].chunk_id] != 0:
                continuing.append(heading)
            elif self._runs_past(heading, page):
                running_on.append(heading)
            elif len(by_section) > 1 and not self._ends_on(heading, page):
                return None
            else:
                confined.append(heading)
        if max(len(continuing), len(confined), len(running_on)) > 1:
            return None
        return [chunk for heading in (*continuing, *confined, *running_on)
                for chunk in by_section[heading]]

    def _runs_past(self, heading: str, page: int) -> bool:
        return any(
            chunk.page_start > page or (chunk.page_end is not None and chunk.page_end > page)
            for chunk in self._sections[heading]
        )

    def _ends_on(self, heading: str, page: int) -> bool:
        return page < self.last_page and all(
            chunk.page_end is not None and chunk.page_end <= page
            for chunk in self._sections[heading]
        )

    def run(
        self, hit: _Stored, step: Literal[1, -1], cap: int, in_scope: _ScopeCheck,
    ) -> tuple[list[_Stored], bool]:
        """Up to 'cap' chunks after (step 1) or before (step -1) the hit in
        reading order, nearest first, and whether the walk was stopped by a
        rule (True) or ended at the cap or the window's edge (False)."""
        order = self.page_order(hit.page_start)
        if order is None:
            return [], True
        position, page_at = order.index(hit), self.pages.index(hit.page_start)
        run: list[_Stored] = []
        current = hit
        while len(run) < cap:
            position += step
            if not 0 <= position < len(order):
                page_at += step
                if not 0 <= page_at < len(self.pages):
                    return run, False
                next_order = self.page_order(self.pages[page_at])
                if next_order is None:
                    return run, True
                order, position = next_order, (0 if step == 1 else len(next_order) - 1)
            candidate = order[position]
            earlier, later = (current, candidate) if step == 1 else (candidate, current)
            before, after = self.ordinal(earlier), self.ordinal(later)
            if before is None or after is None:
                return run, True
            # Same section: the next ordinal, nothing missing between. Another
            # section: only into, or back out of, that section's first chunk.
            if after != (before + 1 if earlier.heading == later.heading else 0):
                return run, True
            if not in_scope(candidate.payload):
                return run, True
            run.append(candidate)
            current = candidate
        return run, False


class _ScopeCheck:
    """A neighbour must be in the request's product/version scope (the
    retrieval scope filter's own conditions) and carry its hit's product
    and version."""

    def __init__(self, product: str | None, version: str | None, hit_payload: dict[str, Any]):
        scope = build_scope_filter(product=product, version=version)
        must = [] if scope is None or not isinstance(scope.must, list) else scope.must
        self._required: dict[str, object] = {}
        # A scope condition this check cannot read admits nothing.
        self._readable = True
        for cond in must:
            key = getattr(cond, "key", None)
            value = getattr(getattr(cond, "match", None), "value", None)
            if isinstance(key, str) and value is not None:
                self._required[key] = value
            else:
                self._readable = False
        self._hit = hit_payload

    def __call__(self, payload: dict[str, Any]) -> bool:
        if any(payload.get(key) != self._hit.get(key) for key in _SCOPE_FIELDS):
            return False
        return self._readable and all(
            payload.get(key) == value for key, value in self._required.items())


async def _lookup(
    reader: StoredChunkReader, collection: str, hits: Sequence[SearchHit],
) -> tuple[dict[str, tuple[str, int]], dict[tuple[str, int], _Window]]:
    """Call 1: the hits' identity fields. Then one scroll per distinct
    (source_rev, page) among them, concurrently: at most 1 + len(hits)
    reads. A failed scroll cancels the others."""
    from qdrant_client import models

    records = await reader.retrieve(
        collection, [hit.chunk_id for hit in hits], with_payload=IDENTITY_FIELDS)
    identities: dict[str, tuple[str, int]] = {}
    for record in records:
        payload = record.payload or {}
        source_rev, start = payload.get("source_rev"), _page(payload.get("page_start"))
        if isinstance(source_rev, str) and source_rev and start is not None:
            identities[str(record.id)] = (source_rev, start)

    async def window(source_rev: str, page: int) -> _Window:
        found, more = await reader.scroll(
            collection,
            scroll_filter=models.Filter(must=[
                models.FieldCondition(key="source_rev", match=models.MatchValue(value=source_rev)),
                models.FieldCondition(key="page_start", range=models.Range(
                    gte=max(page - PAGE_WINDOW, 0), lte=page + PAGE_WINDOW)),
            ]),
            limit=WINDOW_SCROLL_LIMIT, with_payload=True,
        )
        return _Window(source_rev, page + PAGE_WINDOW, found, truncated=more is not None)

    keys = list(dict.fromkeys(
        identities[hit.chunk_id] for hit in hits if hit.chunk_id in identities))
    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(window(*key)) for key in keys]
    return identities, {key: task.result() for key, task in zip(keys, tasks, strict=True)}


def _nearest_first(forward: list[_Stored], backward: list[_Stored], cap: int) -> list[tuple[int, _Stored]]:
    """One hit's neighbours as (offset, chunk), nearest first, the one after
    the hit before the one before it, cut to the per-hit cap."""
    out: list[tuple[int, _Stored]] = []
    for distance in range(max(len(forward), len(backward))):
        if distance < len(forward):
            out.append((distance + 1, forward[distance]))
        if distance < len(backward):
            out.append((-(distance + 1), backward[distance]))
    return out[:cap]


def _place(
    hits: Sequence[SearchHit], found: list[list[tuple[int, _Stored]]], budget: int,
) -> tuple[list[SearchHit], int, int]:
    """The one budget and placement rule. Slots go by distance from the hit
    first and the hit's rank second (every hit's nearest neighbour, then
    every hit's second nearest, ...), so a full budget drops the farthest
    neighbours of the lowest-ranked hits first. A chunk that is already a
    retrieved hit or another hit's neighbour is skipped and takes no slot.
    The neighbours are then appended after the retrieved hits, by parent
    rank and, within a parent, in reading order. Returns the list, the
    number of hits that gained a neighbour and the number cut."""
    held = {hit.chunk_id for hit in hits}
    kept: list[tuple[int, int, _Stored]] = []
    cut = 0
    for distance in range(max((len(run) for run in found), default=0)):
        for rank, run in enumerate(found):
            if distance >= len(run) or run[distance][1].chunk_id in held:
                continue
            if len(kept) >= budget:
                cut += 1
                continue
            offset, chunk = run[distance]
            held.add(chunk.chunk_id)
            kept.append((rank, offset, chunk))
    kept.sort(key=lambda entry: entry[:2])
    neighbours: list[SearchHit] = [
        NeighbourHit(
            **hit_from_payload(chunk.chunk_id, chunk.payload, hits[rank].score).model_dump(),
            neighbour_of=hits[rank].chunk_id,
        )
        for rank, _, chunk in kept
    ]
    return [*hits, *neighbours], len({rank for rank, _, _ in kept}), cut


async def expand_hits(
    reader: StoredChunkReader | None, settings: Settings, hits: list[SearchHit], *,
    product: str | None, version: str | None,
) -> tuple[list[SearchHit], ExpandOutcome]:
    """The hits with their reading-order neighbours appended. Reads stored
    points of 'settings.qdrant_collection' (the request's bound physical
    collection) under one deadline, 'evidence_timeout_s'. A timeout or any
    other failure returns 'hits' itself; cancellation propagates."""
    if not hits:
        return hits, ExpandOutcome("skipped", reason="no_hits")
    if settings.answer_expand_budget == 0:
        return hits, ExpandOutcome("skipped", reason="no_budget")
    if reader is None:
        return hits, ExpandOutcome("fallback", reason="no_reader")
    try:
        async with asyncio.timeout(settings.evidence_timeout_s):
            identities, windows = await _lookup(reader, settings.qdrant_collection, hits)
        cap = settings.answer_expand_per_hit
        found: list[list[tuple[int, _Stored]]] = []
        stopped = 0
        for hit in hits:
            window = windows.get(identities[hit.chunk_id]) if hit.chunk_id in identities else None
            stored = None if window is None or not window.whole else window.by_id.get(hit.chunk_id)
            if window is None or stored is None:
                found.append([])
                stopped += 2
                continue
            in_scope = _ScopeCheck(product, version, stored.payload)
            forward, forward_stopped = window.run(stored, 1, cap, in_scope)
            backward, backward_stopped = window.run(stored, -1, cap, in_scope)
            found.append(_nearest_first(forward, backward, cap))
            stopped += forward_stopped + backward_stopped
        packed, expanded, cut = _place(hits, found, settings.answer_expand_budget)
    except TimeoutError:
        log.warning("neighbour expansion timed out; packing the hits unchanged")
        return hits, ExpandOutcome("fallback", reason="timeout")
    except Exception as exc:  # noqa: BLE001 — an optional stage must not fail the answer
        cause = exc.exceptions[0] if isinstance(exc, ExceptionGroup) else exc
        log.warning("neighbour expansion failed (%s); packing the hits unchanged", error_type(cause))
        return hits, ExpandOutcome("fallback", reason="lookup_error")
    return packed, ExpandOutcome(
        "expanded", added=len(packed) - len(hits), expanded=expanded, stopped=stopped, budget_cut=cut)
