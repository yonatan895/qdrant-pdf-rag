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
