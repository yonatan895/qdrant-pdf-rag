"""Publish-time source catalog (issue #827): one entry per published source revision.

Built from what the staging build already stores: completion markers bound to
the staging physical, plus each chunk's identity labels and physical page span
(`page_start`/`page_end`, 0-based inclusive) and printed `page_label`. No chunk
payload field is added, so the extraction rules version and stored vectors are
unchanged. Entries live in the build's control collection, are written before
the publication receipt (so the content seal covers them), and the receipt
binds their count and digest (`build_schema` 3).

Entries nest their fields under `entry` and carry no top-level `doc_id`,
`source_rev` or other completion keys: control readers treat a top-level
`doc_id` as a completion marker (`stale_completion_markers`). Never stores
manual text, headings, paths or vectors beyond the receipt-style dummies.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import TYPE_CHECKING, Any

from qdrant_client import models

if TYPE_CHECKING:
    from mainframe_rag.ports import QdrantPoints, QdrantReader

CATALOG_SCHEMA = 1
CATALOG_RECORD_TYPE = "source-catalog-entry"

# The one projection the catalog reads from data points: identity labels and
# page location only, never text, headings or context.
DATA_FIELDS = ("source_rev", "doc_id", "sha256", "title", "product", "version",
               "page_start", "page_end", "page_label")

_ENTRY_KEYS = frozenset({"source_revision", "source_sha256", "doc_id", "title", "product", "version",
                         "chunk_count", "pages", "page_labels", "indexed_pages"})
_LABEL_RANGE = "–"
_PAGE_SIZE = 256


class SourceCatalogError(RuntimeError):
    """The build's catalog cannot be built or does not match the build: publication refuses."""


def catalog_point_id(control: str, source_rev: str) -> str:
    """Deterministic per build and revision, like `completion_point_id`."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"source-catalog|{control}|{source_rev}"))


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def catalog_summary(entries: list[dict]) -> dict:
    """The receipt's binding of the exact entry set: schema, count and digest."""
    digest = hashlib.sha256(_canonical(["source-catalog", CATALOG_SCHEMA, len(entries)]))
    for entry in sorted(entries, key=lambda e: e["source_revision"]):
        digest.update(hashlib.sha256(_canonical(entry)).digest())
    return {"schema": CATALOG_SCHEMA, "count": len(entries), "sha256": digest.hexdigest()}


def decode_catalog_summary(value: object) -> dict:
    """A present summary must be exact; the caller decides whether one is required."""
    if not isinstance(value, dict) or set(value) != {"schema", "count", "sha256"}:
        raise ValueError("invalid source catalog summary")
    if type(value["schema"]) is not int or value["schema"] != CATALOG_SCHEMA:
        raise ValueError("unsupported source catalog")
    if type(value["count"]) is not int or value["count"] < 1:
        raise ValueError("invalid source catalog count")
    if not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
        raise ValueError("invalid source catalog digest")
    return value


def _edge_label(label: object, side: int) -> str | None:
    """The printed label of one end of a chunk's span, only when it is
    unambiguous: a single label, or one side of exactly one `a–b` range.
    '' (no honest label, issue #271) and anything else read as None."""
    if not isinstance(label, str) or not label:
        return None
    parts = label.split(_LABEL_RANGE)
    if len(parts) == 1:
        return label
    if len(parts) == 2 and parts[side]:
        return parts[side]
    return None


def _indexed_pages(spans: set[tuple[int, int]]) -> int:
    total, end = 0, -1
    for start, stop in sorted(spans):
        if stop <= end:
            continue
        total += stop - max(start, end + 1) + 1
        end = stop
    return total


def _bound_markers(client: QdrantReader, control: str, physical: str) -> dict[str, Any]:
    """Completion markers that certify this physical (inherited ancestor
    markers certify their own target and are not sources of this build)."""
    from pydantic import ValidationError

    from mainframe_rag.ingest.completion import CompletionRecord

    markers: dict[str, CompletionRecord] = {}
    offset = None
    while True:
        records, offset = client.scroll(control, limit=_PAGE_SIZE, offset=offset, with_payload=True)
        for record in records:
            payload = record.payload or {}
            if "record_type" in payload or payload.get("target_collection") != physical:
                continue
            try:
                marker = CompletionRecord.model_validate(payload)
            except ValidationError as exc:
                raise SourceCatalogError("a completion marker of this build is unreadable") from exc
            if marker.source_rev in markers:
                raise SourceCatalogError("a source revision has two completion markers in this build")
            markers[marker.source_rev] = marker
        if offset is None or not records:
            break
    if not markers:
        raise SourceCatalogError("the build has no completion markers")
    return markers


def build_source_catalog(client: QdrantReader, physical: str) -> list[dict]:
    """One entry per source revision of the staging build, verified against
    its completion markers. Raises SourceCatalogError on any inconsistency."""
    control = physical + "__completions"
    markers = _bound_markers(client, control, physical)
    acc: dict[str, dict] = {}
    offset = None
    while True:
        records, offset = client.scroll(physical, limit=_PAGE_SIZE, offset=offset,
                                        with_payload=list(DATA_FIELDS))
        for record in records:
            payload = record.payload or {}
            rev = payload.get("source_rev")
            if not isinstance(rev, str) or not rev:
                raise SourceCatalogError("a stored chunk has no source revision")
            start, end = payload.get("page_start"), payload.get("page_end")
            if (type(start) is not int or type(end) is not int or start < 0 or end < start):
                raise SourceCatalogError("a stored chunk has an invalid page span")
            a = acc.setdefault(rev, {"count": 0, "labels": {k: set() for k in DATA_FIELDS[1:6]},
                                     "spans": set(), "first": None, "last": None})
            a["count"] += 1
            for key in DATA_FIELDS[1:6]:
                value = payload.get(key)
                if value is not None and not isinstance(value, str):
                    raise SourceCatalogError("a stored chunk has a non-text identity label")
                a["labels"][key].add(value)
            a["spans"].add((start, end))
            first = (start, _edge_label(payload.get("page_label"), 0))
            last = (end, _edge_label(payload.get("page_label"), -1))
            a["first"] = _pick_edge(a["first"], first, lower=True)
            a["last"] = _pick_edge(a["last"], last, lower=False)
        if offset is None or not records:
            break
    if set(acc) != set(markers):
        raise SourceCatalogError("stored source revisions do not match the build's completion markers")
    entries = []
    for rev in sorted(acc):
        a, marker = acc[rev], markers[rev]
        labels = {}
        for key, values in a["labels"].items():
            if len(values) != 1:
                raise SourceCatalogError("a source revision's chunks disagree on its labels")
            labels[key] = next(iter(values))
        if (a["count"] != marker.expected_chunks or labels["doc_id"] != marker.doc_id
                or labels["sha256"] != marker.sha256 or not rev.endswith("|" + marker.sha256)):
            raise SourceCatalogError("a source revision does not match its completion marker")
        first_page, first_label = a["first"]
        last_page, last_label = a["last"]
        entries.append({
            "source_revision": rev,
            "source_sha256": marker.sha256,
            "doc_id": marker.doc_id,
            "title": labels["title"],
            "product": labels["product"],
            "version": labels["version"],
            "chunk_count": a["count"],
            "pages": {"first": first_page, "last": last_page},
            "page_labels": {"first": first_label, "last": last_label},
            "indexed_pages": _indexed_pages(a["spans"]),
        })
    return entries


def _pick_edge(current: tuple[int, str | None] | None, candidate: tuple[int, str | None],
               *, lower: bool) -> tuple[int, str | None]:
    """The extreme physical page; among chunks sharing it, one agreed label
    or None (chunks that disagree on a page's printed label locate nothing)."""
    if current is None:
        return candidate
    if candidate[0] != current[0]:
        better = candidate[0] < current[0] if lower else candidate[0] > current[0]
        return candidate if better else current
    if current[1] is None or candidate[1] is None:
        return (current[0], current[1] or candidate[1])
    return current if current[1] == candidate[1] else (current[0], None)


def _catalog_point_ids(client: QdrantReader, control: str) -> list[str]:
    ids: list[str] = []
    offset = None
    while True:
        records, offset = client.scroll(control, limit=_PAGE_SIZE, offset=offset, with_payload=["record_type"])
        ids.extend(str(r.id) for r in records if (r.payload or {}).get("record_type") == CATALOG_RECORD_TYPE)
        if offset is None or not records:
            break
    return ids


def write_source_catalog(client: QdrantPoints, physical: str, entries: list[dict], dense_dim: int) -> None:
    """Replace every catalog entry in the staging control collection (the
    clone inherits the ancestor's entries, as it does its receipt)."""
    control = physical + "__completions"
    stale = _catalog_point_ids(client, control)
    if stale:
        client.delete(control, points_selector=models.PointIdsList(points=sorted(stale)), wait=True)
    points = [
        models.PointStruct(
            id=catalog_point_id(control, entry["source_revision"]),
            vector={"dense": [0.0] * dense_dim, "bm25": models.SparseVector(indices=[0], values=[1.0])},
            # A copy: the stored payload must never alias the caller's entries.
            payload={"record_type": CATALOG_RECORD_TYPE, "catalog_schema": CATALOG_SCHEMA,
                     "entry": json.loads(_canonical(entry))},
        )
        for entry in entries
    ]
    for i in range(0, len(points), _PAGE_SIZE):
        client.upsert(control, points=points[i : i + _PAGE_SIZE], wait=True)


def read_source_catalog(client: QdrantReader, control: str) -> list[dict]:
    """Every stored entry, validated and sorted. A malformed entry raises."""
    entries: list[dict] = []
    offset = None
    while True:
        records, offset = client.scroll(control, limit=_PAGE_SIZE, offset=offset, with_payload=True)
        for record in records:
            payload = record.payload or {}
            if payload.get("record_type") != CATALOG_RECORD_TYPE:
                continue
            entry = payload.get("entry")
            if (set(payload) != {"record_type", "catalog_schema", "entry"}
                    or payload["catalog_schema"] != CATALOG_SCHEMA
                    or not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS
                    or not isinstance(entry["source_revision"], str)
                    or str(record.id) != catalog_point_id(control, entry["source_revision"])):
                raise SourceCatalogError("a stored source catalog entry is malformed")
            entries.append(entry)
        if offset is None or not records:
            break
    return sorted(entries, key=lambda e: e["source_revision"])


def verify_source_catalog(client: QdrantReader, control: str, summary: object) -> list[dict]:
    """Stored entries must equal the receipt's binding exactly."""
    expected = decode_catalog_summary(summary)
    entries = read_source_catalog(client, control)
    if catalog_summary(entries) != expected:
        raise SourceCatalogError("stored source catalog does not match its publication receipt")
    return entries
