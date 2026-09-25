"""End-to-end simulation tier (marker: ``integration``, run via ``sh scripts/tools/run-task.sh qa:sim``).

Real PDFs -> real ingest into a real Qdrant server (docker, the images.txt
pin) -> agent endpoints over the real app. The model is the only stand-in:
scripts/mock_vllm.py serves deterministic embeddings and a deterministic
reasoning chat over real loopback HTTP, and retrieval runs hash or vLLM-shaped
embed paths exactly as configured. No retrieval/LLM code is monkeypatched.

Skips cleanly when docker (or the pinned image) is unavailable, so the
required pytest gate stays hermetic: plain `pytest` deselects this module via
`-m 'not integration'` (pyproject addopts). Corpus is generated at runtime;
no PDFs ever reach git.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[1]
MOCK_DIM = 32  # must equal the DENSE_DIM the vLLM-shaped variant declares

MOCK_SPEC = importlib.util.spec_from_file_location(
    "mock_vllm_sim", REPO_ROOT / "scripts" / "mock_vllm.py"
)


@pytest.fixture(scope="session")
def qdrant_url():
    """QDRANT_SIM_URL wins (a running server, e.g. `sh scripts/tools/run-task.sh local:qdrant:up`);
    otherwise run the pinned image on an ephemeral loopback port. Lifecycle
    lives in scripts/qdrant_sim.py (shared with the benchmark harness)."""
    from scripts.qdrant_sim import QdrantSimError, start_simulator

    try:
        sim = start_simulator(REPO_ROOT, os.environ.get("QDRANT_SIM_URL"))
    except QdrantSimError as exc:
        pytest.skip(str(exc))
    yield sim.url
    sim.stop()


@pytest.fixture(scope="session", autouse=True)
def _clean_sim_collections(qdrant_url):
    """Sessions must be independent on a warm server: stale points from an
    earlier run (fresh PDF timestamps change every sha) would otherwise be
    deleted-and-reupserted mid-tier, perturbing score ordering."""
    for name in ("sim-hash", "sim-vllm"):
        httpx2.delete(f"{qdrant_url}/collections/{name}", timeout=10.0)
    _drop_publish_fixture(qdrant_url)
    yield


PUBLISH_ALIAS = "sim-publish"


def _drop_publish_fixture(qdrant_url: str) -> None:
    """Remove the alias-publish fixture state (alias + every generation and
    its completions) so a warm server cannot certify a stale live generation."""
    from qdrant_client import QdrantClient, models

    client = QdrantClient(url=qdrant_url, timeout=10)
    try:
        aliases = [a for a in client.get_aliases().aliases if a.alias_name == PUBLISH_ALIAS]
        if aliases:
            client.update_collection_aliases(
                [
                    models.DeleteAliasOperation(
                        delete_alias=models.DeleteAlias(alias_name=PUBLISH_ALIAS)
                    )
                ]
            )
        for desc in client.get_collections().collections:
            if desc.name.startswith(PUBLISH_ALIAS):
                client.delete_collection(desc.name)
    finally:
        client.close()


@pytest.fixture(scope="session")
def mock_url():
    """The model stand-in: deterministic embeddings + chat over real HTTP."""
    old = os.environ.get("MOCK_DIM")
    os.environ["MOCK_DIM"] = str(MOCK_DIM)
    try:
        mod = importlib.util.module_from_spec(MOCK_SPEC)
        MOCK_SPEC.loader.exec_module(mod)
    finally:
        if old is None:
            os.environ.pop("MOCK_DIM", None)
        else:
            os.environ["MOCK_DIM"] = old
    server = mod.ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="session")
def corpus(tmp_path_factory) -> Path:
    from scripts.make_synthetic_pdf import build, build_plain

    root = tmp_path_factory.mktemp("sim-corpus")
    build(root / "SA22-0000-00.pdf")
    # Distinct message_id + title: identical bodies would tie in RRF and flip
    # top-1 between runs (review round 1 blocker).
    build(
        root / "SA22-7777-01.pdf",
        doc_id="SA22-7777-01",
        title="Synthetic Initialization and Tuning Reference",
        message_id="IEB700I",
    )
    build_plain(root / "widget-guide.pdf")
    return root


def _bm25_cache_dir() -> str:
    """Verify the exact selected cache; never fall back from a bad explicit path."""
    from scripts.fetch_bm25_weights import prepared_bm25_cache

    return str(prepared_bm25_cache(REPO_ROOT))


def _ingest(
    monkeypatch,
    qdrant_url: str,
    collection: str,
    corpus: Path,
    progress: Path,
    embed: str = "hash",
    mock_url: str | None = None,
    bm25_cache: str | None = None,
    extra: tuple[str, ...] = (),
) -> list[dict]:
    from mainframe_rag.ingest import run_ingest

    monkeypatch.setenv("QDRANT_URL", qdrant_url)
    monkeypatch.setenv("QDRANT_COLLECTION", collection)
    monkeypatch.setenv("EMBED_MODE", embed)
    if embed == "vllm":
        assert mock_url, "the vLLM-shaped variant needs the mock endpoint URL"
        monkeypatch.setenv("EMBED_BASE_URL", f"{mock_url}/v1")
        monkeypatch.setenv("EMBED_MODEL", "mock-embed")
        monkeypatch.setenv("EMBED_MODEL_REVISION", "mock-rev")
        monkeypatch.setenv("DENSE_DIM", str(MOCK_DIM))
        if bm25_cache:
            monkeypatch.setenv("BM25_CACHE_DIR", bm25_cache)
    # Cached parent-side globals from a previous variant must not leak in.
    # Close the previous client before dropping it — resetting the global
    # alone would leak its httpx pool on every ingest.
    previous_qdrant = run_ingest._worker_qdrant
    if previous_qdrant is not None:
        previous_qdrant.close()
    monkeypatch.setattr(run_ingest, "_worker_qdrant", None)
    monkeypatch.setattr(run_ingest, "_worker_embedder", None)
    rc = run_ingest.main(
        ["--src", str(corpus), "--progress", str(progress), "--workers", "1", *extra]
    )
    assert rc == 0, f"ingest into {collection} failed"
    return [json.loads(line) for line in progress.read_text().splitlines() if line.strip()]


@contextmanager
def _agent(
    monkeypatch,
    qdrant_url: str,
    mock_url: str,
    collection: str,
    *,
    embed: str = "hash",
    bm25_cache: str | None = None,
):
    from mainframe_rag.agent import app as app_mod

    monkeypatch.setenv("QDRANT_URL", qdrant_url)
    monkeypatch.setenv("QDRANT_COLLECTION", collection)
    monkeypatch.setenv("LLM_BASE_URL", f"{mock_url}/v1")
    monkeypatch.setenv("LLM_MODEL_REASONING", "mock-reasoning")
    if embed == "hash":
        monkeypatch.setenv("EMBED_MODE", "hash")
        monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    else:
        monkeypatch.setenv("EMBED_MODE", "vllm")
        monkeypatch.setenv("EMBED_BASE_URL", f"{mock_url}/v1")
        monkeypatch.setenv("EMBED_MODEL", "mock-embed")
        monkeypatch.setenv("EMBED_MODEL_REVISION", "mock-rev")
        monkeypatch.setenv("DENSE_DIM", str(MOCK_DIM))
        if bm25_cache:
            monkeypatch.setenv("BM25_CACHE_DIR", bm25_cache)
    with TestClient(app_mod.app) as client:
        yield client


_MESSAGE_CITE = (
    "SA22-0000-00 Synthetic Operating System Reference, "
    "Chapter 2 Operator messages > IEA500I, p. 1-6"
)


def test_ingest_real_server_and_resume(qdrant_url, corpus, tmp_path, monkeypatch):
    records = _ingest(monkeypatch, qdrant_url, "sim-hash", corpus, tmp_path / "inv.jsonl")
    assert [r["status"] for r in records] == ["upserted"] * 3
    assert all(r["chunks"] > 0 for r in records)
    assert sorted(r["doc_id"] for r in records) == ["SA22-0000-00", "SA22-7777-01", "widget-guide"]

    # Resume: fresh inventory, warm Qdrant -> qdrant-level sha skip, no new work.
    resume = _ingest(monkeypatch, qdrant_url, "sim-hash", corpus, tmp_path / "inv2.jsonl")
    assert [r["status"] for r in resume] == ["skipped"] * 3


def test_alias_publish_rekeys_manifest_across_clone(qdrant_url, corpus, tmp_path, monkeypatch):
    """Issue #391 F5 against the real server: a second publish (changed
    corpus -> new staging generation) clones live's points AND completion
    markers, then must re-key the manifest with the real retrieve projection
    (`retrieve` defaults to `with_vectors=False`). Before the fix the re-key
    silently failed, the inner preflight read the inherited data as legacy,
    and publication aborted; the permissive in-memory fakes hid it."""
    from qdrant_client import QdrantClient
    from scripts.make_synthetic_pdf import build

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest.qdrant_io import resolve_live_collection, scroll_all_points
    from mainframe_rag.ingest.representation import read_manifest

    monkeypatch.setenv("INGEST_ALIAS_PUBLISH", "true")
    local = tmp_path / "publish-corpus"
    local.mkdir()
    for pdf in corpus.iterdir():
        shutil.copy(pdf, local / pdf.name)
    progress = tmp_path / "inv.jsonl"
    first = _ingest(monkeypatch, qdrant_url, PUBLISH_ALIAS, local, progress)
    assert [r["status"] for r in first] == ["upserted"] * 3

    # A new document changes the corpus fingerprint -> a distinct staging
    # generation, so the second publish must clone + re-key live metadata.
    build(
        local / "SA22-8888-02.pdf",
        doc_id="SA22-8888-02",
        title="Synthetic Data Set Utility Reference",
        message_id="IEC900I",
    )
    second = _ingest(monkeypatch, qdrant_url, PUBLISH_ALIAS, local, progress)
    # The progress file is append-only: the second run's records are the tail.
    second_records = second[len(first) :]
    # Completion markers certify their own target_collection, so the cloned
    # staging re-embeds the walked corpus (never a cross-generation skip):
    # all four docs upsert. Before the F5 fix this run aborted instead —
    # the un-rekeyed manifest read as legacy during the inner preflight.
    assert sorted(r["status"] for r in second_records) == ["upserted"] * 4

    settings = Settings(
        _env_file=None,
        qdrant_url=qdrant_url,
        qdrant_collection=PUBLISH_ALIAS,
        embed_mode="hash",
        allow_hash_mode=True,
    )
    client = QdrantClient(url=qdrant_url, timeout=10)
    try:
        physical, legacy = resolve_live_collection(client, settings)
        assert legacy is False and physical is not None
        assert "__gen" in physical, "publish must serve a physical generation behind the alias"
        manifest = read_manifest(client, f"{physical}__completions")
        assert manifest is not None, "staging manifest must be readable after the clone + re-key"
        assert manifest.schema_version == 1
        points = scroll_all_points(
            client, physical, scroll_filter=None, with_payload=["doc_id"], page_size=100
        )
        assert {p.payload.get("doc_id") for p in points} == {
            "SA22-0000-00",
            "SA22-7777-01",
            "SA22-8888-02",
            "widget-guide",
        }
    finally:
        client.close()


def test_alias_publish_revision_migration_keeps_old_generation(
    qdrant_url, mock_url, corpus, tmp_path, monkeypatch
):
    """Issue #391 F2/F3/F4 against the real server: a revision-only change
    derives a distinct staging generation, re-embeds there, and swaps only
    after the contract commits; the old physical keeps its points AND its
    rev-A manifest, and the agent (real serving gate) serves the alias
    target's own contract before, during the migrated state, and after an
    operator alias rollback."""
    from qdrant_client import QdrantClient, models

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest.qdrant_io import resolve_live_collection, scroll_all_points
    from mainframe_rag.ingest.representation import STATE_COMMITTED, read_manifest_record

    _drop_publish_fixture(qdrant_url)
    monkeypatch.setenv("INGEST_ALIAS_PUBLISH", "true")
    monkeypatch.setenv("EMBED_MODEL_REVISION", "")
    local = tmp_path / "rev-corpus"
    local.mkdir()
    for pdf in corpus.iterdir():
        shutil.copy(pdf, local / pdf.name)
    progress = tmp_path / "inv.jsonl"
    first = _ingest(monkeypatch, qdrant_url, PUBLISH_ALIAS, local, progress)
    assert [r["status"] for r in first] == ["upserted"] * 3

    settings = Settings(
        _env_file=None,
        qdrant_url=qdrant_url,
        qdrant_collection=PUBLISH_ALIAS,
        embed_mode="hash",
        allow_hash_mode=True,
    )
    client = QdrantClient(url=qdrant_url, timeout=10)
    try:
        old, legacy = resolve_live_collection(client, settings)
        assert legacy is False and old is not None
        old_record = read_manifest_record(client, f"{old}__completions")
        assert old_record.state == STATE_COMMITTED
        assert old_record.manifest.embed_model_revision == ""

        monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-2")
        second = _ingest(
            monkeypatch, qdrant_url, PUBLISH_ALIAS, local, progress, extra=("--reingest",)
        )
        second_records = second[len(first) :]
        assert sorted(r["status"] for r in second_records) == ["upserted"] * 3, (
            "a revision change re-embeds every walked document"
        )

        new, _ = resolve_live_collection(client, settings)
        assert new != old, "revision-only change must publish a distinct generation"
        assert client.collection_exists(old), "old physical retained for rollback"
        assert read_manifest_record(client, f"{old}__completions") == old_record, (
            "old generation keeps its contract (rollback selects matching metadata)"
        )
        new_record = read_manifest_record(client, f"{new}__completions")
        assert new_record is not None
        assert new_record.state == STATE_COMMITTED
        assert new_record.manifest.embed_model_revision == "rev-2"
        expected_docs = {"SA22-0000-00", "SA22-7777-01", "widget-guide"}
        for physical in (old, new):
            docs = {
                p.payload.get("doc_id")
                for p in scroll_all_points(
                    client, physical, scroll_filter=None, with_payload=["doc_id"], page_size=100
                )
            }
            assert docs == expected_docs

        # Issue #391 F3/F4: the real serving gate resolves the alias to the
        # migrated physical, validates ITS metadata, and serves.
        with _agent(monkeypatch, qdrant_url, mock_url, PUBLISH_ALIAS) as agent_client:
            health = agent_client.get("/healthz")
            assert health.status_code == 200
            assert health.json()["representation"] == "compatible"
            body = agent_client.post(
                "/v1/search", json={"query": "IEA500I operator message"}
            ).json()
            assert body["hits"], "the agent must serve the alias target's own generation"

        # Operator rollback: re-point the alias at the old generation. The
        # agent's next process sees rev-A data AND rev-A metadata together.
        client.update_collection_aliases(
            [
                models.DeleteAliasOperation(
                    delete_alias=models.DeleteAlias(alias_name=PUBLISH_ALIAS)
                ),
                models.CreateAliasOperation(
                    create_alias=models.CreateAlias(collection_name=old, alias_name=PUBLISH_ALIAS)
                ),
            ]
        )
        monkeypatch.setenv("EMBED_MODEL_REVISION", "")
        with _agent(monkeypatch, qdrant_url, mock_url, PUBLISH_ALIAS) as agent_client:
            health = agent_client.get("/healthz")
            assert health.status_code == 200
            assert health.json()["representation"] == "compatible"
            body = agent_client.post(
                "/v1/search", json={"query": "IEA500I operator message"}
            ).json()
            assert body["hits"], "rollback serves the old generation with its own contract"
    finally:
        client.close()


def test_forced_repair_publishes_distinct_generation_on_real_server(
    qdrant_url, corpus, tmp_path, monkeypatch
):
    """Issue #391 current packet, real clone/alias semantics: a forced
    same-contract rebuild (--reingest) publishes a suffixed repair
    generation, keeps the old physical and its manifest byte-identical, and
    swaps the alias only after verification."""
    from qdrant_client import QdrantClient

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest.qdrant_io import resolve_live_collection
    from mainframe_rag.ingest.representation import STATE_COMMITTED, read_manifest_record

    _drop_publish_fixture(qdrant_url)
    monkeypatch.setenv("INGEST_ALIAS_PUBLISH", "true")
    monkeypatch.setenv("EMBED_MODEL_REVISION", "")
    local = tmp_path / "repair-corpus"
    local.mkdir()
    for pdf in corpus.iterdir():
        shutil.copy(pdf, local / pdf.name)
    progress = tmp_path / "inv.jsonl"
    first = _ingest(monkeypatch, qdrant_url, PUBLISH_ALIAS, local, progress)
    assert [r["status"] for r in first] == ["upserted"] * 3

    settings = Settings(
        _env_file=None,
        qdrant_url=qdrant_url,
        qdrant_collection=PUBLISH_ALIAS,
        embed_mode="hash",
        allow_hash_mode=True,
    )
    client = QdrantClient(url=qdrant_url, timeout=10)
    try:
        old, legacy = resolve_live_collection(client, settings)
        assert legacy is False and old is not None
        old_record = read_manifest_record(client, f"{old}__completions")
        assert old_record.state == STATE_COMMITTED
        old_count = client.get_collection(old).points_count

        repaired = _ingest(
            monkeypatch, qdrant_url, PUBLISH_ALIAS, local, progress, extra=("--reingest",)
        )
        assert len(repaired) > len(first), "the repair re-embeds every walked document"
        new, _ = resolve_live_collection(client, settings)
        assert new != old
        assert new.endswith("_1"), f"expected a suffixed repair generation, got {new}"
        assert read_manifest_record(client, f"{old}__completions") == old_record
        assert client.get_collection(old).points_count == old_count, (
            "the serving generation must not be mutated by a repair"
        )
        new_record = read_manifest_record(client, f"{new}__completions")
        assert new_record is not None and new_record.state == STATE_COMMITTED
    finally:
        client.close()
        _drop_publish_fixture(qdrant_url)


def test_subsequent_run_after_repair_steady_state_on_real_server(
    qdrant_url, corpus, tmp_path, monkeypatch
):
    """Issue #391 Q418-R1 against real Qdrant server: a successful repair cuts
    over to a suffixed generation; subsequent ordinary ingest without --reingest
    recognizes live as steady state, performs a read-only verification, and
    allocates no new collections."""
    import shutil

    from qdrant_client import QdrantClient

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.qdrant_io import resolve_live_collection

    _drop_publish_fixture(qdrant_url)
    monkeypatch.setenv("INGEST_ALIAS_PUBLISH", "true")
    monkeypatch.setenv("EMBED_MODEL_REVISION", "")
    local = tmp_path / "repair-steady-corpus"
    local.mkdir()
    for pdf in corpus.iterdir():
        shutil.copy(pdf, local / pdf.name)
    progress = tmp_path / "inv.jsonl"
    first = _ingest(monkeypatch, qdrant_url, PUBLISH_ALIAS, local, progress)
    assert [r["status"] for r in first] == ["upserted"] * 3

    settings = Settings(
        _env_file=None,
        qdrant_url=qdrant_url,
        qdrant_collection=PUBLISH_ALIAS,
        embed_mode="hash",
        allow_hash_mode=True,
    )
    client = QdrantClient(url=qdrant_url, timeout=10)
    try:
        old, _ = resolve_live_collection(client, settings)
        assert old is not None

        # Forced repair: cuts over to suffixed generation
        _ingest(monkeypatch, qdrant_url, PUBLISH_ALIAS, local, progress, extra=("--reingest",))
        repaired, _ = resolve_live_collection(client, settings)
        assert repaired != old
        assert repaired.endswith("_1")
        cols_before = {c.name for c in client.get_collections().collections}

        # Subsequent ordinary run WITHOUT --reingest
        previous = run_ingest._worker_qdrant
        if previous is not None:
            previous.close()
        monkeypatch.setattr(run_ingest, "_worker_qdrant", None)
        monkeypatch.setattr(run_ingest, "_worker_embedder", None)

        assert (
            run_ingest.main(["--src", str(local), "--progress", str(progress), "--workers", "1"])
            == 0
        )
        current, _ = resolve_live_collection(client, settings)
        assert current == repaired
        cols_after = {c.name for c in client.get_collections().collections}
        assert cols_after == cols_before, (
            "ordinary steady-state run must not create new collections"
        )
    finally:
        client.close()
        _drop_publish_fixture(qdrant_url)


def test_migration_scope_proof_blocks_unmarked_point_on_real_server(
    qdrant_url, corpus, tmp_path, monkeypatch
):
    """Issue #391 current packet, real storage semantics: the commit-time
    membership proof reads real scroll projections; an unmarked old-rules
    point blocks the migration commit, the contract stays pending, and the
    point survives the refusal. A complete walk afterward commits (rev-B)."""
    from qdrant_client import QdrantClient, models

    from mainframe_rag.config import HASH_EMBED_DIM
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.representation import (
        STATE_COMMITTED,
        STATE_PENDING,
        read_manifest_record,
    )

    collection = "sim-scope-proof"
    client = QdrantClient(url=qdrant_url, timeout=10)
    try:
        if client.collection_exists(collection):
            client.delete_collection(collection)
        if client.collection_exists(f"{collection}__completions"):
            client.delete_collection(f"{collection}__completions")
        monkeypatch.setenv("EMBED_MODEL_REVISION", "")
        progress = tmp_path / "inv.jsonl"
        first = _ingest(monkeypatch, qdrant_url, collection, corpus, progress)
        assert [r["status"] for r in first] == ["upserted"] * 3

        client.upsert(
            collection,
            points=[
                models.PointStruct(
                    id="00000000-0000-0000-0000-000000000391",
                    vector={
                        "dense": [0.0] * HASH_EMBED_DIM,
                        "bm25": models.SparseVector(indices=[0], values=[1.0]),
                    },
                    payload={
                        "doc_id": "SA99-0000-00",
                        "rules_v": "pre-rp2",
                        "source_rev": "rev-old",
                    },
                )
            ],
            wait=True,
        )

        monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-2")
        previous = run_ingest._worker_qdrant
        if previous is not None:
            previous.close()
        monkeypatch.setattr(run_ingest, "_worker_qdrant", None)
        monkeypatch.setattr(run_ingest, "_worker_embedder", None)
        with pytest.raises(RuntimeError, match="searchable point"):
            run_ingest.main(
                ["--src", str(corpus), "--progress", str(progress), "--workers", "1", "--reingest"]
            )
        record = read_manifest_record(client, f"{collection}__completions")
        assert record is not None and record.state == STATE_PENDING, (
            "an incomplete scope proof leaves the contract pending"
        )
        kept = client.retrieve(
            collection, ids=["00000000-0000-0000-0000-000000000391"], with_payload=True
        )
        assert kept, "unknown data is preserved: refusal is never a deletion instruction"

        client.delete(
            collection,
            points_selector=models.PointIdsList(points=["00000000-0000-0000-0000-000000000391"]),
            wait=True,
        )
        previous = run_ingest._worker_qdrant
        if previous is not None:
            previous.close()
        monkeypatch.setattr(run_ingest, "_worker_qdrant", None)
        monkeypatch.setattr(run_ingest, "_worker_embedder", None)
        assert (
            run_ingest.main(
                ["--src", str(corpus), "--progress", str(progress), "--workers", "1", "--reingest"]
            )
            == 0
        )
        record = read_manifest_record(client, f"{collection}__completions")
        assert record is not None and record.state == STATE_COMMITTED
        assert record.manifest.embed_model_revision == "rev-2"
    finally:
        if client.collection_exists(f"{collection}__completions"):
            client.delete_collection(f"{collection}__completions")
        if client.collection_exists(collection):
            client.delete_collection(collection)
        client.close()


def test_legacy_verification_refuses_corrupt_points_on_real_server(
    qdrant_url, corpus, tmp_path, monkeypatch
):
    """Issue #391 Q417-L1 on real Qdrant server: sourceless legacy points
    fail cutover if chunk IDs, text digests, or rules do not match approved
    evidence. Once verifiable digests are present, the bridge allows cutover."""
    import hashlib

    from qdrant_client import QdrantClient, models

    from mainframe_rag.config import HASH_EMBED_DIM, Settings
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.inventory import load_inventory
    from mainframe_rag.ingest.qdrant_io import resolve_live_collection
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    _drop_publish_fixture(qdrant_url)
    monkeypatch.setenv("INGEST_ALIAS_PUBLISH", "true")
    monkeypatch.setenv("EMBED_MODEL_REVISION", "")
    local = tmp_path / "legacy-corpus"
    local.mkdir()
    for pdf in corpus.iterdir():
        shutil.copy(pdf, local / pdf.name)
    progress = tmp_path / "inv.jsonl"
    first = _ingest(monkeypatch, qdrant_url, PUBLISH_ALIAS, local, progress)
    assert [r["status"] for r in first] == ["upserted"] * 3

    settings = Settings(
        _env_file=None,
        qdrant_url=qdrant_url,
        qdrant_collection=PUBLISH_ALIAS,
        embed_mode="hash",
        allow_hash_mode=True,
    )
    client = QdrantClient(url=qdrant_url, timeout=10)
    try:
        live, _ = resolve_live_collection(client, settings)
        assert live is not None
        rules_v = extraction_rules_version()
        point_id = "00000000-0000-0000-0000-000000000392"
        legacy_doc_id = "SA22-7777-01"
        legacy_sha = "0" * 64

        # 1. Insert a corrupt sourceless legacy point: text in point will not match digest
        client.upsert(
            live,
            points=[
                models.PointStruct(
                    id=point_id,
                    vector={
                        "dense": [0.0] * HASH_EMBED_DIM,
                        "bm25": models.SparseVector(indices=[0], values=[1.0]),
                    },
                    payload={
                        "doc_id": legacy_doc_id,
                        "sha256": legacy_sha,
                        "rules_v": rules_v,
                        "text": "corrupt text",
                    },
                )
            ],
            wait=True,
        )

        # Record approved legacy with digest for expected text "original text"
        h_ids = hashlib.sha256(point_id.encode("utf-8") + b"\0").hexdigest()
        h_content = hashlib.sha256(
            point_id.encode("utf-8") + b"\0" + b"original text" + b"\0"
        ).hexdigest()

        inv = load_inventory(progress)
        template = next(iter(inv.values()))
        legacy_rec = template.model_copy(
            update={
                "path": f"legacy/{legacy_doc_id}.pdf",
                "doc_id": legacy_doc_id,
                "sha256": legacy_sha,
                "source_rev": None,
                "chunks": 1,
                "chunk_ids_digest": h_ids,
                "content_digest": h_content,
                "rules_version": rules_v,
            }
        )
        with open(progress, "a", encoding="utf-8") as f:
            f.write(legacy_rec.model_dump_json() + "\n")

        # Re-run publish: should refuse because content_digest does not match "corrupt text"
        previous = run_ingest._worker_qdrant
        if previous is not None:
            previous.close()
        monkeypatch.setattr(run_ingest, "_worker_qdrant", None)
        monkeypatch.setattr(run_ingest, "_worker_embedder", None)

        with pytest.raises(RuntimeError, match="fail content/digest verification"):
            run_ingest.main(["--src", str(local), "--progress", str(progress), "--workers", "1"])

        # 2. Repair the point so its payload text matches the approved content_digest
        client.upsert(
            live,
            points=[
                models.PointStruct(
                    id=point_id,
                    vector={
                        "dense": [0.0] * HASH_EMBED_DIM,
                        "bm25": models.SparseVector(indices=[0], values=[1.0]),
                    },
                    payload={
                        "doc_id": legacy_doc_id,
                        "sha256": legacy_sha,
                        "rules_v": rules_v,
                        "text": "original text",
                    },
                )
            ],
            wait=True,
        )

        previous = run_ingest._worker_qdrant
        if previous is not None:
            previous.close()
        monkeypatch.setattr(run_ingest, "_worker_qdrant", None)
        monkeypatch.setattr(run_ingest, "_worker_embedder", None)

        # Re-run publish: should succeed now that legacy point is verified
        assert (
            run_ingest.main(["--src", str(local), "--progress", str(progress), "--workers", "1"])
            == 0
        )
        live_after, _ = resolve_live_collection(client, settings)
        assert live_after == live
    finally:
        client.close()
        _drop_publish_fixture(qdrant_url)


def test_search_end_to_end_deterministic(qdrant_url, mock_url, corpus, tmp_path, monkeypatch):
    _ingest(monkeypatch, qdrant_url, "sim-hash", corpus, tmp_path / "inv.jsonl")
    with _agent(monkeypatch, qdrant_url, mock_url, "sim-hash") as client:
        body = client.post("/v1/search", json={"query": "IEA500I operator message"}).json()
        assert body["query_kind"] == "identifier"
        hits = body["hits"]
        assert hits, "message_ids filter must match the ingested message chunk"
        # The filter scopes to doc 1 only (doc 2 carries IEB700I) — every hit
        # must come from it. Do not pin top-1 across equal-text chunks; pin
        # the scoping, the presence of the message chunk, and determinism.
        assert all(h["cite"].startswith("SA22-0000-00 ") for h in hits)
        assert _MESSAGE_CITE in {h["cite"] for h in hits}
        assert "IEA500I" in hits[0]["message_ids"]

        again = client.post("/v1/search", json={"query": "IEA500I operator message"}).json()
        # request_id is per-request by design; the result set must be identical.
        assert again["query_kind"] == body["query_kind"]
        assert again["hits"] == body["hits"], (
            "retrieval must be deterministic for a fixed corpus+query"
        )

        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["qdrant"] is True


def test_answer_deterministic(qdrant_url, mock_url, corpus, tmp_path, monkeypatch):
    _ingest(monkeypatch, qdrant_url, "sim-hash", corpus, tmp_path / "inv.jsonl")
    with _agent(monkeypatch, qdrant_url, mock_url, "sim-hash") as client:
        search = client.post("/v1/search", json={"query": "IEA500I operator message"}).json()
        hit_cites = {h["cite"] for h in search["hits"]}

        body = client.post("/v1/answer", json={"query": "IEA500I operator message"}).json()
        assert body["citations"], "the mock echoes a retrieved cite -> validates"
        assert len(body["citations"]) == 1
        assert body["citations"][0] in hit_cites, "the echoed citation must be a retrieved hit"
        assert body["script"] is not None and "IOSCMDS LIST" in body["script"]
        assert body["answer"].startswith("Based on the retrieved excerpts")
        assert "Citations:" not in body["answer"]

        again = client.post("/v1/answer", json={"query": "IEA500I operator message"}).json()
        # request_id is per-request by design; answer/citations/script must be identical.
        stable = {k: v for k, v in body.items() if k != "request_id"}
        stable_again = {k: v for k, v in again.items() if k != "request_id"}
        assert stable_again == stable, "the answer path must be deterministic end to end"


def test_doc_id_filter_scopes_to_second_doc(qdrant_url, mock_url, corpus, tmp_path, monkeypatch):
    _ingest(monkeypatch, qdrant_url, "sim-hash", corpus, tmp_path / "inv.jsonl")
    with _agent(monkeypatch, qdrant_url, mock_url, "sim-hash") as client:
        body = client.post(
            "/v1/search", json={"query": "SA22-7777-01 initialization parameters"}
        ).json()
        assert body["query_kind"] == "identifier"
        hits = body["hits"]
        assert hits, "doc_id filter must match the second ingested document"
        assert {h["doc_id"] for h in hits} == {"SA22-7777-01"}
        assert all(
            "SA22-7777-01 Synthetic Initialization and Tuning Reference" in h["cite"] for h in hits
        )


def test_plain_doc_retrievable_by_stem_doc_id(qdrant_url, mock_url, corpus, tmp_path, monkeypatch):
    _ingest(monkeypatch, qdrant_url, "sim-hash", corpus, tmp_path / "inv.jsonl")
    with _agent(monkeypatch, qdrant_url, mock_url, "sim-hash") as client:
        body = client.post("/v1/search", json={"query": "widget torque buffer"}).json()
        assert body["query_kind"] == "nl"
        assert body["hits"], "lexical overlap must rank the plain doc"
        assert "widget-guide" in {h["doc_id"] for h in body["hits"]}


def test_eval_retrieval_on_synthetic_corpus(qdrant_url, mock_url, corpus, tmp_path, monkeypatch):
    """The eval harness (scripts/eval_retrieval.py) scores the real pipeline:
    identifier queries must be perfect on the synthetic corpus (filters
    guarantee them); the nl query must reach its doc within recall@5
    (membership, never top-1 across equal-text chunks). Baseline checking
    must produce zero regressions."""
    from scripts.eval_retrieval import (
        GoldenEntry,
        check_baseline,
        evaluate,
        update_baseline,
    )

    from mainframe_rag.config import load_settings

    _ingest(monkeypatch, qdrant_url, "sim-hash", corpus, tmp_path / "inv.jsonl")
    monkeypatch.setenv("QDRANT_URL", qdrant_url)
    monkeypatch.setenv("QDRANT_COLLECTION", "sim-hash")
    monkeypatch.setenv("EMBED_MODE", "hash")

    golden = [
        GoldenEntry(query="IEA500I operator message", expected_doc_ids=["SA22-0000-00"]),
        GoldenEntry(
            query="SA22-7777-01 initialization parameters", expected_doc_ids=["SA22-7777-01"]
        ),
        GoldenEntry(query="widget torque buffer", expected_doc_ids=["widget-guide"]),
    ]
    report = evaluate(golden, load_settings())
    assert report["failures"] == 0 and report["n"] == 3
    assert report["identifier"]["recall@1"] == 1.0
    assert report["identifier"]["mrr"] == 1.0
    assert report["nl"]["recall@5"] == 1.0

    # Baseline roundtrip & check
    baseline_path = tmp_path / "eval-baseline.json"
    update_baseline(report, baseline_path)
    import json

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert check_baseline(report, baseline) == []


def test_vllm_shaped_embed_variant(qdrant_url, mock_url, corpus, tmp_path, monkeypatch):
    """The prod embed path: dense over real HTTP to the mock vLLM endpoint,
    sparse via local fastembed BM25 (weights must already be cached)."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    cache = _bm25_cache_dir()

    records = _ingest(
        monkeypatch,
        qdrant_url,
        "sim-vllm",
        corpus,
        tmp_path / "inv.jsonl",
        embed="vllm",
        mock_url=mock_url,
        bm25_cache=cache,
    )
    assert [r["status"] for r in records] == ["upserted"] * 3

    with _agent(
        monkeypatch, qdrant_url, mock_url, "sim-vllm", embed="vllm", bm25_cache=cache
    ) as client:
        body = client.post("/v1/search", json={"query": "IEA500I operator message"}).json()
        assert body["hits"], "vLLM-shaped embeds must retrieve the ingested message chunk"
        # Doc 2 carries IEB700I, so the message_ids filter scopes to doc 1.
        assert all(h["cite"].startswith("SA22-0000-00 ") for h in body["hits"])
        assert _MESSAGE_CITE in {h["cite"] for h in body["hits"]}

        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {
            "status": "ok",
            "qdrant": True,
            "embed": True,
            "representation": "compatible",
        }


def test_361_inplace_snapshot_restore_keeps_coexisting_revisions(qdrant_url, tmp_path, monkeypatch):
    """Issue #361 closure against the real server: two same-stem editions
    ingested as separate corpora (the joint walk still aborts — unit-tested)
    coexist under one printed doc_id; a snapshot restores the generation
    after the collection is lost, with both revisions, server-side
    product/version scoping, and completion re-verification intact."""
    import pymupdf
    from qdrant_client import QdrantClient, models
    from scripts.make_synthetic_pdf import build_plain

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest.qdrant_io import snapshot_collection, stored_doc_revisions

    collection = "sim-361"
    client = QdrantClient(url=qdrant_url, timeout=30)
    try:
        for name in (collection, f"{collection}__completions"):
            if client.collection_exists(name):
                client.delete_collection(name)
        # One stem, different bytes: the issue's overwrite pair.
        corp_a = tmp_path / "corp-a"
        corp_b = tmp_path / "corp-b"
        corp_a.mkdir()
        corp_b.mkdir()
        build_plain(corp_a / "reference.pdf")
        build_plain(corp_b / "reference.pdf")
        doc = pymupdf.open(corp_b / "reference.pdf")
        page = doc.new_page()
        page.insert_text(
            (72, 72),
            "Edition 2 supplement\nRevised torque tables for the Mk II controller.",
            fontsize=11,
        )
        staged = corp_b / "reference.staged.pdf"
        doc.save(staged, garbage=4)
        doc.close()
        staged.replace(corp_b / "reference.pdf")

        rec_a = _ingest(
            monkeypatch,
            qdrant_url,
            collection,
            corp_a,
            tmp_path / "inv-a.jsonl",
            extra=("--vendor", "vendor-a", "--product", "product-x", "--version", "1.0"),
        )
        rec_b = _ingest(
            monkeypatch,
            qdrant_url,
            collection,
            corp_b,
            tmp_path / "inv-b.jsonl",
            extra=("--vendor", "vendor-b", "--product", "product-y", "--version", "2.0"),
        )
        assert [r["status"] for r in rec_a] == ["upserted"]
        assert [r["status"] for r in rec_b] == ["upserted"]
        assert rec_a[0]["doc_id"] == rec_b[0]["doc_id"] == "reference"
        assert rec_a[0]["source_rev"] != rec_b[0]["source_rev"]

        settings = Settings(
            _env_file=None,
            qdrant_url=qdrant_url,
            qdrant_collection=collection,
            embed_mode="hash",
            allow_hash_mode=True,
        )
        revs = stored_doc_revisions(client, settings, "reference")
        assert revs == {rec_a[0]["source_rev"], rec_b[0]["source_rev"]}
        before = client.get_collection(collection).points_count
        assert before == rec_a[0]["chunks"] + rec_b[0]["chunks"] > 0

        # Server-side revision scoping (the real keyword filter, not a fake).
        def _count(**matches):
            flt = models.Filter(
                must=[
                    models.FieldCondition(key=k, match=models.MatchValue(value=v))
                    for k, v in matches.items()
                ]
            )
            pts, _ = client.scroll(collection, scroll_filter=flt, limit=100)
            return pts

        got_a = _count(doc_id="reference", product="product-x", version="1.0")
        got_b = _count(doc_id="reference", product="product-y", version="2.0")
        assert len(got_a) == rec_a[0]["chunks"] and len(got_b) == rec_b[0]["chunks"]
        assert {p.payload["source_rev"] for p in got_a} == {rec_a[0]["source_rev"]}
        assert {p.payload["source_rev"] for p in got_b} == {rec_b[0]["source_rev"]}

        # Snapshot (the 361B runbook step 1), then lose the collection.
        snap = snapshot_collection(client, collection)
        assert snap
        dl = httpx2.get(f"{qdrant_url}/collections/{collection}/snapshots/{snap}", timeout=120.0)
        assert dl.status_code == 200 and len(dl.content) > 0
        client.delete_collection(collection)
        assert client.collection_exists(collection) is False

        # Operator rollback: restore the snapshot, then re-verify.
        up = httpx2.post(
            f"{qdrant_url}/collections/{collection}/snapshots/upload?priority=snapshot",
            files={"snapshot": (snap, dl.content)},
            timeout=180.0,
        )
        assert up.status_code == 200, up.text[:500]
        assert client.get_collection(collection).points_count == before
        assert stored_doc_revisions(client, settings, "reference") == revs

        # Completions survived (separate collection, never deleted): a resume
        # with fresh inventory re-verifies instead of re-ingesting.
        resume_a = _ingest(
            monkeypatch,
            qdrant_url,
            collection,
            corp_a,
            tmp_path / "resume-a.jsonl",
            extra=("--vendor", "vendor-a", "--product", "product-x", "--version", "1.0"),
        )
        resume_b = _ingest(
            monkeypatch,
            qdrant_url,
            collection,
            corp_b,
            tmp_path / "resume-b.jsonl",
            extra=("--vendor", "vendor-b", "--product", "product-y", "--version", "2.0"),
        )
        assert [r["status"] for r in resume_a] == ["skipped"]
        assert [r["status"] for r in resume_b] == ["skipped"]
    finally:
        client.close()


def test_revision_scoped_retirement_on_real_server(qdrant_url, tmp_path, monkeypatch):
    """R-REV (issue #391): explicit retirement of revision A on a real Qdrant
    server preserves sibling revision B across publication cutover."""
    from qdrant_client import QdrantClient
    from qdrant_client.http import models
    from scripts.make_synthetic_pdf import build as make_pdf
    from scripts.make_synthetic_pdf import build_plain

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest.completion import completion_collection_for
    from mainframe_rag.ingest.qdrant_io import stored_doc_revisions

    alias = "sim-pub-rev"
    monkeypatch.setenv("INGEST_ALIAS_PUBLISH", "true")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")

    client = QdrantClient(qdrant_url, timeout=30.0)
    try:
        # Clean existing test collections/aliases
        for a in client.get_aliases().aliases:
            if a.alias_name == alias:
                client.update_collection_aliases(
                    change_aliases_operations=[
                        models.DeleteAliasOperation(
                            delete_alias=models.DeleteAlias(alias_name=alias)
                        )
                    ]
                )
        for c in client.get_collections().collections:
            if c.name.startswith(alias):
                client.delete_collection(c.name)

        corp = tmp_path / "corp"
        corp.mkdir()
        a_pdf = corp / "doc_a.pdf"
        make_pdf(a_pdf, doc_id="doc_a", title="Doc A Revision 1")
        build_plain(corp / "doc_b.pdf")

        progress_1 = tmp_path / "inv1.jsonl"
        rec_1 = _ingest(
            monkeypatch,
            qdrant_url,
            alias,
            corp,
            progress_1,
            extra=("--vendor", "v", "--product", "p", "--version", "1.0"),
        )
        assert [r["status"] for r in rec_1] == ["upserted", "upserted"]
        rev_1 = next(r["source_rev"] for r in rec_1 if r["doc_id"] == "doc_a")
        assert rev_1

        active_alias_1 = next(
            (a.collection_name for a in client.get_aliases().aliases if a.alias_name == alias),
            None,
        )
        assert active_alias_1 is not None

        # S424-F1: Seed an approved, digest-valid legacy sibling point L for doc_a on real server
        import hashlib

        from mainframe_rag.config import HASH_EMBED_DIM
        from mainframe_rag.ingest.inventory import InventoryRecord
        from mainframe_rag.ingest.rules_version import extraction_rules_version

        rules_v = extraction_rules_version()
        legacy_pt_id = "00000000-0000-0000-0000-000000000777"
        legacy_text = "legacy real qdrant content for doc_a"
        client.upsert(
            active_alias_1,
            points=[
                models.PointStruct(
                    id=legacy_pt_id,
                    vector={
                        "dense": [0.0] * HASH_EMBED_DIM,
                        "bm25": models.SparseVector(indices=[0], values=[1.0]),
                    },
                    payload={
                        "doc_id": "doc_a",
                        "sha256": "7" * 64,
                        "rules_v": rules_v,
                        "text": legacy_text,
                    },
                )
            ],
            wait=True,
        )
        h_ids = hashlib.sha256(legacy_pt_id.encode("utf-8") + b"\0").hexdigest()
        h_content = hashlib.sha256(
            legacy_pt_id.encode("utf-8") + b"\0" + legacy_text.encode("utf-8") + b"\0"
        ).hexdigest()
        leg_rec = InventoryRecord(
            path="legacy/doc_a.pdf",
            sha256="7" * 64,
            doc_id="doc_a",
            pages=1,
            chunks=1,
            seconds=0.0,
            rules_version=rules_v,
            chunk_ids_digest=h_ids,
            content_digest=h_content,
            source_rev=None,
            status="upserted",
        )
        with open(progress_1, "a", encoding="utf-8") as f:
            f.write(leg_rec.model_dump_json() + "\n")

        # Replace doc_a with Revision 2
        make_pdf(a_pdf, doc_id="doc_a", title="Doc A Revision 2 (New Content)")
        progress_2 = tmp_path / "inv2.jsonl"
        # Copy inv1 forward so prior inventory contains rev_1 and approved legacy L
        progress_2.write_text(progress_1.read_text())

        rec_2 = _ingest(
            monkeypatch,
            qdrant_url,
            alias,
            corp,
            progress_2,
            extra=(
                "--vendor",
                "v",
                "--product",
                "p",
                "--version",
                "2.0",
                "--retire-doc",
                f"doc_a@{rev_1}",
            ),
        )
        rev_2 = [r["source_rev"] for r in rec_2 if r["doc_id"] == "doc_a"][-1]
        assert rev_2 != rev_1

        # Check aliases on real server
        active_alias = next(
            (a.collection_name for a in client.get_aliases().aliases if a.alias_name == alias),
            None,
        )
        assert active_alias is not None

        # Verify live collection contains doc_a@rev_2, approved legacy L, and doc_b, but NOT doc_a@rev_1
        settings = Settings(
            _env_file=None,
            qdrant_url=qdrant_url,
            qdrant_collection=active_alias,
            embed_mode="hash",
            allow_hash_mode=True,
        )
        revs_live = stored_doc_revisions(client, settings, "doc_a")
        assert revs_live == {rev_2, None}

        # Assert exact retained legacy point ID and text on real server
        pts_leg = client.retrieve(active_alias, ids=[legacy_pt_id], with_payload=True)
        assert len(pts_leg) == 1
        assert pts_leg[0].payload.get("text") == legacy_text

        # Check completions collection
        completions_name = completion_collection_for(active_alias)
        pts, _ = client.scroll(
            completions_name,
            scroll_filter=models.Filter(
                must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value="doc_a"))]
            ),
        )
        stored_comp_revs = {(p.payload or {}).get("source_rev") for p in pts}
        assert stored_comp_revs == {rev_2}

        # Subsequent ordinary run on real server: steady-state recognizes both L and rev_2 without re-embedding
        from mainframe_rag.ingest import run_ingest

        assert (
            run_ingest.main(
                [
                    "--src",
                    str(corp),
                    "--progress",
                    str(progress_2),
                    "--workers",
                    "1",
                    "--vendor",
                    "v",
                    "--product",
                    "p",
                    "--version",
                    "2.0",
                ]
            )
            == 0
        )
        revs_live_steady = stored_doc_revisions(client, settings, "doc_a")
        assert revs_live_steady == {rev_2, None}
    finally:
        client.close()


def test_forced_migration_retry_retains_verified_document_on_real_qdrant(qdrant_url, tmp_path, monkeypatch):
    """An interrupted migration keeps actual completed payloads/vectors on retry."""
    from concurrent.futures import ThreadPoolExecutor

    from qdrant_client import QdrantClient, models
    from scripts.make_synthetic_pdf import build

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import publish_state_path

    alias = "sim-resume-forced"
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("INGEST_ALIAS_PUBLISH", "true")
    monkeypatch.setenv("INGEST_UPSERT_STREAMS", "1")
    monkeypatch.setattr(run_ingest, "ProcessPoolExecutor", lambda max_workers, mp_context: ThreadPoolExecutor(max_workers=max_workers))
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    build(corpus / "a.pdf", doc_id="SA22-7000-00")
    build(corpus / "b.pdf", doc_id="SA22-7000-01")
    progress = tmp_path / "inventory.jsonl"
    _ingest(monkeypatch, qdrant_url, alias, corpus, progress)
    client = QdrantClient(url=qdrant_url, timeout=30)
    try:
        live = next(a.collection_name for a in client.get_aliases().aliases if a.alias_name == alias)
        monkeypatch.setenv("EMBED_MODEL_REVISION", "resume-synthetic-revision")
        real_upsert = run_ingest._upsert_one

        def fail_second(doc, *args, **kwargs):
            if doc.doc_id == "SA22-7000-01":
                raise RuntimeError("synthetic interrupted document")
            return real_upsert(doc, *args, **kwargs)

        monkeypatch.setattr(run_ingest, "_upsert_one", fail_second)
        argv = ["--src", str(corpus), "--progress", str(progress), "--workers", "1", "--reingest"]
        assert run_ingest.main(argv) == 1
        staging = json.loads(publish_state_path(progress, alias).read_text())["staging"]
        assert next(a.collection_name for a in client.get_aliases().aliases if a.alias_name == alias) == live
        filter_a = models.Filter(must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value="SA22-7000-00"))])

        def stored_a():
            points, offset = client.scroll(staging, scroll_filter=filter_a, limit=100, with_payload=True, with_vectors=True)
            assert points and offset is None
            return [p.model_dump() for p in points]

        before = stored_a()
        real_parse = run_ingest._parse_one
        parsed = []

        def observed_parse(args):
            parsed.append(Path(args[0]).name)
            return real_parse(args)

        monkeypatch.setattr(run_ingest, "_parse_one", observed_parse)
        monkeypatch.setattr(run_ingest, "_upsert_one", real_upsert)
        assert run_ingest.main(argv) == 0
        assert parsed == ["b.pdf"]
        assert stored_a() == before
        assert next(a.collection_name for a in client.get_aliases().aliases if a.alias_name == alias) == staging
        parsed.clear()
        assert run_ingest.main(argv[:-1]) == 0
        assert parsed == []
    finally:
        aliases = [a for a in client.get_aliases().aliases if a.alias_name == alias]
        if aliases:
            client.update_collection_aliases([models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias))])
        for collection in client.get_collections().collections:
            if collection.name.startswith(alias + "__gen"):
                client.delete_collection(collection.name)
        client.close()


@pytest.mark.parametrize(
    ("operations", "fault"),
    [
        (("repair", "retire", "change"), "before"),
        (("change", "repair", "retire"), "after"),
    ],
)
def test_bounded_publication_trace_on_real_server(
    qdrant_url, tmp_path, monkeypatch, operations, fault
):
    """Same literal oracle as the unit traces, with actual clone/alias storage."""
    from qdrant_client import QdrantClient

    from tests.helpers_publication_lifecycle import exercise_publication_trace

    _drop_publish_fixture(qdrant_url)
    client = QdrantClient(url=qdrant_url, timeout=30)
    try:
        exercise_publication_trace(
            tmp_path, monkeypatch, client, operations, fault, PUBLISH_ALIAS
        )
    finally:
        client.close()
        _drop_publish_fixture(qdrant_url)


def test_post_cutover_missing_control_preserves_retry_on_real_server(
    qdrant_url, tmp_path, monkeypatch
):
    """Invalid live controls must not consume an interrupted retirement's recovery state."""
    from qdrant_client import QdrantClient, models

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.inventory import load_inventory
    from mainframe_rag.ingest.publish import publish_state_path
    from mainframe_rag.ingest.representation import manifest_point_id
    from tests.helpers_publication_lifecycle import TEXTS, _records, _write_source

    _drop_publish_fixture(qdrant_url)
    client = QdrantClient(url=qdrant_url, timeout=30)
    alias = PUBLISH_ALIAS
    corpus = tmp_path / "finalization-corpus"
    corpus.mkdir()
    progress = tmp_path / "finalization.jsonl"
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("INGEST_ALIAS_PUBLISH", "true")
    monkeypatch.setenv("QDRANT_COLLECTION", alias)
    monkeypatch.setenv("DENSE_DIM", "256")
    monkeypatch.setenv("EMBED_MODEL_REVISION", "")
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: client)

    def run(*extra):
        return run_ingest.main([
            "--src", str(corpus), "--progress", str(progress), "--workers", "1", *extra,
        ])

    def target():
        return next(a.collection_name for a in client.get_aliases().aliases if a.alias_name == alias)

    try:
        for name, text in TEXTS.items():
            _write_source(corpus, name, text)
        assert run() == 0
        old = target()
        retained = (_records(client, old), _records(client, old + "__completions"))
        (corpus / "beta.pdf").unlink()
        original_swap = run_ingest.swap_alias_to

        def interrupted(*args, **kwargs):
            original_swap(*args, **kwargs)
            raise RuntimeError("cutover completed before interruption")

        with monkeypatch.context() as patch:
            patch.setattr(run_ingest, "swap_alias_to", interrupted)
            with pytest.raises(RuntimeError, match="cutover completed before interruption"):
                run("--retire-doc", "beta")
        current = target()
        assert current != old
        controls = current + "__completions"
        state = publish_state_path(progress, alias)
        state_bytes, progress_bytes = state.read_bytes(), progress.read_bytes()
        published = (_records(client, current), _records(client, controls))
        manifest = client.retrieve(
            controls, [manifest_point_id(controls)], with_payload=True, with_vectors=True
        )[0]
        client.delete(
            controls, points_selector=models.PointIdsList(points=[manifest.id]), wait=True
        )
        with pytest.raises(RuntimeError, match="predates the representation manifest"):
            run("--retire-doc", "beta")
        assert state.read_bytes() == state_bytes
        assert progress.read_bytes() == progress_bytes
        assert target() == current
        assert _records(client, current) == published[0]

        # Explicit fixture recovery restores exact saved control bytes/vectors.
        # Production never repairs corrupt live controls merely to pass a gate.
        client.upsert(
            controls,
            points=[models.PointStruct(id=manifest.id, payload=manifest.payload, vector=manifest.vector)],
            wait=True,
        )
        assert run("--retire-doc", "beta") == 0
        assert not state.exists()
        assert next(r for r in load_inventory(progress).values() if r.doc_id == "beta").status == "retired"
        for _ in range(2):
            assert run() == 0
            assert target() == current
            assert (_records(client, current), _records(client, controls)) == published
            assert (_records(client, old), _records(client, old + "__completions")) == retained
        assert [
            (payload["doc_id"], payload["text"]) for payload, _ in _records(client, current).values()
        ] == [("alpha", TEXTS["alpha"])]
    finally:
        client.close()
        _drop_publish_fixture(qdrant_url)


def test_corrupt_retirement_record_refuses_before_real_deletion(qdrant_url, tmp_path, monkeypatch):
    from qdrant_client import QdrantClient

    from tests.test_ingest_publish import _exercise_corrupt_retirement_retry

    _drop_publish_fixture(qdrant_url)
    client = QdrantClient(url=qdrant_url, timeout=30)
    try:
        _exercise_corrupt_retirement_retry(tmp_path, monkeypatch, client, PUBLISH_ALIAS)
    finally:
        client.close()
        _drop_publish_fixture(qdrant_url)
