"""Ingest inventory: JSONL progress log (path, sha256, doc_id, pages, chunks, status).

Used to resume runs and to report per-file results. One JSON object per line;
writes are append-only small lines so concurrent workers are safe enough.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class InventoryRecord:
    path: str
    sha256: str
    doc_id: str | None = None
    pages: int = 0
    chunks: int = 0
    status: str = "pending"  # pending | parsed | upserted | skipped | dry | error
    seconds: float = 0.0
    error: str | None = None
    finished_at: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)


def load_inventory(progress_path: Path) -> dict[str, InventoryRecord]:
    """Latest record per path from an existing JSONL file."""
    latest: dict[str, InventoryRecord] = {}
    if not progress_path.exists():
        return latest
    for line in progress_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = InventoryRecord(**json.loads(line))
        except (json.JSONDecodeError, TypeError):
            continue
        latest[rec.path] = rec
    return latest


def should_skip(record: InventoryRecord | None, sha256: str, allow_dry: bool = False) -> bool:
    """Skip when this exact file already finished. 'upserted' always skips;
    'dry' only skips for another dry run (a real run still needs embed+upsert)."""
    if not (record and record.sha256 == sha256):
        return False
    return record.status == "upserted" or (allow_dry and record.status == "dry")


def append_record(progress_path: Path, record: InventoryRecord) -> None:
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    with open(progress_path, "a", encoding="utf-8") as f:
        f.write(record.to_json() + "\n")
