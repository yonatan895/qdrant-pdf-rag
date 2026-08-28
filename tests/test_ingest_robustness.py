"""PR B: ingest robustness — crash-safe inventory, batching, indexes-first."""

import inspect
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from mainframe_rag.config import Settings
from mainframe_rag.ingest.chunk import Chunk
from mainframe_rag.ingest.inventory import (
    InventoryRecord,
    append_record,
    load_inventory,
)
from mainframe_rag.ingest.qdrant_io import ensure_collection, upsert_chunks
from tests.test_qdrant_io import RecordingClient, _settings

_INDEXED_FIELDS = {
    "vendor", "product", "version", "doc_id", "chunk_type",
    "message_ids", "members", "sha256", "page_start",
}


def test_inventory_survives_torn_final_line(tmp_path):
    progress = tmp_path / "inventory.jsonl"
    append_record(progress, InventoryRecord(path="a.pdf", sha256="1" * 64, status="upserted", doc_id="A"))
    with open(progress, "a") as f:  # crash mid-write: torn final line
        f.write('{"path": "b.pdf", "sha256": "22')
    assert set(load_inventory(progress)) == {"a.pdf"}


def test_inventory_latest_record_wins(tmp_path):
    progress = tmp_path / "inventory.jsonl"
    append_record(progress, InventoryRecord(path="a.pdf", sha256="1" * 64, status="dry"))
    append_record(progress, InventoryRecord(path="a.pdf", sha256="1" * 64, status="upserted"))
    assert load_inventory(progress)["a.pdf"].status == "upserted"


def test_inventory_error_record_is_typed(tmp_path):
    progress = tmp_path / "inventory.jsonl"
    append_record(
        progress,
        InventoryRecord(
            path="bad.pdf", sha256="", status="error",
            error="cannot open broken document", error_type="RuntimeError",
        ),
    )
    rec = json.loads(progress.read_text().splitlines()[-1])
    assert rec["error_type"] == "RuntimeError"
    assert load_inventory(progress)["bad.pdf"].error_type == "RuntimeError"


def test_batch_size_stays_in_qdrant_skill_band():
    with pytest.raises(ValidationError):
        Settings(batch_size=8, _env_file=None)
    with pytest.raises(ValidationError):
        Settings(batch_size=512, _env_file=None)
    assert Settings(batch_size=64, _env_file=None).batch_size == 64


def _chunk(idx: int) -> Chunk:
    return Chunk(
        chunk_id=f"c{idx}", doc_id="D", heading_path="H", page_start=idx,
        page_label="1-1", chunk_type="narrative", text=f"chunk {idx}",
        message_ids=[], members=[], ordinal=idx,
    )


class UpsertRecordingClient:
    def __init__(self) -> None:
        self.upsert_sizes: list[int] = []

    def upsert(self, _collection, *, points, wait=True):
        self.upsert_sizes.append(len(points))


def test_upsert_is_batched():
    """Embed+upsert happen in settings.batch_size batches, not one giant call."""
    parsed = None
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc

    parsed = ParsedDoc(
        path=Path("x.pdf"), sha256="0" * 64, doc_id="D", title="T",
        product=None, version=None, vendor="unknown", toc=(), page_count=1,
    )
    client = UpsertRecordingClient()
    chunks = [_chunk(i) for i in range(40)]
    vectors = [([0.0], ([1], [1.0])) for _ in chunks]
    settings = Settings(batch_size=16, dense_dim=4, _env_file=None)
    upsert_chunks(client, settings, parsed, chunks, vectors)
    assert client.upsert_sizes == [16, 16, 8]


def test_existing_collection_still_gets_payload_indexes():
    """Indexes-before-load holds on pre-existing collections: a collection
    created before the index set existed must be brought up to date, not
    scanned."""
    client = RecordingClient(exists=True, vector_size=768)
    ensure_collection(client, _settings(768))
    assert {name for name, _ in client.indexes} >= _INDEXED_FIELDS


def test_every_retrieve_filter_field_is_indexed():
    """Cross-layer guard: any payload field retrieve filters on must be in the
    payload-index set, or the filter silently degrades to a scan."""
    from mainframe_rag.retrieve import filters as filters_mod

    client = RecordingClient(exists=False)
    ensure_collection(client, _settings(768))
    indexed = {name for name, _ in client.indexes}

    filter_keys = set()
    for m in __import__("re").finditer(r'key="(\w+)"', inspect.getsource(filters_mod)):
        filter_keys.add(m.group(1))
    assert filter_keys, "no filter keys found — inspect.getsource broke"
    assert filter_keys <= indexed, f"unindexed filter fields: {filter_keys - indexed}"
