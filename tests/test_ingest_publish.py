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
        doc_id = _filter_doc_id(scroll_filter)
        stored = self._resolve(collection)
        if doc_id is not None:
            stored = [p for p in stored if (p.payload or {}).get("doc_id") == doc_id]
        return stored[:limit], None

    def retrieve(self, collection, ids, *, with_payload=True):
        wanted = {str(i) for i in ids}
        return [
            SimpleNamespace(id=p.id, payload=p.payload)
            for p in self._resolve(collection)
            if str(p.id) in wanted
        ]

    def upsert(self, collection, *, points, wait=True):
        physical = self.aliases.get(collection, collection)
        self.collections.setdefault(physical, []).extend(points)
        return SimpleNamespace()

    def delete(self, collection, *, points_selector, wait=True):
        physical = self.aliases.get(collection, collection)
        doc_id = _filter_doc_id(points_selector)
        kept = [p for p in self.collections.get(physical, [])
                if (p.payload or {}).get("doc_id") != doc_id]
        self.collections[physical] = kept
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
    from mainframe_rag.ingest.inventory import InventoryRecord
    from mainframe_rag.ingest.rules_version import extraction_rules_version
    from mainframe_rag.ingest.run_ingest import _DocLocks, _upsert_one
    from tests.test_ingest_completion import _chunks, _vectors

    _publish_env(monkeypatch)
    fake = PublishFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda s: fake)

    rules_v = extraction_rules_version()
    staging = _settings(qdrant_collection="stg", batch_size=16)
    fake.collections["stg"] = []
    chunks = _chunks(doc_id="D1", n=3)
    _upsert_one(_parsed_doc("D1", "a" * 64), chunks, _vectors(3),
                staging, _DocLocks(), None, False, src_labels="||")

    inv: dict[str, InventoryRecord] = {
        "ok.pdf": InventoryRecord(path="ok.pdf", sha256="a" * 64, doc_id="D1",
                                  status="upserted", rules_version=rules_v),
        "missing.pdf": InventoryRecord(path="missing.pdf", sha256="b" * 64, doc_id="D2",
                                       status="upserted", rules_version=rules_v),
        "stale.pdf": InventoryRecord(path="stale.pdf", sha256="c" * 64, doc_id="D1",
                                     status="upserted", rules_version="0" * 16),
        "err.pdf": InventoryRecord(path="err.pdf", sha256="d" * 64, doc_id="D3",
                                   status="error", rules_version=rules_v),
    }
    problems = verify_all_complete(
        fake, staging,
        [("ok.pdf", "a" * 64), ("missing.pdf", "b" * 64), ("stale.pdf", "c" * 64),
         ("err.pdf", "d" * 64), ("ghost.pdf", "e" * 64)],
        inv, rules_v, "||",
    )
    assert sorted(problems) == ["err.pdf", "ghost.pdf", "missing.pdf", "stale.pdf"]


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
