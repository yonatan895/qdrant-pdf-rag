"""Collection schema tests with a recording fake client (no Qdrant needed)."""

from types import SimpleNamespace

import pytest
from qdrant_client import models

from mainframe_rag.config import Settings
from mainframe_rag.ingest.qdrant_io import DimMismatchError, ensure_collection


class RecordingClient:
    def __init__(self, exists=False, vector_size=None):
        self.exists_flag = exists
        self.vector_size = vector_size
        self.created = None
        self.indexes = []

    def collection_exists(self, _name):
        return self.exists_flag

    def get_collection(self, _name):
        dense = SimpleNamespace(size=self.vector_size)
        return SimpleNamespace(
            config=SimpleNamespace(params=SimpleNamespace(vectors={"dense": dense}))
        )

    def create_collection(self, name, **kwargs):
        self.created = kwargs

    def create_payload_index(self, _c, field_name, field_schema):
        self.indexes.append((field_name, field_schema))


def _settings(dim=768):
    return Settings(
        qdrant_url="http://localhost:6333",
        qdrant_collection="mainframe_manuals",
        dense_dim=dim,
    )


def test_ensure_collection_creates_named_vectors_and_sparse():
    client = RecordingClient(exists=False)
    ensure_collection(client, _settings(768))
    vectors = client.created["vectors_config"]
    sparse = client.created["sparse_vectors_config"]
    assert vectors["dense"].size == 768
    assert vectors["dense"].distance == models.Distance.COSINE
    assert vectors["dense"].on_disk is True
    assert vectors["dense"].hnsw_config.m == 16
    assert vectors["dense"].hnsw_config.ef_construct == 128
    assert vectors["dense"].quantization_config.scalar.always_ram is True
    assert sparse["bm25"].modifier == models.Modifier.IDF
    assert client.created["on_disk_payload"] is True


def test_ensure_collection_creates_all_payload_indexes_before_load():
    client = RecordingClient(exists=False)
    ensure_collection(client, _settings(768))
    by_name = dict(client.indexes)
    for kw in ("vendor", "product", "version", "doc_id", "chunk_type",
               "message_ids", "members", "sha256"):
        assert by_name[kw] == models.PayloadSchemaType.KEYWORD, kw
    assert by_name["page_start"] == models.PayloadSchemaType.INTEGER
    assert len(client.indexes) == 9


def test_ensure_collection_fails_fast_on_dim_mismatch():
    client = RecordingClient(exists=True, vector_size=384)
    with pytest.raises(DimMismatchError):
        ensure_collection(client, _settings(768))


def test_ensure_collection_hash_mode_uses_fixed_dim():
    client = RecordingClient(exists=False)
    s = Settings(embed_mode="hash", dense_dim=None, _env_file=None)
    ensure_collection(client, s)
    assert client.created["vectors_config"]["dense"].size == 256


def test_ensure_collection_requires_dense_dim():
    client = RecordingClient(exists=False)
    with pytest.raises(RuntimeError, match="DENSE_DIM"):
        ensure_collection(client, _settings(dim=None))


def test_upsert_chunks_payload_is_slimmed_without_embed_text():
    """PointStruct payloads must store text and metadata without duplicating
    text into embed_text (halving write bytes and storage)."""
    from mainframe_rag.ingest.chunk import Chunk
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc
    from mainframe_rag.ingest.qdrant_io import upsert_chunks

    class UpsertRecordingClient(RecordingClient):
        def __init__(self):
            super().__init__()
            self.upserted_points = []

        def upsert(self, collection_name, *, points, wait=True):
            self.upserted_points.extend(points)
            return True

    client = UpsertRecordingClient()
    parsed = ParsedDoc(
        path="manual.pdf",
        doc_id="SC14-7315-70",
        sha256="abc123",
        vendor="IBM",
        product="z/OS",
        version="3.2",
        title="Sample Manual",
        page_count=10,
    )
    chunk = Chunk(
        chunk_id="00000000-0000-0000-0000-000000000001",
        doc_id="SC14-7315-70",
        heading_path="Chapter 1 > Overview",
        page_start=1,
        page_label="1-1",
        chunk_type="narrative",
        text="This is the main body text of the chunk.",
        message_ids=[],
        members=[],
        ordinal=0,
    )
    vectors = [([0.1] * 4, ([1, 2], [1.0, 2.0]))]

    count = upsert_chunks(client, _settings(4), parsed, [chunk], vectors)
    assert count == 1
    assert len(client.upserted_points) == 1
    payload = client.upserted_points[0].payload
    assert payload["doc_id"] == "SC14-7315-70"
    assert payload["text"] == "This is the main body text of the chunk."
    assert payload["heading_path"] == "Chapter 1 > Overview"
    assert "embed_text" not in payload, "embed_text must not be stored in point payload"

