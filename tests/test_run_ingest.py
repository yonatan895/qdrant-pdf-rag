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


class _FakeQdrant:
    """QdrantPoints double for the parent-side run() path (review round on
    PR D: the non-dry upsert branch had no coverage)."""

    def __init__(self, stored_sha: str | None = None):
        self.stored_sha = stored_sha
        self.upserts: list[int] = []
        self.deletes = 0

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

    def create_payload_index(self, *a, **k):
        from types import SimpleNamespace

        return SimpleNamespace()

    def scroll(self, collection_name, *, scroll_filter, limit, with_payload):
        from types import SimpleNamespace

        if self.stored_sha is None:
            return [], None
        return [SimpleNamespace(payload={"sha256": self.stored_sha})], None

    def upsert(self, collection_name, *, points, wait=True):
        self.upserts.append(len(points))
        from types import SimpleNamespace

        return SimpleNamespace()

    def delete(self, collection_name, *, points_selector, wait=True):
        self.deletes += 1
        from types import SimpleNamespace

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
    assert sum(fake.upserts) == done[0]["chunks_upserted"]


def test_qdrant_level_skip_counts_as_ok(tmp_path, synthetic_pdf, capsys, monkeypatch):
    """Qdrant already holds doc_id at this sha256 (fresh inventory, warm
    Qdrant): files_ok counts it, nothing is upserted or deleted."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.ibm_pdf import sha256_file

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    fake = _FakeQdrant(stored_sha=sha256_file(synthetic_pdf))
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    progress = tmp_path / "inventory.jsonl"
    rc = main(["--src", str(synthetic_pdf.parent), "--progress", str(progress), "--workers", "1"])
    assert rc == 0
    done = [l for l in _stderr_json(capsys) if l.get("action") == "done"]
    assert done[0]["files_ok"] == 1
    assert done[0]["chunks_upserted"] == 0
    assert fake.upserts == [] and fake.deletes == 0


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
    monkeypatch.setenv("DENSE_DIM", "256")  # collection setup passes; the
    # worker's embed call then fails fast on the missing EMBED_BASE_URL
    progress = tmp_path / "inventory.jsonl"
    rc = main(["--src", str(synthetic_pdf.parent), "--progress", str(progress), "--workers", "1"])
    assert rc == 1
    assert fake.upserts == [], "a doc whose embed failed must never be upserted"
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

    def mock_embed_batch(chunks, product, version, title, embedder):
        embed_calls.append(len(chunks))
        return original_embed_batch(chunks, product, version, title, embedder)

    # batch_size min validator is 16; use Settings instance with batch_size=16 or mock chunks
    test_settings = Settings(batch_size=16, embed_mode="hash")
    # To verify slicing, override batch_size attribute to 2 for this test
    object.__setattr__(test_settings, "batch_size", 2)
    monkeypatch.setattr(run_ingest, "_load_worker_settings", lambda: test_settings)
    monkeypatch.setattr(run_ingest, "embed_batch", mock_embed_batch)

    task = (str(synthetic_pdf), None, None, None, str(synthetic_pdf.parent), "dummy_sha", True)
    _rec, _parsed, chunks, vectors = run_ingest._parse_one(task)
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

    record, parsed, chunks, vectors = _parse_one(
        (str(bad_pdf), "IBM", "z/OS", "3.2", str(tmp_path), "test_sha", True)
    )

    assert record.status == "error"
    assert record.error_type is not None
    assert record.error is not None
    assert record.path == str(bad_pdf)
    assert chunks == []
    assert vectors == []

    # Round-trip through pickle to verify IPC safety in ProcessPoolExecutor
    serialized = pickle.dumps((record, parsed, chunks, vectors))
    unpacked_rec, _unpacked_parsed, unpacked_chunks, unpacked_vectors = pickle.loads(serialized)

    assert unpacked_rec.status == "error"
    assert unpacked_rec.error_type == record.error_type
    assert unpacked_rec.error == record.error
    assert unpacked_chunks == []
    assert unpacked_vectors == []


