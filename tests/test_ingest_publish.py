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
        fake, staging_settings, [(str(a_path), inventory[str(a_path)].sha256)],
        inventory, extraction_rules_version(), source_labels(None, None, None),
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
        name for name in fake.collections
        if "__gen" in name and not name.endswith("__completions")
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
        fake, f"{collection}__completions", staging,
        extraction_rules_version(), state=STATE_COMMITTED,
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
        "version": 1, "alias": ALIAS, "staging": recorded,
        "gen_fp": gen_fp, "corpus_fp": corp_fp,
    }
    before = set(fake.collections)
    staging, resumed = resolve_publish_staging(
        fake, settings, gen_fp=gen_fp, corpus_fp=corp_fp, live=live,
        force_reingest=False, state=state,
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
            fake, settings, gen_fp="g" * 16, corpus_fp="c" * 12,
            live=None, force_reingest=False,
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


def _seed_approved_legacy(progress, doc_id):
    """Give a doc approved sourceless history the way a pre-361B inventory
    record does (source_rev None, upserted)."""
    from mainframe_rag.ingest.inventory import load_inventory

    inv = load_inventory(progress)
    paths = [p for p, rec in inv.items() if rec.doc_id == doc_id]
    assert paths, f"no inventory history for {doc_id}"
    template = inv[paths[0]]
    legacy = template.model_copy(
        update={"path": f"legacy/{doc_id}.pdf", "sha256": "0" * 64, "source_rev": None}
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
        DOC_A, DOC_B,
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
    assert any(
        getattr(p, "id", None) == "legacy-a" for p in fake.collections[live_before]
    ), "refusal deletes nothing"


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
        fake, staging, [], inv, "r" * 16,
        retired=frozenset({"D-NAMED"}),
        retire_plan={"D-NAMED": {"revs": {"rev1"}, "legacy": False, "whole": False}},
    )
    assert len(named) == 1 and "rev2" in named[0] and "re-plan" in named[0]

    fake.collections["stg"] = [
        SimpleNamespace(id="leg", payload={"doc_id": "D-LEGACY"}),
    ]
    manual = audit_unmarked_residue(
        fake, staging, [], inv, "r" * 16,
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
        lines = [
            line for line in progress.read_text().splitlines()
            if DOC_A not in line
        ]
        progress.write_text("\n".join(lines) + ("\n" if lines else ""))
        assert DOC_A not in {
            rec.doc_id for rec in load_inventory(progress).values()
        }
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
    is verified expected membership, not a printed-identity match."""
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

    fake.collections[live].append(
        SimpleNamespace(
            id="legacy-a",
            payload={"doc_id": DOC_A, "sha256": "0" * 64, "text": "pre-361B"},
        )
    )
    _seed_approved_legacy(progress, DOC_A)
    assert _run_main(monkeypatch, corpus, progress) == 0
    assert fake.aliases[ALIAS] == live
    assert any(getattr(p, "id", None) == "legacy-a" for p in fake.collections[live])


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
    assert _run_main(
        monkeypatch, corpus, progress, "--retire-doc", DOC_A, "--reingest"
    ) == 0
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
