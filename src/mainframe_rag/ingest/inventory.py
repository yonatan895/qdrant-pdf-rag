"""Ingest inventory: JSONL progress log (path, sha256, doc_id, pages, chunks, status).

Used to resume runs and to report per-file results. One JSON object per line;
writes are append-only small lines so concurrent workers are safe enough.
"""

from __future__ import annotations

import time
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError


class InventoryRecord(BaseModel):
    path: str
    sha256: str
    doc_id: str | None = None
    pages: int = 0
    chunks: int = 0
    status: str = "pending"  # upserted | skipped | dry | error (set before append)
    seconds: float = 0.0
    error: str | None = None
    error_type: str | None = None  # exception class name, for typed triage
    rules_version: str | None = None  # extraction-rules version (issue #124)
    finished_at: float = Field(default_factory=time.time)


def load_inventory(progress_path: Path) -> dict[str, InventoryRecord]:
    """Latest record per path from an existing JSONL file.

    Crash-survival contract: records are appended one JSON object per line
    with an open/close per append (never buffered in-process), so a crash
    mid-run leaves every completed record on disk; a torn final line is
    ignored, not fatal."""
    latest: dict[str, InventoryRecord] = {}
    if not progress_path.exists():
        return latest
    for line in progress_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = InventoryRecord.model_validate_json(line)
        except (ValidationError, ValueError):
            continue
        latest[rec.path] = rec
    return latest


def should_skip(
    record: InventoryRecord | None,
    sha256: str,
    allow_dry: bool = False,
    rules_version: str | None = None,
) -> bool:
    """Skip when this exact file already finished under the SAME extraction
    rules (issue #124): the record carries the rules version its payloads
    were extracted with, and a content-unchanged file must still re-ingest
    when the rules changed — otherwise a regex widening desyncs queries
    (new rules) from payloads (old rules) and recall collapses silently.
    Records from before versioning carry no field and never skip."""
    if not (record and record.sha256 == sha256):
        return False
    if record.rules_version != rules_version:
        return False
    return record.status == "upserted" or (allow_dry and record.status == "dry")


def append_record(progress_path: Path, record: InventoryRecord) -> None:
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    with open(progress_path, "a", encoding="utf-8") as f:
        f.write(record.model_dump_json() + "\n")
