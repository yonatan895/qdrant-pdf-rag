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


def test_ensure_collection_requires_dense_dim():
    client = RecordingClient(exists=False)
    with pytest.raises(RuntimeError, match="DENSE_DIM"):
        ensure_collection(client, _settings(dim=None))
