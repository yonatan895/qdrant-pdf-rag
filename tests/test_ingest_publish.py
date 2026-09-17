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
from pathlib import Path
from types import SimpleNamespace

import pytest

from mainframe_rag.config import Settings
from mainframe_rag.ingest.publish import (
    corpus_fingerprint,
    generation_fingerprint,
    staging_name_for,
    verify_all_complete,
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
                p for p in self.collections.get(physical, [])
                if str(getattr(p, "id", None)) not in wanted
            ]
            return SimpleNamespace()
        doc_id = _filter_doc_id(points_selector)
        rev = _filter_match_value(points_selector, "source_rev")
        self.collections[physical] = [
            p for p in self.collections.get(physical, [])
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
    assert staging_name_for(ALIAS, "a" * 16, "b" * 12, counter=1) == f"{name}_1"
    assert staging_name_for(ALIAS, "a" * 16, "b" * 12, counter=2) == f"{name}_2"


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
                vector={"dense": [0.0] * 256, "bm25": models.SparseVector(indices=[0], values=[1.0])},
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
        "SA22-0000-00", "SA22-0000-01",
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
            payload={"doc_id": "SA22-0000-00", "sha256": "0" * 64, "rules_v": "0" * 16, "text": "stale"},
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
    _upsert_one(_parsed_doc("D1", "a" * 64), chunks, _vectors(3),
                staging, _DocLocks(), None, False, src_labels="||")

    inv: dict[str, InventoryRecord] = {
        "ok.pdf": InventoryRecord(path="ok.pdf", sha256="a" * 64, doc_id="D1",
                                  status="upserted", rules_version=rules_v,
                                  source_rev=source_rev_key("v", "p", "1", "a" * 64)),
        "missing.pdf": InventoryRecord(path="missing.pdf", sha256="b" * 64, doc_id="D2",
                                       status="upserted", rules_version=rules_v),
        "stale.pdf": InventoryRecord(path="stale.pdf", sha256="c" * 64, doc_id="D1",
                                     status="upserted", rules_version="0" * 16),
        "err.pdf": InventoryRecord(path="err.pdf", sha256="d" * 64, doc_id="D3",
                                   status="error", rules_version=rules_v),
        # Pre-361B record: matching content and generation, but no revision
        # stamp — publication cannot prove which revision, so it blocks.
        "legacy.pdf": InventoryRecord(path="legacy.pdf", sha256="a" * 64, doc_id="D1",
                                      status="upserted", rules_version=rules_v),
    }
    problems = verify_all_complete(
        fake, staging,
        [("ok.pdf", "a" * 64), ("missing.pdf", "b" * 64), ("stale.pdf", "c" * 64),
         ("err.pdf", "d" * 64), ("ghost.pdf", "e" * 64), ("legacy.pdf", "a" * 64)],
        inv, rules_v, "||",
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

    return ParsedDoc(path=_Path("d.pdf"), sha256=sha, doc_id=doc_id, title="t",
                     product="p", version="1", vendor="v", page_count=1)


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
    assert _live_manifest_revision(fake, live).embed_model_revision == "", \
        "failed run commits no manifest"


def test_publish_force_same_contract_creates_new_generation_and_preserves_live(tmp_path, monkeypatch):
    """Invariant D4 (Immutable Publication Lifetime Model):
    Publish-mode --reingest with an UNCHANGED contract must publish a distinct
    physical staging generation before alias cutover, never mutating the active
    serving collection in place. Active readers bound to the initial live physical
    never observe partial states, deleted points, or uncommitted manifests."""
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

    live_before = fake.aliases[ALIAS]
    points_before = list(fake.collections[live_before])
    assert points_before, "initial publish must populate live collection"

    # Run same-contract --reingest
    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0

    live_after = fake.aliases[ALIAS]
    # Invariant D4 assertions:
    assert live_after != live_before, "same-contract --reingest must publish a distinct generation"
    assert live_before in fake.collections, "previous live physical must be retained for reader drain / rollback"
    assert fake.collections[live_before] == points_before, "active serving collection must never be mutated in place"

    # New generation contract and coverage assertions:
    assert _live_manifest_revision(fake, live_after).embed_model_revision == ""
    record = read_manifest_record(fake, f"{live_after}__completions")
    assert record is not None and record.state == STATE_COMMITTED
    assert fake.snapshots.get(live_before) is not None, "safety snapshot of previous live must be taken during alias swap"
    assert {p.payload["doc_id"] for p in fake.alias_target_points(ALIAS)} == {"SA22-0000-00"}


def test_clean_rerun_after_reingest_is_noop(tmp_path, monkeypatch):
    """After a same-contract --reingest has published a suffixed generation (e.g. gen_1),
    a subsequent rerun without --reingest must recognize the live generation as already
    matching the corpus and contract, verifying it read-only without flipping alias or creating new collections."""
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    reingest_live = fake.aliases[ALIAS]
    n_collections = len(fake.collections)

    # Clean rerun without --reingest
    assert _run_main(monkeypatch, corpus, progress) == 0
    assert fake.aliases[ALIAS] == reingest_live, "steady state must retain re-ingested live generation"
    assert len(fake.collections) == n_collections, "steady state must not create new collections"


def test_consecutive_reingests_allocate_distinct_generations(tmp_path, monkeypatch):
    """Multiple --reingest invocations on an unchanged corpus allocate successive
    distinct physical generations (e.g. gen_1, gen_2), never colliding or mutating live."""
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0
    gen0 = fake.aliases[ALIAS]

    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    gen1 = fake.aliases[ALIAS]
    assert gen1 != gen0

    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    gen2 = fake.aliases[ALIAS]
    assert gen2 != gen1 and gen2 != gen0
    assert fake.aliases[ALIAS] == gen2
    assert {gen0, gen1, gen2}.issubset(set(fake.collections.keys()))


def test_active_reader_isolation_during_same_contract_reingest(tmp_path, monkeypatch):
    """Active readers bound to the serving generation continue reading its points
    safely while a concurrent or subsequent re-ingest populates a new generation."""
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    progress = tmp_path / "inv.jsonl"
    assert _run_main(monkeypatch, corpus, progress) == 0

    # Reader binds to serving generation
    live_gen = fake.aliases[ALIAS]
    reader_target_points_before = list(fake.collections[live_gen])

    # Ingest executes --reingest
    assert _run_main(monkeypatch, corpus, progress, "--reingest") == 0
    new_live_gen = fake.aliases[ALIAS]
    assert new_live_gen != live_gen

    # Reader querying its bound physical collection sees identical points
    reader_target_points_after = list(fake.collections[live_gen])
    assert reader_target_points_after == reader_target_points_before


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
    assert _live_manifest_revision(fake, old).embed_model_revision == "", \
        "old generation keeps its own contract"
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
    problems = verify_all_complete(
        fake, staging, [("doc.pdf", "a" * 64)], {}, rules_v, "||"
    )
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

    problems = verify_all_complete(
        fake, staging, [("doc.pdf", "a" * 64)], {}, rules_v, "||"
    )
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

    problems = verify_all_complete(
        fake, staging, [("doc.pdf", "a" * 64)], {}, rules_v, "||"
    )
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


# ------------------------------------------------ Invariant D3 (Residue)
def test_verify_all_complete_rejects_unmarked_residue(monkeypatch):
    """Invariant D3: Staging with unreferenced/stray document points fails verification."""
    from qdrant_client import models

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
    staging = _settings(qdrant_collection="stg-residue", batch_size=16)
    write_manifest(fake, "stg-residue__completions", staging, rules_v, state=STATE_COMMITTED)
    fake.collections["stg-residue"] = []

    # Valid doc D1
    chunks = _chunks(doc_id="D1", n=2)
    _upsert_one(_parsed_doc("D1", "a" * 64), chunks, _vectors(2),
                staging, _DocLocks(), None, False, src_labels="||")

    # Injected stray point D_STRAY
    fake.collections["stg-residue"].append(
        models.PointStruct(
            id="00000000-0000-0000-0000-000000000099",
            vector={"dense": [0.0] * 256, "bm25": models.SparseVector(indices=[0], values=[1.0])},
            payload={"doc_id": "D_STRAY", "source_rev": "stray_rev", "sha256": "b" * 64, "rules_v": rules_v, "text": "stray"},
        )
    )

    inv = {
        "ok.pdf": InventoryRecord(
            path="ok.pdf", sha256="a" * 64, doc_id="D1",
            status="upserted", rules_version=rules_v,
            source_rev=source_rev_key("v", "p", "1", "a" * 64),
        ),
    }
    problems = verify_all_complete(
        fake, staging, [("ok.pdf", "a" * 64)], inv, rules_v, "||"
    )
    assert any("unmarked residue detected" in p for p in problems)


def test_verify_all_complete_rejects_orphan_point_without_metadata(monkeypatch):
    """Invariant D3: Staging point with empty/null payload is flagged as unmarked residue."""
    from qdrant_client import models

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
    staging = _settings(qdrant_collection="stg-orphan", batch_size=16)
    write_manifest(fake, "stg-orphan__completions", staging, rules_v, state=STATE_COMMITTED)
    fake.collections["stg-orphan"] = []

    chunks = _chunks(doc_id="D1", n=2)
    _upsert_one(_parsed_doc("D1", "a" * 64), chunks, _vectors(2),
                staging, _DocLocks(), None, False, src_labels="||")

    # Point with empty payload
    fake.collections["stg-orphan"].append(
        models.PointStruct(
            id="00000000-0000-0000-0000-000000000088",
            vector={"dense": [0.0] * 256, "bm25": models.SparseVector(indices=[0], values=[1.0])},
            payload={},
        )
    )

    inv = {
        "ok.pdf": InventoryRecord(
            path="ok.pdf", sha256="a" * 64, doc_id="D1",
            status="upserted", rules_version=rules_v,
            source_rev=source_rev_key("v", "p", "1", "a" * 64),
        ),
    }
    problems = verify_all_complete(
        fake, staging, [("ok.pdf", "a" * 64)], inv, rules_v, "||"
    )
    assert any("unmarked residue detected" in p for p in problems)


def test_verify_all_complete_rejects_point_with_mismatched_rules_version(monkeypatch):
    """Invariant D3: Staging point with mismatched rules_version is flagged as unmarked residue."""
    from qdrant_client import models

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
    staging = _settings(qdrant_collection="stg-wrong-rules", batch_size=16)
    write_manifest(fake, "stg-wrong-rules__completions", staging, rules_v, state=STATE_COMMITTED)
    fake.collections["stg-wrong-rules"] = []

    chunks = _chunks(doc_id="D1", n=2)
    _upsert_one(_parsed_doc("D1", "a" * 64), chunks, _vectors(2),
                staging, _DocLocks(), None, False, src_labels="||")

    src_rev = source_rev_key("v", "p", "1", "a" * 64)
    fake.collections["stg-wrong-rules"].append(
        models.PointStruct(
            id="00000000-0000-0000-0000-000000000077",
            vector={"dense": [0.0] * 256, "bm25": models.SparseVector(indices=[0], values=[1.0])},
            payload={"doc_id": "D1", "source_rev": src_rev, "sha256": "a" * 64, "rules_v": "old_rules_v", "text": "old"},
        )
    )

    inv = {
        "ok.pdf": InventoryRecord(
            path="ok.pdf", sha256="a" * 64, doc_id="D1",
            status="upserted", rules_version=rules_v,
            source_rev=src_rev,
        ),
    }
    problems = verify_all_complete(
        fake, staging, [("ok.pdf", "a" * 64)], inv, rules_v, "||"
    )
    assert any("unmarked residue detected" in p for p in problems)


def test_sweep_unmarked_residue_removes_deleted_doc_points_and_markers(monkeypatch):
    """Invariant D3: sweep_unmarked_residue cleans unreferenced document points
    and completion markers, allowing verification to pass cleanly."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.identity import source_rev_key
    from mainframe_rag.ingest.inventory import InventoryRecord
    from mainframe_rag.ingest.publish import sweep_unmarked_residue
    from mainframe_rag.ingest.representation import STATE_COMMITTED, write_manifest
    from mainframe_rag.ingest.rules_version import extraction_rules_version
    from mainframe_rag.ingest.run_ingest import _DocLocks, _upsert_one
    from tests.test_ingest_completion import _chunks, _vectors

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

    rules_v = extraction_rules_version()
    staging = _settings(qdrant_collection="stg-sweep", batch_size=16)
    write_manifest(fake, "stg-sweep__completions", staging, rules_v, state=STATE_COMMITTED)
    fake.collections["stg-sweep"] = []

    # Insert Doc 1 and Doc 2 into staging
    chunks1 = _chunks(doc_id="D1", n=2)
    _upsert_one(_parsed_doc("D1", "a" * 64), chunks1, _vectors(2),
                staging, _DocLocks(), None, False, src_labels="||")
    chunks2 = _chunks(doc_id="D2", n=3)
    _upsert_one(_parsed_doc("D2", "b" * 64), chunks2, _vectors(3),
                staging, _DocLocks(), None, False, src_labels="||")

    rev1 = source_rev_key("v", "p", "1", "a" * 64)
    rev2 = source_rev_key("v", "p", "1", "b" * 64)
    inv = {
        "d1.pdf": InventoryRecord(path="d1.pdf", sha256="a" * 64, doc_id="D1",
                                  status="upserted", rules_version=rules_v, source_rev=rev1),
        "d2.pdf": InventoryRecord(path="d2.pdf", sha256="b" * 64, doc_id="D2",
                                  status="upserted", rules_version=rules_v, source_rev=rev2),
    }

    # Suppose d2 was deleted: walked contains ONLY d1
    walked = [("d1.pdf", "a" * 64)]

    # Before sweep, verify_all_complete fails because Doc 2 is residue
    problems_before = verify_all_complete(fake, staging, walked, inv, rules_v, "||")
    assert any("unmarked residue detected" in p for p in problems_before)

    # Execute sweep
    swept_count = sweep_unmarked_residue(fake, staging, walked, inv)
    assert swept_count == len(chunks2)

    # After sweep, Doc 2 points are gone from staging
    stg_points = fake.collections["stg-sweep"]
    assert all((p.payload or {}).get("doc_id") == "D1" for p in stg_points)

    # Completions collection has no markers for Doc 2
    comp_points = fake.collections["stg-sweep__completions"]
    for p in comp_points:
        assert (p.payload or {}).get("doc_id") != "D2"

    # Verification passes with 0 problems
    problems_after = verify_all_complete(fake, staging, walked, inv, rules_v, "||")
    assert problems_after == []


def test_publish_cutover_sweeps_deleted_document_points(tmp_path, monkeypatch):
    """Invariant D3 End-to-End: When a corpus document is deleted from disk, publication
    sweeps its points from staging prior to alias cutover; active searchable coverage
    contains zero unmarked residue from the deleted document."""
    from mainframe_rag.ingest import run_ingest

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    doc_b = _build_doc(corpus, "SA22-0000-01_second")
    progress = tmp_path / "inv.jsonl"

    # Run 1: Publish initial state with Doc A and Doc B
    assert _run_main(monkeypatch, corpus, progress) == 0
    gen1 = fake.aliases[ALIAS]
    gen1_doc_ids = {p.payload["doc_id"] for p in fake.alias_target_points(ALIAS)}
    assert gen1_doc_ids == {"SA22-0000-00", "SA22-0000-01"}

    # Delete Doc B from disk
    doc_b.unlink()

    # Run 2: Publish update after deletion
    assert _run_main(monkeypatch, corpus, progress) == 0
    gen2 = fake.aliases[ALIAS]
    assert gen2 != gen1, "Must cut over to new generation"

    # Active coverage contains ZERO points from deleted Doc B
    active_doc_ids = {p.payload["doc_id"] for p in fake.alias_target_points(ALIAS)}
    assert active_doc_ids == {"SA22-0000-00"}, "Active searchable coverage must contain zero residue from deleted document"

    # Gen 1 is preserved intact for rollback
    gen1_preserved = {p.payload["doc_id"] for p in fake.collections[gen1]}
    assert gen1_preserved == {"SA22-0000-00", "SA22-0000-01"}

    # Gen 2 completion markers contain no entry for Doc B
    gen2_comp = fake.collections[f"{gen2}__completions"]
    for p in gen2_comp:
        assert (p.payload or {}).get("doc_id") != "SA22-0000-01"


def test_publish_unmarked_residue_blocks_alias_cutover_if_unswept(tmp_path, monkeypatch):
    """Invariant D3 End-to-End Safety: If unswept residue exists in staging,
    verify_all_complete flags it, publication aborts fail-closed, and alias is untouched."""
    from qdrant_client import models

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _build_doc(corpus, "SA22-0000-00_first")
    progress = tmp_path / "inv.jsonl"

    assert _run_main(monkeypatch, corpus, progress) == 0
    live_before = fake.aliases[ALIAS]

    # Hook sweep_unmarked_residue to inject a stray point after sweep (simulating unswept residue)
    orig_sweep = run_ingest.sweep_unmarked_residue

    def _sweep_and_inject(client, staging_settings, walked, inventory):
        res = orig_sweep(client, staging_settings, walked, inventory)
        stg = staging_settings.qdrant_collection
        fake.collections[stg].append(
            models.PointStruct(
                id="00000000-0000-0000-0000-000000000099",
                vector={"dense": [0.0] * 256, "bm25": models.SparseVector(indices=[0], values=[1.0])},
                payload={"doc_id": "RESIDUE", "source_rev": "stale_rev", "sha256": "x", "rules_v": extraction_rules_version(), "text": "unswept"},
            )
        )
        return res

    monkeypatch.setattr(run_ingest, "sweep_unmarked_residue", _sweep_and_inject)

    # Ingest update with force_reingest
    with pytest.raises(RuntimeError, match="unmarked residue detected"):
        _run_main(monkeypatch, corpus, progress, "--reingest")

    # Alias is untouched, previous live remains serving
    assert fake.aliases[ALIAS] == live_before, "Alias must remain untouched when staging has unmarked residue"

