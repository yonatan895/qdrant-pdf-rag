"""Dry-run ingest end-to-end on the synthetic fixture: parse, chunk, inventory.

No Qdrant, no embeddings (air-gap Job does that; CI uses --dry-run).
"""

import json

from mainframe_rag.ingest.run_ingest import main


def test_dry_run_ingest(tmp_path, synthetic_pdf, capsys):
    progress = tmp_path / "inventory.jsonl"
    rc = main(["--src", str(synthetic_pdf.parent), "--progress", str(progress),
               "--workers", "1", "--dry-run"])
    assert rc == 0
    records = [json.loads(l) for l in progress.read_text().splitlines() if l.strip()]
    assert len(records) == 1
    rec = records[0]
    assert rec["doc_id"] == "SA22-0000-00"
    assert rec["pages"] == 8
    assert rec["chunks"] > 0
    assert rec["status"] == "dry"


def test_dry_run_skips_already_processed(tmp_path, synthetic_pdf):
    progress = tmp_path / "inventory.jsonl"
    args = ["--src", str(synthetic_pdf.parent), "--progress", str(progress),
            "--workers", "1", "--dry-run"]
    assert main(args) == 0
    assert main(args) == 0  # second run: same sha256 -> skipped, no new records
    records = [json.loads(l) for l in progress.read_text().splitlines() if l.strip()]
    assert len(records) == 1


def test_resolve_workers_is_a_cap(tmp_path):
    from mainframe_rag.config import Settings
    from mainframe_rag.ingest.run_ingest import resolve_workers

    settings = Settings(_env_file=None)
    assert resolve_workers(None, settings) >= 1
    assert resolve_workers(0, settings) == 1
    assert resolve_workers(-5, settings) == 1
    assert resolve_workers(10_000, settings) <= 2 * (__import__("os").cpu_count() or 2)


def test_corrupt_pdf_writes_typed_error_record(tmp_path, synthetic_pdf):
    """One bad PDF must not kill the run; the error record carries the
    exception type for triage."""
    import json

    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"this is not a pdf at all")
    progress = tmp_path / "inventory.jsonl"
    rc = main(["--src", str(tmp_path), "--progress", str(progress),
               "--workers", "1", "--dry-run"])
    assert rc == 1
    records = [json.loads(l) for l in progress.read_text().splitlines() if l.strip()]
    err = [r for r in records if r["status"] == "error"]
    assert err and err[0]["path"] == str(bad)
    assert err[0]["error_type"]


def _stderr_json(capsys) -> list[dict]:
    out = capsys.readouterr().err
    lines = []
    for line in out.splitlines():
        if line.strip():
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                lines.append(parsed)
    return lines


def test_summary_counters_ok_run(tmp_path, synthetic_pdf, capsys):
    """PR D: files ok / failed / chunks upserted, one summary line per run."""
    progress = tmp_path / "inventory.jsonl"
    rc = main(["--src", str(synthetic_pdf.parent), "--progress", str(progress),
               "--workers", "1", "--dry-run"])
    assert rc == 0
    # Second run: the file is skipped — still an ok outcome. (One capsys
    # read at the end: the StreamHandler binds stderr at creation.)
    rc = main(["--src", str(synthetic_pdf.parent), "--progress", str(progress),
               "--workers", "1", "--dry-run"])
    assert rc == 0
    done = [l for l in _stderr_json(capsys) if l.get("action") == "done"]
    assert len(done) == 2
    for summary in done:
        assert summary["files_ok"] == 1
        assert summary["files_failed"] == 0
        assert summary["chunks_upserted"] == 0  # dry run: parsed, not upserted
        assert summary["elapsed_ms"] >= 0


def test_summary_counters_failed_run(tmp_path, synthetic_pdf, capsys):
    (tmp_path / "corrupt.pdf").write_bytes(b"not a pdf")
    progress = tmp_path / "inventory.jsonl"
    rc = main(["--src", str(tmp_path), "--progress", str(progress),
               "--workers", "1", "--dry-run"])
    assert rc == 1
    done = [l for l in _stderr_json(capsys) if l.get("action") == "done"]
    assert len(done) == 1
    assert done[0]["files_failed"] == 1


def _filter_match_value(filter_obj, key: str) -> str | None:
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


def _filter_doc_id(filter_obj) -> str | None:
    """Extract a doc_id equality value from a Filter/FilterSelector shape."""
    return _filter_match_value(filter_obj, "doc_id")


class _FakeQdrant:
    """QdrantPoints double for the parent-side run() path (review round on
    PR D: the non-dry upsert branch had no coverage).

    Stores points per collection so the verified-completion contract
    (issue #359) exercises the claimed path: skips require a written
    completion plus verifiable points, never a single sampled marker."""

    def __init__(self, stored_sha: str | None = None, stored_rules_v: str | None = None,
                 sample_rules_v: str | None = None):
        from mainframe_rag.ingest.rules_version import extraction_rules_version

        self.stored_sha = stored_sha
        # A stored collection defaults to the CURRENT rules so existing
        # skip tests model a matching collection; tests pass an explicit
        # value (including "" for a legacy pre-versioning collection) to
        # exercise the extraction-rules gate (issue #124). sample_rules_v
        # models a MIXED collection (startup sample reads current, but an
        # individual doc carries an older/unstamped rules_v) — the state
        # the live re-stamp run exposed.
        self.stored_rules_v = (
            extraction_rules_version() if stored_rules_v is None else stored_rules_v
        )
        self.sample_rules_v = (
            self.stored_rules_v if sample_rules_v is None else sample_rules_v
        )
        self.upserts: list[int] = []
        self.upsert_calls: list[tuple[str, int]] = []
        self.deletes = 0
        self._points: dict[str, list] = {}
        self.created_collections: list[str] = []

    def get_aliases(self):
        from types import SimpleNamespace

        return SimpleNamespace(aliases=[])

    def collection_exists(self, collection_name):
        return True

    def get_collection(self, collection_name):
        from types import SimpleNamespace

        from mainframe_rag.config import HASH_EMBED_DIM

        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(vectors={"dense": SimpleNamespace(size=HASH_EMBED_DIM)})
            )
        )

    def create_collection(self, collection_name, **kwargs):
        self.created_collections.append(collection_name)
        return True

    def create_payload_index(self, *a, **k):
        from types import SimpleNamespace

        return SimpleNamespace()

    def update_collection(self, collection_name, *, optimizer_config=None):
        return True

    def scroll(self, collection_name, *, scroll_filter=None, limit=1, with_payload=None, offset=None):
        from types import SimpleNamespace

        if with_payload == ["rules_v"] and _filter_doc_id(scroll_filter) is None:
            # Startup rules gate sample (issue #124): empty collection vs
            # versioned points. Doc-level reads below serve stored points.
            if self.stored_sha is None and not self._points.get(collection_name):
                return [], None
            if self._points.get(collection_name):
                return [self._points[collection_name][0]], None
            return [SimpleNamespace(payload={"rules_v": self.sample_rules_v})], None
        # Doc-level reads serve stored points only — the seeded sampling
        # marker above never leaks into a doc_id-filtered read (issue #361:
        # revision listing and marker reads must observe, never assume).
        doc_id = _filter_doc_id(scroll_filter)
        rev = _filter_match_value(scroll_filter, "source_rev")
        stored = self._points.get(collection_name, [])
        if doc_id is not None:
            stored = [p for p in stored if (p.payload or {}).get("doc_id") == doc_id]
        if rev is not None:
            stored = [p for p in stored if (p.payload or {}).get("source_rev") == rev]
        return stored[:limit], None

    def upsert(self, collection_name, *, points, wait=True):
        self.upserts.append(len(points))
        self.upsert_calls.append((collection_name, len(points)))
        # Production upsert overwrites same-id points (manifest recommit,
        # marker rewrite); the double must too, or reads see stale firsts.
        stored = self._points.setdefault(collection_name, [])
        ids = {str(p.id) for p in points}
        stored[:] = [p for p in stored if str(p.id) not in ids]
        stored.extend(points)
        from types import SimpleNamespace

        return SimpleNamespace()

    def retrieve(self, collection_name, ids, *, with_payload=True, with_vectors=False):
        from types import SimpleNamespace

        wanted = {str(i) for i in ids}
        return [
            SimpleNamespace(id=p.id, payload=p.payload)
            for p in self._points.get(collection_name, [])
            if str(p.id) in wanted
        ]

    def delete(self, collection_name, *, points_selector, wait=True):
        from types import SimpleNamespace

        self.deletes += 1
        ids = getattr(points_selector, "points", None)
        if ids is not None:
            # PointIdsList (precise completion invalidation, issue #361).
            # Seeded sampling markers carry no id and never match.
            wanted = {str(i) for i in ids}
            self._points[collection_name] = [
                p for p in self._points.get(collection_name, [])
                if str(getattr(p, "id", None)) not in wanted
            ]
            return SimpleNamespace()
        doc_id = _filter_doc_id(points_selector)
        rev = _filter_match_value(points_selector, "source_rev")
        if doc_id is None and rev is None:
            self._points[collection_name] = []
        else:
            self._points[collection_name] = [
                p for p in self._points.get(collection_name, [])
                if not (
                    (doc_id is None or (p.payload or {}).get("doc_id") == doc_id)
                    and (rev is None or (p.payload or {}).get("source_rev") == rev)
                )
            ]
        return SimpleNamespace()


def test_upsert_path_counters_with_fake_qdrant(tmp_path, synthetic_pdf, capsys, monkeypatch):
    """Non-dry run with a fake port: files_ok counts the upsert and
    chunks_upserted matches the points actually sent."""
    from mainframe_rag.ingest import run_ingest

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    progress = tmp_path / "inventory.jsonl"
    rc = main(["--src", str(synthetic_pdf.parent), "--progress", str(progress), "--workers", "1"])
    assert rc == 0
    done = [l for l in _stderr_json(capsys) if l.get("action") == "done"]
    assert done[0]["files_ok"] == 1
    assert done[0]["chunks_upserted"] > 0
    from mainframe_rag.config import Settings as _Settings

    main_collection = _Settings(_env_file=None).qdrant_collection
    main_sent = sum(n for collection, n in fake.upsert_calls if collection == main_collection)
    assert main_sent == done[0]["chunks_upserted"]


def test_qdrant_level_skip_counts_as_ok(tmp_path, synthetic_pdf, capsys, monkeypatch):
    """Qdrant already holds a VERIFIED completion for this generation (second
    run, fresh inventory, warm Qdrant): files_ok counts it, nothing is
    upserted or deleted. A bare sampled point without a completion must
    never skip (issue #359) — that case is pinned in
    test_ingest_completion.py."""
    from mainframe_rag.ingest import run_ingest

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    progress = tmp_path / "inventory.jsonl"
    assert main(["--src", str(synthetic_pdf.parent), "--progress", str(progress), "--workers", "1"]) == 0
    first_upserts = list(fake.upserts)
    assert first_upserts, "first run must publish points + completion"
    fake.upserts.clear()
    deletes_after_first = fake.deletes
    # Fresh inventory, warm Qdrant: parse again, verify completion, skip.
    progress.unlink()
    rc = main(["--src", str(synthetic_pdf.parent), "--progress", str(progress), "--workers", "1"])
    assert rc == 0
    done = [l for l in _stderr_json(capsys) if l.get("action") == "done"]
    assert done[-1]["files_ok"] == 1
    assert done[-1]["chunks_upserted"] == 0
    assert fake.upserts == [] and fake.deletes == deletes_after_first


def test_embed_failfast_upserts_nothing(tmp_path, synthetic_pdf, monkeypatch):
    """Embedding happens in the parse workers now, so an embed failure fails
    the whole task — the upsert stage never sees the doc. Exercised through
    the real runtime path: EMBED_MODE=vllm without EMBED_BASE_URL fails fast
    in the worker on first use (no monkeypatching of spawn workers)."""
    from mainframe_rag.ingest import run_ingest

    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    monkeypatch.delenv("EMBED_BASE_URL", raising=False)
    monkeypatch.delenv("EMBED_MODEL", raising=False)
    monkeypatch.setenv("EMBED_MODE", "vllm")
    monkeypatch.setenv("EMBED_MODEL_REVISION", "test-rev")
    monkeypatch.setenv("DENSE_DIM", "256")  # collection setup passes; the
    # worker's embed call then fails fast on the missing EMBED_BASE_URL
    progress = tmp_path / "inventory.jsonl"
    rc = main(["--src", str(synthetic_pdf.parent), "--progress", str(progress), "--workers", "1"])
    assert rc == 1
    # The run-level representation manifest still commits (contract, not
    # doc data); what must never happen is a doc-point upsert.
    from mainframe_rag.config import Settings as _Settings

    main_collection = _Settings(_env_file=None).qdrant_collection
    assert all(c != main_collection for c, _ in fake.upsert_calls), \
        "a doc whose embed failed must never be upserted"
    records = [json.loads(l) for l in progress.read_text().splitlines() if l.strip()]
    assert records and all(r["status"] == "error" for r in records)
    assert all(r["error_type"] == "RuntimeError" for r in records)


def test_doc_locks_are_per_doc_id():
    """Colliding doc_ids (shared form numbers) must serialize their
    check-delete-upsert sequence; distinct doc_ids must not contend."""
    import threading
    import time

    from mainframe_rag.ingest.ibm_pdf import ParsedDoc
    from mainframe_rag.ingest.run_ingest import _DocLocks

    locks = _DocLocks()
    assert locks.get("SC14-7315-70") is locks.get("SC14-7315-70")
    assert locks.get("SC23-6845-22") is not locks.get("SC14-7315-70")

    # Concurrency test: same doc_id cannot execute critical section concurrently
    active = 0
    max_active = 0
    counter_lock = threading.Lock()
    barrier = threading.Barrier(2)

    parsed = ParsedDoc(
        path="manual.pdf",
        doc_id="SC14-7315-70",
        sha256="abc",
        vendor="ibm",
        product=None,
        version=None,
        title="Title",
        page_count=1,
    )

    def run_worker():
        nonlocal active, max_active
        barrier.wait()
        with locks.get(parsed.doc_id):
            with counter_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with counter_lock:
                active -= 1

    t1 = threading.Thread(target=run_worker)
    t2 = threading.Thread(target=run_worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert max_active == 1, "threads with the same doc_id must not execute critical section simultaneously"


def test_worker_embed_batches_by_batch_size(synthetic_pdf, monkeypatch):
    """_parse_one must slice chunks by settings.batch_size rather than
    passing all chunks to embed_batch in a single call."""
    from mainframe_rag.config import Settings
    from mainframe_rag.ingest import run_ingest

    embed_calls: list[int] = []
    original_embed_batch = run_ingest.embed_batch

    def mock_embed_batch(chunks, product, version, title, embedder, contexts=None):
        embed_calls.append(len(chunks))
        return original_embed_batch(chunks, product, version, title, embedder, contexts)

    # batch_size min validator is 16; use Settings instance with batch_size=16 or mock chunks
    test_settings = Settings(batch_size=16, embed_mode="hash")
    # To verify slicing, override batch_size attribute to 2 for this test
    object.__setattr__(test_settings, "batch_size", 2)
    monkeypatch.setattr(run_ingest, "_load_worker_settings", lambda: test_settings)
    monkeypatch.setattr(run_ingest, "embed_batch", mock_embed_batch)

    task = (str(synthetic_pdf), None, None, None, str(synthetic_pdf.parent), "dummy_sha", True, None)
    _rec, _parsed, chunks, vectors, contexts = run_ingest._parse_one(task)
    assert len(chunks) > 2
    assert contexts == {}
    assert len(chunks) > 2
    assert all(c <= 2 for c in embed_calls), f"every embed call must be <= batch_size=2: {embed_calls}"
    assert sum(embed_calls) == len(chunks)
    assert len(vectors) == len(chunks)



def test_bulk_load_disables_and_restores_indexing(tmp_path, synthetic_pdf, monkeypatch):
    """INGEST_BULK_LOAD raises the indexing threshold before the first upsert
    and restores the server default after the last one (Qdrant skill:
    HNSW builds must not compete with a bulk load). Default off."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.qdrant_io import (
        BULK_INDEXING_THRESHOLD_KB,
        DEFAULT_INDEXING_THRESHOLD_KB,
    )

    fake = _FakeQdrant()
    fake.events: list[tuple[str, object]] = []
    original_upsert = fake.upsert

    def upsert(collection_name, *, points, wait=True):
        fake.events.append(("upsert", len(points)))
        return original_upsert(collection_name, points=points, wait=wait)

    def update_collection(collection_name, *, optimizer_config):
        fake.events.append(("indexing_threshold", optimizer_config.indexing_threshold))
        return True

    fake.upsert = upsert
    fake.update_collection = update_collection
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    monkeypatch.setenv("INGEST_BULK_LOAD", "true")
    progress = tmp_path / "inventory.jsonl"
    rc = main(["--src", str(synthetic_pdf.parent), "--progress", str(progress), "--workers", "1"])
    assert rc == 0
    thresholds = [v for kind, v in fake.events if kind == "indexing_threshold"]
    assert thresholds[0] == BULK_INDEXING_THRESHOLD_KB, "indexing disabled before the load"
    assert thresholds[-1] == DEFAULT_INDEXING_THRESHOLD_KB, "restored after the load"
    upsert_idx = [i for i, (kind, _) in enumerate(fake.events) if kind == "upsert"]
    assert upsert_idx, "the run must have upserted points"
    assert upsert_idx[0] > 0 and upsert_idx[-1] < len(fake.events) - 1, (
        "restore happens strictly after all upserts"
    )


def test_parse_one_error_isolation_and_picklability(tmp_path):
    """Ensure worker errors are converted to plain data and round-trip through
    multiprocessing pickling across process boundaries without crashing."""
    import pickle

    from mainframe_rag.ingest.run_ingest import _parse_one

    bad_pdf = tmp_path / "corrupt_worker.pdf"
    bad_pdf.write_bytes(b"not a valid pdf file")

    record, parsed, chunks, vectors, contexts = _parse_one(
        (str(bad_pdf), "IBM", "z/OS", "3.2", str(tmp_path), "test_sha", True, None)
    )

    assert record.status == "error"
    assert record.error_type is not None
    assert record.error is not None
    assert record.path == str(bad_pdf)
    assert chunks == []
    assert vectors == []
    assert contexts == {}

    # Round-trip through pickle to verify IPC safety in ProcessPoolExecutor
    serialized = pickle.dumps((record, parsed, chunks, vectors, contexts))
    unpacked_rec, _unpacked_parsed, unpacked_chunks, unpacked_vectors, unpacked_contexts = pickle.loads(serialized)

    assert unpacked_rec.status == "error"
    assert unpacked_rec.error_type == record.error_type
    assert unpacked_rec.error == record.error
    assert unpacked_chunks == []
    assert unpacked_vectors == []
    assert unpacked_contexts == {}




# --------------------------------------- representation migration (391 F2) ---
def _migration_args(corpus, progress, *extra):
    return ["--src", str(corpus), "--progress", str(progress), "--workers", "1", *extra]


def _marker_generations(fake, collection):
    comp = f"{collection}__completions"
    return {
        (p.payload or {}).get("generation_id")
        for p in fake._points.get(comp, [])
        if (p.payload or {}).get("doc_id")
    }


def _second_doc(corpus):
    from scripts.make_synthetic_pdf import build

    build(
        corpus / "SA22-7777-01.pdf",
        doc_id="SA22-7777-01",
        title="Synthetic Initialization and Tuning Reference",
        message_id="IEB700I",
    )


def test_revision_change_blocks_skip_and_force_reembeds(tmp_path, synthetic_pdf, monkeypatch):
    """Issue #391 F2 (revision-only change): the versioned fingerprint embeds
    the operator revision, so A markers are ineligible under B — the run
    refuses without --reingest and re-embeds every document with it. The
    evidence is the marker generation id, never unchanged text or point ids."""
    import pytest

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.completion import doc_generation_id
    from mainframe_rag.ingest.ibm_pdf import sha256_file
    from mainframe_rag.ingest.representation import STATE_COMMITTED, read_manifest_record
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-A")
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    progress = tmp_path / "inventory.jsonl"
    args = _migration_args(synthetic_pdf.parent, progress)
    assert main(args) == 0
    a_model = Settings(_env_file=None, embed_model_revision="rev-A")
    b_model = Settings(_env_file=None, embed_model_revision="rev-B")
    sha = sha256_file(synthetic_pdf)
    rules_v = extraction_rules_version()
    a_gen = doc_generation_id(a_model, sha, rules_v, "||")
    b_gen = doc_generation_id(b_model, sha, rules_v, "||")
    assert a_gen != b_gen, "revision-only change must alter the generation identity"
    assert _marker_generations(fake, a_model.qdrant_collection) == {a_gen}

    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-B")
    with pytest.raises(RuntimeError, match="representation drift on embed_model_revision"):
        main(list(args))
    assert _marker_generations(fake, a_model.qdrant_collection) == {a_gen}, \
        "refused run deletes or rewrites nothing"

    assert main([*args, "--reingest"]) == 0
    assert _marker_generations(fake, a_model.qdrant_collection) == {b_gen}
    record = read_manifest_record(fake, f"{a_model.qdrant_collection}__completions")
    assert record.state == STATE_COMMITTED
    assert record.manifest.embed_model_revision == "rev-B"


def test_force_reingest_still_requires_attested_revision(tmp_path, synthetic_pdf, monkeypatch):
    """A force flag may bypass rejection of old data, never the requirement
    to identify the new representation (issue #391 F2)."""
    import pytest

    from mainframe_rag.ingest import run_ingest

    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    monkeypatch.setenv("EMBED_MODE", "vllm")
    monkeypatch.setenv("DENSE_DIM", "256")
    monkeypatch.delenv("EMBED_MODEL_REVISION", raising=False)
    progress = tmp_path / "inventory.jsonl"
    with pytest.raises(RuntimeError, match="EMBED_MODEL_REVISION"):
        main(_migration_args(synthetic_pdf.parent, progress, "--reingest"))
    assert fake.upserts == [], "attestation fails before any point write"


def test_interrupted_migration_stays_pending_until_forced_resume(
    tmp_path, synthetic_pdf, monkeypatch
):
    """A crash between the pending declaration and the first document rewrite
    must not look compatible: a normal rerun refuses (A vectors must not skip
    as B) and --reingest resumes to a committed B contract."""
    import pytest

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.representation import (
        STATE_COMMITTED,
        STATE_PENDING,
        begin_manifest,
        read_manifest_record,
    )
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-A")
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    progress = tmp_path / "inventory.jsonl"
    args = _migration_args(synthetic_pdf.parent, progress)
    assert main(args) == 0
    model_a = Settings(_env_file=None, embed_model_revision="rev-A")
    markers_a = _marker_generations(fake, model_a.qdrant_collection)

    model_b = Settings(_env_file=None, embed_model_revision="rev-B")
    comp = f"{model_b.qdrant_collection}__completions"
    begin_manifest(fake, comp, model_b, extraction_rules_version())
    assert read_manifest_record(fake, comp).state == STATE_PENDING

    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-B")
    with pytest.raises(RuntimeError, match="unfinished representation migration"):
        main(list(args))
    assert _marker_generations(fake, model_a.qdrant_collection) == markers_a

    assert main([*args, "--reingest"]) == 0
    record = read_manifest_record(fake, comp)
    assert record.state == STATE_COMMITTED
    assert record.manifest.embed_model_revision == "rev-B"


def test_partial_migration_failure_leaves_pending_then_resume_commits(
    tmp_path, synthetic_pdf, monkeypatch
):
    """A failure after partial document work must never certify the new
    contract: it stays pending (skips and serving blocked) and the forced
    rerun completes it once the corpus is whole."""
    import shutil

    import pytest

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.representation import (
        STATE_COMMITTED,
        STATE_PENDING,
        read_manifest_record,
    )

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    shutil.copy(synthetic_pdf, corpus / synthetic_pdf.name)
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-A")
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    progress = tmp_path / "inventory.jsonl"
    args = _migration_args(corpus, progress)
    assert main(args) == 0
    collection = Settings(_env_file=None).qdrant_collection
    comp = f"{collection}__completions"

    bad = corpus / "broken.pdf"
    bad.write_bytes(b"not a pdf")
    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-B")
    assert main([*args, "--reingest"]) == 1
    record = read_manifest_record(fake, comp)
    assert record.state == STATE_PENDING, "failures must not certify the contract"
    with pytest.raises(RuntimeError, match="unfinished representation migration"):
        main(list(args))

    bad.unlink()
    assert main([*args, "--reingest"]) == 0
    record = read_manifest_record(fake, comp)
    assert record.state == STATE_COMMITTED
    assert record.manifest.embed_model_revision == "rev-B"


def test_stale_generation_marker_blocks_commit(tmp_path, synthetic_pdf, monkeypatch):
    """Commit-time scope proof (issue #391 F2): a source walk that no longer
    covers every stored doc cannot commit the new contract while the old
    representation's markers (and vectors) remain searchable."""
    import shutil

    import pytest

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.representation import (
        STATE_PENDING,
        read_manifest_record,
    )

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    shutil.copy(synthetic_pdf, corpus / synthetic_pdf.name)
    _second_doc(corpus)
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-A")
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    progress = tmp_path / "inventory.jsonl"
    args = _migration_args(corpus, progress)
    assert main(args) == 0
    collection = Settings(_env_file=None).qdrant_collection
    comp = f"{collection}__completions"
    assert len(_marker_generations(fake, collection)) == 2

    second = corpus / "SA22-7777-01.pdf"
    second.unlink()
    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-B")
    with pytest.raises(RuntimeError, match="representation migration incomplete"):
        main([*args, "--reingest"])
    assert read_manifest_record(fake, comp).state == STATE_PENDING

    shutil.copy(synthetic_pdf, corpus / synthetic_pdf.name)  # no-op keeper
    _second_doc(corpus)
    assert main([*args, "--reingest"]) == 0
    assert len(_marker_generations(fake, collection)) == 2
    assert read_manifest_record(fake, comp).state == "committed"


def test_limit_allows_fresh_subset_but_refuses_revision_migration(
    tmp_path, synthetic_pdf, monkeypatch
):
    """--limit on a fresh empty target is a deliberate subset bootstrap; the
    same flag during a representation migration is refused (issue #391 F2)."""
    import shutil

    import pytest

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.representation import (
        STATE_COMMITTED,
        read_manifest_record,
    )

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    shutil.copy(synthetic_pdf, corpus / synthetic_pdf.name)
    _second_doc(corpus)
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    progress = tmp_path / "inventory.jsonl"
    args = _migration_args(corpus, progress, "--limit", "1")
    assert main(args) == 0
    collection = Settings(_env_file=None).qdrant_collection
    comp = f"{collection}__completions"
    assert read_manifest_record(fake, comp).state == STATE_COMMITTED
    assert len(_marker_generations(fake, collection)) == 1

    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-B")
    with pytest.raises(RuntimeError, match="--limit refuses a representation migration"):
        main([*args, "--reingest"])


def test_unmarked_searchable_point_blocks_migration_commit(tmp_path, monkeypatch):
    """Issue #391 current packet (counterexample 1): the commit-time residue
    scan reads completion markers only, so an unmarked old-rules point was
    certified as the wanted representation. The scope proof must verify the
    actual searchable membership; unknown data blocks the commit and stays."""
    import pytest

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.representation import STATE_PENDING, read_manifest_record

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    collection = Settings(_env_file=None).qdrant_collection
    from types import SimpleNamespace

    fake._points[collection] = [
        SimpleNamespace(
            id="legacy-unmarked",
            payload={"doc_id": "SA99-0000-00", "rules_v": "pre-rp2", "source_rev": "rev-old"},
        )
    ]
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    progress = tmp_path / "inventory.jsonl"
    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-B")
    with pytest.raises(RuntimeError, match="searchable point"):
        main(_migration_args(corpus, progress, "--reingest"))
    comp = f"{collection}__completions"
    record = read_manifest_record(fake, comp)
    assert record is not None and record.state == STATE_PENDING, (
        "an incomplete scope proof must leave the contract pending"
    )
    assert any(
        str(getattr(p, "id", "")) == "legacy-unmarked"
        for p in fake._points[collection]
    ), "unknown data is preserved: refusal is never a deletion instruction"


def test_empty_target_migration_commits_when_nothing_is_searchable(tmp_path, monkeypatch):
    """Control for counterexample 1: a truly empty target exposes no stale
    vectors, so the pending bootstrap still commits — the new scope proof is
    not a blanket refusal."""
    from mainframe_rag.config import Settings
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.representation import STATE_COMMITTED, read_manifest_record

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    collection = Settings(_env_file=None).qdrant_collection
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    progress = tmp_path / "inventory.jsonl"
    assert main(_migration_args(corpus, progress, "--reingest")) == 0
    record = read_manifest_record(fake, f"{collection}__completions")
    assert record is not None and record.state == STATE_COMMITTED
