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


def test_upsert_one_is_all_or_nothing_per_doc(synthetic_pdf, monkeypatch):
    """The one-owner refactor's contract: an embed failure on a later batch
    leaves the doc completely un-upserted (the old interleaved loop could
    land earlier batches before failing). Resume deletes by doc_id on sha
    mismatch, but no partial doc should ever exist to begin with."""
    import pymupdf

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.chrome import strip_chrome
    from mainframe_rag.ingest.chunk import make_chunks
    from mainframe_rag.ingest.ibm_pdf import parse_pdf

    parsed = parse_pdf(synthetic_pdf, sha256="ab" * 32)
    doc = pymupdf.open(synthetic_pdf)
    try:
        page_texts = [doc[i].get_text() for i in range(doc.page_count)]
        labels = [doc[i].get_label() for i in range(doc.page_count)]
    finally:
        doc.close()
    chunks = make_chunks(parsed, strip_chrome(page_texts), labels)

    settings = Settings(embed_mode="hash", _env_file=None)
    settings.batch_size = 2  # 7 chunks -> 4 batches; the ge=16 env bound stays

    class RaisingEmbedder:
        def __init__(self):
            self.dense_calls = 0

        def dense(self, texts):
            self.dense_calls += 1
            if self.dense_calls >= 2:  # batch 1 embeds fine, batch 2 explodes
                raise RuntimeError("embed endpoint exploded on batch 2")
            return [[0.0] * 4 for _ in texts]

        def sparse(self, texts):
            return [([1], [1.0]) for _ in texts]

    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    embedder = RaisingEmbedder()
    monkeypatch.setattr(run_ingest, "_get_embedder", lambda settings: embedder)

    import pytest

    with pytest.raises(RuntimeError, match="embed endpoint exploded"):
        run_ingest._upsert_one(parsed, chunks, settings)
    assert embedder.dense_calls >= 2, "test must fail on a LATER batch, not batch 1"
    assert fake.upserts == [], "a partial doc must never be upserted"
