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
