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
                params=SimpleNamespace(vectors={"dense": SimpleNamespace(size=self.dim)})
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

    def scroll(self, collection, *, scroll_filter=None, limit=10, with_payload=None, offset=None):
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
def test_staging_name_shape_and_determinism():
    name = staging_name_for(ALIAS, "a" * 16, "b" * 12)
    assert re.fullmatch(r"mainframe_manuals__gen[0-9a-f]{28}", name)
    assert staging_name_for(ALIAS, "a" * 16, "b" * 12) == name
    assert staging_name_for(ALIAS, "c" * 16, "b" * 12) != name


def test_corpus_fingerprint_order_invariant_and_sensitive():
    entries = [("b.pdf", "1" * 64), ("a.pdf", "2" * 64)]
    assert corpus_fingerprint(entries) == corpus_fingerprint(list(reversed(entries)))
    assert corpus_fingerprint(entries) != corpus_fingerprint([("a.pdf", "2" * 64)])
    assert re.fullmatch(r"[0-9a-f]{12}", corpus_fingerprint(entries))


def test_generation_fingerprint_sensitive_to_inputs():
    from mainframe_rag.ingest.rules_version import extraction_rules_version

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
def test_first_publish_creates_alias_and_serves(tmp_path, monkeypatch):
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    assert _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl") == 0

    gens = _gen_collections(fake)
    assert len(gens) == 1, f"one staging generation expected, got {gens}"
    assert fake.aliases.get(ALIAS) == gens[0]
    assert gens[0] != ALIAS
    points = fake.alias_target_points(ALIAS)
    assert points, "alias must serve the published generation"
    assert {p.payload["doc_id"] for p in points} == {"SA22-0000-00"}
    assert f"{gens[0]}__completions" in fake.collections


def test_clean_rerun_is_noop(tmp_path, monkeypatch):
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    target = fake.aliases[ALIAS]
    n_collections = len(fake.collections)
    assert _run_main(monkeypatch, corpus, progress) == 0
    assert fake.aliases[ALIAS] == target, "steady state must not flip the alias"
    assert len(fake.collections) == n_collections, "no new staging on a clean rerun"


def test_refresh_publishes_new_generation_and_keeps_old(tmp_path, monkeypatch):
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
    from mainframe_rag.ingest.representation import read_manifest

    assert read_manifest(fake, f"{gen2}__completions") is not None


def _staging_settings(collection: str) -> Settings:
    return _settings(qdrant_collection=collection)


def test_staging_metadata_repair_after_interrupted_metadata_clone(tmp_path, monkeypatch):
    """Issue #391 F5: a crash after the data clone but before the completions
    clone leaves staging data with no readable metadata. Reuse must repair
    (re-transfer live's contract), not publish from incomplete preparation."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import ensure_staging
    from mainframe_rag.ingest.representation import read_manifest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    assert _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl") == 0
    live = fake.aliases[ALIAS]
    live_points = list(fake.collections[live])
    live_completions = list(fake.collections[f"{live}__completions"])

    staging = "mainframe_manuals__genDEADBEEF0123456789ab"
    fake.collections[staging] = list(live_points)  # data clone got through

    assert ensure_staging(fake, _settings(), _staging_settings(staging), live) == "repaired"
    assert read_manifest(fake, f"{staging}__completions") is not None
    assert fake.aliases[ALIAS] == live, "repair never moves the alias"
    assert fake.collections[live] == live_points, "live data untouched"
    assert fake.collections[f"{live}__completions"] == live_completions, "live metadata untouched"


def test_staging_metadata_repair_after_failed_rekey(tmp_path, monkeypatch):
    """Metadata was cloned but the re-key never ran: the copied manifest
    point still carries the live id, so staging reads no manifest. Reuse
    re-keys verbatim from live instead of reading inherited state as legacy."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import ensure_staging
    from mainframe_rag.ingest.representation import read_manifest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    assert _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl") == 0
    live = fake.aliases[ALIAS]
    contract = read_manifest(fake, f"{live}__completions")
    assert contract is not None

    staging = "mainframe_manuals__genFEDCBA9876543210abcd"
    fake.collections[staging] = list(fake.collections[live])
    fake.collections[f"{staging}__completions"] = list(fake.collections[f"{live}__completions"])

    assert ensure_staging(fake, _settings(), _staging_settings(staging), live) == "repaired"
    assert read_manifest(fake, f"{staging}__completions") == contract
    assert fake.aliases[ALIAS] == live


def test_incomplete_metadata_transfer_never_publishes(tmp_path, monkeypatch):
    """A source that cannot yield its manifest vector must fail preparation
    loudly: no silent 'reused', live untouched, no publish path."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import ensure_staging

    class _NoVectorStore(PublishFake):
        def retrieve(self, collection, ids, *, with_payload=True, with_vectors=False):
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

    staging = "mainframe_manuals__gen00112233445566778899"
    with pytest.raises(RuntimeError, match="staging metadata transfer failed"):
        ensure_staging(fake, _settings(), _staging_settings(staging), live)
    assert fake.aliases[ALIAS] == live
    assert fake.collections[live] == live_points
    assert fake.collections[f"{live}__completions"] == live_completions


def test_staging_invisible_until_swap(tmp_path, monkeypatch):
    """Reads through the alias during staging see the old generation only."""
    from qdrant_client import models

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


def test_legacy_migration_preserves_then_clears(tmp_path, monkeypatch):
    """Pre-alias physical layout: snapshot it, converge staging, delete the
    squatter, create the alias. Stale rules without --reingest fail closed."""
    from qdrant_client import models

    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    progress = tmp_path / "inv.jsonl"
    fake.collections[ALIAS] = [
        models.PointStruct(
            id="00000000-0000-0000-0000-000000000001",
            vector={"dense": [0.0] * 256, "bm25": models.SparseVector(indices=[0], values=[1.0])},
            payload={
                "doc_id": "SA22-0000-00",
                "sha256": "0" * 64,
                "rules_v": "0" * 16,
                "text": "stale",
            },
        )
    ]
    with pytest.raises(RuntimeError, match="extraction-rules mismatch"):
        _run_main(monkeypatch, corpus, progress)
    assert ALIAS not in fake.aliases, "failed migration creates no alias"

    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    assert ALIAS in fake.aliases
    gen = fake.aliases[ALIAS]
    assert gen != ALIAS and ALIAS not in fake.collections, "squatter cleared after safety snapshot"
    assert fake.snapshots.get(ALIAS), "legacy generation preserved as a snapshot"
    assert {p.payload["doc_id"] for p in fake.alias_target_points(ALIAS)} == {"SA22-0000-00"}


def test_publish_refuses_limit_and_empty_corpus(tmp_path, monkeypatch):
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    with pytest.raises(RuntimeError, match="refuses --limit"):
        _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl", "--limit", "1")
    assert fake.aliases == {}
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(RuntimeError, match="empty corpus"):
        _run_main(monkeypatch, empty, tmp_path / "inv2.jsonl")
    assert fake.aliases == {}


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
    problems = verify_all_complete(
        fake,
        staging,
        [
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
    assert sorted(problems) == ["err.pdf", "ghost.pdf", "legacy.pdf", "missing.pdf", "stale.pdf"]


def test_verify_all_complete_refuses_pending_contract(monkeypatch):
    """Issue #391 F2: a pending migration contract blocks the swap even when
    every walked document verifies — publication certifies a committed
    generation, never an unfinished re-embed."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.representation import (
        STATE_COMMITTED,
        STATE_PENDING,
        read_manifest_record,
        write_manifest,
    )
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)
    staging = _settings(qdrant_collection="stg-pending")
    rules_v = extraction_rules_version()
    write_manifest(fake, "stg-pending__completions", staging, rules_v, state=STATE_PENDING)
    problems = verify_all_complete(fake, staging, [], {}, rules_v, "||")
    assert problems == ["stg-pending: contract 'pending'"]
    write_manifest(fake, "stg-pending__completions", staging, rules_v, state=STATE_COMMITTED)
    assert verify_all_complete(fake, staging, [], {}, rules_v, "||") == []
    record = read_manifest_record(fake, "stg-pending__completions")
    assert record.state == STATE_COMMITTED


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


def test_rollback_repoint(tmp_path, monkeypatch):
    """Operator rollback: re-point the alias at the kept previous generation."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.qdrant_io import swap_alias_to

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
    assert fake.aliases[ALIAS] != gen1

    swap_alias_to(fake, _settings(), gen1, fake.aliases[ALIAS])
    assert fake.aliases[ALIAS] == gen1
    assert {p.payload["doc_id"] for p in fake.alias_target_points(ALIAS)} == {"SA22-0000-00"}


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


def test_flag_off_creates_no_alias_or_generations(tmp_path, monkeypatch):
    """Default-off pin: the legacy in-place path creates no alias, no
    staging generations, no snapshots."""
    from mainframe_rag.ingest import run_ingest

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    monkeypatch.delenv("INGEST_ALIAS_PUBLISH", raising=False)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    assert _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl") == 0
    assert fake.aliases == {}
    assert all("__gen" not in name for name in fake.collections)
    assert fake.snapshots == {}


def _live_manifest_revision(fake, live):
    from mainframe_rag.ingest.representation import read_manifest

    return read_manifest(fake, f"{live}__completions")


def test_steady_live_with_drifted_revision_fails_closed(tmp_path, monkeypatch):
    """Issue #391 F2: a revision-only change derives a DIFFERENT staging
    generation, so the unforced run clones live and fails in the inner
    preflight (drift, `--reingest` remediation). Alias, live points, and
    live metadata stay untouched — a failed migration leaves A serving."""
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live = fake.aliases[ALIAS]
    points_before = list(fake.collections[live])
    assert _live_manifest_revision(fake, live).embed_model_revision == ""

    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-2")
    with pytest.raises(RuntimeError, match="representation drift on embed_model_revision"):
        _run_main(monkeypatch, corpus, progress)
    assert fake.aliases[ALIAS] == live, "failed run moves no alias"
    assert fake.collections[live] == points_before, "failed run touches no live point"
    assert _live_manifest_revision(fake, live).embed_model_revision == "", (
        "failed run commits no manifest"
    )


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
        if kwargs.get("_publish_target") is None:
            return real_inner(*args, **kwargs)
        observed["alias_target"] = fake.aliases[ALIAS]
        observed["live_points"] = [p.id for p in fake.collections[live]]
        observed["reader"] = asyncio.run(
            resolve_serving_generation(
                fake, _settings(qdrant_collection=ALIAS), extraction_rules_version()
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


def test_forced_repair_partial_corpus_refuses_and_preserves_live(tmp_path, monkeypatch):
    """A repair build must certify the same complete corpus as any publish:
    a removed file without an approved removal blocks the swap, the old
    generation keeps serving, and its points are untouched."""
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _, b_path = _two_doc_corpus(corpus)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live = fake.aliases[ALIAS]
    points_before = [(p.id, dict(p.payload or {})) for p in fake.collections[live]]

    b_path.rename(tmp_path / "doc_b.pdf.held")
    with pytest.raises(RuntimeError, match="unmarked residue"):
        _run_main(monkeypatch, corpus, progress, "--reingest")
    assert fake.aliases[ALIAS] == live
    assert [(p.id, dict(p.payload or {})) for p in fake.collections[live]] == points_before


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
        if kwargs.get("_publish_target") is None:
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


def test_forced_repair_swap_then_crash_finalizes_read_only(tmp_path, monkeypatch):
    """A crash between the repair swap and sidecar cleanup resumes through
    the read-only steady-state path: no second repair generation, no
    mutation of the generation already serving."""
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0

    real_clear = run_ingest.clear_publish_state
    monkeypatch.setattr(run_ingest, "clear_publish_state", lambda *a, **k: False)
    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    repaired = fake.aliases[ALIAS]
    gens_before = _gen_collections(fake)
    points_after = [(p.id, dict(p.payload or {})) for p in fake.collections[repaired]]

    monkeypatch.setattr(run_ingest, "clear_publish_state", real_clear)
    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    assert fake.aliases[ALIAS] == repaired
    assert _gen_collections(fake) == gens_before, "finalize allocates nothing"
    assert [(p.id, dict(p.payload or {})) for p in fake.collections[repaired]] == points_after


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


def test_subsequent_run_changed_revision_after_repair_derives_new_generation(tmp_path, monkeypatch):
    """Issue #391 Q418-R1 counterexample 2: after a forced repair, changing the
    model revision changes gen_fp so publication metadata in live no longer
    matches; an ordinary run allocates a new staging generation instead of
    re-verifying live as steady state."""
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inv.jsonl"

    assert _run_main(monkeypatch, corpus, progress) == 0
    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    repaired = fake.aliases[ALIAS]

    # Change revision: gen_fp changes, so it must not be recognized as steady state
    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-new")
    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    after_rev = fake.aliases[ALIAS]
    assert after_rev != repaired


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
        write_publication_metadata,
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
    write_publication_metadata(fake, live_completions, settings, gen_fp="gen1", corpus_fp="corp1")
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
    write_publication_metadata(
        fake, staging1_completions, settings, gen_fp="gen2", corpus_fp="corp2"
    )
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
    write_publication_metadata(
        fake, staging2_completions, settings, gen_fp="gen3", corpus_fp="corp3"
    )
    stg2_receipts_after_pub = [
        p
        for p in fake.collections[staging2_completions]
        if (p.payload or {}).get("record_type") == _PUBLICATION_METADATA_PREFIX
    ]
    assert len(stg2_receipts_after_pub) == 1
    assert str(stg2_receipts_after_pub[0].id) == publication_metadata_point_id(staging2_completions)


def test_publish_force_revision_change_migrates_to_new_generation(tmp_path, monkeypatch):
    """Issue #391 F2: a revision-only change must never reconverge the live
    physical. The versioned fingerprint embeds the operator revision, so
    --reingest derives a distinct staging generation, re-embeds into it,
    verifies, and swaps; the old generation keeps its points AND its rev-A
    manifest for metadata rollback, and the serving generation's contract is
    committed (never a half-rebuilt B)."""
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

    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-2")
    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    new = fake.aliases[ALIAS]
    assert new != old, "a representation change must publish a distinct generation"
    assert old in fake.collections, "old physical retained for rollback"
    assert _live_manifest_revision(fake, old).embed_model_revision == "", (
        "old generation keeps its own contract"
    )
    old_docs = {
        (p.payload or {}).get("doc_id")
        for p in fake.collections[old]
        if (p.payload or {}).get("doc_id")
    }
    assert old_docs == {"SA22-0000-00"}, "old generation keeps its points"
    assert _live_manifest_revision(fake, new).embed_model_revision == "rev-2"
    record = read_manifest_record(fake, f"{new}__completions")
    assert record is not None and record.state == STATE_COMMITTED
    assert {p.payload["doc_id"] for p in fake.alias_target_points(ALIAS)} == {"SA22-0000-00"}


def test_verify_all_complete_refuses_missing_manifest_on_populated_staging(monkeypatch):
    """Invariant D2: Populated staging without a manifest blocks publication;
    empty bootstrap (0 walked docs) remains permitted."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)
    staging = _settings(qdrant_collection="stg-nomanifest")
    rules_v = extraction_rules_version()

    # Case A: Populated target (walked is non-empty) -> missing manifest is a blocking problem
    problems = verify_all_complete(fake, staging, [("doc.pdf", "a" * 64)], {}, rules_v, "||")
    assert "stg-nomanifest: missing or unreadable metadata manifest" in problems

    # Case B: Empty bootstrap (walked is empty) -> missing manifest is permitted
    assert verify_all_complete(fake, staging, [], {}, rules_v, "||") == []


def test_verify_all_complete_refuses_corrupt_manifest_on_populated_staging(monkeypatch):
    """Invariant D2: Staging with unparseable/corrupt manifest payload blocks publication."""
    from types import SimpleNamespace

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.representation import manifest_point_id
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)
    staging = _settings(qdrant_collection="stg-corrupt")
    rules_v = extraction_rules_version()

    completions = "stg-corrupt__completions"
    fake.collections[completions] = [
        SimpleNamespace(
            id=manifest_point_id(completions),
            payload={
                "record_type": "manifest",
                "manifest": "not-a-valid-manifest-dict",
                "state": "committed",
            },
        )
    ]

    problems = verify_all_complete(fake, staging, [("doc.pdf", "a" * 64)], {}, rules_v, "||")
    assert "stg-corrupt: missing or unreadable metadata manifest" in problems


def test_verify_all_complete_refuses_representation_drift(monkeypatch):
    """Invariant D2: Committed manifest with drifted representation blocks publication."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.representation import STATE_COMMITTED, write_manifest
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)
    staging = _settings(qdrant_collection="stg-drift", embed_model="model-a")
    rules_v = extraction_rules_version()

    # Write manifest under model-b
    drift_settings = staging.model_copy(update={"embed_model": "model-b"})
    write_manifest(fake, "stg-drift__completions", drift_settings, rules_v, state=STATE_COMMITTED)

    problems = verify_all_complete(fake, staging, [("doc.pdf", "a" * 64)], {}, rules_v, "||")
    assert any("stg-drift: representation drift on embed_model" in p for p in problems)


def test_publish_missing_manifest_prevents_alias_cutover(tmp_path, monkeypatch):
    """Invariant D2 E2E: Full publication flow aborts and leaves alias untouched
    when the staging manifest is missing at publication gate."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.representation import manifest_point_id

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")

    # Hook commit_manifest to suppress writing the committed manifest point,
    # simulating a missing or dropped manifest at publication gate
    orig_commit = run_ingest.commit_manifest

    def _suppress_commit(client, completions_collection, settings, rules_v):
        orig_commit(client, completions_collection, settings, rules_v)
        # Remove manifest point
        comp_points = fake.collections.get(completions_collection, [])
        mp_id = manifest_point_id(completions_collection)
        fake.collections[completions_collection] = [p for p in comp_points if str(p.id) != mp_id]
        return ""

    monkeypatch.setattr(run_ingest, "commit_manifest", _suppress_commit)

    with pytest.raises(RuntimeError, match="missing or unreadable metadata manifest"):
        _run_main(monkeypatch, corpus, tmp_path / "inv.jsonl")

    assert fake.aliases == {}, "Alias cutover must be refused when metadata manifest is missing"


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
    approved removal refuses cutover; the approved doc is not silently
    retired and the live generation is untouched."""
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
    assert _live_doc_ids(fake) == {DOC_A, DOC_B}

    held = tmp_path / "doc_b.pdf.held"
    b_path.rename(held)
    with pytest.raises(RuntimeError, match="unmarked residue"):
        _run_main(monkeypatch, corpus, progress)
    assert fake.aliases[ALIAS] == live_before
    assert _live_doc_ids(fake) == {DOC_A, DOC_B}


def test_explicit_retire_doc_succeeds_with_rollback_history(tmp_path, monkeypatch):
    """R1 acceptance: an explicitly approved removal publishes; the
    superseded physical (with the retired doc) and its safety snapshot
    are retained for rollback."""
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

    b_path.rename(tmp_path / "doc_b.pdf.held")
    assert _run_main(monkeypatch, corpus, progress, "--retire-doc", DOC_B) == 0
    live_after = fake.aliases[ALIAS]
    assert live_after != live_before
    assert _live_doc_ids(fake) == {DOC_A}
    assert live_before in fake.collections
    assert {p.payload["doc_id"] for p in fake.collections[live_before]} == {DOC_A, DOC_B}
    assert fake.snapshots.get(live_before), "superseded generation keeps a safety snapshot"


def test_retire_unknown_doc_fails_closed(tmp_path, monkeypatch):
    """R1 acceptance: retiring a document with no approved history fails
    before any mutation (typo guard) — no staging, no alias move."""
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _two_doc_corpus(corpus)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    before_aliases = dict(fake.aliases)
    before_collections = set(fake.collections)

    with pytest.raises(RuntimeError, match="no approved history"):
        _run_main(monkeypatch, corpus, progress, "--retire-doc", "SA99-9999-99")
    assert dict(fake.aliases) == before_aliases
    assert set(fake.collections) == before_collections


def test_retire_walked_doc_fails_closed(tmp_path, monkeypatch):
    """R1 acceptance: a retirement contradicting the walked corpus (file
    still present) fails before any mutation."""
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _two_doc_corpus(corpus)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0

    with pytest.raises(RuntimeError, match="still present"):
        _run_main(monkeypatch, corpus, progress, "--retire-doc", DOC_A)
    assert _live_doc_ids(fake) == {DOC_A, DOC_B}


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
        if kwargs.get("_publish_target") is None:
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


def test_interrupted_forced_run_resumes_same_staging(tmp_path, monkeypatch):
    """R2 acceptance: an interrupted forced rebuild resumes its recorded
    unfinished staging instead of allocating another suffix; the previous
    live generation stays unchanged throughout."""
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
    live_before = fake.aliases[ALIAS]
    _build_doc_with_id(corpus, "doc_b", DOC_B)

    real_inner = run_ingest._run_impl

    def dying_inner(*args, **kwargs):
        if kwargs.get("_publish_target") is None:
            return real_inner(*args, **kwargs)
        raise RuntimeError("injected mid-build crash")

    monkeypatch.setattr(run_ingest, "_run_impl", dying_inner)
    with pytest.raises(RuntimeError, match="injected mid-build crash"):
        _run_main(monkeypatch, corpus, progress, "--reingest")
    assert fake.aliases[ALIAS] == live_before
    state_path = publish_state_path(progress, ALIAS)
    assert state_path.exists()
    import json

    staging = json.loads(state_path.read_text())["staging"]
    assert staging in fake.collections
    assert staging != live_before

    monkeypatch.setattr(run_ingest, "_run_impl", real_inner)
    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    assert fake.aliases[ALIAS] == staging
    assert not state_path.exists()
    data_gens = [
        name for name in fake.collections if "__gen" in name and not name.endswith("__completions")
    ]
    assert sorted(data_gens) == sorted([live_before, staging])
    assert _live_doc_ids(fake) == {DOC_A, DOC_B}


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


def test_inplace_run_uses_progress_lock_only(tmp_path, monkeypatch):
    """R2 acceptance: the in-place (non-publish) path keeps its progress
    lock and takes no target lock; explicit removals are rejected there."""
    from mainframe_rag.ingest import run_ingest

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.delenv("INGEST_ALIAS_PUBLISH", raising=False)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    assert (tmp_path / "inv.jsonl.lock").exists()
    assert list(tmp_path.glob("publish-*.lock")) == []
    assert list(tmp_path.glob("publish-*.json")) == []

    with pytest.raises(RuntimeError, match="INGEST_ALIAS_PUBLISH"):
        _run_main(monkeypatch, corpus, progress, "--retire-doc", DOC_A)


# ------------------------------------------------- Review round on #409 (issue #405)


def _write_committed_manifest(fake, collection):
    from mainframe_rag.ingest.representation import STATE_COMMITTED, write_manifest
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    staging = _settings(qdrant_collection=collection)
    write_manifest(
        fake,
        f"{collection}__completions",
        staging,
        extraction_rules_version(),
        state=STATE_COMMITTED,
    )


def test_suffixed_crash_retry_resumes_recorded_staging(monkeypatch):
    """B1: a crash after the sidecar records a suffixed allocation resumes
    that same staging on retry with identical inputs — no new collection,
    no strand."""
    from mainframe_rag.ingest.publish import (
        resolve_publish_staging,
        staging_name_for,
    )

    _publish_env(monkeypatch)
    fake = PublishFake()
    settings = _settings(qdrant_collection=ALIAS)
    gen_fp = "g" * 16
    corp_fp = "c" * 12
    base = staging_name_for(ALIAS, gen_fp, corp_fp)
    live = "live-gen"
    fake.collections[live] = []
    # Derived base is a retained published generation ...
    fake.collections[base] = [SimpleNamespace(id="p", payload={"doc_id": "X"})]
    _write_committed_manifest(fake, base)
    # ... so the first build allocated base_1, recorded it, then crashed
    # before cutover (the clone carries a committed manifest verbatim).
    recorded = f"{base}_1"
    fake.collections[recorded] = [SimpleNamespace(id="q", payload={"doc_id": "X"})]
    _write_committed_manifest(fake, recorded)
    state = {
        "version": 1,
        "alias": ALIAS,
        "staging": recorded,
        "gen_fp": gen_fp,
        "corpus_fp": corp_fp,
    }
    before = set(fake.collections)
    staging, resumed = resolve_publish_staging(
        fake,
        settings,
        gen_fp=gen_fp,
        corpus_fp=corp_fp,
        live=live,
        force_reingest=False,
        state=state,
    )
    assert (staging, resumed) == (recorded, True)
    assert set(fake.collections) == before, "resume allocates nothing"


def test_foreign_publish_state_fails_closed(monkeypatch):
    """A sidecar recording different inputs than the current run is never
    acted on — only the operator may abandon it."""
    from mainframe_rag.ingest.publish import resolve_publish_staging

    _publish_env(monkeypatch)
    fake = PublishFake()
    settings = _settings(qdrant_collection=ALIAS)
    with pytest.raises(RuntimeError, match="different inputs"):
        resolve_publish_staging(
            fake,
            settings,
            gen_fp="g" * 16,
            corpus_fp="c" * 12,
            live=None,
            force_reingest=False,
            state={"staging": "other", "gen_fp": "h" * 16, "corpus_fp": "c" * 12},
        )


def test_recorded_serving_generation_finalizes_read_only(tmp_path, monkeypatch):
    """SF3: a crash between swap and sidecar cleanup (suffixed staging now
    serving) finalizes through the read-only steady-state path on retry —
    no rebuild, no build into live, no new generation."""
    from mainframe_rag.ingest import run_ingest

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
    assert _run_main(monkeypatch, dir_b, progress, "--retire-doc", DOC_A) == 0
    # Revisit the older input set: derived base is retained, so the build
    # allocates a suffix. Suppress the post-swap cleanup to simulate the
    # crash landing between cutover and sidecar removal.
    real_clear = run_ingest.clear_publish_state
    monkeypatch.setattr(run_ingest, "clear_publish_state", lambda *a, **k: False)
    assert _run_main(monkeypatch, dir_a, progress, "--retire-doc", DOC_B) == 0
    live = fake.aliases[ALIAS]
    assert live != ALIAS and live.endswith("_1"), f"expected suffixed staging, got {live}"
    gens_before = _gen_collections(fake)
    assert _live_doc_ids(fake) == {DOC_A}

    monkeypatch.setattr(run_ingest, "clear_publish_state", real_clear)
    assert _run_main(monkeypatch, dir_a, progress, "--retire-doc", DOC_B) == 0
    assert fake.aliases[ALIAS] == live
    assert _gen_collections(fake) == gens_before, "finalize allocates nothing"
    assert _live_doc_ids(fake) == {DOC_A}


def test_plan_retire_mixed_history_covers_approved_legacy():
    """B2: whole-document retirement covers approved sourceless history
    alongside named revisions (the #409 reproducer, fixed expectation)."""
    from mainframe_rag.ingest.completion import plan_retire_deletes

    assert plan_retire_deletes({"DOC": {"rev1", None}}, {"DOC": {None}}) == {
        "DOC": {"revs": {"rev1"}, "legacy": True, "whole": True}
    }


def test_plan_retire_partial_leaves_legacy():
    """A named-only retirement never takes legacy points, whole or not."""
    from mainframe_rag.ingest.completion import plan_retire_deletes

    assert plan_retire_deletes({"DOC": {"rev1", None}}, {"DOC": {"rev1"}}) == {
        "DOC": {"revs": {"rev1"}, "legacy": False, "whole": False}
    }
    assert plan_retire_deletes({"DOC": {"rev1"}}, {"DOC": {None}}) == {
        "DOC": {"revs": {"rev1"}, "legacy": False, "whole": True}
    }


def _seed_approved_legacy(progress, doc_id, *, points=None, sha256="0" * 64, rules_v=None):
    """Give a doc approved sourceless history the way a pre-361B inventory
    record does (source_rev None, upserted)."""
    import hashlib

    from mainframe_rag.ingest.inventory import load_inventory

    inv = load_inventory(progress)
    paths = [p for p, rec in inv.items() if rec.doc_id == doc_id]
    assert paths, f"no inventory history for {doc_id}"
    template = inv[paths[0]]
    if rules_v is None:
        rules_v = template.rules_version

    if points is not None:
        chunks = len(points)
        ids = sorted(str(p.id) for p in points)
        h_ids = hashlib.sha256()
        for cid in ids:
            h_ids.update(cid.encode("utf-8"))
            h_ids.update(b"\0")
        chunk_ids_digest = h_ids.hexdigest()
        h_content = hashlib.sha256()
        for cid in ids:
            pt = next(p for p in points if str(p.id) == cid)
            text = str((pt.payload or {}).get("text") or "")
            h_content.update(cid.encode("utf-8"))
            h_content.update(b"\0")
            h_content.update(text.encode("utf-8"))
            h_content.update(b"\0")
        content_digest = h_content.hexdigest()
    else:
        chunks = template.chunks
        chunk_ids_digest = template.chunk_ids_digest
        content_digest = template.content_digest

    legacy = template.model_copy(
        update={
            "path": f"legacy/{doc_id}.pdf",
            "sha256": sha256,
            "source_rev": None,
            "chunks": chunks,
            "chunk_ids_digest": chunk_ids_digest,
            "content_digest": content_digest,
            "rules_version": rules_v,
        }
    )
    with open(progress, "a", encoding="utf-8") as f:
        f.write(legacy.model_dump_json() + "\n")


def test_whole_doc_retire_covers_mixed_history(tmp_path, monkeypatch):
    """B2 end to end: a document with a named revision plus approved
    sourceless history retires completely — named points, legacy points,
    and legacy markers all go; rollback history is retained."""
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _two_doc_corpus(corpus)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live_before = fake.aliases[ALIAS]

    fake.collections[live_before].append(
        SimpleNamespace(id="legacy-a", payload={"doc_id": DOC_A, "text": "pre-361B"})
    )
    _seed_approved_legacy(progress, DOC_A)
    (corpus / "doc_a.pdf").unlink()

    assert _run_main(monkeypatch, corpus, progress, "--retire-doc", DOC_A) == 0
    live_after = fake.aliases[ALIAS]
    assert live_after != live_before
    assert {p.payload["doc_id"] for p in fake.collections[live_after]} == {DOC_B}
    assert live_before in fake.collections
    assert {p.payload["doc_id"] for p in fake.collections[live_before]} == {
        DOC_A,
        DOC_B,
    }


def test_partial_retire_leaves_legacy_with_named_path(tmp_path, monkeypatch):
    """B2: a named-only retirement beside approved legacy refuses, and the
    message names the whole-document way through instead of dead-ending."""
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

    fake.collections[live_before].append(
        SimpleNamespace(id="legacy-a", payload={"doc_id": DOC_A, "text": "pre-361B"})
    )
    _seed_approved_legacy(progress, DOC_A)
    rev = load_inventory(progress)[str(a_path)].source_rev
    assert rev
    a_path.unlink()

    with pytest.raises(RuntimeError, match="never takes legacy points"):
        _run_main(monkeypatch, corpus, progress, "--retire-doc", f"{DOC_A}@{rev}")
    assert fake.aliases[ALIAS] == live_before
    assert any(getattr(p, "id", None) == "legacy-a" for p in fake.collections[live_before]), (
        "refusal deletes nothing"
    )


def test_audit_names_gap_for_unexpected_remnants(monkeypatch):
    """B2: the retired-doc audit names the operator path for each remnant
    shape — unexpected revisions (re-plan) vs sourceless residue with no
    approved history (manual resolution)."""
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
        retire_plan={"D-NAMED": {"revs": {"rev1"}, "legacy": False, "whole": False}},
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
        retire_plan={"D-LEGACY": {"revs": {"rev1"}, "legacy": False, "whole": True}},
    )
    assert len(manual) == 1 and "no approved sourceless history" in manual[0]


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


def test_walked_doc_unapproved_legacy_stray_refuses(tmp_path, monkeypatch):
    """Issue #391 current packet (counterexample 2): sharing a walked doc_id
    is not attributed coverage. A sourceless point with no approved legacy
    membership refuses the steady-state re-verify; the point is preserved and
    the alias does not move."""
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
    _make_completed_legacy_fixture(fake, live)

    fake.collections[live].append(
        SimpleNamespace(id="stray-a", payload={"doc_id": DOC_A, "text": "pre-361B"})
    )
    with pytest.raises(RuntimeError, match="unmarked residue"):
        _run_main(monkeypatch, corpus, progress)
    assert fake.aliases[ALIAS] == live, "refusal moves no alias"
    assert any(getattr(p, "id", None) == "stray-a" for p in fake.collections[live]), (
        "unknown residue is preserved, never deleted"
    )


def test_walked_doc_approved_legacy_stray_publishes(tmp_path, monkeypatch):
    """The pre-361B bridge survives with content attribution: a sourceless
    point whose (doc_id, sha256) matches an approved legacy inventory record
    and passes full digest/rules verification is permitted."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live = fake.aliases[ALIAS]
    _make_completed_legacy_fixture(fake, live)

    rules_v = extraction_rules_version()
    legacy_pt = SimpleNamespace(
        id="legacy-a",
        payload={"doc_id": DOC_A, "sha256": "0" * 64, "rules_v": rules_v, "text": "pre-361B"},
    )
    fake.collections[live].append(legacy_pt)
    _seed_approved_legacy(progress, DOC_A, points=[legacy_pt])
    assert _run_main(monkeypatch, corpus, progress) == 0
    assert fake.aliases[ALIAS] == live
    assert any(getattr(p, "id", None) == "legacy-a" for p in fake.collections[live])


def test_walked_doc_approved_legacy_unexpected_chunk_id_refuses(tmp_path, monkeypatch):
    """Issue #391 Q417-L1 counterexample 1: unexpected chunk ID carrying an approved
    sha256 fails digest verification and refuses cutover."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live = fake.aliases[ALIAS]
    _make_completed_legacy_fixture(fake, live)

    rules_v = extraction_rules_version()
    expected_pt = SimpleNamespace(
        id="legacy-a",
        payload={"doc_id": DOC_A, "sha256": "0" * 64, "rules_v": rules_v, "text": "pre-361B"},
    )
    _seed_approved_legacy(progress, DOC_A, points=[expected_pt])
    corrupt_pt = SimpleNamespace(
        id="legacy-unexpected-id",
        payload={"doc_id": DOC_A, "sha256": "0" * 64, "rules_v": rules_v, "text": "pre-361B"},
    )
    fake.collections[live].append(corrupt_pt)
    with pytest.raises(RuntimeError, match="fail content/digest verification"):
        _run_main(monkeypatch, corpus, progress)
    assert fake.aliases[ALIAS] == live


def test_walked_doc_approved_legacy_altered_text_refuses(tmp_path, monkeypatch):
    """Issue #391 Q417-L1 counterexample 2: altered text carrying an approved sha256
    fails digest verification and refuses cutover."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live = fake.aliases[ALIAS]
    _make_completed_legacy_fixture(fake, live)

    rules_v = extraction_rules_version()
    expected_pt = SimpleNamespace(
        id="legacy-a",
        payload={
            "doc_id": DOC_A,
            "sha256": "0" * 64,
            "rules_v": rules_v,
            "text": "pre-361B original",
        },
    )
    _seed_approved_legacy(progress, DOC_A, points=[expected_pt])
    altered_pt = SimpleNamespace(
        id="legacy-a",
        payload={
            "doc_id": DOC_A,
            "sha256": "0" * 64,
            "rules_v": rules_v,
            "text": "pre-361B altered",
        },
    )
    fake.collections[live].append(altered_pt)
    with pytest.raises(RuntimeError, match="fail content/digest verification"):
        _run_main(monkeypatch, corpus, progress)
    assert fake.aliases[ALIAS] == live


def test_walked_doc_approved_legacy_incompatible_rules_refuses(tmp_path, monkeypatch):
    """Issue #391 Q417-L1 counterexample 3: incompatible extraction rules in approved legacy
    record refuse cutover and require reingest."""
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
    _make_completed_legacy_fixture(fake, live)

    legacy_pt = SimpleNamespace(
        id="legacy-a",
        payload={"doc_id": DOC_A, "sha256": "0" * 64, "rules_v": "old_rules_v", "text": "pre-361B"},
    )
    fake.collections[live].append(legacy_pt)
    _seed_approved_legacy(progress, DOC_A, points=[legacy_pt], rules_v="old_rules_v")
    with pytest.raises(RuntimeError, match="extraction rules 'old_rules_v' mismatch current rules"):
        _run_main(monkeypatch, corpus, progress)
    assert fake.aliases[ALIAS] == live


def test_walked_doc_approved_legacy_extra_chunk_refuses(tmp_path, monkeypatch):
    """Issue #391 Q417-L1 counterexample 4: extra chunk carrying approved sha256
    causes chunk count mismatch and refuses cutover."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live = fake.aliases[ALIAS]
    _make_completed_legacy_fixture(fake, live)

    rules_v = extraction_rules_version()
    pt1 = SimpleNamespace(
        id="legacy-a1",
        payload={
            "doc_id": DOC_A,
            "sha256": "0" * 64,
            "rules_v": rules_v,
            "text": "pre-361B part 1",
        },
    )
    _seed_approved_legacy(progress, DOC_A, points=[pt1])
    pt2 = SimpleNamespace(
        id="legacy-a2",
        payload={
            "doc_id": DOC_A,
            "sha256": "0" * 64,
            "rules_v": rules_v,
            "text": "pre-361B part 2",
        },
    )
    fake.collections[live].extend([pt1, pt2])
    with pytest.raises(RuntimeError, match="chunk count mismatch: expected 1, found 2"):
        _run_main(monkeypatch, corpus, progress)
    assert fake.aliases[ALIAS] == live


def test_walked_doc_legacy_no_digests_fails_closed_with_reingest(tmp_path, monkeypatch):
    """Issue #391 Q417-L1 counterexample 5: legacy document lacking digests fails closed
    with explicit --reingest remediation message."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.inventory import load_inventory
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live = fake.aliases[ALIAS]
    _make_completed_legacy_fixture(fake, live)

    rules_v = extraction_rules_version()
    legacy_pt = SimpleNamespace(
        id="legacy-a",
        payload={"doc_id": DOC_A, "sha256": "0" * 64, "rules_v": rules_v, "text": "pre-361B"},
    )
    fake.collections[live].append(legacy_pt)
    inv = load_inventory(progress)
    template = next(iter(inv.values()))
    legacy = template.model_copy(
        update={
            "path": f"legacy/{DOC_A}.pdf",
            "sha256": "0" * 64,
            "source_rev": None,
            "chunks": 1,
            "chunk_ids_digest": "",
            "content_digest": "",
            "rules_version": rules_v,
        }
    )
    with open(progress, "a", encoding="utf-8") as f:
        f.write(legacy.model_dump_json() + "\n")

    with pytest.raises(
        RuntimeError, match="has no verifiable chunk/content digest — re-ingest with --reingest"
    ):
        _run_main(monkeypatch, corpus, progress)
    assert fake.aliases[ALIAS] == live


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


def test_q417_l1_legacy_retirement_survives_cleanup_and_ordinary_runs(tmp_path, monkeypatch):
    """Issue #391 S422-F1: publish valid named + legacy data -> explicitly retire the legacy
    document -> successful cutover/cleanup -> two unchanged ordinary runs. Both are successful,
    read-only with respect to corpus/markers, and allocate no new generation."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.inventory import load_inventory
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _, b_path = _two_doc_corpus(corpus)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live_initial = fake.aliases[ALIAS]

    # Convert DOC_B into approved legacy history in live and inventory
    b_path.unlink()
    fake.collections[live_initial] = [
        p
        for p in fake.collections[live_initial]
        if (getattr(p, "payload", {}) or {}).get("doc_id") != DOC_B
    ]
    rules_v = extraction_rules_version()
    legacy_pt = SimpleNamespace(
        id="legacy-b",
        payload={
            "doc_id": DOC_B,
            "sha256": "0" * 64,
            "rules_v": rules_v,
            "text": "pre-361B legacy B",
        },
    )
    fake.collections[live_initial].append(legacy_pt)
    _seed_approved_legacy(progress, DOC_B, points=[legacy_pt])

    # 1. Explicitly retire legacy document B
    assert _run_main(monkeypatch, corpus, progress, "--retire-doc", DOC_B) == 0
    live_after_retire = fake.aliases[ALIAS]
    assert live_after_retire != live_initial
    assert {p.payload["doc_id"] for p in fake.collections[live_after_retire]} == {DOC_A}

    # Verify inventory recorded retirement
    inv = load_inventory(progress)
    legacy_rec = next(rec for rec in inv.values() if rec.doc_id == DOC_B)
    assert legacy_rec.status == "retired"

    # 2. First ordinary run without --retire-doc: must succeed as steady state (already_live)
    assert _run_main(monkeypatch, corpus, progress) == 0
    assert fake.aliases[ALIAS] == live_after_retire

    # 3. Second ordinary run without --retire-doc: must also succeed as steady state
    assert _run_main(monkeypatch, corpus, progress) == 0
    assert fake.aliases[ALIAS] == live_after_retire


def test_q417_l1_legacy_retirement_crash_before_cutover_resumes_safely(tmp_path, monkeypatch):
    """Issue #391 S422-F1: failure injected before cutover leaves old live generation intact,
    preserves approved membership, and subsequent retry succeeds."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.inventory import load_inventory
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _, b_path = _two_doc_corpus(corpus)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live_before = fake.aliases[ALIAS]

    b_path.unlink()
    rules_v = extraction_rules_version()
    legacy_pt = SimpleNamespace(
        id="legacy-b",
        payload={
            "doc_id": DOC_B,
            "sha256": "0" * 64,
            "rules_v": rules_v,
            "text": "pre-361B legacy B",
        },
    )
    fake.collections[live_before].append(legacy_pt)
    _seed_approved_legacy(progress, DOC_B, points=[legacy_pt])

    # Inject failure into swap_alias_to
    real_swap = run_ingest.swap_alias_to

    def failing_swap(*args, **kwargs):
        raise RuntimeError("injected crash before cutover")

    monkeypatch.setattr(run_ingest, "swap_alias_to", failing_swap)
    with pytest.raises(RuntimeError, match="injected crash before cutover"):
        _run_main(monkeypatch, corpus, progress, "--retire-doc", DOC_B)

    # Old live is intact, alias unchanged, inventory not marked retired
    assert fake.aliases[ALIAS] == live_before
    inv = load_inventory(progress)
    assert next(rec for rec in inv.values() if rec.doc_id == DOC_B).status == "upserted"

    # Restore swap_alias_to and retry: retry succeeds, alias swaps, retirement committed
    monkeypatch.setattr(run_ingest, "swap_alias_to", real_swap)
    assert _run_main(monkeypatch, corpus, progress, "--retire-doc", DOC_B) == 0
    live_after = fake.aliases[ALIAS]
    assert live_after != live_before
    assert {p.payload["doc_id"] for p in fake.collections[live_after]} == {DOC_A}
    inv_after = load_inventory(progress)
    assert next(rec for rec in inv_after.values() if rec.doc_id == DOC_B).status == "retired"


def test_q417_l1_legacy_retirement_crash_after_cutover_recovers_retire_plan(tmp_path, monkeypatch):
    """Issue #391 S422-F1: failure injected after swap but before cleanup leaves a sidecar
    with retire_plan; the next run recognizes live == staging, commits retirement disposition,
    and succeeds without reallocating."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.inventory import load_inventory
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _, b_path = _two_doc_corpus(corpus)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live = fake.aliases[ALIAS]

    b_path.unlink()
    rules_v = extraction_rules_version()
    legacy_pt = SimpleNamespace(
        id="legacy-b",
        payload={
            "doc_id": DOC_B,
            "sha256": "0" * 64,
            "rules_v": rules_v,
            "text": "pre-361B legacy B",
        },
    )
    fake.collections[live].append(legacy_pt)
    _seed_approved_legacy(progress, DOC_B, points=[legacy_pt])

    # Intercept run_ingest right after swap_alias_to before commit_retired_inventory
    real_swap = run_ingest.swap_alias_to

    def interrupted_swap(*args, **kwargs):
        real_swap(*args, **kwargs)
        raise RuntimeError("injected crash after cutover before sidecar cleanup")

    monkeypatch.setattr(run_ingest, "swap_alias_to", interrupted_swap)
    with pytest.raises(RuntimeError, match="injected crash after cutover before sidecar cleanup"):
        _run_main(monkeypatch, corpus, progress, "--retire-doc", DOC_B)

    # Cutover completed (alias points to new staging collection)
    live_after_swap = fake.aliases[ALIAS]
    assert live_after_swap != live
    # But inventory was not updated yet because of the crash
    inv = load_inventory(progress)
    assert next(rec for rec in inv.values() if rec.doc_id == DOC_B).status == "upserted"

    # Restore normal swap and run ordinary run: recognizes live == staging with pending retire_plan
    monkeypatch.setattr(run_ingest, "swap_alias_to", real_swap)
    assert _run_main(monkeypatch, corpus, progress) == 0
    assert fake.aliases[ALIAS] == live_after_swap

    # Inventory is now committed as retired
    inv_final = load_inventory(progress)
    assert next(rec for rec in inv_final.values() if rec.doc_id == DOC_B).status == "retired"


def test_q417_l1_unexpected_missing_retained_legacy_fails_control(tmp_path, monkeypatch):
    """Issue #391 S422-F1 failing control: an approved legacy document in inventory that is NOT
    retired must still exist and verify; if its chunks are unexpectedly missing, cutover refuses."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _, b_path = _two_doc_corpus(corpus)
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    live = fake.aliases[ALIAS]

    b_path.unlink()
    # Seed approved legacy B in inventory, but do NOT add its points to Qdrant (missing chunks)
    rules_v = extraction_rules_version()
    fake_pt = SimpleNamespace(
        id="legacy-b",
        payload={
            "doc_id": DOC_B,
            "sha256": "0" * 64,
            "rules_v": rules_v,
            "text": "pre-361B legacy B",
        },
    )
    _seed_approved_legacy(progress, DOC_B, points=[fake_pt])

    with pytest.raises(RuntimeError, match="chunk count mismatch: expected 1, found 0"):
        _run_main(monkeypatch, corpus, progress)
    assert fake.aliases[ALIAS] == live


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
    _make_completed_legacy_fixture(fake, live_1)
    inv_1 = load_inventory(progress)
    rev_a = inv_1[str(a_path)].source_rev
    assert rev_a is not None

    # S424-F1: Seed approved, digest-valid legacy sibling L under the same doc_id
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    rules_v = extraction_rules_version()
    legacy_pt = SimpleNamespace(
        id="legacy-doc-a",
        vector={"dense": [0.25] * 256, "bm25": models.SparseVector(indices=[3], values=[0.5])},
        payload={
            "doc_id": DOC_A,
            "sha256": "a" * 64,
            "rules_v": rules_v,
            "text": "pre-361B legacy Doc A",
        },
    )
    fake.collections[live_1].append(legacy_pt)
    _seed_approved_legacy(progress, DOC_A, points=[legacy_pt], sha256="a" * 64)

    # Step 2: Now replace doc_a.pdf with revision B
    make_pdf(a_path, doc_id=DOC_A, title="Synthetic Doc A Rev 2 (Updated)")
    assert _run_main(monkeypatch, corpus, progress, "--retire-doc", f"{DOC_A}@{rev_a}") == 0
    live_2 = fake.aliases[ALIAS]
    assert live_2 != live_1
    inv_2 = load_inventory(progress)
    rev_b = inv_2[str(a_path)].source_rev
    assert rev_b is not None and rev_b != rev_a

    # Verify live_2 has DOC_A@rev_b, DOC_B, and approved legacy L, but NO DOC_A@rev_a
    live_points = fake.collections[live_2]
    revs_in_live = {
        (p.payload.get("doc_id"), p.payload.get("source_rev"))
        for p in live_points
        if p.payload.get("doc_id") == DOC_A
    }
    assert (DOC_A, rev_b) in revs_in_live
    assert (DOC_A, rev_a) not in revs_in_live

    # S424-F1: Assert exact retained legacy point ID and text
    legacy_pts_live2 = [p for p in live_points if getattr(p, "id", None) == "legacy-doc-a"]
    assert len(legacy_pts_live2) == 1
    assert legacy_pts_live2[0].payload.get("text") == "pre-361B legacy Doc A"

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

    # S424-F1: Subsequent ordinary run verifies both L and rev_b in steady state without re-embedding
    assert _run_main(monkeypatch, corpus, progress) == 0
    assert fake.aliases[ALIAS] == live_2
    assert any(getattr(p, "id", None) == "legacy-doc-a" for p in fake.collections[live_2])


def test_retire_revision_a_with_walked_revision_a_fails_closed(tmp_path, monkeypatch):
    """R-REV: retiring revision A while revision A is still present in the walked
    corpus fails closed before mutation."""
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
    live = fake.aliases[ALIAS]
    rev_a = load_inventory(progress)[str(a_path)].source_rev
    assert rev_a is not None

    with pytest.raises(RuntimeError, match="still present in the walked corpus"):
        _run_main(monkeypatch, corpus, progress, "--retire-doc", f"{DOC_A}@{rev_a}")
    assert fake.aliases[ALIAS] == live, "refusal does not move alias"


def test_whole_document_retirement_with_walked_revision_b_fails_closed(tmp_path, monkeypatch):
    """R-REV: whole-document retirement (--retire-doc DOC_A) fails closed if any
    revision of DOC_A is walked."""
    from scripts.make_synthetic_pdf import build as make_pdf

    from mainframe_rag.ingest import run_ingest

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
    live = fake.aliases[ALIAS]

    # Replace with rev 2
    make_pdf(a_path, doc_id=DOC_A, title="Synthetic Doc A Rev 2")
    with pytest.raises(RuntimeError, match="still present in the walked corpus"):
        _run_main(monkeypatch, corpus, progress, "--retire-doc", DOC_A)
    assert fake.aliases[ALIAS] == live


def test_retire_revision_a_with_deleted_legacy_sibling_fails_verification(tmp_path, monkeypatch):
    """S424-F1: an injected deletion of retained legacy sibling L causes final
    verification to fail, refusing cutover."""
    from scripts.make_synthetic_pdf import build as make_pdf

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.inventory import load_inventory
    from mainframe_rag.ingest.rules_version import extraction_rules_version

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

    rules_v = extraction_rules_version()
    legacy_pt = SimpleNamespace(
        id="legacy-doc-a",
        vector={"dense": [0.25] * 256, "bm25": models.SparseVector(indices=[3], values=[0.5])},
        payload={
            "doc_id": DOC_A,
            "sha256": "a" * 64,
            "rules_v": rules_v,
            "text": "pre-361B legacy Doc A",
        },
    )
    fake.collections[live_1].append(legacy_pt)
    _seed_approved_legacy(progress, DOC_A, points=[legacy_pt], sha256="a" * 64)

    # In Step 2: replace with rev B, but inject deletion of legacy point in staging before verification
    make_pdf(a_path, doc_id=DOC_A, title="Synthetic Doc A Rev 2")

    real_impl = run_ingest._run_impl

    def delete_legacy_impl(*args, **kwargs):
        rc = real_impl(*args, **kwargs)
        target = kwargs.get("_publish_target")
        if target and target.staging in fake.collections:
            fake.collections[target.staging] = [
                p
                for p in fake.collections[target.staging]
                if getattr(p, "id", None) != "legacy-doc-a"
            ]
        return rc

    monkeypatch.setattr(run_ingest, "_run_impl", delete_legacy_impl)
    with pytest.raises(RuntimeError, match="chunk count mismatch"):
        _run_main(monkeypatch, corpus, progress, "--retire-doc", f"{DOC_A}@{rev_a}")
    assert fake.aliases[ALIAS] == live_1, "cutover refused: live alias unchanged"


def test_whole_doc_retire_deletes_both_named_and_legacy_history(tmp_path, monkeypatch):
    """S424-F1: whole-document retirement (--retire-doc DOC) removes both
    named revisions and approved legacy history."""
    from scripts.make_synthetic_pdf import build as make_pdf

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.rules_version import extraction_rules_version

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

    rules_v = extraction_rules_version()
    legacy_pt = SimpleNamespace(
        id="legacy-doc-a",
        vector={"dense": [0.25] * 256, "bm25": models.SparseVector(indices=[3], values=[0.5])},
        payload={
            "doc_id": DOC_A,
            "sha256": "a" * 64,
            "rules_v": rules_v,
            "text": "pre-361B legacy Doc A",
        },
    )
    fake.collections[live_1].append(legacy_pt)
    _seed_approved_legacy(progress, DOC_A, points=[legacy_pt], sha256="a" * 64)

    a_path.unlink()
    assert _run_main(monkeypatch, corpus, progress, "--retire-doc", DOC_A) == 0
    live_2 = fake.aliases[ALIAS]
    assert live_2 != live_1
    assert not any((p.payload or {}).get("doc_id") == DOC_A for p in fake.collections[live_2])
    assert any(getattr(p, "id", None) == "legacy-doc-a" for p in fake.collections[live_1])


def test_retire_retry_after_inventory_append_succeeds(tmp_path, monkeypatch):
    """S424-F2: when an interrupted run appends revision B's inventory record,
    retrying the same command recovers the authorized retirement plan from
    the publish_state sidecar and cutover succeeds safely."""
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

    # Retry the IDENTICAL command: recovers retire_plan from publish_state sidecar
    assert _run_main(monkeypatch, corpus, progress, "--retire-doc", f"{DOC_A}@{rev_a}") == 0
    live_2 = fake.aliases[ALIAS]
    assert live_2 != live_1
    assert not any((p.payload or {}).get("source_rev") == rev_a for p in fake.collections[live_2])


def test_retire_retry_with_changed_retirements_fails_closed(tmp_path, monkeypatch):
    """S424-F2: changing the requested retirement on retry does not inherit
    another transaction's authorization and fails closed."""
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
    inv_1 = load_inventory(progress)
    rev_a = inv_1[str(a_path)].source_rev

    make_pdf(a_path, doc_id=DOC_A, title="Synthetic Doc A Rev 2")

    def fail_before_swap(*args, **kwargs):
        raise RuntimeError("simulated crash before swap")

    monkeypatch.setattr(run_ingest, "apply_approved_removals", fail_before_swap)
    with pytest.raises(RuntimeError, match="simulated crash"):
        _run_main(monkeypatch, corpus, progress, "--retire-doc", f"{DOC_A}@{rev_a}")

    # Retry with a different retirement request: fails closed
    monkeypatch.undo()
    _publish_env(monkeypatch)
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    with pytest.raises(RuntimeError, match="refusing a build the current inputs cannot explain"):
        _run_main(monkeypatch, corpus, progress, "--retire-doc", DOC_B)


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


def test_conflict_preflight_walked_retired_revision_fails_closed(tmp_path, monkeypatch):
    """S424-F3: walking the retired revision refuses in preflight before any mutation."""
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
    make_pdf(a_path, doc_id=DOC_A, title="Doc A Content")
    _build_doc_with_id(corpus, "doc_b", DOC_B)

    assert (
        _run_main(
            monkeypatch, corpus, progress, "--vendor", "ibm", "--product", "zos", "--version", "3.1"
        )
        == 0
    )
    live_1 = fake.aliases[ALIAS]
    inv_1 = load_inventory(progress)
    rev_3_1 = inv_1[str(a_path)].source_rev

    # Walking the file with version 3.1 while requesting retirement of 3.1 refuses before mutation
    with pytest.raises(RuntimeError, match="still present in the walked corpus"):
        _run_main(
            monkeypatch,
            corpus,
            progress,
            "--vendor",
            "ibm",
            "--product",
            "zos",
            "--version",
            "3.1",
            "--retire-doc",
            f"{DOC_A}@{rev_3_1}",
        )
    assert fake.aliases[ALIAS] == live_1


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
    retire_plan = {"DOC_A": {"revs": {"rev1"}, "legacy": False, "whole": False}}
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
    retire_plan_whole = {"DOC_A": {"revs": {"rev1", "rev2"}, "legacy": False, "whole": True}}
    count_whole, labels_whole = stale_completion_markers(
        fake,
        settings,
        "new_digest",
        exclude_doc_ids=frozenset({"DOC_A"}),
        retire_plan=retire_plan_whole,
    )
    assert count_whole == 0
    assert labels_whole == []


@pytest.mark.parametrize("changed_representation", [False, True])
@pytest.mark.parametrize("damage", [None, "text", "point", "marker", "marker_digest", "wrong_target"])
def test_forced_build_retry_keeps_verified_document_checkpoint(tmp_path, monkeypatch, changed_representation, damage):
    """A completed document survives a later document failure without re-embedding.

    Exercise the real planner, parser/embedding and completion proof with the
    storage double; threads keep captured worker calls visible to the test.
    """
    import json
    from concurrent.futures import ThreadPoolExecutor

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import publish_state_path

    _publish_env(monkeypatch)
    monkeypatch.setenv("INGEST_UPSERT_STREAMS", "1")
    monkeypatch.setattr(run_ingest, "ProcessPoolExecutor", lambda max_workers, mp_context: ThreadPoolExecutor(max_workers=max_workers))
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


@pytest.mark.parametrize("after_commit", [False, True])
def test_forced_retry_after_all_checkpoints_does_no_embedding(tmp_path, monkeypatch, after_commit):
    """Crashing around manifest commit must not redo a fully checkpointed build."""
    from concurrent.futures import ThreadPoolExecutor

    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    monkeypatch.setattr(run_ingest, "ProcessPoolExecutor", lambda max_workers, mp_context: ThreadPoolExecutor(max_workers=max_workers))
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


def test_staging_distribution_no_policy_is_passthrough():
    fake = _DistFake(missing=("genA", "genA__completions"))
    assert verify_staging_distribution(fake, _settings()) == []


def test_staging_distribution_matching_pair_passes():
    staging = "genA"
    fake = _DistFake(
        {
            staging: _dist_params(),
            f"{staging}__completions": _dist_params(),
        }
    )
    assert verify_staging_distribution(fake, _dist_settings(staging)) == []


def test_staging_distribution_false_ha_corpus_refused():
    """Three peers mean nothing with one copy: an RF1 staging corpus must
    refuse cutover even when coverage would otherwise pass."""
    staging = "genA"
    fake = _DistFake(
        {
            staging: _dist_params(6, 1, 1),
            f"{staging}__completions": _dist_params(),
        }
    )
    problems = verify_staging_distribution(
        fake, _dist_settings(staging, qdrant_write_consistency_factor=1)
    )
    assert problems
    assert any("replication_factor=1" in p for p in problems)
    assert any("snapshot-gated" in p for p in problems)
    assert any(staging in p for p in problems)


def test_staging_distribution_control_only_mismatch_refused():
    """The paired control collection gates cutover too, not just the corpus."""
    staging = "genA"
    fake = _DistFake(
        {
            staging: _dist_params(),
            f"{staging}__completions": _dist_params(6, 1, 1),
        }
    )
    problems = verify_staging_distribution(
        fake, _dist_settings(staging, qdrant_write_consistency_factor=1)
    )
    assert problems
    assert any(f"{staging}__completions" in p for p in problems)
    assert any("replication_factor=1" in p for p in problems)


def test_staging_distribution_unknown_values_refused():
    """Unlike the lenient ingest compatibility check, publication never
    certifies an unknown topology."""
    staging = "genA"
    fake = _DistFake(
        {
            staging: SimpleNamespace(
                shard_number=None, replication_factor=None, write_consistency_factor=None
            ),
            f"{staging}__completions": _dist_params(),
        }
    )
    problems = verify_staging_distribution(fake, _dist_settings(staging))
    assert problems
    assert any("unknown/unreadable" in p for p in problems)


def test_staging_distribution_missing_collection_refused():
    fake = _DistFake(
        {"genA__completions": _dist_params()},
        missing=("genA",),
    )
    problems = verify_staging_distribution(fake, _dist_settings("genA"))
    assert problems
    assert any("absent" in p for p in problems)


def test_staging_distribution_unreadable_collection_refused():
    fake = _DistFake(get_error=RuntimeError("config read failed"))
    fake.collection_exists = lambda _name: True  # type: ignore[method-assign]
    problems = verify_staging_distribution(fake, _dist_settings("genA"))
    assert problems
    assert any("unreadable" in p for p in problems)


def test_staging_distribution_partial_policy_checks_only_selected_keys():
    """A partial explicit selection gates only its keys; unselected keys,
    even wildly different, do not block."""
    staging = "genA"
    fake = _DistFake(
        {
            staging: _dist_params(shards=99, rf=3, w=2),
            f"{staging}__completions": _dist_params(shards=99, rf=3, w=2),
        }
    )
    settings = _settings(
        qdrant_collection=staging, qdrant_replication_factor=3, _env_file=None
    )
    assert verify_staging_distribution(fake, settings) == []


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
    from qdrant_client import models

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


def test_staging_placement_no_policy_is_passthrough():
    assert verify_staging_placement({}, _settings()) == []


def test_staging_placement_healthy_pair_passes():
    assert verify_staging_placement(_healthy_placement_map(), _placement_settings()) == []


def test_staging_placement_partial_policy_refused():
    """Actual copies cannot be judged without the complete tuple — unlike
    the configured check, placement needs S and RF to know what to count."""
    settings = _settings(qdrant_collection="genA", qdrant_replication_factor=3)
    problems = verify_staging_placement(_healthy_placement_map(), settings)
    assert problems
    assert any("partial policy" in p for p in problems)


def test_staging_placement_no_endpoints_refused():
    """Zero observations never certify, even though the configured values
    could match: unknown placement is unverifiable, never green."""
    problems = verify_staging_placement({}, _placement_settings())
    assert problems
    assert any("QDRANT_PEER_URLS" in p for p in problems)
    assert any("entry endpoint" in p for p in problems)


def test_staging_placement_too_few_endpoints_refused():
    mapping = _healthy_placement_map()
    one = {next(iter(mapping)): mapping[next(iter(mapping))]}
    problems = verify_staging_placement(one, _placement_settings())
    assert problems
    assert any("cannot hold replication_factor=3" in p for p in problems)


def test_staging_placement_unreachable_peer_refused():
    """A dead peer is degraded, not healthy: cutover waits for all three
    ACTIVE copies while the old generation keeps serving."""
    mapping = _healthy_placement_map()
    dead_url = "http://peer2:6333"
    mapping[dead_url] = _PlacementFake(
        peer_id=None, status_error=ConnectionError("refused"), info_error=ConnectionError("refused")
    )
    problems = verify_staging_placement(mapping, _placement_settings())
    assert problems
    assert any("degraded" in p for p in problems)


def test_staging_placement_duplicate_peer_identities_refused():
    """Three URLs reaching two peers (a Service masquerading as a third
    copy) must refuse, never count one peer's report twice."""
    mapping = _healthy_placement_map()
    urls = list(mapping)
    mapping[urls[2]] = _PlacementFake(
        peer_id=101, infos=mapping[urls[0]]._infos
    )
    problems = verify_staging_placement(mapping, _placement_settings())
    assert problems
    assert any("distinct peer id" in p for p in problems)


def test_staging_placement_transfer_in_progress_refused():
    """ACTIVE copies with a replica transfer still running are recovering,
    not healthy: the swap waits for catch-up to finish."""
    mapping = _healthy_placement_map()
    urls = list(mapping)
    first = mapping[urls[0]]
    infos = dict(first._infos)
    infos["genA"] = _placement_info(
        101, 6, [(shard, "ACTIVE") for shard in range(6)], transfers=[(0, 101, 202)]
    )
    mapping[urls[0]] = _PlacementFake(peer_id=101, infos=infos)
    problems = verify_staging_placement(mapping, _placement_settings("genA"))
    assert problems
    assert any("recovering" in p for p in problems)


def test_staging_placement_under_replicated_shard_refused():
    """A shard with only two ACTIVE copies (one stale) is degraded even
    when every endpoint answers and the cluster looks coherent."""
    mapping = _healthy_placement_map()
    urls = list(mapping)
    first = mapping[urls[0]]
    infos = dict(first._infos)
    local = [(shard, "ACTIVE") for shard in range(1, 6)] + [(0, "DEAD")]
    infos["genA"] = _placement_info(101, 6, local)
    mapping[urls[0]] = _PlacementFake(peer_id=101, infos=infos)
    problems = verify_staging_placement(mapping, _placement_settings("genA"))
    assert problems
    assert any("genA" in p for p in problems)
    assert any("degraded" in p or "2/3" in p for p in problems)


def test_staging_placement_single_node_profile_passes():
    """The explicit 1/1/1 profile judges its single ACTIVE copy through its
    one endpoint — a standalone server reports no cluster peer id."""

    class _Standalone:
        def cluster_status(self):
            return SimpleNamespace(status="disabled")

        def collection_cluster_info(self, name):
            return _placement_info(999, 1, [(0, "ACTIVE")])

    settings = _settings(
        qdrant_collection="genA",
        qdrant_shard_number=1,
        qdrant_replication_factor=1,
        qdrant_write_consistency_factor=1,
    )
    assert verify_staging_placement({"http://127.0.0.1:6333": _Standalone()}, settings) == []


def test_staging_placement_single_node_unreachable_refused():
    class _Down:
        def cluster_status(self):
            raise ConnectionError("refused")

        def collection_cluster_info(self, name):
            raise ConnectionError("refused")

    settings = _settings(
        qdrant_collection="genA",
        qdrant_shard_number=1,
        qdrant_replication_factor=1,
        qdrant_write_consistency_factor=1,
    )
    problems = verify_staging_placement({"http://127.0.0.1:6333": _Down()}, settings)
    assert problems
    assert any("unreachable" in p for p in problems)


def test_peer_endpoints_parsing():
    assert Settings(_env_file=None).qdrant_peer_endpoints() == ()
    settings = Settings(
        _env_file=None, qdrant_peer_urls="http://a:6333, http://b:6333 ,,"
    )
    assert settings.qdrant_peer_endpoints() == ("http://a:6333", "http://b:6333")


@pytest.mark.parametrize("fault", ["before", "after"])
@pytest.mark.parametrize(
    "operations",
    tuple(permutations(("change", "repair", "retire"))),
)
def test_bounded_publication_lifecycle_traces(tmp_path, monkeypatch, operations, fault):
    from tests.helpers_publication_lifecycle import exercise_publication_trace

    exercise_publication_trace(tmp_path, monkeypatch, PublishFake(), operations, fault, ALIAS)


@pytest.mark.parametrize("operation", ["repair", "retire"])
@pytest.mark.parametrize(
    "gate",
    [
        "check_ingest_compatible", "verify_staging_distribution", "_placement_clients",
        "verify_all_complete", "read_build_binding",
    ],
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


@pytest.mark.parametrize("damage", [
    "version_bool", "version_float", "plan_list", "entry_scalar", "revs_string",
    "revs_nested", "revs_number", "legacy_string", "whole_number", "missing_whole",
    "extra_entry_field", "empty_doc", "partial_legacy", "empty_selection",
    "requests_string", "request_number", "request_empty", "plan_extra_doc",
    "partial_widened", "whole_widened", "duplicate_key",
])
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
        retire_plan={"Manual Alpha": {"revs": {"rev-1"}, "legacy": False, "whole": False}},
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
    elif damage == "legacy_string":
        entry["legacy"] = "false"
    elif damage == "whole_number":
        entry["whole"] = 1
    elif damage == "missing_whole":
        del entry["whole"]
    elif damage == "extra_entry_field":
        entry["all_revisions"] = True
    elif damage == "empty_doc":
        record["retire_plan"][""] = record["retire_plan"].pop("Manual Alpha")
    elif damage == "partial_legacy":
        entry["legacy"] = True
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
        raw = raw.replace('"legacy": false', '"legacy": false, "legacy": true')
    path.write_text(raw)
    with pytest.raises(RuntimeError, match="publish state"):
        read_publish_state(progress, ALIAS)
    assert path.read_text() == raw


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("fields", ["neither", "plan", "requests", "both"])
def test_valid_publish_record_preserves_supported_optional_fields(tmp_path, fields, version):
    from mainframe_rag.ingest.publish import read_publish_state, write_publish_state

    plan = {
        "Manual Alpha": {"revs": {"rev-1", "rev-2"}, "legacy": True, "whole": True},
        "Manual Beta": {"revs": {"rev-3"}, "legacy": False, "whole": False},
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
    state["retire_plan"]["beta"]["legacy"] = "false"
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
    assert receipts[0]["build_schema"] == 2
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


def test_new_writer_refuses_unfinished_old_format_before_mutation(tmp_path, monkeypatch):
    import json

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import publish_state_path

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inventory.jsonl"
    sidecar = publish_state_path(progress, ALIAS)
    sidecar.write_text(json.dumps({
        "version": 1, "alias": ALIAS, "staging": "legacy-candidate",
        "gen_fp": "old-recipe", "corpus_fp": "old-corpus",
    }))
    before = sidecar.read_bytes()
    with pytest.raises(RuntimeError, match="old-format"):
        _run_main(monkeypatch, corpus, progress)
    assert sidecar.read_bytes() == before
    assert not fake.collections and not fake.aliases
    assert not progress.exists()


def test_build_uuid_precedes_writes_and_sealed_retry_is_read_only(tmp_path, monkeypatch):
    _exercise_build_uuid_recovery(tmp_path, monkeypatch, PublishFake(), ALIAS)


@pytest.mark.parametrize("damage", ["version", "uuid", "predecessor", "control", "partial-alias", "redirect-alias", "missing-control"])
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
    assert (fake.collections, fake.aliases, sidecar.read_bytes(), progress.read_bytes()) == before


@pytest.mark.parametrize("target_kind", ["logical", "physical", "build-alias", "retained"])
def test_in_place_writer_cannot_mutate_full_build(tmp_path, monkeypatch, target_kind):
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
    target = ALIAS
    if target_kind in ("physical", "retained"):
        target = physical
    elif target_kind == "build-alias":
        target = next(name for name, value in fake.aliases.items()
                      if name != ALIAS and value == physical)
    if target_kind == "retained":
        assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    monkeypatch.setenv("INGEST_ALIAS_PUBLISH", "false")
    monkeypatch.setenv("QDRANT_COLLECTION", target)
    before = deepcopy(fake.collections), dict(fake.aliases), progress.read_bytes()
    with pytest.raises(RuntimeError, match="immutable build"):
        _run_main(monkeypatch, corpus, progress, "--reingest")
    assert (fake.collections, fake.aliases, progress.read_bytes()) == before


def test_completed_legacy_backfill_never_creates_build_identity(tmp_path, monkeypatch):
    from copy import deepcopy

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import delete_publication_metadata, read_publication_record

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc_with_id(corpus, "doc_a", DOC_A)
    progress = tmp_path / "inventory.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    physical = fake.aliases[ALIAS]
    controls = physical + "__completions"
    # Construct the supported old completed format: committed manifest and
    # completion/content records, ordinary alias, no full build aliases/receipt.
    fake.aliases = {ALIAS: physical}
    delete_publication_metadata(fake, controls)
    before = deepcopy(fake.collections), dict(fake.aliases), progress.read_bytes()
    def unavailable(*args, **kwargs):
        raise RuntimeError("legacy receipt unavailable")
    with monkeypatch.context() as patch:
        patch.setattr(run_ingest, "write_publication_metadata", unavailable)
        with pytest.raises(RuntimeError, match="legacy receipt unavailable"):
            _run_main(monkeypatch, corpus, progress)
    assert (fake.collections, fake.aliases, progress.read_bytes()) == before
    assert _run_main(monkeypatch, corpus, progress) == 0
    record = read_publication_record(fake, controls)
    assert record is not None
    assert not {"build_schema", "build_id", "logical_alias", "data_collection"}.intersection(record)
    assert fake.aliases == {ALIAS: physical}
    published = deepcopy(fake.collections)
    for _ in range(2):
        assert _run_main(monkeypatch, corpus, progress) == 0
        assert fake.collections == published
    assert fake.collections[physical] == before[0][physical]


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


def _make_completed_legacy_fixture(client, physical, alias=ALIAS):
    """Legacy coverage tests predate build certificates; construct that exact format."""
    from mainframe_rag.ingest.publish import publication_metadata_point_id

    control = physical + "__completions"
    record = client.retrieve(control, [publication_metadata_point_id(control)],
                             with_payload=True, with_vectors=True)[0]
    payload = dict(record.payload)
    for key in ("build_schema", "build_id", "logical_alias", "data_collection", "content_seal"):
        payload.pop(key, None)
    client.upsert(control, points=[models.PointStruct(id=record.id, payload=payload, vector=record.vector)], wait=True)
    operations = [models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=a.alias_name))
                  for a in client.get_aliases().aliases
                  if a.alias_name.startswith(alias + "__build_") and
                  a.collection_name in (physical, control)]
    if operations:
        client.update_collection_aliases(operations)


@pytest.mark.parametrize("damage", ["lost-document", "text", "dense", "sparse", "control", "seal-missing", "seal-schema"])
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
    assert (fake.collections, fake.aliases, sidecar.read_bytes(), progress.read_bytes()) == before


def _exercise_retained_content_seal(tmp_path, monkeypatch, client, alias, damage):
    from qdrant_client import models

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


@pytest.mark.parametrize("damage", ["lost-document", "dense", "sparse"])
def test_retained_seal_detects_stored_content_loss(tmp_path, monkeypatch, damage):
    _exercise_retained_content_seal(tmp_path, monkeypatch, PublishFake(), ALIAS, damage)


def test_content_seal_canonical_projection_and_pagination():
    import hashlib
    import json
    import uuid

    from qdrant_client import models

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
    from qdrant_client import models

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
    before = deepcopy(fake.collections), sidecar.read_bytes(), progress.read_bytes()
    fake.fail_swap = None
    with pytest.raises(RuntimeError, match="old-format"):
        _run_main(monkeypatch, corpus, progress)
    assert (fake.collections, sidecar.read_bytes(), progress.read_bytes()) == before
    assert not fake.aliases
