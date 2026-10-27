"""Shared builders and point-store doubles for the ingest unit suites.

Behavior pins stay explicit in call arguments, never in subclasses
([testing.md#shared-doubles](../docs/testing.md#shared-doubles-share-builders-pin-behavior)).
A store that upserts overwrites same-id points, retrieves vectors only when
asked, and resolves the alias map before touching a collection — the client
semantics the production call relies on (issue #391 F2/F5, #361).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from mainframe_rag.config import Settings
from mainframe_rag.ingest.chunk import Chunk, make_chunk_id
from mainframe_rag.ingest.ibm_pdf import ParsedDoc


def settings(**overrides) -> Settings:
    kw = {"embed_mode": "hash", "_env_file": None}
    kw.update(overrides)
    return Settings(**kw)


def parsed_doc(doc_id="DOC1", sha="a" * 64, vendor="v", product="p", version="1") -> ParsedDoc:
    return ParsedDoc(
        path=Path("d.pdf"), sha256=sha, doc_id=doc_id, title="t",
        product=product, version=version, vendor=vendor, page_count=1,
    )


def chunks(doc_id="DOC1", n=3, tag="body", rev=None) -> list[Chunk]:
    """Chunks with literal ids from `rev` when given, else the legacy
    f-string id shape; one chunk per page so spans stay single-page."""
    return [
        Chunk(
            chunk_id=make_chunk_id(rev, "H", i, 0) if rev is not None else f"{doc_id}-c{i:04d}",
            doc_id=doc_id, heading_path="H", page_start=i, page_label=str(i + 1),
            chunk_type="narrative", text=f"{tag} chunk {i}", message_ids=[],
            members=[], ordinal=i,
        )
        for i in range(n)
    ]


def vectors(n):
    return [([0.1] * 4, ([1], [1.0])) for _ in range(n)]


def filter_match_value(filter_obj, key: str) -> str | None:
    """Extract one MatchValue from a Filter/FilterSelector shape (one rule
    for every key — doc_id and source_rev selectors share it)."""
    filt = getattr(filter_obj, "filter", filter_obj)
    must = getattr(filt, "must", None) or []
    for cond in must:
        if getattr(cond, "key", None) == key:
            match = getattr(cond, "match", None)
            value = getattr(match, "value", None)
            if value is not None:
                return str(value)
    return None


def filter_doc_id(filter_obj) -> str | None:
    """Extract a doc_id equality value from a Filter/FilterSelector shape."""
    return filter_match_value(filter_obj, "doc_id")


class PointStore:
    """Collection-keyed QdrantPoints double with alias and pagination
    semantics. `exists=False` mirrors a server that answers every collection
    lookup; failure injection stays in the owning suite's subclass."""

    def __init__(self, dim: int = 256, *, exists: bool = True) -> None:
        self.dim = dim
        self.exists = exists
        self.collections: dict[str, list] = {}
        self.aliases: dict[str, str] = {}
        self.upserts = 0
        self.upsert_calls: list[tuple[str, int]] = []
        self.deletes = 0
        self.created_collections: list[str] = []

    @property
    def _points(self) -> dict[str, list]:
        return self.collections

    def _physical(self, name: str) -> str:
        return self.aliases.get(name, name)

    def _filtered(self, name: str, scroll_filter) -> list:
        doc_id = filter_doc_id(scroll_filter)
        rev = filter_match_value(scroll_filter, "source_rev")
        return self.points_for(name, doc_id=doc_id, source_rev=rev)

    def points_for(self, name: str, *, doc_id=None, source_rev=None) -> list:
        return [
            p
            for p in self.collections.get(self._physical(name), [])
            if (doc_id is None or (p.payload or {}).get("doc_id") == doc_id)
            and (source_rev is None or (p.payload or {}).get("source_rev") == source_rev)
        ]

    # -- collection surface -------------------------------------------
    def get_aliases(self):
        return SimpleNamespace(
            aliases=[
                SimpleNamespace(alias_name=a, collection_name=c)
                for a, c in sorted(self.aliases.items())
            ]
        )

    def collection_exists(self, name):
        return self.exists or name in self.collections

    def get_collection(self, name):
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors={"dense": SimpleNamespace(size=self.dim)},
                    shard_number=1,
                    replication_factor=1,
                )
            ),
            points_count=len(self.collections.get(self._physical(name), [])),
        )

    def create_collection(self, name, **kwargs):
        self.collections.setdefault(name, [])
        self.created_collections.append(name)
        return True

    def delete_collection(self, name):
        del self.collections[name]  # snapshots survive (server restore source)
        return True

    def create_payload_index(self, *a, **k):
        return SimpleNamespace()

    def update_collection(self, *a, **k):
        return True

    # -- points surface ------------------------------------------------
    def scroll(self, collection, *, scroll_filter=None, limit=10, with_payload=None,
               with_vectors=False, offset=None):
        stored = self._filtered(collection, scroll_filter)
        start = int(offset) if offset is not None else 0
        page = stored[start : start + limit]
        nxt = str(start + limit) if start + limit < len(stored) else None
        return page, nxt

    def retrieve(self, collection, ids, *, with_payload=True, with_vectors=False):
        wanted = {str(i) for i in ids}
        return [
            SimpleNamespace(
                id=p.id,
                payload=p.payload,
                # Real client default: no vector unless explicitly requested
                # (issue #391 F5 — a double that always returns one hides a
                # missing vector projection).
                vector=getattr(p, "vector", None) if with_vectors else None,
            )
            for p in self.collections.get(self._physical(collection), [])
            if str(p.id) in wanted
        ]

    def upsert(self, collection, *, points, wait=True):
        self.upserts += 1
        self.upsert_calls.append((collection, len(points)))
        # Production upsert overwrites same-id points (manifest recommit,
        # marker rewrite); the double must too, or reads see stale firsts.
        stored = self.collections.setdefault(self._physical(collection), [])
        ids = {str(p.id) for p in points}
        stored[:] = [p for p in stored if str(p.id) not in ids]
        stored.extend(points)
        return SimpleNamespace()

    def delete(self, collection, *, points_selector, wait=True):
        self.deletes += 1
        physical = self._physical(collection)
        ids = getattr(points_selector, "points", None)
        if ids is not None:
            # PointIdsList (precise completion invalidation, issue #361).
            wanted = {str(i) for i in ids}
            self.collections[physical] = [
                p
                for p in self.collections.get(physical, [])
                if str(getattr(p, "id", None)) not in wanted
            ]
            return SimpleNamespace()
        doc_id = filter_doc_id(points_selector)
        rev = filter_match_value(points_selector, "source_rev")
        self.collections[physical] = [
            p
            for p in self.collections.get(physical, [])
            if not (
                (doc_id is None or (p.payload or {}).get("doc_id") == doc_id)
                and (rev is None or (p.payload or {}).get("source_rev") == rev)
            )
        ]
        return SimpleNamespace()
