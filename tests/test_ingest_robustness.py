"""PR B: ingest robustness — crash-safe inventory, batching, indexes-first."""

import inspect
import json
import re
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
    "message_ids", "members", "sha256", "source_rev", "page_start",
}


def test_inventory_is_crash_safe_latest_wins_and_typed(tmp_path):
    progress = tmp_path / "inventory.jsonl"
    append_record(progress, InventoryRecord(path="a.pdf", sha256="1" * 64, status="dry", doc_id="A"))
    append_record(progress, InventoryRecord(path="a.pdf", sha256="1" * 64, status="upserted", doc_id="A"))
    record = InventoryRecord(
        path="book.pdf", sha256="f" * 64, doc_id="SC14-0000-00", pages=50, chunks=12,
        status="error", error="Corrupt header", error_type="ValueError",
    )
    append_record(progress, record)
    assert json.loads(progress.read_text().splitlines()[-1])["error_type"] == "ValueError"
    # Torn final line and corrupt/foreign lines are skipped, never fatal.
    with open(progress, "a", encoding="utf-8") as f:
        f.write('{"path": "b.pdf", "sha256": "22')
        f.write("\n\nnot a json object\n")
        f.write('{"invalid": "schema without required fields"}\n')
        f.write('{"path": "incomplete.pdf", "sha256"\n')
    records = load_inventory(progress)
    assert set(records) == {"a.pdf", "book.pdf"}
    assert records["a.pdf"].status == "upserted", "latest record wins"
    assert records["book.pdf"].error_type == "ValueError"
    assert records["book.pdf"].finished_at > 0


def test_batch_size_stays_in_bounds():
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


def test_payload_indexes_precede_load_and_cover_every_filter_field():
    """Indexes-before-load holds on pre-existing collections (a collection
    created before the index set existed is brought up to date, not scanned),
    and any payload field retrieve filters on must be indexed or the filter
    silently degrades to a scan."""
    from mainframe_rag.retrieve import filters as filters_mod

    existing = RecordingClient(exists=True, vector_size=768)
    ensure_collection(existing, _settings(768))
    assert {name for name, _ in existing.indexes} >= _INDEXED_FIELDS

    client = RecordingClient(exists=False)
    ensure_collection(client, _settings(768))
    indexed = {name for name, _ in client.indexes}
    filter_keys = set(re.findall(r'key="(\w+)"', inspect.getsource(filters_mod)))
    assert filter_keys, "no filter keys found — inspect.getsource broke"
    assert filter_keys <= indexed, f"unindexed filter fields: {filter_keys - indexed}"


def test_models_ipc_pickle_round_trip():
    """Verify that all core domain models cleanly serialize/deserialize across IPC boundaries (ProcessPoolExecutor)."""
    import pickle

    from mainframe_rag.agent.answer import ParsedAnswer
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc
    from mainframe_rag.ports import ChatMessage
    from mainframe_rag.retrieve.filters import QueryIdentifiers
    from mainframe_rag.retrieve.query import SearchHit

    doc = ParsedDoc(
        path=Path("docs/manual.pdf"),
        sha256="a" * 64,
        doc_id="SC14-7315-70",
        title="Sample Title",
        vendor="ibm",
        product="z/OS",
        version="3.1",
        toc=((1, "Intro", 1), (2, "Details", 5)),
        page_count=10,
    )
    chunk = _chunk(1)
    record = InventoryRecord(path="docs/manual.pdf", sha256="a" * 64, status="upserted", doc_id="SC14-7315-70")
    identifiers = QueryIdentifiers(doc_ids=["SC14-7315-70"], message_ids=["IEA500I"])
    hit = SearchHit(
        chunk_id="chunk-1",
        score=0.95,
        cite="SC14-7315-70 Manual, p. 1",
        heading="Intro",
        text="Sample body text",
        doc_id="SC14-7315-70",
        title="Sample Title",
        page_label="1",
        chunk_type="narrative",
        message_ids=("IEA500I",),
    )
    msg = ChatMessage(role="user", content="hello")
    answer = ParsedAnswer(answer="Answer prose", citations=["SC14-7315-70 Manual, p. 1"], script="//JOB")

    for obj in (doc, chunk, record, identifiers, hit, msg, answer):
        restored = pickle.loads(pickle.dumps(obj))
        assert restored == obj
        assert type(restored) is type(obj)
