"""Alias-publication regressions (issue #359 req 4/5).

With INGEST_ALIAS_PUBLISH=true, ingest converges a versioned staging
generation and swaps the collection alias only after every document
verifies. Readers see a complete old or complete new generation; swap
failure keeps the previous live generation serving; superseded physicals
(plus safety snapshots) are kept for operator rollback/GC.

Hermetic: PublishFake models alias + snapshot semantics with failure
injection; real PDFs only where main() needs files (built at runtime).
"""

from __future__ import annotations

import re
from itertools import permutations
from pathlib import Path
from types import SimpleNamespace

import pytest
from qdrant_client import models

from mainframe_rag.config import Settings
from mainframe_rag.ingest.publish import (
    corpus_fingerprint,
    generation_fingerprint,
    staging_name_for,
    verify_all_complete,
    verify_staging_distribution,
    verify_staging_placement,
)
from tests.test_run_ingest import _filter_doc_id

ALIAS = "mainframe_manuals"


@pytest.fixture(autouse=True)
def _parse_in_threads(monkeypatch):
    """Parse in worker threads instead of a spawned process pool.

    Every real `run_ingest.main` here would otherwise pay a fresh interpreter
    plus a full package import (about 0.9 s). The pool is not what these tests
    claim: they prove publication, cutover and recovery against the storage
    double. The spawn pool stays covered by the unpatched ingest suites
    (tests/test_run_ingest.py, tests/test_ingest_robustness.py).
    """
    from concurrent.futures import ThreadPoolExecutor

    from mainframe_rag.ingest import run_ingest

    monkeypatch.setattr(
        run_ingest, "ProcessPoolExecutor",
        lambda max_workers, mp_context: ThreadPoolExecutor(max_workers=max_workers),
    )


def _settings(**overrides):
    kw = {"embed_mode": "hash", "_env_file": None}
    kw.update(overrides)
    return Settings(**kw)


class PublishFake:
    """QdrantPoints double with alias + snapshot/clone semantics."""

    def __init__(self, dim: int = 256):
        self.dim = dim
        self.collections: dict[str, list] = {}
        self.aliases: dict[str, str] = {}
        self.snapshots: dict[str, dict[str, list]] = {}
        self.fail_swap: str | None = None  # "raise" | "reject"
        self.fail_snapshot = False
        self.fail_recover = False
        self.drop_recovered = 0
        self._snap_counter = 0
        self.alias_calls: list[list] = []

    # -- collections -------------------------------------------------
    def collection_exists(self, name):
        return name in self.collections

    def get_collection(self, name):
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(vectors={"dense": SimpleNamespace(size=self.dim)},
                                       shard_number=1, replication_factor=1)
            ),
            points_count=len(self.collections[name]),
        )

    def create_collection(self, name, **kwargs):
        self.collections.setdefault(name, [])
        return True

    def delete_collection(self, name):
        del self.collections[name]  # snapshots survive (server-side restore source)
        return True

    def create_payload_index(self, *a, **k):
        return SimpleNamespace()

    def update_collection(self, *a, **k):
        return True

    # -- aliases ------------------------------------------------------
    def get_aliases(self):
        return SimpleNamespace(
            aliases=[
                SimpleNamespace(alias_name=a, collection_name=c)
                for a, c in sorted(self.aliases.items())
            ]
        )

    def update_collection_aliases(self, ops):
        self.alias_calls.append(list(ops))
        if self.fail_swap == "raise":
            raise RuntimeError("injected swap failure")
        if self.fail_swap == "reject":
            return False
        staged: list[tuple] = []
        for op in ops:
            delete = getattr(op, "delete_alias", None)
            create = getattr(op, "create_alias", None)
            if delete is not None:
                if delete.alias_name not in self.aliases:
                    raise RuntimeError(f"alias {delete.alias_name!r} missing")
                staged.append(("delete", delete.alias_name))
            elif create is not None:
                staged.append(("create", create.alias_name, create.collection_name))
            else:
                raise AssertionError(f"unexpected alias op: {op!r}")
        for item in staged:  # atomic: validated first, applied after
            if item[0] == "delete":
                del self.aliases[item[1]]
            else:
                self.aliases[item[1]] = item[2]
        return True

    # -- snapshots -----------------------------------------------------
    def create_snapshot(self, collection, *, wait=True):
        if self.fail_snapshot:
            raise RuntimeError("injected snapshot failure")
        self._snap_counter += 1
        name = f"{collection}-snap{self._snap_counter}.snapshot"
        self.snapshots.setdefault(collection, {})[name] = list(self.collections[collection])
        return SimpleNamespace(name=name)

    def recover_snapshot(self, collection, location, *, priority=None, wait=True):
        if self.fail_recover:
            raise RuntimeError("injected recover failure")
        parts = location.split("/")
        src, snap = parts[-2], parts[-1]
        points = list(self.snapshots[src][snap])
        if self.drop_recovered:
            points = points[self.drop_recovered :]
        self.collections[collection] = points
        return True

    # -- points ---------------------------------------------------------
    def _resolve(self, name):
        return self.collections.setdefault(self.aliases.get(name, name), [])

    def scroll(self, collection, *, scroll_filter=None, limit=10, with_payload=None, with_vectors=False, offset=None):
        from tests.test_run_ingest import _filter_match_value

        doc_id = _filter_doc_id(scroll_filter)
        rev = _filter_match_value(scroll_filter, "source_rev")
        stored = self._resolve(collection)
        if doc_id is not None:
            stored = [p for p in stored if (p.payload or {}).get("doc_id") == doc_id]
        if rev is not None:
            stored = [p for p in stored if (p.payload or {}).get("source_rev") == rev]
        start = 0 if offset is None else int(offset)
        page = stored[start : start + limit]
        next_offset = str(start + limit) if start + limit < len(stored) else None
        return page, next_offset

    def retrieve(self, collection, ids, *, with_payload=True, with_vectors=False):
        wanted = {str(i) for i in ids}
        return [
            SimpleNamespace(id=p.id, payload=p.payload, vector=p.vector if with_vectors else None)
            for p in self._resolve(collection)
            if str(p.id) in wanted
        ]

    def upsert(self, collection, *, points, wait=True):
        # Production upsert overwrites same-id points (manifest recommit,
        # marker rewrite); the double must too, or reads see stale firsts.
        physical = self.aliases.get(collection, collection)
        stored = self.collections.setdefault(physical, [])
        ids = {str(p.id) for p in points}
        stored[:] = [p for p in stored if str(p.id) not in ids]
        stored.extend(points)
        return SimpleNamespace()

    def delete(self, collection, *, points_selector, wait=True):
        from tests.test_run_ingest import _filter_match_value

        physical = self.aliases.get(collection, collection)
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
        doc_id = _filter_doc_id(points_selector)
        rev = _filter_match_value(points_selector, "source_rev")
        self.collections[physical] = [
            p
            for p in self.collections.get(physical, [])
            if not (
                (doc_id is None or (p.payload or {}).get("doc_id") == doc_id)
                and (rev is None or (p.payload or {}).get("source_rev") == rev)
            )
        ]
        return SimpleNamespace()

    def alias_target_points(self, alias):
        return list(self.collections.get(self.aliases.get(alias, ""), []))


def _publish_env(monkeypatch):
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("INGEST_ALIAS_PUBLISH", "true")
    monkeypatch.delenv("DENSE_DIM", raising=False)


def _build_doc(directory: Path, stem: str) -> Path:
    from scripts.make_synthetic_pdf import build

    out = directory / f"{stem}.pdf"
    build(out)
    return out


def _run_main(monkeypatch, corpus: Path, progress: Path, *extra: str) -> int:
    from mainframe_rag.ingest import run_ingest

    return run_ingest.main(
        ["--src", str(corpus), "--progress", str(progress), "--workers", "1", *extra]
    )


def _gen_collections(fake: PublishFake) -> list[str]:
    return sorted(n for n in fake.collections if "__gen" in n and not n.endswith("__completions"))


# ---------------------------------------------------------------- naming
def test_publication_identity_addresses_distinct_generations():
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    name = staging_name_for(ALIAS, "a" * 16, "b" * 12)
    assert re.fullmatch(r"mainframe_manuals__gen[0-9a-f]{28}", name)
    assert staging_name_for(ALIAS, "a" * 16, "b" * 12) == name
    assert staging_name_for(ALIAS, "c" * 16, "b" * 12) != name

    entries = [("b.pdf", "1" * 64), ("a.pdf", "2" * 64)]
    assert corpus_fingerprint(entries) == corpus_fingerprint(list(reversed(entries)))
    assert corpus_fingerprint(entries) != corpus_fingerprint([("a.pdf", "2" * 64)])
    assert re.fullmatch(r"[0-9a-f]{12}", corpus_fingerprint(entries))

    settings = _settings()
    rules_v = extraction_rules_version()
    base = generation_fingerprint(settings, rules_v, "||")
    assert generation_fingerprint(settings, rules_v, "||") == base
    assert generation_fingerprint(settings, rules_v, "|Solaris|") != base
    assert generation_fingerprint(settings, "0" * 16, "||") != base
    # Issue #391 F2: the operator revision addresses a distinct staging
    # generation, so a revision-only change can never reconverge live.
    assert generation_fingerprint(_settings(embed_model_revision="rev-2"), rules_v, "||") != base
    assert generation_fingerprint(_settings(dense_query_prefix="OTHER:"), rules_v, "||") == base


# ---------------------------------------------------------------- publish
def test_first_publish_creates_alias_and_clean_rerun_is_noop(tmp_path, monkeypatch):
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0

    gens = _gen_collections(fake)
    assert len(gens) == 1, f"one staging generation expected, got {gens}"
    assert fake.aliases.get(ALIAS) == gens[0]
    assert gens[0] != ALIAS
    points = fake.alias_target_points(ALIAS)
    assert points, "alias must serve the published generation"
    assert {p.payload["doc_id"] for p in points} == {"SA22-0000-00"}
    assert f"{gens[0]}__completions" in fake.collections

    n_collections = len(fake.collections)
    assert _run_main(monkeypatch, corpus, progress) == 0
    assert fake.aliases[ALIAS] == gens[0], "steady state must not flip the alias"
    assert len(fake.collections) == n_collections, "no new staging on a clean rerun"


def test_refresh_publishes_new_generation_keeps_old_and_rolls_back(tmp_path, monkeypatch):
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.qdrant_io import swap_alias_to
    from mainframe_rag.ingest.representation import read_manifest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    gen1 = fake.aliases[ALIAS]

    _build_doc(corpus, "SA22-0000-01_second")
    assert _run_main(monkeypatch, corpus, progress) == 0
    gen2 = fake.aliases[ALIAS]
    assert gen2 != gen1, "changed corpus must address a new generation"
    live_docs = {p.payload["doc_id"] for p in fake.alias_target_points(ALIAS)}
    assert live_docs == {"SA22-0000-00", "SA22-0000-01"}
    # Previous generation preserved with its own content + completions.
    assert gen1 in fake.collections
    assert {p.payload["doc_id"] for p in fake.collections[gen1]} == {"SA22-0000-00"}
    assert f"{gen1}__completions" in fake.collections
    # The new staging inherited live's contract through the clone + re-key
    # (issue #391 F5): the fake honors the real retrieve projection default,
    # so this passes only because rekey_manifest requests vectors explicitly.
    assert read_manifest(fake, f"{gen2}__completions") is not None

    # Operator rollback: re-point the alias at the kept previous generation.
    swap_alias_to(fake, _settings(), gen1, gen2)
    assert fake.aliases[ALIAS] == gen1
    assert {p.payload["doc_id"] for p in fake.alias_target_points(ALIAS)} == {"SA22-0000-00"}


def _staging_settings(collection: str) -> Settings:
    return _settings(qdrant_collection=collection)


def test_incomplete_staging_metadata_is_repaired_or_refused_never_published(tmp_path, monkeypatch):
    """Issue #391 F5: preparation interrupted after the data clone (no
    completions) or after the metadata clone (no re-key) is repaired by
    re-transferring live's contract verbatim; a source that cannot yield its
    manifest vector fails loudly. The alias and live never move."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import ensure_staging
    from mainframe_rag.ingest.representation import read_manifest

    class _NoVectorStore(PublishFake):
        omit_vectors = False

        def retrieve(self, collection, ids, *, with_payload=True, with_vectors=False):
            if not self.omit_vectors:
                return super().retrieve(collection, ids, with_payload=with_payload, with_vectors=with_vectors)
            wanted = {str(i) for i in ids}
            return [
                SimpleNamespace(id=p.id, payload=p.payload, vector=None)
                for p in self._resolve(collection)
                if str(p.id) in wanted
            ]

    _publish_env(monkeypatch)
    fake = _NoVectorStore()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    assert _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl") == 0
    live = fake.aliases[ALIAS]
    live_points = list(fake.collections[live])
    live_completions = list(fake.collections[f"{live}__completions"])
    contract = read_manifest(fake, f"{live}__completions")
    assert contract is not None

    def assert_live_untouched():
        assert fake.aliases[ALIAS] == live, "preparation never moves the alias"
        assert fake.collections[live] == live_points, "live data untouched"
        assert fake.collections[f"{live}__completions"] == live_completions, "live metadata untouched"

    # Crash after the data clone but before the completions clone.
    staging = "mainframe_manuals__genDEADBEEF0123456789ab"
    fake.collections[staging] = list(live_points)
    assert ensure_staging(fake, _settings(), _staging_settings(staging), live) == "repaired"
    assert read_manifest(fake, f"{staging}__completions") == contract
    assert_live_untouched()

    # Metadata cloned but the re-key never ran: the copied manifest point still
    # carries the live id, so staging reads no manifest until it is re-keyed.
    staging = "mainframe_manuals__genFEDCBA9876543210abcd"
    fake.collections[staging] = list(live_points)
    fake.collections[f"{staging}__completions"] = list(live_completions)
    assert ensure_staging(fake, _settings(), _staging_settings(staging), live) == "repaired"
    assert read_manifest(fake, f"{staging}__completions") == contract
    assert_live_untouched()

    # A source that cannot yield its manifest vector fails preparation.
    fake.omit_vectors = True
    with pytest.raises(RuntimeError, match="staging metadata transfer failed"):
        ensure_staging(
            fake, _settings(), _staging_settings("mainframe_manuals__gen00112233445566778899"), live
        )
    assert_live_untouched()


def test_staging_invisible_until_swap(tmp_path, monkeypatch):
    """Reads through the alias during staging see the old generation only."""

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import ensure_staging

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    assert _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl") == 0
    gen1 = fake.aliases[ALIAS]

    settings = _settings(qdrant_collection="mainframe_manuals__genDEADBEEF0123456789ab")
    assert ensure_staging(fake, _settings(), settings, gen1) == "cloned"
    fake.upsert(
        settings.qdrant_collection,
        points=[
            models.PointStruct(
                id="00000000-0000-0000-0000-000000000099",
                vector={
                    "dense": [0.0] * 256,
                    "bm25": models.SparseVector(indices=[0], values=[1.0]),
                },
                payload={"doc_id": "UNCOMMITTED", "sha256": "x", "rules_v": "y", "text": "t"},
            )
        ],
    )
    assert {p.payload["doc_id"] for p in fake.alias_target_points(ALIAS)} == {"SA22-0000-00"}


def test_swap_failure_preserves_live_then_recovers(tmp_path, monkeypatch):
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    gen1 = fake.aliases[ALIAS]

    _build_doc(corpus, "SA22-0000-01_second")
    fake.fail_swap = "raise"
    with pytest.raises(RuntimeError, match="swap failure"):
        _run_main(monkeypatch, corpus, progress)
    assert fake.aliases[ALIAS] == gen1, "failed swap keeps the previous generation live"
    assert {p.payload["doc_id"] for p in fake.alias_target_points(ALIAS)} == {"SA22-0000-00"}

    fake.fail_swap = None
    assert _run_main(monkeypatch, corpus, progress) == 0
    gen2 = fake.aliases[ALIAS]
    assert gen2 != gen1
    assert {p.payload["doc_id"] for p in fake.alias_target_points(ALIAS)} == {
        "SA22-0000-00",
        "SA22-0000-01",
    }


def test_swap_rejection_raises_without_applying(monkeypatch):
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.qdrant_io import swap_alias_to

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    fake.collections["g1"] = []
    fake.aliases[ALIAS] = "g1"
    fake.fail_swap = "reject"
    with pytest.raises(RuntimeError, match="rejected"):
        swap_alias_to(fake, _settings(), "g2", "g1")
    assert fake.aliases[ALIAS] == "g1"


def test_swap_refuses_when_the_alias_left_its_predecessor(monkeypatch):
    """Issue #1019 (S2-19): the swap re-reads the alias right before the
    atomic call and refuses when it no longer names the predecessor the
    cutover was planned against; no alias operation is sent."""
    from mainframe_rag.ingest.qdrant_io import swap_alias_to

    fake = PublishFake()
    for name in ("g1", "g2", "g3"):
        fake.collections[name] = []
    fake.aliases[ALIAS] = "g3"
    with pytest.raises(RuntimeError, match="moved during publication"):
        swap_alias_to(fake, _settings(), "g2", "g1")
    with pytest.raises(RuntimeError, match="moved during publication"):
        swap_alias_to(fake, _settings(), "g2", None)
    assert fake.aliases[ALIAS] == "g3"
    assert fake.alias_calls == []




@pytest.mark.parametrize("case", ["limit", "empty-corpus", "revision-collision"])
def test_publish_refusals_precede_any_allocation(tmp_path, monkeypatch, case):
    from scripts.make_synthetic_pdf import build as make_pdf

    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    extra: tuple[str, ...] = ()
    if case == "limit":
        _build_doc(corpus, "SA22-0000-00_first")
        extra, match = ("--limit", "1"), "refuses --limit"
    elif case == "empty-corpus":
        match = "empty corpus"
    else:
        # Two distinct byte-streams sharing one printed doc_id.
        for sub, title in (("a", "Edition one"), ("b", "Edition two")):
            (corpus / sub).mkdir()
            make_pdf(corpus / sub / "dup.pdf", doc_id="SA22-0000-00", title=title)
        match = "revision collision"
    with pytest.raises(RuntimeError, match=match):
        _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl", *extra)
    assert fake.aliases == {}
    assert not any("__gen" in c for c in fake.collections)


def test_verify_failure_blocks_swap(tmp_path, monkeypatch):
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    monkeypatch.setattr(run_ingest, "verify_all_complete", lambda *a, **k: ["ghost.pdf"])
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    with pytest.raises(RuntimeError, match="alias untouched"):
        _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl")
    assert fake.aliases == {}, "unverified staging never swaps"


def test_verify_all_complete_matrix(monkeypatch):
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.identity import source_rev_key
    from mainframe_rag.ingest.inventory import InventoryRecord
    from mainframe_rag.ingest.representation import STATE_COMMITTED, write_manifest
    from mainframe_rag.ingest.rules_version import extraction_rules_version
    from mainframe_rag.ingest.run_ingest import _DocLocks, _upsert_one
    from tests.test_ingest_completion import _chunks, _vectors

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

    rules_v = extraction_rules_version()
    staging = _settings(qdrant_collection="stg", batch_size=16)
    write_manifest(fake, "stg__completions", staging, rules_v, state=STATE_COMMITTED)
    fake.collections["stg"] = []
    chunks = _chunks(doc_id="D1", n=3)
    _upsert_one(
        _parsed_doc("D1", "a" * 64),
        chunks,
        _vectors(3),
        staging,
        _DocLocks(),
        None,
        False,
        src_labels="||",
    )

    inv: dict[str, InventoryRecord] = {
        "ok.pdf": InventoryRecord(
            path="ok.pdf",
            sha256="a" * 64,
            doc_id="D1",
            status="upserted",
            rules_version=rules_v,
            source_rev=source_rev_key("v", "p", "1", "a" * 64),
        ),
        "missing.pdf": InventoryRecord(
            path="missing.pdf",
            sha256="b" * 64,
            doc_id="D2",
            status="upserted",
            rules_version=rules_v,
        ),
        "stale.pdf": InventoryRecord(
            path="stale.pdf",
            sha256="c" * 64,
            doc_id="D1",
            status="upserted",
            rules_version="0" * 16,
        ),
        "err.pdf": InventoryRecord(
            path="err.pdf", sha256="d" * 64, doc_id="D3", status="error", rules_version=rules_v
        ),
        # Pre-361B record: matching content and generation, but no revision
        # stamp — publication cannot prove which revision, so it blocks.
        "legacy.pdf": InventoryRecord(
            path="legacy.pdf",
            sha256="a" * 64,
            doc_id="D1",
            status="upserted",
            rules_version=rules_v,
        ),
    }
    inv["unstored.pdf"] = InventoryRecord(
        path="unstored.pdf",
        sha256="f" * 64,
        doc_id="D4",
        status="upserted",
        rules_version=rules_v,
        source_rev=source_rev_key("v", "p", "1", "f" * 64),
    )
    problems = verify_all_complete(
        fake,
        staging,
        [
            ("unstored.pdf", "f" * 64),
            ("ok.pdf", "a" * 64),
            ("missing.pdf", "b" * 64),
            ("stale.pdf", "c" * 64),
            ("err.pdf", "d" * 64),
            ("ghost.pdf", "e" * 64),
            ("legacy.pdf", "a" * 64),
        ],
        inv,
        rules_v,
        "||",
    )
    assert sorted(problems) == [
        "err.pdf", "ghost.pdf", "legacy.pdf", "missing.pdf", "stale.pdf", "unstored.pdf",
    ]


def _seed_manifest_point(fake, staging, **payload):
    from mainframe_rag.ingest.representation import manifest_point_id

    completions = f"{staging.qdrant_collection}__completions"
    fake.collections[completions] = [
        SimpleNamespace(id=manifest_point_id(completions), payload=payload)
    ]


@pytest.mark.parametrize(
    "case",
    ["missing", "wrong-record-type", "manifest-not-dict", "manifest-bad-schema", "state-not-string",
     "retrieve-raises"],
)
def test_verify_all_complete_refuses_unreadable_manifest_on_populated_staging(case):
    """Invariant D2: a populated staging whose manifest is absent, corrupt or
    unreadable blocks publication."""
    from mainframe_rag.ingest.representation import STATE_COMMITTED, build_manifest
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    fake = PublishFake()
    staging = _settings(qdrant_collection="stg-unreadable")
    rules_v = extraction_rules_version()
    good = build_manifest(staging, rules_v).model_dump(mode="json")
    record = {"record_type": "representation-manifest", "manifest": good, "state": STATE_COMMITTED}
    if case == "wrong-record-type":
        _seed_manifest_point(fake, staging, **{**record, "record_type": "completion-marker"})
    elif case == "manifest-not-dict":
        _seed_manifest_point(fake, staging, **{**record, "manifest": "not a dict"})
    elif case == "manifest-bad-schema":
        _seed_manifest_point(fake, staging, **{**record, "manifest": {"bad": "schema"}})
    elif case == "state-not-string":
        _seed_manifest_point(fake, staging, **{**record, "state": 123})
    elif case == "retrieve-raises":
        fake.collections["stg-unreadable__completions"] = []

        def exploding(*args, **kwargs):
            raise RuntimeError("Database connection timed out")

        fake.retrieve = exploding
    problems = verify_all_complete(fake, staging, [("doc.pdf", "a" * 64)], {}, rules_v, "||")
    assert "stg-unreadable: missing or unreadable metadata manifest" in problems
    if case == "missing":
        # Empty bootstrap (no walked documents) is the one permitted absence.
        assert verify_all_complete(fake, staging, [], {}, rules_v, "||") == []


@pytest.mark.parametrize("state", ["pending", "COMMITTED"])
def test_verify_all_complete_refuses_a_non_committed_contract(state):
    """Publication certifies a committed generation, never an unfinished
    re-embed (issue #391 F2); the comparison is exact (no case folding), and
    the block holds even with nothing walked."""
    from mainframe_rag.ingest.representation import write_manifest
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    rules_v = extraction_rules_version()
    fake = PublishFake()
    staging = _settings(qdrant_collection="stg-states")
    write_manifest(fake, "stg-states__completions", staging, rules_v, state=state)
    problems = verify_all_complete(fake, staging, [("doc.pdf", "a" * 64)], {}, rules_v, "||")
    assert f"stg-states: contract {state!r}" in problems
    assert verify_all_complete(fake, staging, [], {}, rules_v, "||") == [f"stg-states: contract {state!r}"]


@pytest.mark.parametrize(
    ("settings_kw", "manifest_kw", "fields"),
    [
        ({}, {"rules": "old_rules_v_1234"}, "extraction_rules"),
        (
            {"bm25_model": "bm25_v1", "bm25_weights_revision": "rev1"},
            {"bm25_model": "bm25_v2", "bm25_weights_revision": "rev2"},
            "sparse_model, sparse_weights_revision",
        ),
        (
            {"embed_mode": "vllm", "embed_model": "model-a", "embed_model_revision": "rev1", "dense_dim": 512},
            {"embed_model": "model-b", "dense_dim": 768, "rules": "other_rules_v"},
            "extraction_rules, embed_model, dense_dim",
        ),
    ],
)
def test_verify_all_complete_refuses_representation_drift(settings_kw, manifest_kw, fields):
    """A committed contract that drifted on any re-embed coordinate blocks
    publication, naming each drifted field."""
    from mainframe_rag.ingest.representation import write_manifest
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    rules_v = extraction_rules_version()
    fake = PublishFake()
    staging = _settings(qdrant_collection="stg-drift", **settings_kw)
    manifest_kw = dict(manifest_kw)
    rules = manifest_kw.pop("rules", rules_v)
    write_manifest(
        fake, "stg-drift__completions", staging.model_copy(update=manifest_kw), rules, state="committed"
    )
    problems = verify_all_complete(fake, staging, [("doc.pdf", "a" * 64)], {}, rules_v, "||")
    assert any(f"stg-drift: representation drift on {fields}" in p for p in problems)


def test_verify_all_complete_ignores_record_only_prefix_drift():
    from mainframe_rag.ingest.representation import write_manifest
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    rules_v = extraction_rules_version()
    fake = PublishFake()
    staging = _settings(qdrant_collection="stg-record-only", dense_query_prefix="PREFIX_A: ")
    write_manifest(
        fake, "stg-record-only__completions",
        staging.model_copy(update={"dense_query_prefix": "PREFIX_B: "}), rules_v, state="committed",
    )
    assert verify_all_complete(fake, staging, [], {}, rules_v, "||") == []


def _parsed_doc(doc_id, sha):
    from pathlib import Path as _Path

    from mainframe_rag.ingest.ibm_pdf import ParsedDoc

    return ParsedDoc(
        path=_Path("d.pdf"),
        sha256=sha,
        doc_id=doc_id,
        title="t",
        product="p",
        version="1",
        vendor="v",
        page_count=1,
    )


def test_dangling_alias_publishes_fresh(tmp_path, monkeypatch):
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    fake.aliases[ALIAS] = "ghost-collection"
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    assert _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl") == 0
    gen = fake.aliases[ALIAS]
    assert gen != "ghost-collection" and gen in fake.collections
    assert {p.payload["doc_id"] for p in fake.alias_target_points(ALIAS)} == {"SA22-0000-00"}


def _live_manifest_revision(fake, live):
    from mainframe_rag.ingest.representation import read_manifest

    return read_manifest(fake, f"{live}__completions")


def test_publish_force_same_contract_repairs_distinct_generation(tmp_path, monkeypatch):
    """Issue #391 current packet (counterexample 3): a forced same-contract
    rebuild must never mutate the serving generation. `--reingest` allocates
    a distinct resumable repair generation, re-embeds there, verifies, and
    swaps; the old physical keeps its points AND its manifest for rollback,
    and no self-swap snapshot churns."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.completion import completion_collection_for
    from mainframe_rag.ingest.representation import read_manifest_record

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live = fake.aliases[ALIAS]
    points_before = [(p.id, dict(p.payload or {})) for p in fake.collections[live]]
    manifest_before = read_manifest_record(fake, completion_collection_for(live))

    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    repaired = fake.aliases[ALIAS]
    assert repaired != live, "a forced repair must publish a distinct generation"
    assert repaired.endswith("_1"), f"expected a resumable repair build, got {repaired}"
    assert [(p.id, dict(p.payload or {})) for p in fake.collections[live]] == points_before, (
        "the serving generation must be byte-identical after a repair"
    )
    assert read_manifest_record(fake, completion_collection_for(live)) == manifest_before, (
        "the old generation keeps its own contract for rollback"
    )
    assert fake.snapshots.get(live), "superseded generation keeps a safety snapshot"
    assert {p.payload["doc_id"] for p in fake.alias_target_points(ALIAS)} == {DOC_A}


def test_forced_repair_never_touches_live_during_build(tmp_path, monkeypatch):
    """Reader isolation: while the repair build runs, the alias still
    resolves to the original generation, its points are unchanged, and the
    serving gate validates that physical as compatible; only the post-verify
    swap moves the alias."""
    import asyncio

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.representation import resolve_serving_generation
    from mainframe_rag.ingest.rules_version import extraction_rules_version
    from mainframe_rag.ports import AsyncReaderAdapter

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live = fake.aliases[ALIAS]
    live_points = [p.id for p in fake.collections[live]]

    observed: dict = {}
    real_inner = run_ingest._run_impl

    def observing_inner(*args, **kwargs):
        if kwargs.get("staging") is None:
            return real_inner(*args, **kwargs)
        observed["alias_target"] = fake.aliases[ALIAS]
        observed["live_points"] = [p.id for p in fake.collections[live]]
        observed["reader"] = asyncio.run(
            resolve_serving_generation(
                AsyncReaderAdapter(fake), _settings(qdrant_collection=ALIAS),
                extraction_rules_version(),
            )
        )
        return real_inner(*args, **kwargs)

    monkeypatch.setattr(run_ingest, "_run_impl", observing_inner)
    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    assert observed["alias_target"] == live, "the alias never moves before the verified swap"
    assert observed["live_points"] == live_points, "the live physical is not mutated mid-build"
    physical, outcome, _ = observed["reader"]
    assert (physical, outcome) == (live, "compatible"), (
        "an active reader keeps a validated compatible generation during the repair"
    )
    assert fake.aliases[ALIAS] != live, "the repair swapped only after verification"


def test_interrupted_same_contract_repair_resumes_recorded_build(tmp_path, monkeypatch):
    """A crash mid-repair resumes the recorded repair generation (distinct
    from live) instead of allocating another suffix; live stays unchanged
    throughout."""
    import json

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import publish_state_path

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live = fake.aliases[ALIAS]
    points_before = [(p.id, dict(p.payload or {})) for p in fake.collections[live]]

    real_inner = run_ingest._run_impl

    def dying_inner(*args, **kwargs):
        if kwargs.get("staging") is None:
            return real_inner(*args, **kwargs)
        raise RuntimeError("injected mid-repair crash")

    monkeypatch.setattr(run_ingest, "_run_impl", dying_inner)
    with pytest.raises(RuntimeError, match="injected mid-repair crash"):
        _run_main(monkeypatch, corpus, progress, "--reingest")
    assert fake.aliases[ALIAS] == live
    state_path = publish_state_path(progress, ALIAS)
    staging = json.loads(state_path.read_text())["staging"]
    assert staging.endswith("_1") and staging in fake.collections
    assert [(p.id, dict(p.payload or {})) for p in fake.collections[live]] == points_before

    monkeypatch.setattr(run_ingest, "_run_impl", real_inner)
    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    assert fake.aliases[ALIAS] == staging
    assert not state_path.exists()
    assert _gen_collections(fake) == sorted([live, staging])


def test_subsequent_ordinary_run_recognizes_repair_steady_state(tmp_path, monkeypatch):
    """Issue #391 Q418-R1: a successful forced repair cuts over to a suffixed
    generation; on a subsequent ordinary run without --reingest for the same
    inputs, publication metadata proves live matches (gen_fp, corpus_fp). The
    run treats live as steady state directly, re-verifies read-only, and
    allocates no second staging generation."""
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inv.jsonl"

    # 1. Initial build: canonical staging generation published
    assert _run_main(monkeypatch, corpus, progress) == 0
    initial = fake.aliases[ALIAS]

    # 2. Forced repair: builds and cuts over to suffixed generation (e.g. initial_1)
    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    repaired = fake.aliases[ALIAS]
    assert repaired != initial
    assert repaired.endswith("_1")
    gens_after_repair = _gen_collections(fake)
    points_after_repair = [(p.id, dict(p.payload or {})) for p in fake.collections[repaired]]

    # 3. Subsequent ordinary run WITHOUT --reingest on same corpus
    assert _run_main(monkeypatch, corpus, progress) == 0
    assert fake.aliases[ALIAS] == repaired
    assert _gen_collections(fake) == gens_after_repair, (
        "steady-state run must not allocate new generation"
    )
    assert [
        (p.id, dict(p.payload or {})) for p in fake.collections[repaired]
    ] == points_after_repair

    # Second subsequent ordinary run: must also perform zero allocations, no writes
    assert _run_main(monkeypatch, corpus, progress) == 0
    assert fake.aliases[ALIAS] == repaired
    assert _gen_collections(fake) == gens_after_repair, (
        "second ordinary run must not allocate new generation"
    )
    assert [
        (p.id, dict(p.payload or {})) for p in fake.collections[repaired]
    ] == points_after_repair

    # 4. A new-format build cannot lose its binding and silently become legacy.
    from mainframe_rag.ingest.publish import completion_collection_for, delete_publication_metadata

    repaired_completions = completion_collection_for(repaired)
    from copy import deepcopy

    saved_controls = deepcopy(fake.collections[repaired_completions])
    delete_publication_metadata(fake, repaired_completions)
    damaged = deepcopy(fake.collections)
    with pytest.raises(RuntimeError, match="live build controls"):
        _run_main(monkeypatch, corpus, progress)
    assert fake.aliases[ALIAS] == repaired
    assert fake.collections == damaged
    fake.collections[repaired_completions] = saved_controls
    assert _run_main(monkeypatch, corpus, progress) == 0

    # 5. Counterexample 1: Changed corpus derives new generation
    _build_doc_with_id(corpus, "doc_b", DOC_B)
    assert _run_main(monkeypatch, corpus, progress) == 0
    new_corpus_gen = fake.aliases[ALIAS]
    assert new_corpus_gen != repaired
    assert new_corpus_gen in _gen_collections(fake)

    # 6. Counterexample 2: a model-revision change changes gen_fp, so live is
    # not recognized as steady state and a new generation is derived.
    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-new")
    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    assert fake.aliases[ALIAS] != new_corpus_gen


def test_q418_r1_publication_receipt_not_accumulated_across_clones():
    """Issue #391 S423-N1: publication receipts must not accumulate in completions
    collections across repeated generation clones. _transfer_staging_metadata removes
    any inherited receipt from staging while preserving completions and manifest."""
    from types import SimpleNamespace

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest.publish import (
        _PUBLICATION_METADATA_PREFIX,
        _transfer_staging_metadata,
        completion_collection_for,
        completion_collection_name,
        publication_metadata_point_id,
        read_publication_metadata,
    )
    from mainframe_rag.ingest.representation import (
        commit_manifest,
        read_manifest_record,
    )

    fake = PublishFake()
    settings = Settings(qdrant_url="http://localhost:6333", qdrant_collection="col1")

    # 1. Setup live generation with completions, manifest, and publication receipt
    live = "col1__gen1"
    live_completions = completion_collection_for(live)
    staging1 = "col1__gen2"
    staging1_settings = settings.model_copy(update={"qdrant_collection": staging1})
    staging1_completions = completion_collection_name(staging1_settings)

    fake.collections[live] = []
    fake.collections[live_completions] = []

    commit_manifest(fake, live_completions, settings, "rules-1")
    live_manifest = read_manifest_record(fake, live_completions)
    assert live_manifest is not None

    # Add a document completion marker
    doc_marker = SimpleNamespace(
        id="doc-marker-1",
        payload={
            "doc_id": "DOC1",
            "rules_version": "rules-1",
            "target_collection": live_completions,
        },
        vector=[0.0],
    )
    fake.collections[live_completions].append(doc_marker)

    # Write publication metadata to live
    def place_receipt(completions, physical, gen_fp, corpus_fp, build_id):
        # A bound (schema-1) receipt, placed directly: this test is about
        # transfer, not about sealing real content.
        fake.collections.setdefault(completions, []).append(SimpleNamespace(
            id=publication_metadata_point_id(completions),
            payload={"record_type": _PUBLICATION_METADATA_PREFIX, "target_collection": completions,
                     "build_schema": 1, "build_id": build_id, "logical_alias": "col1",
                     "data_collection": physical, "gen_fp": gen_fp, "corpus_fp": corpus_fp},
            vector=[0.0],
        ))

    place_receipt(live_completions, live, "gen1", "corp1", "11111111-1111-4111-8111-111111111111")
    assert read_publication_metadata(fake, live_completions) == ("gen1", "corp1")

    # 2. Transfer metadata to staging1 (simulates preparation)
    _transfer_staging_metadata(fake, settings, staging1_settings, live)

    # Staging1 manifest must be preserved and rekeyed
    stg1_manifest = read_manifest_record(fake, staging1_completions)
    assert stg1_manifest is not None
    assert stg1_manifest == live_manifest

    # Doc marker must be preserved
    assert any(
        (p.payload or {}).get("doc_id") == "DOC1" for p in fake.collections[staging1_completions]
    )

    # Ancestral publication receipt from live MUST be removed from staging1
    stg1_receipts = [
        p
        for p in fake.collections[staging1_completions]
        if (p.payload or {}).get("record_type") == _PUBLICATION_METADATA_PREFIX
    ]
    assert len(stg1_receipts) == 0, (
        f"staging must have 0 receipts after transfer, got {len(stg1_receipts)}"
    )

    # 3. Simulate publication of staging1: write staging1's receipt
    place_receipt(staging1_completions, staging1, "gen2", "corp2", "22222222-2222-4222-8222-222222222222")
    stg1_receipts_after_pub = [
        p
        for p in fake.collections[staging1_completions]
        if (p.payload or {}).get("record_type") == _PUBLICATION_METADATA_PREFIX
    ]
    assert len(stg1_receipts_after_pub) == 1
    assert str(stg1_receipts_after_pub[0].id) == publication_metadata_point_id(staging1_completions)

    # 4. Clone staging1 into staging2: repeated generation cloning
    staging2 = "col1__gen3"
    staging2_settings = settings.model_copy(update={"qdrant_collection": staging2})
    staging2_completions = completion_collection_name(staging2_settings)

    _transfer_staging_metadata(fake, settings, staging2_settings, staging1)

    # Staging2 must NOT accumulate staging1's receipt or live's receipt
    stg2_receipts = [
        p
        for p in fake.collections[staging2_completions]
        if (p.payload or {}).get("record_type") == _PUBLICATION_METADATA_PREFIX
    ]
    assert len(stg2_receipts) == 0, (
        f"staging2 must have 0 receipts after transfer, got {len(stg2_receipts)}"
    )

    # Doc marker and manifest are still preserved
    assert read_manifest_record(fake, staging2_completions) is not None
    assert any(
        (p.payload or {}).get("doc_id") == "DOC1" for p in fake.collections[staging2_completions]
    )

    # Publish staging2
    place_receipt(staging2_completions, staging2, "gen3", "corp3", "33333333-3333-4333-8333-333333333333")
    stg2_receipts_after_pub = [
        p
        for p in fake.collections[staging2_completions]
        if (p.payload or {}).get("record_type") == _PUBLICATION_METADATA_PREFIX
    ]
    assert len(stg2_receipts_after_pub) == 1
    assert str(stg2_receipts_after_pub[0].id) == publication_metadata_point_id(staging2_completions)


def test_revision_only_change_fails_unforced_and_migrates_when_forced(tmp_path, monkeypatch):
    """Issue #391 F2: a revision-only change derives a DIFFERENT staging
    generation, so the unforced run clones live and fails in the inner
    preflight (drift, `--reingest` remediation) leaving alias, live points and
    live metadata untouched. `--reingest` then re-embeds into that distinct
    generation, verifies and swaps; the old generation keeps its points AND
    its rev-A manifest for rollback, and the serving contract is committed
    (never a half-rebuilt B)."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.representation import STATE_COMMITTED, read_manifest_record

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    old = fake.aliases[ALIAS]
    points_before = list(fake.collections[old])
    assert _live_manifest_revision(fake, old).embed_model_revision == ""

    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-2")
    with pytest.raises(RuntimeError, match="representation drift on embed_model_revision"):
        _run_main(monkeypatch, corpus, progress)
    assert fake.aliases[ALIAS] == old, "failed run moves no alias"
    assert fake.collections[old] == points_before, "failed run touches no live point"
    assert _live_manifest_revision(fake, old).embed_model_revision == "", "failed run commits no manifest"

    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    new = fake.aliases[ALIAS]
    assert new != old, "a representation change must publish a distinct generation"
    assert old in fake.collections, "old physical retained for rollback"
    assert _live_manifest_revision(fake, old).embed_model_revision == "", (
        "old generation keeps its own contract"
    )
    assert fake.collections[old] == points_before, "old generation keeps its points"
    assert _live_manifest_revision(fake, new).embed_model_revision == "rev-2"
    record = read_manifest_record(fake, f"{new}__completions")
    assert record is not None and record.state == STATE_COMMITTED
    assert {p.payload["doc_id"] for p in fake.alias_target_points(ALIAS)} == {"SA22-0000-00"}


@pytest.mark.parametrize("mode", ["dropped", "pending"])
def test_publish_unusable_manifest_prevents_alias_cutover(tmp_path, monkeypatch, mode):
    """Invariant D2 E2E: the full publication flow aborts and leaves the alias
    untouched when the staging manifest is dropped or left pending at the
    publication gate."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.representation import STATE_PENDING, manifest_point_id, write_manifest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    orig_commit = run_ingest.commit_manifest

    def _unusable_commit(client, completions_collection, settings, rules_v):
        if mode == "pending":
            return write_manifest(client, completions_collection, settings, rules_v, state=STATE_PENDING)
        orig_commit(client, completions_collection, settings, rules_v)
        mp_id = manifest_point_id(completions_collection)
        fake.collections[completions_collection] = [
            p for p in fake.collections.get(completions_collection, []) if str(p.id) != mp_id
        ]
        return ""

    monkeypatch.setattr(run_ingest, "commit_manifest", _unusable_commit)
    match = {"dropped": "missing or unreadable metadata manifest", "pending": "contract 'pending'"}[mode]
    with pytest.raises(RuntimeError, match=match):
        _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl")
    assert fake.aliases == {}, "alias cutover must be refused"


@pytest.mark.parametrize("damage", ["dropped", "pending"])
def test_steady_state_reverification_refuses_unusable_live_manifest(tmp_path, monkeypatch, damage):
    """An ordinary rerun of identical inputs re-verifies the live generation
    read-only and fails closed when its manifest is gone or pending; a
    contract-less live is unversioned data that --reingest refuses as well,
    never migrating or mutating the serving generation (issue #391)."""
    from copy import deepcopy

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.representation import (
        STATE_PENDING,
        manifest_point_id,
        write_manifest,
    )
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live = fake.aliases[ALIAS]
    live_points = [p.id for p in fake.collections[live]]
    completions = f"{live}__completions"
    if damage == "dropped":
        mp_id = manifest_point_id(completions)
        fake.collections[completions] = [p for p in fake.collections[completions] if str(p.id) != mp_id]
        match = "predates the representation manifest"
    else:
        write_manifest(
            fake, completions, _settings(qdrant_collection=live), extraction_rules_version(),
            state=STATE_PENDING,
        )
        match = "unfinished representation migration"
    before = deepcopy(fake.collections), dict(fake.aliases)
    with pytest.raises(RuntimeError, match=match):
        _run_main(monkeypatch, corpus, progress)
    assert (fake.collections, fake.aliases) == before, "the refusal mutates nothing"

    if damage == "dropped":
        # A contract-less live is unversioned data: --reingest refuses too and
        # never migrates it into a new generation.
        with pytest.raises(RuntimeError, match=match):
            _run_main(monkeypatch, corpus, progress, "--reingest")
        assert fake.aliases[ALIAS] == live
        assert [p.id for p in fake.collections[live]] == live_points


# ------------------------------------------------- R1 intended set (issue #405)


def _build_doc_with_id(directory: Path, stem: str, doc_id: str) -> Path:
    from scripts.make_synthetic_pdf import build as make_pdf

    out = directory / f"{stem}.pdf"
    make_pdf(out, doc_id=doc_id)
    return out


DOC_A = "SA22-0000-00"
DOC_B = "SA22-0000-01"


def _two_doc_corpus(directory: Path):
    a = _build_doc_with_id(directory, "doc_a", DOC_A)
    b = _build_doc_with_id(directory, "doc_b", DOC_B)
    return a, b


def _live_doc_ids(fake: PublishFake):
    return {p.payload["doc_id"] for p in fake.alias_target_points(ALIAS)}


def test_partial_walk_refuses_and_preserves_unretired_doc(tmp_path, monkeypatch):
    """R1 acceptance: a partial walk (missing mount/copy) without an
    approved removal refuses cutover, for an ordinary run and a forced repair
    alike (a repair certifies the same complete corpus as any publish); the
    approved doc is not silently retired and the live generation is untouched."""
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _, b_path = _two_doc_corpus(corpus)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live_before = fake.aliases[ALIAS]
    points_before = [(p.id, dict(p.payload or {})) for p in fake.collections[live_before]]
    assert _live_doc_ids(fake) == {DOC_A, DOC_B}

    held = tmp_path / "doc_b.pdf.held"
    b_path.rename(held)
    for extra in ((), ("--reingest",)):
        with pytest.raises(RuntimeError, match="unmarked residue"):
            _run_main(monkeypatch, corpus, progress, *extra)
        assert fake.aliases[ALIAS] == live_before
        assert [(p.id, dict(p.payload or {})) for p in fake.collections[live_before]] == points_before
        assert _live_doc_ids(fake) == {DOC_A, DOC_B}


@pytest.mark.parametrize("case", ["unknown-doc", "walked-revision", "replaced-revision"])
def test_retire_refusals_mutate_nothing(tmp_path, monkeypatch, case):
    """R1 / R-REV acceptance: a typo (no approved history) and a retirement
    contradicting the walked corpus (a named revision still present, or any
    revision of the document still walked) fail before any mutation."""
    from scripts.make_synthetic_pdf import build as make_pdf

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.inventory import load_inventory
    from mainframe_rag.ingest.publish import publish_state_path

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    a_path, _ = _two_doc_corpus(corpus)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    rev_a = load_inventory(progress)[str(a_path)].source_rev
    assert rev_a is not None
    if case == "unknown-doc":
        flag, match = "SA99-9999-99", "no approved history"
    elif case == "walked-revision":
        flag, match = f"{DOC_A}@{rev_a}", "still present in the walked corpus"
    else:
        make_pdf(a_path, doc_id=DOC_A, title="Synthetic Doc A Rev 2")
        flag, match = DOC_A, "still present in the walked corpus"
    sidecar = publish_state_path(progress, ALIAS)
    before = _resolution_snapshot(fake, progress, sidecar)
    with pytest.raises(RuntimeError, match=match):
        _run_main(monkeypatch, corpus, progress, "--retire-doc", flag)
    assert _resolution_snapshot(fake, progress, sidecar) == before


def test_unmarked_legacy_residue_blocks_cutover_without_deletion(tmp_path, monkeypatch):
    """R1 acceptance: the residue audit is read-only — an unattributable
    legacy point blocks verification and is still present afterwards."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.completion import source_labels
    from mainframe_rag.ingest.inventory import load_inventory
    from mainframe_rag.ingest.publish import verify_all_complete
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    a_path = _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live = fake.aliases[ALIAS]

    from types import SimpleNamespace

    fake.collections[live].append(
        SimpleNamespace(id="legacy-stray", payload={"doc_id": "SA99-0000-00", "text": "old"})
    )
    staging_settings = _settings(qdrant_collection=live)
    inventory = load_inventory(progress)
    problems = verify_all_complete(
        fake,
        staging_settings,
        [(str(a_path), inventory[str(a_path)].sha256)],
        inventory,
        extraction_rules_version(),
        source_labels(None, None, None),
    )
    assert any("unmarked residue" in p for p in problems)
    assert any(getattr(p, "id", None) == "legacy-stray" for p in fake.collections[live])


# ------------------------------------------------- R2 writer lifecycle (issue #405)


def test_concurrent_publish_same_alias_serializes(tmp_path, monkeypatch):
    """R2 acceptance: two publishers for one target never interleave — the
    second fails closed on the target lock while the first holds it."""
    import threading

    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0

    # Changed corpus so the contended runs take the distinct-staging path
    # (identical inputs would steady-state re-verify without building).
    _build_doc_with_id(corpus, "doc_b", DOC_B)

    entered = threading.Event()
    release = threading.Event()
    real_inner = run_ingest._run_impl

    def slow_inner(*args, **kwargs):
        if kwargs.get("staging") is None:
            return real_inner(*args, **kwargs)
        entered.set()
        assert release.wait(timeout=60)
        return real_inner(*args, **kwargs)

    monkeypatch.setattr(run_ingest, "_run_impl", slow_inner)
    outcomes: list = []

    def run_publisher():
        try:
            outcomes.append(_run_main(monkeypatch, corpus, progress))
        except Exception as exc:  # noqa: BLE001 — recorded, asserted below
            outcomes.append(exc)

    thread_a = threading.Thread(target=run_publisher)
    thread_a.start()
    assert entered.wait(timeout=60)
    thread_b = threading.Thread(target=run_publisher)
    thread_b.start()
    thread_b.join(timeout=60)
    release.set()
    thread_a.join(timeout=60)

    assert len(outcomes) == 2
    codes = sorted(0 if o == 0 else 1 for o in outcomes)
    assert codes == [0, 1]
    failure = next(o for o in outcomes if o != 0)
    assert "another publish" in str(failure)


def test_overtaken_publisher_refuses_stale_cutover(tmp_path, monkeypatch):
    """R2 acceptance: a publisher overtaken after verification (lock-bypass
    writer, e.g. another host) refuses instead of switching the alias back
    to its older candidate."""
    import threading

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import verify_all_complete as real_verify
    from mainframe_rag.ingest.qdrant_io import swap_alias_to

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live_before = fake.aliases[ALIAS]
    _build_doc_with_id(corpus, "doc_b", DOC_B)

    paused = threading.Event()
    release = threading.Event()

    def pausing_verify(*args, **kwargs):
        problems = real_verify(*args, **kwargs)
        paused.set()
        assert release.wait(timeout=60)
        return problems

    monkeypatch.setattr(run_ingest, "verify_all_complete", pausing_verify)
    errors: list = []

    def run_publisher():
        try:
            _run_main(monkeypatch, corpus, progress)
        except Exception as exc:  # noqa: BLE001 — asserted below
            errors.append(exc)

    thread_a = threading.Thread(target=run_publisher)
    thread_a.start()
    assert paused.wait(timeout=60)
    # Lock-bypass writer cuts over a newer generation mid-flight.
    fake.create_collection("G2manual")
    swap_alias_to(fake, _settings(qdrant_collection=ALIAS), "G2manual", live_before)
    release.set()
    thread_a.join(timeout=60)

    assert len(errors) == 1
    assert "moved during publication" in str(errors[0])
    assert fake.aliases[ALIAS] == "G2manual"


def test_stale_publisher_cannot_revert_a_newer_publication(tmp_path, monkeypatch):
    """Issue #1019 (S2-19): publisher A (its own progress directory, the
    unsupported lock-bypass setup) passes its pre-swap live recheck, then
    pauses in the catalog/seal phase while publisher B publishes a newer
    three-document generation. A must refuse at the swap instead of switching
    the alias back to its two-document candidate; B's publication keeps
    serving."""
    import shutil
    import threading

    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus_a = tmp_path / "corpus_a"
    corpus_a.mkdir()
    _build_doc_with_id(corpus_a, "doc_a", DOC_A)
    progress_a = tmp_path / "a" / "inv.jsonl"
    progress_a.parent.mkdir()
    assert _run_main(monkeypatch, corpus_a, progress_a) == 0
    live_before = fake.aliases[ALIAS]
    _build_doc_with_id(corpus_a, "doc_b", DOC_B)
    corpus_b = tmp_path / "corpus_b"
    corpus_b.mkdir()
    for name in ("doc_a.pdf", "doc_b.pdf"):  # B walks the same bytes plus one more
        shutil.copyfile(corpus_a / name, corpus_b / name)
    _build_doc_with_id(corpus_b, "doc_c", "SA22-0000-02")
    progress_b = tmp_path / "b" / "inv.jsonl"
    progress_b.parent.mkdir()

    paused = threading.Event()
    release = threading.Event()
    real_catalog = run_ingest.build_source_catalog
    calls: list = []

    def pausing_catalog(*args, **kwargs):
        calls.append(threading.current_thread().name)
        if len(calls) == 1:  # publisher A only: after its recheck, before the swap
            paused.set()
            assert release.wait(timeout=60)
        return real_catalog(*args, **kwargs)

    monkeypatch.setattr(run_ingest, "build_source_catalog", pausing_catalog)
    errors: list = []

    def run_publisher_a():
        try:
            errors.append(_run_main(monkeypatch, corpus_a, progress_a))
        except Exception as exc:  # noqa: BLE001 — asserted below
            errors.append(exc)

    thread_a = threading.Thread(target=run_publisher_a, name="publisher-a")
    thread_a.start()
    try:
        assert paused.wait(timeout=60)
        assert fake.aliases[ALIAS] == live_before, "A has passed its recheck, not swapped"
        assert _run_main(monkeypatch, corpus_b, progress_b) == 0
        newer = fake.aliases[ALIAS]
        assert newer != live_before
        assert _live_doc_ids(fake) == {DOC_A, DOC_B, "SA22-0000-02"}
    finally:
        release.set()
        thread_a.join(timeout=60)

    assert len(errors) == 1 and isinstance(errors[0], RuntimeError), errors
    assert "moved during publication" in str(errors[0])
    assert fake.aliases[ALIAS] == newer, "the newer publication keeps serving"
    assert _live_doc_ids(fake) == {DOC_A, DOC_B, "SA22-0000-02"}


def test_revisit_retained_generation_never_mutated(tmp_path, monkeypatch):
    """R2 acceptance: republishing an older input set allocates a new
    staging generation; the retained published generation (points and
    manifest) is never mutated, even as workspace."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.completion import completion_collection_for
    from mainframe_rag.ingest.representation import read_manifest_record

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    dir_a = tmp_path / "corpus_a"
    dir_a.mkdir()
    _build_doc_with_id(dir_a, "doc_a", DOC_A)
    dir_b = tmp_path / "corpus_b"
    dir_b.mkdir()
    _build_doc_with_id(dir_b, "doc_b", DOC_B)
    progress = tmp_path / "inv.jsonl"

    assert _run_main(monkeypatch, dir_a, progress) == 0
    gen_a = fake.aliases[ALIAS]
    points_before = [(p.id, dict(p.payload or {})) for p in fake.collections[gen_a]]
    manifest_before = read_manifest_record(fake, completion_collection_for(gen_a))

    assert _run_main(monkeypatch, dir_b, progress, "--retire-doc", DOC_A) == 0
    gen_b = fake.aliases[ALIAS]
    assert gen_b != gen_a
    assert _live_doc_ids(fake) == {DOC_B}

    assert _run_main(monkeypatch, dir_a, progress, "--retire-doc", DOC_B) == 0
    gen_a2 = fake.aliases[ALIAS]
    assert gen_a2 != gen_a
    assert [(p.id, dict(p.payload or {})) for p in fake.collections[gen_a]] == points_before
    assert read_manifest_record(fake, completion_collection_for(gen_a)) == manifest_before
    assert _live_doc_ids(fake) == {DOC_A}


# ------------------------------------------------- Review round on #409 (issue #405)


@pytest.mark.parametrize("requested", [{None}, {"rev1"}], ids=["whole", "named"])
def test_plan_retire_deletes_refuses_revision_less_approved_history(requested):
    """Approved history without a source revision is unsupported pre-revision
    data: both whole-document and named retirement refuse before any delete,
    while ordinary named history still plans exactly."""
    from mainframe_rag.ingest.completion import plan_retire_deletes

    with pytest.raises(RuntimeError, match="pre-revision data is unsupported"):
        plan_retire_deletes({"DOC": {"rev1", None}}, {"DOC": requested})
    assert plan_retire_deletes({"DOC": {"rev1"}}, {"DOC": {None}}) == {
        "DOC": {"revs": {"rev1"}, "whole": True}
    }








def test_audit_names_gap_for_unexpected_remnants(monkeypatch):
    """B2: the retired-doc audit names the operator path for each remnant
    shape — unexpected revisions (re-plan) vs revision-less residue
    (unsupported pre-revision data)."""
    from mainframe_rag.ingest.inventory import InventoryRecord
    from mainframe_rag.ingest.publish import audit_unmarked_residue

    _publish_env(monkeypatch)
    fake = PublishFake()
    staging = _settings(qdrant_collection="stg")
    fake.collections["stg"] = [
        SimpleNamespace(id="r2", payload={"doc_id": "D-NAMED", "source_rev": "rev2"}),
        SimpleNamespace(id="leg", payload={"doc_id": "D-LEGACY"}),
    ]
    inv: dict[str, InventoryRecord] = {}
    named = audit_unmarked_residue(
        fake,
        staging,
        [],
        inv,
        "r" * 16,
        retired=frozenset({"D-NAMED"}),
        retire_plan={"D-NAMED": {"revs": {"rev1"}, "whole": False}},
    )
    assert len(named) == 1 and "rev2" in named[0] and "re-plan" in named[0]

    fake.collections["stg"] = [
        SimpleNamespace(id="leg", payload={"doc_id": "D-LEGACY"}),
    ]
    manual = audit_unmarked_residue(
        fake,
        staging,
        [],
        inv,
        "r" * 16,
        retired=frozenset({"D-LEGACY"}),
        retire_plan={"D-LEGACY": {"revs": {"rev1"}, "whole": True}},
    )
    assert len(manual) == 1 and "pre-revision data is unsupported" in manual[0]


def test_retire_plan_validated_under_target_lock(tmp_path, monkeypatch):
    """SF1: retirement planning reads the freshest inventory under the
    target lock — an approval revoked before lock acquisition fails closed
    instead of publishing from a stale plan."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.inventory import load_inventory

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    a_path, _ = _two_doc_corpus(corpus)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live_before = fake.aliases[ALIAS]
    gens_before = _gen_collections(fake)
    a_path.unlink()

    real_acquire = run_ingest.acquire_publish_lock

    def revoking_acquire(progress_path, alias):
        lines = [line for line in progress.read_text().splitlines() if DOC_A not in line]
        progress.write_text("\n".join(lines) + ("\n" if lines else ""))
        assert DOC_A not in {rec.doc_id for rec in load_inventory(progress).values()}
        return real_acquire(progress_path, alias)

    monkeypatch.setattr(run_ingest, "acquire_publish_lock", revoking_acquire)
    with pytest.raises(RuntimeError, match="no approved history"):
        _run_main(monkeypatch, corpus, progress, "--retire-doc", DOC_A)
    assert fake.aliases[ALIAS] == live_before
    assert _gen_collections(fake) == gens_before, "stale plan builds nothing"


def _revision_less_case(tmp_path, monkeypatch):
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live = fake.aliases[ALIAS]
    return fake, corpus, progress, live

def test_walked_doc_revision_less_point_refuses_without_deletion(tmp_path, monkeypatch):
    """Issue #391 current packet (counterexample 2): sharing a walked doc_id is
    not attributed coverage. A point without a source revision refuses the
    steady-state re-verify, is preserved, and the alias does not move."""
    fake, corpus, progress, live = _revision_less_case(tmp_path, monkeypatch)
    stray = SimpleNamespace(id="stray-a", payload={"doc_id": DOC_A, "text": "pre-361B"})
    fake.collections[live].append(stray)
    with pytest.raises(RuntimeError, match="unmarked residue"):
        _run_main(monkeypatch, corpus, progress)
    assert fake.aliases[ALIAS] == live, "refusal moves no alias"
    assert any(getattr(q, "id", None) == "stray-a" for q in fake.collections[live]), (
        "unknown residue is preserved, never deleted"
    )

def test_pre_publication_layout_refuses_without_mutation(tmp_path, monkeypatch):
    """A physical collection carrying the configured name with no alias (the
    pre-publication layout) is unsupported: the run refuses before any
    write, the flat collection is untouched and no staging appears."""
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    flat = [SimpleNamespace(id="flat-a", payload={"doc_id": DOC_A, "text": "pre-publication"})]
    fake.collections[ALIAS] = list(flat)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    with pytest.raises(RuntimeError, match="pre-publication layout is no longer supported"):
        _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl")
    assert fake.collections == {ALIAS: flat}, "refusal writes nothing"
    assert fake.aliases == {}


def test_unbound_live_generation_refuses_without_mutation(tmp_path, monkeypatch):
    """A live generation without a build binding (a pre-build publication:
    no control record, no immutable build aliases) is unsupported. The
    ordinary run refuses instead of backfilling a receipt, and neither the
    alias nor the live points change."""
    from mainframe_rag.ingest.publish import publication_metadata_point_id

    fake, corpus, progress, live = _revision_less_case(tmp_path, monkeypatch)
    control = live + "__completions"
    receipt = publication_metadata_point_id(control)
    fake.collections[control] = [q for q in fake.collections[control] if str(q.id) != receipt]
    fake.aliases = {ALIAS: live}
    before = {name: [q.id for q in points] for name, points in fake.collections.items()}
    with pytest.raises(RuntimeError, match="has no build binding"):
        _run_main(monkeypatch, corpus, progress)
    assert fake.aliases == {ALIAS: live}, "refusal moves no alias"
    assert {name: [q.id for q in points] for name, points in fake.collections.items()} == before


def test_pre_build_publication_record_is_refused():
    """A publication control record without build fields decodes as an
    unsupported pre-build publication, never as a valid binding."""
    from mainframe_rag.ingest.publish import decode_publication_record

    control = "corpus__gen0__completions"
    with pytest.raises(RuntimeError, match="pre-build publication record: unsupported"):
        decode_publication_record(
            {"record_type": "publication-metadata", "target_collection": control}, control
        )


def test_publish_migration_with_explicit_retire_single_run(tmp_path, monkeypatch):
    """Issue #391 counterexample 1 in publish mode: a representation migration
    plus an explicitly approved removal still commits and swaps in one run.
    The approved removal is applied before the verification that gates the
    swap, and the superseded generation is retained."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.representation import STATE_COMMITTED, read_manifest_record

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    a_path, _ = _two_doc_corpus(corpus)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live = fake.aliases[ALIAS]

    a_path.unlink()
    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-B")
    assert _run_main(monkeypatch, corpus, progress, "--retire-doc", DOC_A, "--reingest") == 0
    new = fake.aliases[ALIAS]
    assert new != live
    assert _live_doc_ids(fake) == {DOC_B}
    assert {p.payload["doc_id"] for p in fake.collections[live]} == {DOC_A, DOC_B}
    record = read_manifest_record(fake, f"{new}__completions")
    assert record is not None and record.state == STATE_COMMITTED
    assert record.manifest.embed_model_revision == "rev-B"


def test_publish_migration_retire_unapproved_stray_refuses(tmp_path, monkeypatch):
    """The commit-time excusal of an approved removal is bounded: a stray
    revision under the retired doc survives the approved named-revision
    removal, and the post-removal audit refuses the swap (re-plan path)."""
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    a_path, _ = _two_doc_corpus(corpus)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live = fake.aliases[ALIAS]
    fake.collections[live].append(
        SimpleNamespace(
            id="stray-a",
            payload={"doc_id": DOC_A, "sha256": "f" * 64, "source_rev": "rev-old"},
        )
    )

    a_path.unlink()
    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-B")
    with pytest.raises(RuntimeError, match="still present"):
        _run_main(monkeypatch, corpus, progress, "--retire-doc", DOC_A, "--reingest")
    assert fake.aliases[ALIAS] == live, "refusal moves no alias"
    assert any(getattr(p, "id", None) == "stray-a" for p in fake.collections[live])


def test_zero_point_marker_does_not_block_migration(tmp_path, monkeypatch):
    """Issue #1005 (S2-5): a completion marker whose revision has no stored
    points (orphaned by the pre-#1004 sibling sweep) certifies no searchable
    vector. A representation migration over the complete corpus must commit
    and cut over without editing control collections, while the coverage
    proof still attributes every searchable point."""
    import uuid as uuid_mod

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.representation import STATE_COMMITTED, read_manifest_record

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _two_doc_corpus(corpus)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live = fake.aliases[ALIAS]
    controls = fake.collections[f"{live}__completions"]
    marker = next(p for p in controls if (p.payload or {}).get("doc_id") == DOC_A)
    orphan_rev = "ibm|zos|2.5|" + "e" * 64
    controls.append(models.PointStruct(
        id=str(uuid_mod.uuid4()), vector=marker.vector,
        payload={**marker.payload, "source_rev": orphan_rev, "sha256": "e" * 64},
    ))
    assert orphan_rev not in _revisions_in(fake, live), "the marker certifies no point"

    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-2")
    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    new = fake.aliases[ALIAS]
    assert new != live
    record = read_manifest_record(fake, f"{new}__completions")
    assert record is not None and record.state == STATE_COMMITTED
    assert record.manifest.embed_model_revision == "rev-2"
    assert _live_doc_ids(fake) == {DOC_A, DOC_B}










def test_retire_revision_a_while_retaining_and_walking_revision_b_succeeds(tmp_path, monkeypatch):
    """R-REV (issue #391): explicit retirement of revision A (DOC@REV_A)
    removes REV_A while preserving sibling revision B (DOC@REV_B) across
    planning, walked-document checks, deletion, completion invalidation, and cutover."""
    from scripts.make_synthetic_pdf import build as make_pdf

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.completion import completion_collection_for
    from mainframe_rag.ingest.inventory import load_inventory

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    progress = tmp_path / "inv.jsonl"

    # Step 1: Ingest revision A of DOC_A, along with DOC_B
    a_path = corpus / "doc_a.pdf"
    make_pdf(a_path, doc_id=DOC_A, title="Synthetic Doc A Rev 1")
    _build_doc_with_id(corpus, "doc_b", DOC_B)
    assert _run_main(monkeypatch, corpus, progress) == 0
    live_1 = fake.aliases[ALIAS]
    inv_1 = load_inventory(progress)
    rev_a = inv_1[str(a_path)].source_rev
    assert rev_a is not None

    # Step 2: Now replace doc_a.pdf with revision B
    make_pdf(a_path, doc_id=DOC_A, title="Synthetic Doc A Rev 2 (Updated)")
    assert _run_main(monkeypatch, corpus, progress, "--retire-doc", f"{DOC_A}@{rev_a}") == 0
    live_2 = fake.aliases[ALIAS]
    assert live_2 != live_1
    inv_2 = load_inventory(progress)
    rev_b = inv_2[str(a_path)].source_rev
    assert rev_b is not None and rev_b != rev_a

    # Verify live_2 has DOC_A@rev_b and DOC_B, but NO DOC_A@rev_a
    live_points = fake.collections[live_2]
    revs_in_live = {
        (p.payload.get("doc_id"), p.payload.get("source_rev"))
        for p in live_points
        if p.payload.get("doc_id") == DOC_A
    }
    assert (DOC_A, rev_b) in revs_in_live
    assert (DOC_A, rev_a) not in revs_in_live

    # Verify completion markers in live_2__completions
    comp_coll = completion_collection_for(live_2)
    comp_points = fake.collections.get(comp_coll, [])
    comp_revs = {
        (p.payload.get("doc_id"), p.payload.get("source_rev"))
        for p in comp_points
        if (p.payload or {}).get("doc_id") == DOC_A
    }
    assert (DOC_A, rev_b) in comp_revs
    assert (DOC_A, rev_a) not in comp_revs

    # Verify superseded physical still preserves DOC_A@rev_a for rollback
    assert any(
        (p.payload or {}).get("doc_id") == DOC_A and (p.payload or {}).get("source_rev") == rev_a
        for p in fake.collections[live_1]
    )

    # The next ordinary run verifies rev_b in steady state without re-embedding
    assert _run_main(monkeypatch, corpus, progress) == 0
    assert fake.aliases[ALIAS] == live_2


def _resolution_snapshot(fake: PublishFake, progress: Path, sidecar: Path):
    """Everything a publication refusal must leave byte-identical: points
    and payloads per collection, aliases and alias operations, snapshots,
    the inventory and the publish sidecar."""
    return (
        {
            name: [(getattr(p, "id", None), dict(getattr(p, "payload", None) or {})) for p in points]
            for name, points in fake.collections.items()
        },
        dict(fake.aliases),
        len(fake.alias_calls),
        sorted(fake.snapshots),
        progress.read_bytes() if progress.exists() else None,
        sidecar.read_bytes() if sidecar.exists() else None,
    )


@pytest.mark.parametrize(
    "case",
    ["foreign_sidecar", "old_format_sidecar", "retirement_mismatch", "rebound_live"],
)
def test_publication_resolution_refusal_writes_nothing(tmp_path, monkeypatch, case):
    """Issue #583 S6: every refusal while publication resolves its
    predecessor, build identity, retirement plan and staging target happens
    before its first write — storage, aliases, inventory and the publish
    sidecar are unchanged afterwards."""
    import json

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import (
        publish_state_path,
        read_publication_metadata,
        write_publish_state,
    )

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _two_doc_corpus(corpus)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live = fake.aliases[ALIAS]
    fingerprints = read_publication_metadata(fake, live + "__completions")
    assert fingerprints is not None
    gen_fp, corp_fp = fingerprints
    sidecar = publish_state_path(progress, ALIAS)
    extra: tuple[str, ...] = ()
    if case == "foreign_sidecar":
        write_publish_state(progress, ALIAS, "foreign-candidate", "f" * 16, "c" * 12, previous=live)
        match = "different inputs"
    elif case == "old_format_sidecar":
        sidecar.write_text(json.dumps({
            "version": 1, "alias": ALIAS, "staging": "old-candidate",
            "gen_fp": gen_fp, "corpus_fp": corp_fp,
        }))
        match = "old-format"
    elif case == "retirement_mismatch":
        write_publish_state(
            progress, ALIAS, "recorded-candidate", gen_fp, corp_fp, retire_docs=(DOC_B,), previous=live
        )
        match = "different retirements"
    else:
        # A sidecar naming the live generation under another build identity.
        write_publish_state(progress, ALIAS, live, gen_fp, corp_fp, previous=None)
        match = "does not match its recorded identity"
    before = _resolution_snapshot(fake, progress, sidecar)
    with pytest.raises(RuntimeError, match=match):
        _run_main(monkeypatch, corpus, progress, *extra)
    assert _resolution_snapshot(fake, progress, sidecar) == before, f"{case}: refusal wrote state"




def test_retire_retry_recovers_its_plan_and_refuses_a_changed_request(tmp_path, monkeypatch):
    """S424-F2: when an interrupted run appends revision B's inventory record,
    changing the requested retirement does not inherit another transaction's
    authorization (fails closed), while retrying the identical command
    recovers the authorized plan from the publish_state sidecar and the
    cutover succeeds safely."""
    from scripts.make_synthetic_pdf import build as make_pdf

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.inventory import load_inventory

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    progress = tmp_path / "inv.jsonl"

    a_path = corpus / "doc_a.pdf"
    make_pdf(a_path, doc_id=DOC_A, title="Synthetic Doc A Rev 1")
    _build_doc_with_id(corpus, "doc_b", DOC_B)
    assert _run_main(monkeypatch, corpus, progress) == 0
    live_1 = fake.aliases[ALIAS]
    inv_1 = load_inventory(progress)
    rev_a = inv_1[str(a_path)].source_rev

    # Replace with rev B
    make_pdf(a_path, doc_id=DOC_A, title="Synthetic Doc A Rev 2")

    # Simulate interruption after document upsert/inventory append, before cutover
    real_apply = run_ingest.apply_approved_removals
    failed_once = False

    def fail_after_append(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("simulated crash after inventory append before swap")
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(run_ingest, "apply_approved_removals", fail_after_append)
    with pytest.raises(RuntimeError, match="simulated crash"):
        _run_main(monkeypatch, corpus, progress, "--retire-doc", f"{DOC_A}@{rev_a}")

    # Prior inventory now has rev B as the latest record for doc_a.pdf
    inv_interrupted = load_inventory(progress)
    assert inv_interrupted[str(a_path)].source_rev != rev_a

    # Old live data remains usable on failure
    assert fake.aliases[ALIAS] == live_1
    assert any((p.payload or {}).get("source_rev") == rev_a for p in fake.collections[live_1])

    # A different retirement request is refused.
    with pytest.raises(RuntimeError, match="refusing a build the current inputs cannot explain"):
        _run_main(monkeypatch, corpus, progress, "--retire-doc", DOC_B)
    assert fake.aliases[ALIAS] == live_1

    # Retry the IDENTICAL command: recovers retire_plan from publish_state sidecar
    assert _run_main(monkeypatch, corpus, progress, "--retire-doc", f"{DOC_A}@{rev_a}") == 0
    live_2 = fake.aliases[ALIAS]
    assert live_2 != live_1
    assert not any((p.payload or {}).get("source_rev") == rev_a for p in fake.collections[live_2])


def test_conflict_preflight_same_bytes_changed_label_replaces_old_revision(tmp_path, monkeypatch):
    """S424-F3: same bytes + changed explicit label can replace/retire the old revision;
    conflict preflight derives intended revision from current parsed labels rather than
    historical inventory record."""
    from scripts.make_synthetic_pdf import build as make_pdf

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.inventory import load_inventory

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    progress = tmp_path / "inv.jsonl"

    a_path = corpus / "doc_a.pdf"
    make_pdf(a_path, doc_id=DOC_A, title="Doc A Initial Content")
    _build_doc_with_id(corpus, "doc_b", DOC_B)

    # Step 1: Ingest with version 3.1
    assert (
        _run_main(
            monkeypatch, corpus, progress, "--vendor", "ibm", "--product", "zos", "--version", "3.1"
        )
        == 0
    )
    live_1 = fake.aliases[ALIAS]
    inv_1 = load_inventory(progress)
    rev_3_1 = inv_1[str(a_path)].source_rev
    assert rev_3_1 and "3.1" in rev_3_1

    # Step 2: Same bytes, new version 3.2, retiring 3.1
    assert (
        _run_main(
            monkeypatch,
            corpus,
            progress,
            "--vendor",
            "ibm",
            "--product",
            "zos",
            "--version",
            "3.2",
            "--retire-doc",
            f"{DOC_A}@{rev_3_1}",
        )
        == 0
    )
    live_2 = fake.aliases[ALIAS]
    assert live_2 != live_1
    inv_2 = load_inventory(progress)
    rev_3_2 = inv_2[str(a_path)].source_rev
    assert rev_3_2 and "3.2" in rev_3_2
    assert not any((p.payload or {}).get("source_rev") == rev_3_1 for p in fake.collections[live_2])


def _revisions_in(fake: PublishFake, physical: str) -> set:
    return {(p.payload or {}).get("source_rev") for p in fake.collections.get(physical, [])}


def _marker_revisions_in(fake: PublishFake, physical: str) -> set:
    return {
        (p.payload or {}).get("source_rev")
        for p in fake.collections.get(f"{physical}__completions", [])
        if (p.payload or {}).get("doc_id")
    }


def test_duplicate_relabel_never_drops_a_committed_sibling_revision(tmp_path, monkeypatch):
    """Issue #1004 (S2-4): a byte-identical copy under another layout path wins
    the duplicate election, so the published revision's own file is no longer
    ingested. Its committed points must not be swept from staging as residue
    (the inherited marker certifies them): publication refuses with the
    documented residue message, live keeps serving the old revision, and only
    an explicit --retire-doc removes it, leaving no orphaned marker."""
    import shutil

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.inventory import load_inventory
    from mainframe_rag.ingest.publish import publish_state_path

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    progress = tmp_path / "inv.jsonl"
    old_path = corpus / "ibm" / "zos" / "2.5" / "SA22-0000-00_alpha.pdf"
    _build_doc_with_id(old_path.parent, old_path.stem, DOC_A)
    assert _run_main(monkeypatch, corpus, progress) == 0
    live_1 = fake.aliases[ALIAS]
    rev_old = load_inventory(progress)[str(old_path)].source_rev
    assert rev_old and "|2.5|" in rev_old
    points_before = [(p.id, dict(p.payload or {})) for p in fake.collections[live_1]]

    new_path = corpus / "0ibm" / "zos" / "3.0" / "SA22-0000-00_alpha.pdf"
    new_path.parent.mkdir(parents=True)
    shutil.copyfile(old_path, new_path)
    with pytest.raises(RuntimeError, match="unmarked residue"):
        _run_main(monkeypatch, corpus, progress)
    assert fake.aliases[ALIAS] == live_1, "a refusal moves no alias"
    assert [(p.id, dict(p.payload or {})) for p in fake.collections[live_1]] == points_before
    rev_new = load_inventory(progress)[str(new_path)].source_rev
    assert rev_new and rev_new != rev_old
    staging = next(g for g in _gen_collections(fake) if g != live_1)
    assert _revisions_in(fake, staging) == {rev_old, rev_new}, "sibling kept, never swept"

    # Documented path through: abandon the refused build, retire explicitly.
    publish_state_path(progress, ALIAS).unlink()
    assert _run_main(monkeypatch, corpus, progress, "--retire-doc", f"{DOC_A}@{rev_old}") == 0
    live_2 = fake.aliases[ALIAS]
    assert live_2 not in (live_1, staging)
    assert _revisions_in(fake, live_2) == {rev_new}
    assert _marker_revisions_in(fake, live_2) == {rev_new}, "no orphaned marker"
    assert _revisions_in(fake, live_1) == {rev_old}, "superseded generation retained"
    # The next ordinary run is steady state.
    assert _run_main(monkeypatch, corpus, progress) == 0
    assert fake.aliases[ALIAS] == live_2


def test_relabel_after_inventory_loss_recovers_by_republishing_original_labels(tmp_path, monkeypatch):
    """Issue #1005 (S2-5), documented recovery: a relabel run with a lost
    progress file has no lineage, so the published revision stays and
    cutover refuses as residue. The inventory is the only retirement
    authority (SF1), so `--retire-doc` cannot name the published revision
    either. Supported path, with no control-collection edit: abandon the
    refused build, republish under the ORIGINAL labels (forced, since the
    serving generation cannot be re-certified without inventory) to rebuild
    the inventory record, then relabel; lineage replaces the old revision,
    leaving no orphaned marker, and the next ordinary run is steady state."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.inventory import load_inventory
    from mainframe_rag.ingest.publish import publish_state_path

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    path = _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inv.jsonl"
    original = ("--vendor", "ibm", "--product", "zos", "--version", "2.5")
    relabel = ("--vendor", "ibm", "--product", "zos", "--version", "3.0")
    assert _run_main(monkeypatch, corpus, progress, *original) == 0
    live_1 = fake.aliases[ALIAS]
    rev_old = load_inventory(progress)[str(path)].source_rev
    progress.unlink()  # the inventory is lost

    with pytest.raises(RuntimeError, match="unmarked residue"):
        _run_main(monkeypatch, corpus, progress, *relabel)
    publish_state_path(progress, ALIAS).unlink()  # abandon the refused build
    with pytest.raises(RuntimeError, match="unapproved revision"):
        _run_main(monkeypatch, corpus, progress, *relabel, "--retire-doc", f"{DOC_A}@{rev_old}")
    assert fake.aliases[ALIAS] == live_1, "refusals move no alias"

    assert _run_main(monkeypatch, corpus, progress, *original, "--reingest") == 0
    assert load_inventory(progress)[str(path)].source_rev == rev_old, "inventory rebuilt"
    live_2 = fake.aliases[ALIAS]
    assert _revisions_in(fake, live_2) == {rev_old}

    assert _run_main(monkeypatch, corpus, progress, *relabel) == 0
    live_3 = fake.aliases[ALIAS]
    rev_new = load_inventory(progress)[str(path)].source_rev
    assert rev_new != rev_old
    assert _revisions_in(fake, live_3) == {rev_new}
    assert _marker_revisions_in(fake, live_3) == {rev_new}, "no orphaned marker"
    assert _revisions_in(fake, live_1) == {rev_old}, "superseded generation retained"
    assert _run_main(monkeypatch, corpus, progress, *relabel) == 0
    assert fake.aliases[ALIAS] == live_3, "the next ordinary run is steady state"


def _lost_inventory_relabel(tmp_path, monkeypatch, fake):
    """Publish DOC_A under version 2.5, lose the progress file, then relabel to
    3.0: the refused build is abandoned. Returns (corpus, progress, path,
    relabel args, live, old revision)."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.inventory import load_inventory
    from mainframe_rag.ingest.publish import publish_state_path

    _publish_env(monkeypatch)
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    path = _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inv.jsonl"
    labels = ("--vendor", "ibm", "--product", "zos")
    assert _run_main(monkeypatch, corpus, progress, *labels, "--version", "2.5") == 0
    live = fake.aliases[ALIAS]
    rev_old = load_inventory(progress)[str(path)].source_rev
    progress.unlink()  # the inventory is lost
    relabel = (*labels, "--version", "3.0")
    with pytest.raises(RuntimeError, match="unmarked residue"):
        _run_main(monkeypatch, corpus, progress, *relabel)
    publish_state_path(progress, ALIAS).unlink()  # abandon the refused build
    return corpus, progress, path, relabel, live, rev_old


def test_authorize_from_catalog_retires_a_published_revision_after_inventory_loss(tmp_path, monkeypatch):
    """Issue #1005 (S2-5, option B): with the inventory lost, the published
    revision cannot be retired from the inventory. `--authorize-from-catalog`
    lets --retire-doc name it because the live build's sealed source catalog
    lists it and its marker and points verify in the live generation. Without
    the flag the same state still refuses. The next ordinary run is steady
    and never brings the retired revision back."""
    from mainframe_rag.ingest.inventory import load_inventory

    fake = PublishFake()
    corpus, progress, path, relabel, live_1, rev_old = _lost_inventory_relabel(
        tmp_path, monkeypatch, fake
    )
    retire = ("--retire-doc", f"{DOC_A}@{rev_old}")
    with pytest.raises(RuntimeError, match="unapproved revision"):
        _run_main(monkeypatch, corpus, progress, *relabel, *retire)
    assert fake.aliases[ALIAS] == live_1, "without the flag the inventory is the only authority"

    assert _run_main(monkeypatch, corpus, progress, *relabel, *retire, "--authorize-from-catalog") == 0
    live_2 = fake.aliases[ALIAS]
    assert live_2 != live_1
    rev_new = load_inventory(progress)[str(path)].source_rev
    assert _revisions_in(fake, live_2) == {rev_new}
    assert _marker_revisions_in(fake, live_2) == {rev_new}, "no orphaned marker"
    assert _revisions_in(fake, live_1) == {rev_old}, "superseded generation retained"

    assert _run_main(monkeypatch, corpus, progress, *relabel) == 0
    assert fake.aliases[ALIAS] == live_2, "the next ordinary run is steady state"
    assert rev_old not in _revisions_in(fake, live_2), "the retired revision stays retired"


@pytest.mark.parametrize("case", ["without_retire_doc", "not_in_catalog", "uncommitted"])
def test_authorize_from_catalog_refusals_mutate_nothing(tmp_path, monkeypatch, case):
    """Issue #1005 (option B): the catalog authority is explicit and bounded.
    The flag without --retire-doc, a revision the live catalog does not list,
    and a listed revision whose live marker and points no longer verify each
    refuse with a fixed message and a stable code, before any build."""
    fake = PublishFake()
    corpus, progress, _, relabel, live, rev_old = _lost_inventory_relabel(
        tmp_path, monkeypatch, fake
    )
    if case == "without_retire_doc":
        extra: tuple[str, ...] = ()
        code = "retire_catalog_requires_retire_doc"
    elif case == "not_in_catalog":
        extra = ("--retire-doc", f"{DOC_A}@ibm|zos|2.5|{'f' * 64}")
        code = "retire_not_in_catalog"
    else:
        extra = ("--retire-doc", f"{DOC_A}@{rev_old}")
        code = "retire_not_committed"
        damaged = next(p for p in fake.collections[live] if p.payload["source_rev"] == rev_old)
        fake.collections[live].remove(damaged)
    gens_before = _gen_collections(fake)
    with pytest.raises(RuntimeError) as excinfo:
        _run_main(monkeypatch, corpus, progress, *relabel, *extra, "--authorize-from-catalog")
    assert getattr(excinfo.value, "code", None) == code
    assert str(excinfo.value).startswith(f"{code}: ")
    assert "/" not in str(excinfo.value).split("(")[0], "fixed text, ids only"
    assert fake.aliases[ALIAS] == live
    assert _gen_collections(fake) == gens_before, "a refusal builds nothing"


def test_same_path_reissue_with_a_new_form_number_cuts_over_by_lineage(tmp_path, monkeypatch):
    """Issue #1006 (S2-6): the path's inventory lineage names the revision it
    replaces even when the reissue prints another form number. The ordinary
    rerun deletes that revision (points and markers) and cuts over; no
    --retire-doc is needed and nothing of the old revision stays live."""
    from scripts.make_synthetic_pdf import build as make_pdf

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.inventory import load_inventory

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    progress = tmp_path / "inv.jsonl"
    manual = corpus / "manual.pdf"
    make_pdf(manual, doc_id="GX28-1234-00", title="Original Issue")
    _build_doc_with_id(corpus, "doc_b", DOC_B)
    assert _run_main(monkeypatch, corpus, progress) == 0
    live_1 = fake.aliases[ALIAS]
    old = load_inventory(progress)[str(manual)]
    assert old.doc_id == "GX28-1234-00" and old.source_rev

    make_pdf(manual, doc_id="SC33-5678-00", title="Reissued Manual")
    assert _run_main(monkeypatch, corpus, progress) == 0
    live_2 = fake.aliases[ALIAS]
    assert live_2 != live_1
    new = load_inventory(progress)[str(manual)]
    assert new.doc_id == "SC33-5678-00" and new.source_rev != old.source_rev
    assert _live_doc_ids(fake) == {"SC33-5678-00", DOC_B}
    assert old.source_rev not in _revisions_in(fake, live_2)
    assert old.source_rev not in _marker_revisions_in(fake, live_2), "no orphaned marker"
    assert old.source_rev in _revisions_in(fake, live_1), "superseded generation retained"
    assert _run_main(monkeypatch, corpus, progress) == 0
    assert fake.aliases[ALIAS] == live_2, "the next ordinary run is steady state"


def test_stale_completion_markers_revision_scoped_exclusion():
    """R-REV: stale_completion_markers excuses only the retired revision,
    not retained sibling revisions of the same document."""
    from mainframe_rag.ingest.completion import stale_completion_markers

    fake = PublishFake()
    settings = _settings(qdrant_collection="coll")
    completions = "coll__completions"
    fake.collections[completions] = [
        SimpleNamespace(
            id="p1",
            payload={"doc_id": "DOC_A", "source_rev": "rev1", "manifest_digest": "old"},
        ),
        SimpleNamespace(
            id="p2",
            payload={"doc_id": "DOC_A", "source_rev": "rev2", "manifest_digest": "old"},
        ),
    ]
    # Retiring only rev1:
    retire_plan = {"DOC_A": {"revs": {"rev1"}, "whole": False}}
    count, labels = stale_completion_markers(
        fake,
        settings,
        "new_digest",
        exclude_doc_ids=frozenset({"DOC_A"}),
        retire_plan=retire_plan,
    )
    assert count == 1
    assert "DOC_A@rev2" in labels

    # Retiring whole document:
    retire_plan_whole = {"DOC_A": {"revs": {"rev1", "rev2"}, "whole": True}}
    count_whole, labels_whole = stale_completion_markers(
        fake,
        settings,
        "new_digest",
        exclude_doc_ids=frozenset({"DOC_A"}),
        retire_plan=retire_plan_whole,
    )
    assert count_whole == 0
    assert labels_whole == []


@pytest.mark.parametrize(
    ("changed_representation", "damage"),
    [(False, damage) for damage in (None, "text", "point", "marker", "marker_digest")]
    + [(True, None)],
)
def test_forced_build_retry_keeps_verified_document_checkpoint(tmp_path, monkeypatch, changed_representation, damage):
    """A completed document survives a later document failure without re-embedding.

    Exercise the real planner, parser/embedding and completion proof with the
    storage double; threads keep captured worker calls visible to the test.
    """
    import json

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import publish_state_path

    _publish_env(monkeypatch)
    monkeypatch.setenv("INGEST_UPSERT_STREAMS", "1")
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _two_doc_corpus(corpus)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live = fake.aliases[ALIAS]
    live_before = [p.model_dump() for p in fake.collections[live]]
    if changed_representation:
        monkeypatch.setenv("EMBED_MODEL_REVISION", "new-synthetic-revision")

    original_parse = run_ingest._parse_one
    parsed = []

    def capture_parse(args):
        parsed.append(Path(args[0]).stem)
        return original_parse(args)

    original_upsert = run_ingest._upsert_one
    upserted = []

    def failing_upsert(doc, *args, **kwargs):
        if doc.doc_id == DOC_B:
            raise RuntimeError("synthetic connection interruption")
        upserted.append(doc.doc_id)
        return original_upsert(doc, *args, **kwargs)

    monkeypatch.setattr(run_ingest, "_parse_one", capture_parse)
    monkeypatch.setattr(run_ingest, "_upsert_one", failing_upsert)
    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 1
    assert sorted(parsed) == ["doc_a", "doc_b"]  # New forced build must redo both.
    assert upserted == [DOC_A]
    assert fake.aliases[ALIAS] == live
    assert [p.model_dump() for p in fake.collections[live]] == live_before
    state_path = publish_state_path(progress, ALIAS)
    staging = json.loads(state_path.read_text())["staging"]
    checkpoint = [p.model_dump() for p in fake.collections[staging] if p.payload.get("doc_id") == DOC_A]
    if damage == "text":
        next(p for p in fake.collections[staging] if p.payload.get("doc_id") == DOC_A).payload["text"] = "Corrupt stored text"
    elif damage == "point":
        point = next(p for p in fake.collections[staging] if p.payload.get("doc_id") == DOC_A)
        fake.collections[staging].remove(point)
    elif damage in ("marker", "marker_digest", "wrong_target"):
        markers = fake.collections[staging + "__completions"]
        marker = next(p for p in markers if p.payload.get("doc_id") == DOC_A and p.payload.get("target_collection") == staging)
        if damage == "marker":
            markers.remove(marker)
        elif damage == "marker_digest":
            marker.payload["manifest_digest"] = "stale-contract"
        else:
            marker.payload["target_collection"] = live
    parsed.clear()

    def capture_upsert(doc, *args, **kwargs):
        upserted.append(doc.doc_id)
        return original_upsert(doc, *args, **kwargs)

    upserted.clear()
    monkeypatch.setattr(run_ingest, "_upsert_one", capture_upsert)
    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    assert sorted(parsed) == (["doc_a", "doc_b"] if damage else ["doc_b"])
    assert sorted(upserted) == ([DOC_A, DOC_B] if damage else [DOC_B])
    assert fake.aliases[ALIAS] == staging and not state_path.exists()
    if damage is None:
        assert [p.model_dump() for p in fake.collections[staging] if p.payload.get("doc_id") == DOC_A] == checkpoint
    assert [p.model_dump() for p in fake.collections[live]] == live_before
    parsed.clear()
    upserted.clear()
    assert _run_main(monkeypatch, corpus, progress) == 0
    assert parsed == upserted == []  # The next ordinary operation is read-only.
    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    assert sorted(parsed) == ["doc_a", "doc_b"]  # A new deliberate repair still rebuilds all.


def test_interrupted_first_build_resumes_only_under_explicit_reingest(tmp_path, monkeypatch):
    """Issue #737: the contract's only resumption of an interrupted first build.

    An ordinary build into an empty alias leaves its staging contract pending
    when a document fails. A plain retry must refuse before any parse or write;
    the explicit --reingest retry (what LOCAL_INGEST_REINGEST/--ingest-reingest
    select) reuses the verified checkpoint, embeds only the rest, publishes
    once, and the next ordinary operation is read-only.
    """
    import json

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import publish_state_path

    _publish_env(monkeypatch)
    monkeypatch.setenv("INGEST_UPSERT_STREAMS", "1")
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _two_doc_corpus(corpus)
    progress = tmp_path / "inv.jsonl"

    original_parse = run_ingest._parse_one
    original_upsert = run_ingest._upsert_one
    parsed: list[str] = []
    upserted: list[str] = []

    def capture_parse(args):
        parsed.append(Path(args[0]).stem)
        return original_parse(args)

    def failing_upsert(doc, *args, **kwargs):
        if doc.doc_id == DOC_B:
            raise RuntimeError("synthetic connection interruption")
        upserted.append(doc.doc_id)
        return original_upsert(doc, *args, **kwargs)

    monkeypatch.setattr(run_ingest, "_parse_one", capture_parse)
    monkeypatch.setattr(run_ingest, "_upsert_one", failing_upsert)
    assert _run_main(monkeypatch, corpus, progress) == 1
    assert ALIAS not in fake.aliases  # No partial corpus is ever published.
    state_path = publish_state_path(progress, ALIAS)
    staging = json.loads(state_path.read_text())["staging"]
    checkpoint = [p.model_dump() for p in fake.collections[staging] if p.payload.get("doc_id") == DOC_A]
    assert checkpoint and upserted == [DOC_A]

    def capture_upsert(doc, *args, **kwargs):
        upserted.append(doc.doc_id)
        return original_upsert(doc, *args, **kwargs)

    monkeypatch.setattr(run_ingest, "_upsert_one", capture_upsert)
    stored = {name: [p.model_dump() for p in points] for name, points in fake.collections.items()}
    parsed.clear()
    upserted.clear()
    with pytest.raises(RuntimeError, match="unfinished representation migration"):
        _run_main(monkeypatch, corpus, progress)
    assert parsed == upserted == []
    assert {name: [p.model_dump() for p in points] for name, points in fake.collections.items()} == stored
    assert ALIAS not in fake.aliases

    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    assert parsed == ["doc_b"] and upserted == [DOC_B]
    assert fake.aliases[ALIAS] == staging and not state_path.exists()
    assert [p.model_dump() for p in fake.collections[staging] if p.payload.get("doc_id") == DOC_A] == checkpoint
    parsed.clear()
    upserted.clear()
    assert _run_main(monkeypatch, corpus, progress) == 0
    assert parsed == upserted == []


@pytest.mark.parametrize("after_commit", [False, True])
def test_forced_retry_after_all_checkpoints_does_no_embedding(tmp_path, monkeypatch, after_commit):
    """Crashing around manifest commit must not redo a fully checkpointed build."""

    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _two_doc_corpus(corpus)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live = fake.aliases[ALIAS]
    monkeypatch.setenv("EMBED_MODEL_REVISION", "new-synthetic-revision")
    real_commit = run_ingest._commit_migration_representation

    def interrupted_commit(*args, **kwargs):
        if after_commit:
            real_commit(*args, **kwargs)
        raise RuntimeError("synthetic crash around commit")

    monkeypatch.setattr(run_ingest, "_commit_migration_representation", interrupted_commit)
    with pytest.raises(RuntimeError, match="synthetic crash around commit"):
        _run_main(monkeypatch, corpus, progress, "--reingest")
    assert fake.aliases[ALIAS] == live
    monkeypatch.setattr(run_ingest, "_commit_migration_representation", real_commit)

    def no_more_embedding(*args, **kwargs):
        raise AssertionError("all documents already have durable verified checkpoints")

    monkeypatch.setattr(run_ingest, "_parse_one", no_more_embedding)
    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    assert fake.aliases[ALIAS] != live
    assert _run_main(monkeypatch, corpus, progress) == 0


# ------------------------------------------- staging distribution gate (#360)
class _DistFake:
    """Minimal QdrantPoints double for the cutover distribution gate."""

    def __init__(self, params_by_collection=None, missing=(), get_error=None):
        self.params_by_collection = dict(params_by_collection or {})
        self.missing = set(missing)
        self.get_error = get_error

    def collection_exists(self, name):
        return name not in self.missing

    def get_collection(self, name):
        if self.get_error is not None:
            raise self.get_error
        if name not in self.params_by_collection:
            raise RuntimeError(f"collection {name} not found")
        params = self.params_by_collection[name]
        return SimpleNamespace(config=SimpleNamespace(params=params))


def _dist_settings(staging="genA", **overrides):
    kw = {
        "embed_mode": "hash",
        "qdrant_collection": staging,
        "qdrant_shard_number": 6,
        "qdrant_replication_factor": 3,
        "qdrant_write_consistency_factor": 2,
        "_env_file": None,
    }
    kw.update(overrides)
    return Settings(**kw)


def _dist_params(shards=6, rf=3, w=2):
    return SimpleNamespace(
        shard_number=shards, replication_factor=rf, write_consistency_factor=w
    )


def test_staging_distribution_gate_passes_only_the_selected_policy():
    staging = "genA"
    # No policy: passthrough, even with nothing stored.
    assert verify_staging_distribution(_DistFake(missing=(staging, f"{staging}__completions")), _settings()) == []
    matching = _DistFake({staging: _dist_params(), f"{staging}__completions": _dist_params()})
    assert verify_staging_distribution(matching, _dist_settings(staging)) == []
    # A partial explicit selection gates only its keys; unselected keys, even
    # wildly different, do not block.
    odd = _DistFake({
        staging: _dist_params(shards=99, rf=3, w=2),
        f"{staging}__completions": _dist_params(shards=99, rf=3, w=2),
    })
    partial = _settings(qdrant_collection=staging, qdrant_replication_factor=3, _env_file=None)
    assert verify_staging_distribution(odd, partial) == []


def _unreadable_dist_fake():
    fake = _DistFake(get_error=RuntimeError("config read failed"))
    fake.collection_exists = lambda _name: True  # type: ignore[method-assign]
    return fake


@pytest.mark.parametrize(
    "case",
    ["false-ha-corpus", "control-only", "unknown-values", "missing-collection", "unreadable-collection", "wrong-shard-layout"],
)
def test_staging_distribution_gate_refuses_mismatched_or_unverifiable_layouts(case):
    """Three peers mean nothing with one copy; the paired control collection
    gates cutover too; unlike the lenient ingest check, publication never
    certifies an unknown topology; refusals name the migration class."""
    staging = "genA"
    controls = f"{staging}__completions"
    one_copy = _dist_settings(staging, qdrant_write_consistency_factor=1)
    unknown = SimpleNamespace(shard_number=None, replication_factor=None, write_consistency_factor=None)
    fake, settings, needles = {
        "false-ha-corpus": (
            _DistFake({staging: _dist_params(6, 1, 1), controls: _dist_params()}),
            one_copy, ["replication_factor=1", "snapshot-gated", staging]),
        "control-only": (
            _DistFake({staging: _dist_params(), controls: _dist_params(6, 1, 1)}),
            one_copy, [controls, "replication_factor=1"]),
        "unknown-values": (
            _DistFake({staging: unknown, controls: _dist_params()}),
            _dist_settings(staging), ["unknown/unreadable"]),
        "missing-collection": (
            _DistFake({controls: _dist_params()}, missing=(staging,)),
            _dist_settings(staging), ["absent"]),
        "unreadable-collection": (
            _unreadable_dist_fake(), _dist_settings(staging), ["unreadable"]),
        "wrong-shard-layout": (
            _DistFake({staging: _dist_params(shards=3), controls: _dist_params(shards=3)}),
            _dist_settings(), ["shard-layout change", "rebuilt generation"]),
    }[case]
    text = "\n".join(verify_staging_distribution(fake, settings))
    assert text
    for needle in needles:
        assert needle in text, needle


class _PlacementFake:
    """Per-endpoint client double for the ACTIVE-copy gate: cluster_status
    plus collection_cluster_info, with failure injection per method."""

    def __init__(
        self,
        *,
        peer_id,
        members=(101, 202, 303),
        consensus="working",
        status_error=None,
        infos=None,
        info_error=None,
    ):
        self._peer_id = peer_id
        self._members = members
        self._consensus = consensus
        self._status_error = status_error
        self._infos = dict(infos or {})
        self._info_error = info_error

    def cluster_status(self):
        if self._status_error is not None:
            raise self._status_error
        return SimpleNamespace(
            status="enabled",
            peer_id=self._peer_id,
            peers={str(p): SimpleNamespace(uri=f"http://peer{p}:6335") for p in self._members},
            consensus_thread_status=SimpleNamespace(
                consensus_thread_status=self._consensus
            ),
        )

    def collection_cluster_info(self, name):
        if self._info_error is not None:
            raise self._info_error
        return self._infos[name]


def _placement_info(peer_id, shard_count, local_states, *, transfers=()):

    return models.CollectionClusterInfo(
        peer_id=peer_id,
        shard_count=shard_count,
        local_shards=[
            models.LocalShardInfo(
                shard_id=shard_id, points_count=10, state=models.ReplicaState[state]
            )
            for shard_id, state in local_states
        ],
        remote_shards=[],
        shard_transfers=[
            models.ShardTransferInfo(shard_id=shard_id, **{"from": src}, to=dst, sync=False)
            for shard_id, src, dst in transfers
        ],
    )


def _healthy_placement_map(staging="genA", peers=(101, 202, 303)):
    from mainframe_rag.ingest.completion import completion_collection_for

    control = completion_collection_for(staging)
    mapping = {}
    for index, peer in enumerate(peers):
        infos = {
            name: _placement_info(
                peer, 6, [(shard, "ACTIVE") for shard in range(6)]
            )
            for name in (staging, control)
        }
        mapping[f"http://peer{index}:6333"] = _PlacementFake(peer_id=peer, infos=infos)
    return mapping


def _placement_settings(staging="genA", **overrides):
    kw = {
        "embed_mode": "hash",
        "qdrant_collection": staging,
        "qdrant_shard_number": 6,
        "qdrant_replication_factor": 3,
        "qdrant_write_consistency_factor": 2,
        "_env_file": None,
    }
    kw.update(overrides)
    return Settings(**kw)


def test_staging_placement_gate_passes_healthy_and_single_node_profiles():
    assert Settings(_env_file=None).qdrant_peer_endpoints() == ()
    parsed = Settings(_env_file=None, qdrant_peer_urls="http://a:6333, http://b:6333 ,,")
    assert parsed.qdrant_peer_endpoints() == ("http://a:6333", "http://b:6333")
    assert verify_staging_placement({}, _settings()) == []
    assert verify_staging_placement(_healthy_placement_map(), _placement_settings()) == []

    class _Standalone:
        def cluster_status(self):
            return SimpleNamespace(status="disabled")

        def collection_cluster_info(self, name):
            return _placement_info(999, 1, [(0, "ACTIVE")])

    # The explicit 1/1/1 profile judges its single ACTIVE copy through its one
    # endpoint — a standalone server reports no cluster peer id.
    single = _settings(
        qdrant_collection="genA", qdrant_shard_number=1, qdrant_replication_factor=1,
        qdrant_write_consistency_factor=1,
    )
    assert verify_staging_placement({"http://127.0.0.1:6333": _Standalone()}, single) == []

    class _Down:
        def cluster_status(self):
            raise ConnectionError("refused")

        def collection_cluster_info(self, name):
            raise ConnectionError("refused")

    problems = verify_staging_placement({"http://127.0.0.1:6333": _Down()}, single)
    assert any("unreachable" in p for p in problems)


@pytest.mark.parametrize(
    "case",
    ["partial-policy", "no-endpoints", "too-few-endpoints", "unreachable-peer", "duplicate-peer",
     "transfer-in-progress", "under-replicated-shard"],
)
def test_staging_placement_gate_refuses_unverifiable_or_degraded_copies(case):
    """Actual copies cannot be judged without the complete policy tuple, zero
    observations never certify, and a dead peer, a Service masquerading as a
    third copy, a running transfer or a stale replica are degraded, not
    healthy: cutover waits while the old generation keeps serving."""
    healthy = _healthy_placement_map()
    urls = list(healthy)
    infos = dict(healthy[urls[0]]._infos)
    mapping, settings, needles = healthy, _placement_settings("genA"), []
    if case == "partial-policy":
        settings = _settings(qdrant_collection="genA", qdrant_replication_factor=3)
        needles = ["partial policy"]
    elif case == "no-endpoints":
        mapping, needles = {}, ["QDRANT_PEER_URLS", "entry endpoint"]
    elif case == "too-few-endpoints":
        mapping, needles = {urls[0]: healthy[urls[0]]}, ["cannot hold replication_factor=3"]
    elif case == "unreachable-peer":
        mapping[urls[2]] = _PlacementFake(
            peer_id=None, status_error=ConnectionError("refused"), info_error=ConnectionError("refused"))
        needles = ["degraded"]
    elif case == "duplicate-peer":
        mapping[urls[2]] = _PlacementFake(peer_id=101, infos=infos)
        needles = ["distinct peer id"]
    elif case == "transfer-in-progress":
        infos["genA"] = _placement_info(
            101, 6, [(shard, "ACTIVE") for shard in range(6)], transfers=[(0, 101, 202)])
        mapping[urls[0]] = _PlacementFake(peer_id=101, infos=infos)
        needles = ["recovering"]
    else:
        infos["genA"] = _placement_info(101, 6, [(s_, "ACTIVE") for s_ in range(1, 6)] + [(0, "DEAD")])
        mapping[urls[0]] = _PlacementFake(peer_id=101, infos=infos)
        needles = ["genA"]
    text = "\n".join(verify_staging_placement(mapping, settings))
    assert text
    for needle in needles:
        assert needle in text, needle
    if case == "under-replicated-shard":
        assert "degraded" in text or "2/3" in text


@pytest.mark.parametrize("fault", ["before", "after"])
@pytest.mark.parametrize(
    "operations",
    tuple(permutations(("change", "repair", "retire"))),
)
def test_bounded_publication_lifecycle_traces(tmp_path, monkeypatch, operations, fault):
    from tests.helpers_publication_lifecycle import exercise_publication_trace

    exercise_publication_trace(tmp_path, monkeypatch, PublishFake(), operations, fault, ALIAS)


@pytest.mark.parametrize(
    ("operation", "gate"),
    [
        ("repair", gate)
        for gate in (
            "check_ingest_compatible", "verify_all_complete", "read_build_binding",
        )
    ]
    + [("retire", "verify_all_complete")],
)
def test_failed_post_cutover_revalidation_preserves_finalization(
    tmp_path, monkeypatch, operation, gate
):
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import publish_state_path
    from tests.helpers_publication_lifecycle import _records

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _, removed = _two_doc_corpus(corpus)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    old = fake.aliases[ALIAS]
    retained = (_records(fake, old), _records(fake, old + "__completions"))
    extra = ("--reingest",)
    if operation == "retire":
        removed.unlink()
        extra = ("--retire-doc", DOC_B)

    original_swap = run_ingest.swap_alias_to

    def after_cutover(*args, **kwargs):
        original_swap(*args, **kwargs)
        raise RuntimeError("interrupted finalization")

    with monkeypatch.context() as patch:
        patch.setattr(run_ingest, "swap_alias_to", after_cutover)
        with pytest.raises(RuntimeError, match="interrupted finalization"):
            _run_main(monkeypatch, corpus, progress, *extra)
    current = fake.aliases[ALIAS]
    assert current != old
    state_path = publish_state_path(progress, ALIAS)
    saved_state = state_path.read_bytes()
    saved_progress = progress.read_bytes()
    published = (_records(fake, current), _records(fake, current + "__completions"))

    def unavailable(*args, **kwargs):
        raise RuntimeError("unavailable certification evidence")

    with monkeypatch.context() as patch:
        patch.setattr(run_ingest, gate, unavailable)
        with pytest.raises(RuntimeError, match="unavailable certification evidence"):
            _run_main(monkeypatch, corpus, progress, *extra)
    assert state_path.exists(), "failed revalidation consumed the recovery sidecar"
    assert state_path.read_bytes() == saved_state
    assert progress.read_bytes() == saved_progress, "failed revalidation committed retirement"
    assert fake.aliases[ALIAS] == current
    assert (_records(fake, current), _records(fake, current + "__completions")) == published

    assert _run_main(monkeypatch, corpus, progress, *extra) == 0
    assert not state_path.exists()
    for _ in range(2):
        assert _run_main(monkeypatch, corpus, progress) == 0
        assert fake.aliases[ALIAS] == current
        assert (_records(fake, current), _records(fake, current + "__completions")) == published
        assert (_records(fake, old), _records(fake, old + "__completions")) == retained


@pytest.mark.parametrize("damage", ["version_bool", "plan_list", "revs_string", "old_legacy_field", "missing_whole", "extra_entry_field", "empty_selection", "request_number", "plan_extra_doc", "duplicate_key",
    "request_empty", "entry_scalar", "whole_widened", "partial_widened"])
def test_corrupt_publish_record_refuses_without_rewriting(tmp_path, damage):
    import json

    from mainframe_rag.ingest.publish import (
        publish_state_path,
        read_publish_state,
        write_publish_state,
    )

    progress = tmp_path / "progress.jsonl"
    write_publish_state(
        progress, ALIAS, ALIAS + "__genbuild", "build", "corpus",
        retire_plan={"Manual Alpha": {"revs": {"rev-1"}, "whole": False}},
        retire_docs=("Manual Alpha@rev-1",),
    )
    path = publish_state_path(progress, ALIAS)
    record = json.loads(path.read_text())
    entry = record["retire_plan"]["Manual Alpha"]
    if damage == "version_bool":
        record["version"] = True
    elif damage == "version_float":
        record["version"] = 1.0
    elif damage == "plan_list":
        record["retire_plan"] = []
    elif damage == "entry_scalar":
        record["retire_plan"]["Manual Alpha"] = "discard me"
    elif damage == "revs_string":
        entry["revs"] = "rev-1"
    elif damage == "revs_nested":
        entry["revs"] = [["rev-1"]]
    elif damage == "revs_number":
        entry["revs"] = [1]
    elif damage == "old_legacy_field":
        entry["legacy"] = False  # a sidecar from before revision-less support was removed
    elif damage == "whole_number":
        entry["whole"] = 1
    elif damage == "missing_whole":
        del entry["whole"]
    elif damage == "extra_entry_field":
        entry["all_revisions"] = True
    elif damage == "empty_doc":
        record["retire_plan"][""] = record["retire_plan"].pop("Manual Alpha")
    elif damage == "empty_selection":
        entry["revs"] = []
    elif damage == "requests_string":
        record["retire_docs"] = "Manual Alpha@rev-1"
    elif damage == "request_number":
        record["retire_docs"] = [1]
    elif damage == "request_empty":
        record["retire_docs"] = ["Manual Alpha@"]
    elif damage == "plan_extra_doc":
        record["retire_plan"]["Unrequested"] = dict(entry)
    elif damage == "partial_widened":
        entry["revs"] = ["rev-1", "rev-2"]
    elif damage == "whole_widened":
        entry["whole"] = True
    raw = json.dumps(record)
    if damage == "duplicate_key":
        raw = raw.replace('"whole": false', '"whole": false, "whole": true')
    path.write_text(raw)
    with pytest.raises(RuntimeError, match="publish state"):
        read_publish_state(progress, ALIAS)
    assert path.read_text() == raw, damage



@pytest.mark.parametrize(
    ("version", "fields"), [(1, "both"), (2, "both"), (2, "neither")]
)
def test_valid_publish_record_preserves_supported_optional_fields(tmp_path, version, fields):
    from mainframe_rag.ingest.publish import read_publish_state, write_publish_state

    plan = {
        "Manual Alpha": {"revs": {"rev-1", "rev-2"}, "whole": True},
        "Manual Beta": {"revs": {"rev-3"}, "whole": False},
    }
    flags = (" Manual Alpha ", "Manual Beta@rev-3")
    kwargs = {}
    if fields in ("plan", "both"):
        kwargs["retire_plan"] = plan
    if fields in ("requests", "both"):
        kwargs["retire_docs"] = flags
    progress = tmp_path / "progress.jsonl"
    write_publish_state(progress, ALIAS, ALIAS + "__genbuild", "build", "corpus", **kwargs)
    if version == 1:
        import json

        from mainframe_rag.ingest.publish import publish_state_path

        path = publish_state_path(progress, ALIAS)
        record = json.loads(path.read_text())
        record["version"] = 1
        del record["build_id"], record["previous"]
        path.write_text(json.dumps(record))
    result = read_publish_state(progress, ALIAS)
    assert result["version"] == version
    assert result["staging"] == ALIAS + "__genbuild"
    if "retire_plan" in kwargs:
        assert result["retire_plan"] == plan
    else:
        assert "retire_plan" not in result
    if "retire_docs" in kwargs:
        assert result["retire_docs"] == list(flags)
    else:
        assert "retire_docs" not in result



def _exercise_corrupt_retirement_retry(tmp_path, monkeypatch, client, alias):
    import json

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import publish_state_path
    from tests.helpers_publication_lifecycle import TEXTS, _records, _write_source

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for name, text in TEXTS.items():
        _write_source(corpus, name, text)
    progress = tmp_path / "progress.jsonl"
    _publish_env(monkeypatch)
    monkeypatch.setenv("QDRANT_COLLECTION", alias)
    monkeypatch.setenv("DENSE_DIM", "256")
    monkeypatch.setenv("EMBED_MODEL_REVISION", "")
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: client)

    def run(*args):
        return _run_main(monkeypatch, corpus, progress, *args)

    def target():
        return next(a.collection_name for a in client.get_aliases().aliases if a.alias_name == alias)

    assert run() == 0
    live = target()
    (corpus / "beta.pdf").unlink()

    def interrupt(*args, **kwargs):
        raise RuntimeError("interrupted before retirement deletion")

    with monkeypatch.context() as patch:
        patch.setattr(run_ingest, "apply_approved_removals", interrupt)
        with pytest.raises(RuntimeError, match="interrupted before retirement deletion"):
            run("--retire-doc", "beta")
    state_path = publish_state_path(progress, alias)
    valid = state_path.read_bytes()
    state = json.loads(valid)
    staging = state["staging"]
    collections = (live, live + "__completions", staging, staging + "__completions")
    before = {name: _records(client, name) for name in collections}
    progress_before = progress.read_bytes()
    state["retire_plan"]["beta"]["whole"] = "false"
    state_path.write_text(json.dumps(state))
    corrupt = state_path.read_bytes()
    with pytest.raises(RuntimeError, match="invalid retirement authorization in publish state"):
        run("--retire-doc", "beta")
    assert target() == live
    assert state_path.read_bytes() == corrupt
    assert progress.read_bytes() == progress_before
    assert {name: _records(client, name) for name in collections} == before

    # Restoring the original record is an explicit test recovery, never a parser fallback.
    state_path.write_bytes(valid)
    assert run("--retire-doc", "beta") == 0
    assert target() == staging
    assert not state_path.exists()
    published = (_records(client, staging), _records(client, staging + "__completions"))
    assert [(p["doc_id"], p["text"]) for p, _ in published[0].values()] == [("alpha", TEXTS["alpha"])]
    for _ in range(2):
        assert run() == 0
        assert target() == staging
        assert (_records(client, staging), _records(client, staging + "__completions")) == published
        assert _records(client, live) == before[live]
        assert _records(client, live + "__completions") == before[live + "__completions"]


def test_corrupt_retirement_retry_preserves_stored_state(tmp_path, monkeypatch):
    _exercise_corrupt_retirement_retry(tmp_path, monkeypatch, PublishFake(), ALIAS)


def _exercise_build_uuid_recovery(tmp_path, monkeypatch, fake, alias):
    import json
    import uuid

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import publish_state_path
    from tests.helpers_publication_lifecycle import _records

    _publish_env(monkeypatch)
    monkeypatch.setenv("QDRANT_COLLECTION", alias)
    monkeypatch.setenv("DENSE_DIM", "256")
    monkeypatch.setenv("EMBED_MODEL_REVISION", "")
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inventory.jsonl"
    sidecar = publish_state_path(progress, alias)
    create = fake.create_collection
    allocated = []

    def require_allocation(*args, **kwargs):
        state = json.loads(sidecar.read_text())
        assert state["version"] == 2
        assert str(uuid.UUID(state["build_id"])) == state["build_id"]
        allocated.append(state["build_id"])
        return create(*args, **kwargs)

    monkeypatch.setattr(fake, "create_collection", require_allocation)
    swap = run_ingest.swap_alias_to
    def fail_swap(*args, **kwargs):
        raise RuntimeError("injected swap failure")
    monkeypatch.setattr(run_ingest, "swap_alias_to", fail_swap)
    with pytest.raises(RuntimeError, match="injected swap failure"):
        _run_main(monkeypatch, corpus, progress)
    state = json.loads(sidecar.read_text())
    build_id = state["build_id"]
    physical = state["staging"]
    assert allocated and set(allocated) == {build_id}
    def aliases():
        return {a.alias_name: a.collection_name for a in fake.get_aliases().aliases}

    assert not any(name == alias or name.startswith(alias + "__build_") for name in aliases())
    frozen = (_records(fake, physical), _records(fake, physical + "__completions"))
    receipts = [p for p, _ in frozen[1].values() if p.get("record_type") == "publication-metadata"]
    assert len(receipts) == 1
    assert receipts[0]["build_id"] == build_id
    assert receipts[0]["build_schema"] == 3
    assert receipts[0]["data_collection"] == physical
    assert receipts[0]["logical_alias"] == alias

    monkeypatch.setattr(run_ingest, "swap_alias_to", swap)
    with monkeypatch.context() as patch:
        def refuse_mutation(*args, **kwargs):
            pytest.fail("sealed retry or ordinary read-only run mutated stored points")
        for method in ("create_collection", "upsert", "delete", "recover_snapshot"):
            patch.setattr(fake, method, refuse_mutation)
        for extra in (("--reingest",), (), ()):
            assert _run_main(monkeypatch, corpus, progress, *extra) == 0
            assert not sidecar.exists()
            assert (_records(fake, physical), _records(fake, physical + "__completions")) == frozen
    assert {name: target for name, target in aliases().items()
            if name == alias or name.startswith(alias + "__build_")} == {
        alias: physical,
        f"{alias}__build_{build_id}": physical,
        f"{alias}__build_{build_id}__completions": physical + "__completions",
    }
    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    successor = aliases()[alias]
    assert successor != physical
    assert (_records(fake, physical), _records(fake, physical + "__completions")) == frozen
    successor_receipts = [p for p, _ in _records(fake, successor + "__completions").values()
                          if p.get("record_type") == "publication-metadata"]
    assert len(successor_receipts) == 1
    assert successor_receipts[0]["build_id"] != build_id
    assert aliases()[f"{alias}__build_{build_id}"] == physical


def test_build_uuid_precedes_writes_and_sealed_retry_is_read_only(tmp_path, monkeypatch):
    _exercise_build_uuid_recovery(tmp_path, monkeypatch, PublishFake(), ALIAS)


@pytest.mark.parametrize("damage", ["version", "uuid", "predecessor", "control", "partial-alias", "missing-control"])
def test_sealed_retry_refuses_inconsistent_build_without_mutation(tmp_path, monkeypatch, damage):
    import json
    from copy import deepcopy

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import publication_metadata_point_id, publish_state_path

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inventory.jsonl"
    sidecar = publish_state_path(progress, ALIAS)
    fake.fail_swap = "raise"
    with pytest.raises(RuntimeError, match="injected swap failure"):
        _run_main(monkeypatch, corpus, progress)
    state = json.loads(sidecar.read_text())
    physical = state["staging"]
    control = physical + "__completions"
    data_alias = ALIAS + "__build_" + state["build_id"]
    receipt = next(p for p in fake.collections[control]
                   if str(p.id) == publication_metadata_point_id(control))
    if damage == "version":
        state["version"] = 99
    elif damage == "uuid":
        state["build_id"] = "00000000-0000-0000-0000-000000000000"
    elif damage == "predecessor":
        state["previous"] = "unobserved-predecessor"
    elif damage == "control":
        receipt.payload["build_id"] = "12345678-1234-4234-8234-123456789abc"
    elif damage == "partial-alias":
        fake.aliases[data_alias] = physical
    elif damage == "redirect-alias":
        fake.aliases[data_alias] = "another-physical"
    elif damage == "missing-control":
        fake.aliases[data_alias] = physical
        fake.collections[control].remove(receipt)
    sidecar.write_text(json.dumps(state))
    before = deepcopy(fake.collections), dict(fake.aliases), sidecar.read_bytes(), progress.read_bytes()
    fake.fail_swap = None
    with pytest.raises(RuntimeError):
        _run_main(monkeypatch, corpus, progress, "--reingest")
    assert (fake.collections, fake.aliases, sidecar.read_bytes(), progress.read_bytes()) == before, damage



@pytest.mark.parametrize("target_kind", ["physical", "retained", "build-alias"])
def test_publication_refuses_a_generation_targeted_by_name(tmp_path, monkeypatch, target_kind):
    """QDRANT_COLLECTION naming a generation's physical collection (or a
    build alias) is a misconfiguration, never a legacy layout: before the
    refusal, publication "migrated" the serving physical — copied it aside
    and deleted the collection its logical alias still served. The refusal
    mutates nothing, and the next ordinary publication still succeeds."""
    from copy import deepcopy

    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inventory.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    physical = fake.aliases[ALIAS]
    target = physical
    if target_kind == "retained":
        assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
        assert fake.aliases[ALIAS] != physical, "the first build is now retained"
    elif target_kind == "build-alias":
        target = next(name for name, value in fake.aliases.items()
                      if name != ALIAS and value == physical)
    before = deepcopy(fake.collections), dict(fake.aliases), progress.read_bytes()
    monkeypatch.setenv("QDRANT_COLLECTION", target)
    with pytest.raises(
        RuntimeError, match=None if target_kind == "build-alias" else "publish through its logical alias"
    ):
        _run_main(monkeypatch, corpus, progress, "--reingest")
    assert (fake.collections, fake.aliases, progress.read_bytes()) == before
    monkeypatch.setenv("QDRANT_COLLECTION", ALIAS)
    assert _run_main(monkeypatch, corpus, progress) == 0
    assert fake.aliases[ALIAS] in fake.collections




def test_new_allocation_never_reuses_orphan_controls():
    from copy import deepcopy

    from mainframe_rag.ingest.publish import _fresh_staging_candidate, resolve_publish_staging

    fake = PublishFake()
    settings = Settings(_env_file=None, qdrant_collection=ALIAS, embed_mode="hash")
    base = staging_name_for(ALIAS, "recipe", "corpus")
    # Controls alone reserve a physical name, even if its data was lost.
    fake.collections[base + "__completions"] = []
    fake.collections[base + "_1__completions"] = []
    before = deepcopy(fake.collections)
    with pytest.raises(RuntimeError, match="unrecorded unfinished build"):
        resolve_publish_staging(fake, settings, gen_fp="recipe", corpus_fp="corpus",
                                live=None, force_reingest=False, state=None)
    assert _fresh_staging_candidate(fake, base, None) == base + "_2"
    assert fake.collections == before




@pytest.mark.parametrize("damage", ["lost-document", "sparse", "control", "seal-missing", "seal-schema"])
def test_sealed_content_damage_refuses_retry_without_mutation(tmp_path, monkeypatch, damage):
    import json
    from copy import deepcopy

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import publication_metadata_point_id, publish_state_path

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _two_doc_corpus(corpus)
    progress = tmp_path / "inventory.jsonl"
    fake.fail_swap = "raise"
    with pytest.raises(RuntimeError, match="injected swap failure"):
        _run_main(monkeypatch, corpus, progress)
    sidecar = publish_state_path(progress, ALIAS)
    physical = json.loads(sidecar.read_text())["staging"]
    control = physical + "__completions"
    receipt = next(p for p in fake.collections[control] if str(p.id) == publication_metadata_point_id(control))
    if damage == "lost-document":
        for name in (physical, control):
            fake.collections[name] = [p for p in fake.collections[name] if p.payload.get("doc_id") != DOC_A]
    elif damage == "text":
        fake.collections[physical][0].payload["text"] = "Unexpected replacement"
    elif damage == "dense":
        fake.collections[physical][0].vector["dense"][0] += 0.25
    elif damage == "sparse":
        fake.collections[physical][0].vector["bm25"].values[0] += 0.5
    elif damage == "control":
        next(p for p in fake.collections[control] if p.payload.get("doc_id") == DOC_A).payload["finished_at"] += 1
    elif damage == "seal-missing":
        del receipt.payload["content_seal"]
    else:
        receipt.payload["content_seal"]["schema"] = 999
    before = deepcopy(fake.collections), dict(fake.aliases), sidecar.read_bytes(), progress.read_bytes()
    fake.fail_swap = None
    with pytest.raises(RuntimeError, match="seal|build control"):
        _run_main(monkeypatch, corpus, progress, "--reingest")
    assert (fake.collections, fake.aliases, sidecar.read_bytes(), progress.read_bytes()) == before, damage



def _exercise_retained_content_seal(tmp_path, monkeypatch, client, alias, damage):

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import verify_publication_seal
    from tests.helpers_publication_lifecycle import TEXTS, _records, _write_source

    _publish_env(monkeypatch)
    monkeypatch.setenv("QDRANT_COLLECTION", alias)
    monkeypatch.setenv("DENSE_DIM", "256")
    monkeypatch.setenv("EMBED_MODEL_REVISION", "")
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: client)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for name, text in TEXTS.items():
        _write_source(corpus, name, text)
    progress = tmp_path / "inventory.jsonl"
    def target():
        return next(a.collection_name for a in client.get_aliases().aliases if a.alias_name == alias)

    assert _run_main(monkeypatch, corpus, progress) == 0
    retained = target()
    control = retained + "__completions"
    original = _records(client, retained)
    assert sorted((p["doc_id"], p["text"]) for p, _ in original.values()) == sorted(TEXTS.items())
    assert verify_publication_seal(client, control) is True
    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    current = target()
    assert current != retained
    assert _records(client, retained) == original
    assert verify_publication_seal(client, control) is True
    current_before = (_records(client, current), _records(client, current + "__completions"))
    receipt_before = {i: value for i, value in _records(client, control).items()
                      if value[0].get("record_type") == "publication-metadata"}
    if damage == "lost-document":
        # Remove a whole intended member AND its completion. Surviving markers
        # cannot establish what is missing; the retained seal can.
        for collection in (retained, control):
            ids = [i for i, (payload, _) in _records(client, collection).items() if payload.get("doc_id") == "beta"]
            assert ids
            client.delete(collection, points_selector=models.PointIdsList(points=ids), wait=True)
        assert [(p["doc_id"], p["text"]) for p, _ in _records(client, retained).values()] == [("alpha", TEXTS["alpha"])]
    else:
        point_id, (payload, vector) = next(iter(original.items()))
        vector = dict(vector)
        if damage == "dense":
            vector["dense"] = [1.0] + [0.0] * 255
        else:
            assert damage == "sparse"
            vector["bm25"] = models.SparseVector(indices=[2147483646], values=[9.0])
        client.upsert(retained, points=[models.PointStruct(id=point_id, payload=payload, vector=vector)], wait=True)
        observed = _records(client, retained)
        assert {i: p for i, (p, _) in observed.items()} == {i: p for i, (p, _) in original.items()}
        assert observed[point_id][1]["dense" if damage == "dense" else "bm25"] != original[point_id][1]["dense" if damage == "dense" else "bm25"]
    damaged = (_records(client, retained), _records(client, control))
    assert {i: value for i, value in damaged[1].items()
            if value[0].get("record_type") == "publication-metadata"} == receipt_before
    with pytest.raises(RuntimeError, match="does not match its seal"):
        verify_publication_seal(client, control)
    assert (_records(client, retained), _records(client, control)) == damaged
    for _ in range(2):
        assert _run_main(monkeypatch, corpus, progress) == 0
        assert target() == current
        assert (_records(client, current), _records(client, current + "__completions")) == current_before
        assert (_records(client, retained), _records(client, control)) == damaged


@pytest.mark.parametrize("damage", ["lost-document", "sparse"])
def test_retained_seal_detects_stored_content_loss(tmp_path, monkeypatch, damage):
    _exercise_retained_content_seal(tmp_path, monkeypatch, PublishFake(), ALIAS, damage)


def test_content_seal_canonical_projection_and_pagination():
    import hashlib
    import json
    import uuid

    from mainframe_rag.ingest.seal import capture_content_seal

    data_id = "12345678-1234-4234-8234-123456789abc"
    point = models.Record(id=data_id, payload={"text": "Alpha\nβ", "n": 2},
                          vector={"dense": [0.25, -0.5], "bm25": models.SparseVector(indices=[3], values=[1.5])})
    fake = PublishFake()
    fake.collections = {"physical": [point], "physical__completions": []}
    args = {"build_id": data_id, "alias": "corpus | x", "physical": "physical", "gen_fp": "g", "corpus_fp": "c", "receipt_id": "receipt"}
    seal = capture_content_seal(fake, **args)
    # Independent literal serialization: catches omission of vectors, payload,
    # identity, whitespace, Unicode or the whole binding from the certificate.
    literal = b'["12345678-1234-4234-8234-123456789abc",{"n":2,"text":"Alpha\\n\\u03b2"},{"bm25":{"indices":[3],"values":[1.5]},"dense":[0.25,-0.5]}]'
    prefix = b'["build-content-seal",1,["12345678-1234-4234-8234-123456789abc","corpus | x","physical","g","c"],"data",1]'
    assert seal["data"] == {"count": 1, "sha256": hashlib.sha256(prefix + hashlib.sha256(literal).digest()).hexdigest()}
    # More than one page; traversal and object-key order do not change the root.
    fake.collections["physical"] = [point.model_copy(update={"id": str(uuid.UUID(int=n+1))}) for n in range(300)]
    many = capture_content_seal(fake, **args)
    assert many["data"]["count"] == 300
    fake.collections["physical"].reverse()
    for p in fake.collections["physical"]:
        p.payload = json.loads('{"n":2,"text":"Alpha\\nβ"}')
    assert capture_content_seal(fake, **args) == many
    assert capture_content_seal(fake, **{**args, "build_id": "another"}) != many


@pytest.mark.parametrize("damage", ["projection", "nonfinite", "duplicate", "offset-loop"])
def test_content_seal_refuses_incomplete_scan(damage):

    from mainframe_rag.ingest.seal import capture_content_seal

    point = SimpleNamespace(id="12345678-1234-4234-8234-123456789abc", payload={"text": "Original"}, vector={"dense": [0.25], "bm25": models.SparseVector(indices=[1], values=[1.0])})
    fake = PublishFake()
    fake.collections = {"physical": [point], "physical__completions": []}
    if damage == "projection":
        point.vector = None
    elif damage == "nonfinite":
        point.vector["dense"][0] = float("nan")
    elif damage == "duplicate":
        fake.collections["physical"].append(point)
    else:
        fake.scroll = lambda *a, **kw: ([point], "never-progresses")
    with pytest.raises(ValueError):
        capture_content_seal(fake, build_id="b", alias="a", physical="physical", gen_fp="g", corpus_fp="c", receipt_id="receipt")



def test_completed_binding_without_seal_is_not_retroactively_certified(tmp_path, monkeypatch):
    from copy import deepcopy

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import verify_publication_seal

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inventory.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    physical = fake.aliases[ALIAS]
    control = physical + "__completions"
    receipt = next(p for p in fake.collections[control] if p.payload.get("record_type") == "publication-metadata")
    receipt.payload["build_schema"] = 1
    del receipt.payload["content_seal"]
    del receipt.payload["source_catalog"]  # a real schema-1 receipt never had one (#827)
    before = deepcopy(fake.collections), dict(fake.aliases)
    assert verify_publication_seal(fake, control) is False
    assert _run_main(monkeypatch, corpus, progress) == 0
    assert (fake.collections, fake.aliases) == before
    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    assert fake.aliases[ALIAS] != physical
    assert verify_publication_seal(fake, fake.aliases[ALIAS] + "__completions") is True
    assert fake.collections[physical] == before[0][physical]
    assert fake.collections[control] == before[0][control]


@pytest.mark.parametrize("damage", ["missing-receipt", "corrupt-seal"])
def test_publication_verifies_receipt_readback_before_cutover(tmp_path, monkeypatch, damage):
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    upsert = fake.upsert
    def lose_receipt(collection, *, points, wait=True):
        if any(p.payload.get("record_type") == "publication-metadata" for p in points):
            if damage == "missing-receipt":
                return SimpleNamespace()
            points[0].payload["content_seal"]["data"]["sha256"] = "0" * 64
        return upsert(collection, points=points, wait=wait)
    monkeypatch.setattr(fake, "upsert", lose_receipt)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inventory.jsonl"
    with pytest.raises(RuntimeError, match="seal"):
        _run_main(monkeypatch, corpus, progress)
    assert not fake.aliases and not fake.alias_calls


def test_unfinished_schema_one_sealed_build_requires_its_old_release(tmp_path, monkeypatch):
    import json
    from copy import deepcopy

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import publish_state_path

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inventory.jsonl"
    fake.fail_swap = "raise"
    with pytest.raises(RuntimeError, match="injected swap failure"):
        _run_main(monkeypatch, corpus, progress)
    sidecar = publish_state_path(progress, ALIAS)
    control = json.loads(sidecar.read_text())["staging"] + "__completions"
    receipt = next(p for p in fake.collections[control] if p.payload.get("record_type") == "publication-metadata")
    receipt.payload["build_schema"] = 1
    del receipt.payload["content_seal"]
    del receipt.payload["source_catalog"]  # a real schema-1 receipt never had one (#827)
    before = deepcopy(fake.collections), sidecar.read_bytes(), progress.read_bytes()
    fake.fail_swap = None
    with pytest.raises(RuntimeError, match="old-format"):
        _run_main(monkeypatch, corpus, progress)
    assert (fake.collections, sidecar.read_bytes(), progress.read_bytes()) == before
    assert not fake.aliases


# ------------------------------------------------ distributed recovery guard (#360)
class _TopologyFake(PublishFake):
    """PublishFake whose collections report a configured shard/replica layout."""

    def __init__(self, shard_number=1, replication_factor=1):
        super().__init__()
        self.layout = (shard_number, replication_factor)

    def get_collection(self, name):
        info = super().get_collection(name)
        info.config.params.shard_number, info.config.params.replication_factor = self.layout
        return info


def _seed_live_pair(fake, live="mainframe_manuals__genLIVE"):

    fake.collections[live] = [
        models.PointStruct(
            id="00000000-0000-0000-0000-000000000001",
            vector={"dense": [0.0] * 256, "bm25": models.SparseVector(indices=[0], values=[1.0])},
            payload={"doc_id": "D", "text": "kept"},
        )
    ]
    fake.collections[f"{live}__completions"] = []
    fake.aliases[ALIAS] = live
    return live


def _point_records(fake, name):
    return [(str(p.id), dict(p.payload or {}), getattr(p, "vector", None)) for p in fake.collections[name]]


class _RecordingTopologyFake(_TopologyFake):
    """Records collection creation arguments and every mutation kind."""

    def __init__(self, *layout):
        super().__init__(*layout)
        self.created: dict[str, dict] = {}
        self.mutations: list[str] = []

    def create_collection(self, name, **kwargs):
        self.created[name] = kwargs
        self.mutations.append("create")
        return super().create_collection(name, **kwargs)

    def create_snapshot(self, *args, **kwargs):
        self.mutations.append("snapshot")
        return super().create_snapshot(*args, **kwargs)

    def recover_snapshot(self, *args, **kwargs):
        self.mutations.append("recover")
        return super().recover_snapshot(*args, **kwargs)

    def delete_collection(self, *args, **kwargs):
        self.mutations.append("delete")
        return super().delete_collection(*args, **kwargs)


@pytest.mark.parametrize("layout", [(6, 3), (None, 1), ("missing", "6")])
def test_clone_copies_any_source_topology_exactly_through_the_api(layout):
    """Issue #360: staging preparation copies points (ids, named vectors,
    payloads) through scroll/upsert, so distributed sources are copied
    shard-complete; no node-local snapshot is taken and live is unchanged."""
    from mainframe_rag.ingest.qdrant_io import clone_collection

    fake = _RecordingTopologyFake(*layout)
    live = _seed_live_pair(fake)
    before = _point_records(fake, live)
    clone_collection(fake, _settings(), live, "mainframe_manuals__genNEW")
    assert _point_records(fake, "mainframe_manuals__genNEW") == before
    assert _point_records(fake, live) == before and fake.aliases[ALIAS] == live
    assert fake.snapshots == {} and "snapshot" not in fake.mutations and "delete" not in fake.mutations


def test_clone_creates_staging_with_the_selected_distribution_policy():
    from mainframe_rag.ingest.qdrant_io import clone_collection

    fake = _RecordingTopologyFake(1, 1)
    live = _seed_live_pair(fake)
    settings = _settings(
        qdrant_shard_number=6, qdrant_replication_factor=3, qdrant_write_consistency_factor=2
    )
    clone_collection(fake, settings, live, "mainframe_manuals__genNEW")
    created = fake.created["mainframe_manuals__genNEW"]
    assert (created["shard_number"], created["replication_factor"], created["write_consistency_factor"]) == (6, 3, 2)
    assert created["vectors_config"]["dense"].size == 256


@pytest.mark.parametrize("case", ["unreadable-source", "existing-target"])
def test_clone_refuses_before_any_write(case):
    from mainframe_rag.ingest.qdrant_io import clone_collection

    class _Unreadable(_RecordingTopologyFake):
        def get_collection(self, name):
            raise ConnectionError("peer down")

    fake = _Unreadable() if case == "unreadable-source" else _RecordingTopologyFake(1, 1)
    live = _seed_live_pair(fake)
    if case == "existing-target":
        fake.collections["mainframe_manuals__genNEW"] = []
    with pytest.raises(
        (ConnectionError, RuntimeError), match="peer down" if case == "unreadable-source" else "already exists"
    ):
        clone_collection(fake, _settings(), live, "mainframe_manuals__genNEW")
    assert fake.mutations == []
    if case == "unreadable-source":
        assert "mainframe_manuals__genNEW" not in fake.collections


def test_clone_refuses_a_partial_copy():
    """A copy that lost a point must never become a publish base."""
    from mainframe_rag.ingest.qdrant_io import clone_collection

    class _Lossy(_RecordingTopologyFake):
        def upsert(self, collection, *, points, wait=True):
            if collection == "mainframe_manuals__genNEW":
                points = points[1:]
            return super().upsert(collection, points=points, wait=wait)

    fake = _Lossy(6, 3)
    live = _seed_live_pair(fake)
    with pytest.raises(RuntimeError, match="unverified: 0 of 1 point ids"):
        clone_collection(fake, _settings(), live, "mainframe_manuals__genNEW")
    assert _point_records(fake, live) and fake.aliases[ALIAS] == live


@pytest.mark.parametrize(
    ("field", "value"),
    [("shard_number", v) for v in (None, 0, "missing")]
    + [("replication_factor", None)],
)
def test_unknown_topology_legacy_migration_never_deletes_the_squatter(tmp_path, monkeypatch, field, value):
    """A legacy physical squatting on the alias name is never snapshotted or
    deleted while its topology is unknown: the run refuses (here at the
    commit proof, since the squatter holds an unwalked point; the walked
    case is test_legacy_migration_refuses_distributed_squatter_before_any_delete)
    with the squatter's points, and the absent alias, unchanged."""
    from mainframe_rag.ingest import run_ingest

    class UnknownTopology(_RecordingTopologyFake):
        def get_collection(self, name):
            info = super().get_collection(name)
            if value == "missing":
                delattr(info.config.params, field)
            else:
                setattr(info.config.params, field, value)
            return info

    fake = UnknownTopology()
    live = _seed_live_pair(fake, live=ALIAS)
    fake.aliases.clear()
    before = _point_records(fake, live)
    _publish_env(monkeypatch)
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    with pytest.raises(RuntimeError):
        _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl", "--reingest")
    assert "delete" not in fake.mutations and fake.snapshots == {}
    assert _point_records(fake, live) == before and ALIAS not in fake.aliases







@pytest.fixture(scope="module", autouse=True)
def _publication_typecheck(request, tmp_path_factory):
    """Start the strict mypy run for `test_publication_checks_are_read_only_by_type`
    when the module starts, so its ~7 s cold start overlaps the other tests
    instead of being waited on serially. Same command, same inputs; the test
    below collects the result. Nothing starts unless that test is selected."""
    if not any(item.name == "test_publication_checks_are_read_only_by_type" for item in request.session.items):
        yield None
        return
    import subprocess
    import sys

    tmp_path = tmp_path_factory.mktemp("publication_typecheck")
    command = [sys.executable, "-m", "mypy", "--strict", "--follow-imports=silent", "--no-incremental"]
    imports = """
import qdrant_client

from mainframe_rag.config import Settings
from mainframe_rag.ingest.publish import (
    audit_unmarked_residue,
    ensure_staging,
    resolve_publish_staging,
    verify_staging_distribution,
)
from mainframe_rag.ingest.qdrant_io import delete_by_revision, swap_alias_to
from mainframe_rag.ingest.representation import check_ingest_compatible
from mainframe_rag.ingest.run_ingest import (
    _certify_live,
    _placement_clients,
    _recertify_live,
    _resolve_publication,
)
from mainframe_rag.ports import QdrantReader
from pathlib import Path
"""
    good = tmp_path / "good.py"
    good.write_text(imports + """

def checks(r: QdrantReader, real: qdrant_client.QdrantClient, s: Settings) -> None:
    verify_staging_distribution(r, s)
    audit_unmarked_residue(r, s, [], {}, "rules")
    check_ingest_compatible(r, s, "c__completions", "rules")
    resolve_publish_staging(real, s, gen_fp="g", corpus_fp="c", live=None,
                            force_reingest=False, state=None)
    res = _resolve_publication(r, Path("src"), Path("progress"), s, vendor=None, product=None,
                               version=None, force_reingest=False, prewalked=[],
                               retire_docs=None, rules_v="rules")
    _certify_live(res, r, Path("progress"), s, s, prewalked=[], rules_v="rules")
""")

    bad = tmp_path / "bad.py"
    bad.write_text(imports + """

def writes(r: QdrantReader, s: Settings) -> None:
    ensure_staging(r, s, s, None)
    delete_by_revision(r, s, "rev")
    swap_alias_to(r, s, "staging", None)
    res = _resolve_publication(r, Path("src"), Path("progress"), s, vendor=None, product=None,
                               version=None, force_reingest=False, prewalked=[],
                               retire_docs=None, rules_v="rules")
    _recertify_live(res, r, Path("progress"), s, prewalked=[], rules_v="rules")
    with _placement_clients(s, r) as placement:
        if placement is not None:
            for peer in placement.values():
                peer.delete_collection("staging")
""")
    process = subprocess.Popen([*command, str(good), str(bad)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        yield process, good
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_publication_checks_are_read_only_by_type(_publication_typecheck):
    """Issue #369: publication's resolution and verification helpers take
    the read-only `QdrantReader`, so the checker used by qa:typecheck proves
    they cannot write (transitively: a reader can only reach other reader
    helpers), while every helper that mutates storage requires the writer
    protocol and rejects a reader. The publish decision step and the
    re-certification checks are readers; re-certification's receipt
    backfill is its one write and needs the writer."""
    import subprocess

    process, good = _publication_typecheck
    stdout, stderr = process.communicate()
    rejected = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
    assert rejected.returncode == 1, rejected.stdout + rejected.stderr
    assert str(good) not in rejected.stdout + rejected.stderr, rejected.stdout + rejected.stderr
    for name in ("ensure_staging", "delete_by_revision", "swap_alias_to"):
        assert (f'Argument 1 to "{name}" has incompatible type "QdrantReader"; '
                'expected "QdrantPoints"') in rejected.stdout
    assert ('Argument 2 to "_recertify_live" has incompatible type "QdrantReader"; '
            'expected "QdrantPoints"') in rejected.stdout
    # The placement map (review of #699): the explicit 1/1/1 profile yields
    # the publisher's own client, so its handles must be readers too.
    assert '"QdrantReader" has no attribute "delete_collection"' in rejected.stdout
    assert "Found 5 errors" in rejected.stdout


def test_sealed_build_cannot_serve_until_published():
    """A sealed (unpublished) build refuses to serve; published and retained
    builds pass the same check."""
    from mainframe_rag.ingest.build import BuildBinding, build_aliases, require_published_binding

    build_id = "0b0d3b0e-5d4e-4a43-9a40-3f1d2f6a7c11"
    data, control = build_aliases("corpus", build_id)
    binding = BuildBinding(build_id, "corpus", data, "gen-fp", "corpus-fp")
    with pytest.raises(ValueError, match="unpublished build cannot serve"):
        require_published_binding(binding, "corpus", data, {})
    retained = {data: data, control: data + "__completions"}
    require_published_binding(binding, "corpus", data, retained)
    require_published_binding(binding, "corpus", data, {**retained, "corpus": data})


# ------------------------------------------------ source catalog (#827)
def _marker(physical, doc_id, sha, rev, expected, *, target=None):
    from mainframe_rag.ingest.completion import CompletionRecord

    return CompletionRecord(
        doc_id=doc_id, sha256=sha, rules_v="rules", target_collection=target or physical,
        generation_id=f"{sha}|gen|", expected_chunks=expected, chunk_ids_digest="c", content_digest="d",
        manifest_digest="m", source_rev=rev,
    ).model_dump()


def _chunk(i, rev, doc_id, sha, title, product, version, start, end, label):
    return SimpleNamespace(id=f"00000000-0000-4000-8000-{i:012d}", vector=None, payload={
        "source_rev": rev, "doc_id": doc_id, "sha256": sha, "title": title, "product": product,
        "version": version, "page_start": start, "page_end": end, "page_label": label,
        "text": f"SENTINEL manual text {i}", "heading_path": "SENTINEL heading",
    })


class _ProjectionFake(PublishFake):
    """Records each scroll's payload projection, and projects like the server."""

    def __init__(self):
        super().__init__()
        self.projections: list[tuple[str, object]] = []

    def scroll(self, collection, *, scroll_filter=None, limit=10, with_payload=None, with_vectors=False,
               offset=None):
        self.projections.append((collection, with_payload))
        page, nxt = super().scroll(collection, scroll_filter=scroll_filter, limit=limit,
                                   with_payload=with_payload, offset=offset)
        if isinstance(with_payload, list):
            page = [SimpleNamespace(id=p.id, vector=None,
                                    payload={k: v for k, v in (p.payload or {}).items() if k in with_payload})
                    for p in page]
        return page, nxt


def _catalog_store():
    """Three published revisions (two share a doc_id), an inherited ancestor
    marker and the manifest point; independent literal page facts."""
    phys = "corpus__gen_catalog"
    sha_a, sha_b, sha_c = "a" * 64, "b" * 64, "c" * 64
    r1, r2, r3 = f"ibm|z/os|2.5|{sha_a}", f"ibm|z/os|3.1|{sha_b}", f"||4.5|{sha_c}"
    fake = _ProjectionFake()
    fake.collections[phys] = [
        _chunk(1, r1, "SC27-1", sha_a, "Guide", "z/OS", "2.5", 0, 0, "i"),
        _chunk(2, r1, "SC27-1", sha_a, "Guide", "z/OS", "2.5", 1, 2, "ii–1"),
        _chunk(3, r1, "SC27-1", sha_a, "Guide", "z/OS", "2.5", 5, 5, "4"),
        _chunk(4, r2, "SC27-1", sha_b, "Guide", "z/OS", "3.1", 3, 3, ""),
        _chunk(5, r2, "SC27-1", sha_b, "Guide", "z/OS", "3.1", 3, 4, ""),
        _chunk(6, r3, "GC31-2", sha_c, "Codes", None, "4.5", 10, 12, "A-1–A-3"),
    ]
    fake.collections[phys + "__completions"] = [
        SimpleNamespace(id="m1", vector=None, payload=_marker(phys, "SC27-1", sha_a, r1, 3)),
        SimpleNamespace(id="m2", vector=None, payload=_marker(phys, "SC27-1", sha_b, r2, 2)),
        SimpleNamespace(id="m3", vector=None, payload=_marker(phys, "GC31-2", sha_c, r3, 1)),
        SimpleNamespace(id="m4", vector=None, payload=_marker(phys, "OLD-1", "d" * 64, "x|y|z|" + "d" * 64, 9,
                                                              target="corpus__gen_ancestor")),
        SimpleNamespace(id="man", vector=None, payload={"record_type": "representation-manifest"}),
    ]
    expected = [
        {"source_revision": r1, "source_sha256": sha_a, "doc_id": "SC27-1", "title": "Guide", "product": "z/OS",
         "version": "2.5", "chunk_count": 3, "pages": {"first": 0, "last": 5},
         "page_labels": {"first": "i", "last": "4"}, "indexed_pages": 4},
        {"source_revision": r2, "source_sha256": sha_b, "doc_id": "SC27-1", "title": "Guide", "product": "z/OS",
         "version": "3.1", "chunk_count": 2, "pages": {"first": 3, "last": 4},
         "page_labels": {"first": None, "last": None}, "indexed_pages": 2},
        {"source_revision": r3, "source_sha256": sha_c, "doc_id": "GC31-2", "title": "Codes", "product": None,
         "version": "4.5", "chunk_count": 1, "pages": {"first": 10, "last": 12},
         "page_labels": {"first": "A-1", "last": "A-3"}, "indexed_pages": 3},
    ]
    return fake, phys, expected


def test_source_catalog_entries_are_exact_and_read_labels_and_pages_only():
    """Issue #827: one entry per revision bound to this build (an inherited
    ancestor marker is not a source), with the physical extent, the printed
    labels at its two ends (None where no chunk locates them honestly) and
    the pages that carry text (gaps visible). The data scan projects only
    identity and page fields: manual text never enters the catalog."""
    from mainframe_rag.ingest.catalog import DATA_FIELDS, build_source_catalog

    fake, phys, expected = _catalog_store()
    entries = build_source_catalog(fake, phys)
    assert entries == expected
    data_scans = [proj for coll, proj in fake.projections if coll == phys]
    assert data_scans and all(proj == list(DATA_FIELDS) for proj in data_scans)
    assert "SENTINEL" not in repr(entries)


@pytest.mark.parametrize("damage", [
    "marker-without-data", "data-without-marker", "count", "doc-id", "label-disagrees",
    "page-span", "duplicate-marker", "no-markers", "sha",
])
def test_source_catalog_refuses_a_build_it_cannot_account_for(damage):
    from mainframe_rag.ingest.catalog import SourceCatalogError, build_source_catalog

    fake, phys, _ = _catalog_store()
    data, control = fake.collections[phys], fake.collections[phys + "__completions"]
    if damage == "marker-without-data":
        fake.collections[phys] = [p for p in data if p.payload["doc_id"] != "GC31-2"]
    elif damage == "data-without-marker":
        fake.collections[phys + "__completions"] = [p for p in control if p.id != "m3"]
    elif damage == "count":
        control[0].payload["expected_chunks"] = 4
    elif damage == "doc-id":
        data[0].payload["doc_id"] = "SC27-9"
    elif damage == "label-disagrees":
        data[1].payload["version"] = "2.4"
    elif damage == "page-span":
        data[2].payload["page_end"] = 4
    elif damage == "duplicate-marker":
        control.append(SimpleNamespace(id="m1b", vector=None, payload=dict(control[0].payload)))
    elif damage == "no-markers":
        fake.collections[phys + "__completions"] = control[3:]
    elif damage == "sha":
        control[2].payload["sha256"] = "e" * 64
    with pytest.raises(SourceCatalogError):
        build_source_catalog(fake, phys)


def test_receipt_schemas_bind_the_source_catalog_summary():
    """Schema 3 requires an exact catalog summary; schema 1 and 2 receipts
    still decode (their builds serve without a catalog) and cannot carry one."""
    from mainframe_rag.ingest.build import decode_build_binding
    from mainframe_rag.ingest.catalog import catalog_summary

    control = "corpus__gen_x__completions"
    seal = {"schema": 1, "data": {"count": 1, "sha256": "a" * 64}, "control": {"count": 2, "sha256": "b" * 64}}
    base = {"record_type": "publication-metadata", "target_collection": control, "build_schema": 3,
            "build_id": "12345678-1234-4234-8234-123456789abc", "logical_alias": "corpus",
            "data_collection": "corpus__gen_x", "gen_fp": "g", "corpus_fp": "c", "content_seal": seal}
    summary = catalog_summary(_catalog_store()[2])
    assert decode_build_binding({**base, "source_catalog": summary}, control).physical == "corpus__gen_x"
    for bad in (None, {**summary, "count": 0}, {**summary, "sha256": "x"}, {**summary, "schema": 2},
                {**summary, "extra": 1}):
        payload = {**base, "source_catalog": bad} if bad is not None else dict(base)
        with pytest.raises(ValueError):
            decode_build_binding(payload, control)
    assert decode_build_binding({**base, "build_schema": 2}, control).build_id == base["build_id"]
    with pytest.raises(ValueError, match="schema 3"):
        decode_build_binding({**base, "build_schema": 2, "source_catalog": summary}, control)


def _catalog_entries(fake, physical):
    from mainframe_rag.ingest.catalog import read_source_catalog

    return read_source_catalog(fake, physical + "__completions")


def test_publication_seals_its_source_catalog_and_the_next_runs_keep_it_exact(tmp_path, monkeypatch):
    """Publication writes one entry per source before the receipt, binds
    the summary in a schema-3 receipt, and the content seal covers the
    entries. An ordinary rerun re-certifies read-only; a tampered entry is
    refused; a changed corpus replaces the inherited entries and a retired
    document disappears from the next build's catalog."""
    from copy import deepcopy

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.catalog import (
        CATALOG_RECORD_TYPE,
        build_source_catalog,
        catalog_summary,
    )
    from mainframe_rag.ingest.completion import stale_completion_markers
    from mainframe_rag.ingest.publish import read_publication_record

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inventory.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    first = fake.aliases[ALIAS]
    entries = _catalog_entries(fake, first)
    assert [e["doc_id"] for e in entries] == [DOC_A]
    assert entries == build_source_catalog(fake, first)
    markers = [p.payload for p in fake.collections[first + "__completions"]
               if p.payload.get("target_collection") == first and "record_type" not in p.payload]
    assert entries[0]["chunk_count"] == markers[0]["expected_chunks"]
    assert entries[0]["source_revision"] == markers[0]["source_rev"]
    receipt = read_publication_record(fake, first + "__completions")
    assert receipt["build_schema"] == 3 and receipt["source_catalog"] == catalog_summary(entries)
    # Entries never look like completion markers to the control readers.
    entry_points = [p for p in fake.collections[first + "__completions"]
                    if p.payload.get("record_type") == CATALOG_RECORD_TYPE]
    assert len(entry_points) == 1 and "doc_id" not in entry_points[0].payload
    settings = _settings(qdrant_collection=first)
    assert stale_completion_markers(fake, settings, receipt["gen_fp"])[0] == 1  # only the real marker

    before = deepcopy(fake.collections), dict(fake.aliases)
    assert _run_main(monkeypatch, corpus, progress) == 0
    assert (fake.collections, fake.aliases) == before

    entry_points[0].payload["entry"]["title"] = "tampered"
    with pytest.raises(RuntimeError):
        _run_main(monkeypatch, corpus, progress)
    entry_points[0].payload["entry"] = deepcopy(entries[0])

    _build_doc_with_id(corpus, "doc_b", DOC_B)
    assert _run_main(monkeypatch, corpus, progress) == 0
    second = fake.aliases[ALIAS]
    assert second != first
    assert sorted(e["doc_id"] for e in _catalog_entries(fake, second)) == [DOC_A, DOC_B]
    assert _catalog_entries(fake, first) == entries  # the retained build is untouched

    (corpus / "doc_a.pdf").unlink()
    assert _run_main(monkeypatch, corpus, progress, "--retire-doc", DOC_A) == 0
    third = fake.aliases[ALIAS]
    assert [e["doc_id"] for e in _catalog_entries(fake, third)] == [DOC_B]


@pytest.mark.parametrize("damage", ["lost-entry", "altered-entry"])
def test_a_catalog_that_does_not_read_back_is_never_sealed(tmp_path, monkeypatch, damage):
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.catalog import CATALOG_RECORD_TYPE

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    upsert = fake.upsert

    def damaged(collection, *, points, wait=True):
        if any((p.payload or {}).get("record_type") == CATALOG_RECORD_TYPE for p in points):
            if damage == "lost-entry":
                return SimpleNamespace()
            points[0].payload["entry"]["indexed_pages"] += 1
        return upsert(collection, points=points, wait=wait)

    monkeypatch.setattr(fake, "upsert", damaged)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    with pytest.raises(RuntimeError, match="source catalog"):
        _run_main(monkeypatch, corpus, tmp_path / "inventory.jsonl")
    assert not fake.aliases and not fake.alias_calls
    # Checked before the receipt, so the damage was never sealed: an
    # ordinary retry rewrites the entries and publishes them exactly.
    monkeypatch.setattr(fake, "upsert", upsert)
    assert _run_main(monkeypatch, corpus, tmp_path / "inventory.jsonl") == 0
    published = fake.aliases[ALIAS]
    from mainframe_rag.ingest.catalog import build_source_catalog

    assert _catalog_entries(fake, published) == build_source_catalog(fake, published)


def test_interrupted_catalog_publication_resumes_with_identical_entries(tmp_path, monkeypatch):
    """A crash after the entries are written and before the receipt leaves
    the alias untouched; the retry rebuilds byte-identical entries and
    publishes. A sealed schema-2 candidate (an older release's interrupted
    build) has no catalog and is refused as old-format, without mutation."""
    import json
    from copy import deepcopy

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import publish_state_path

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inventory.jsonl"
    with monkeypatch.context() as patch:
        def crash(*args, **kwargs):
            raise RuntimeError("injected crash before the receipt")
        patch.setattr(run_ingest, "write_publication_metadata", crash)
        with pytest.raises(RuntimeError, match="injected crash"):
            _run_main(monkeypatch, corpus, progress)
    staging = json.loads(publish_state_path(progress, ALIAS).read_text())["staging"]
    written = _catalog_entries(fake, staging)
    assert written and not fake.aliases
    assert _run_main(monkeypatch, corpus, progress) == 0
    assert fake.aliases[ALIAS] == staging
    assert _catalog_entries(fake, staging) == written

    fake = PublishFake()  # a separate store: an older release's interrupted, sealed build
    other = tmp_path / "other"
    other.mkdir()
    _build_doc_with_id(other, "doc_b", DOC_B)
    progress_b = tmp_path / "inventory_b.jsonl"
    fake.fail_swap = "raise"
    with pytest.raises(RuntimeError, match="injected swap failure"):
        _run_main(monkeypatch, other, progress_b)
    sidecar = publish_state_path(progress_b, ALIAS)
    control = json.loads(sidecar.read_text())["staging"] + "__completions"
    receipt = next(p for p in fake.collections[control] if p.payload.get("record_type") == "publication-metadata")
    receipt.payload["build_schema"] = 2
    del receipt.payload["source_catalog"]
    before = deepcopy(fake.collections), dict(fake.aliases), sidecar.read_bytes()
    fake.fail_swap = None
    with pytest.raises(RuntimeError, match="old-format"):
        _run_main(monkeypatch, other, progress_b)
    assert (fake.collections, fake.aliases, sidecar.read_bytes()) == before
