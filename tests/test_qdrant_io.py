"""Collection schema tests with a recording fake client (no Qdrant needed)."""

from types import SimpleNamespace

import pytest
from qdrant_client import models

from mainframe_rag.config import Settings
from mainframe_rag.ingest.qdrant_io import (
    CollectionPolicyMismatchError,
    DimMismatchError,
    ensure_collection,
)


class RecordingClient:
    def __init__(self, exists=False, vector_size=None, live_params=None):
        self.exists_flag = exists
        self.vector_size = vector_size
        self.live_params = live_params
        self.created = None
        self.indexes = []

    def collection_exists(self, _name):
        return self.exists_flag

    def get_collection(self, _name):
        dense = SimpleNamespace(size=self.vector_size)
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors={"dense": dense}, **(self.live_params or {})
                )
            )
        )

    def create_collection(self, name, **kwargs):
        self.created = kwargs

    def create_payload_index(self, _c, field_name, field_schema):
        self.indexes.append((field_name, field_schema))


def _settings(dim=768, **overrides):
    kw = {
        "qdrant_url": "http://localhost:6333",
        "qdrant_collection": "mainframe_manuals",
        "dense_dim": dim,
    }
    kw.update(overrides)
    return Settings(**kw)


_POLICY = {
    "qdrant_shard_number": 6,
    "qdrant_replication_factor": 2,
    "qdrant_write_consistency_factor": 1,
}


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
               "message_ids", "members", "system_codes", "sha256", "source_rev"):
        assert by_name[kw] == models.PayloadSchemaType.KEYWORD, kw
    assert by_name["page_start"] == models.PayloadSchemaType.INTEGER
    assert len(client.indexes) == 11


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


def test_ensure_collection_unset_policy_creates_without_distribution_keys():
    """Unset policy reproduces today's server-default creation exactly
    (issue #360): no shard/replication/consistency keys travel."""
    client = RecordingClient(exists=False)
    ensure_collection(client, _settings(768))
    for key in ("shard_number", "replication_factor", "write_consistency_factor"):
        assert key not in client.created


def test_ensure_collection_forwards_selected_policy_verbatim():
    client = RecordingClient(exists=False)
    ensure_collection(client, _settings(768, **_POLICY))
    assert client.created["shard_number"] == 6
    assert client.created["replication_factor"] == 2
    assert client.created["write_consistency_factor"] == 1


def test_ensure_collection_existing_matching_policy_passes_without_recreation():
    live = {"shard_number": 6, "replication_factor": 2, "write_consistency_factor": 1}
    client = RecordingClient(exists=True, vector_size=768, live_params=live)
    ensure_collection(client, _settings(768, **_POLICY))
    assert client.created is None  # examined, never recreated


def test_ensure_collection_existing_partial_policy_checks_only_selected_keys():
    """Unset keys are not examined: an operator selecting only replication
    does not fail on the shard count the server chose."""
    live = {"shard_number": 99, "replication_factor": 2, "write_consistency_factor": 1}
    client = RecordingClient(exists=True, vector_size=768, live_params=live)
    ensure_collection(client, _settings(768, qdrant_replication_factor=2))
    assert client.created is None


def test_ensure_collection_existing_mismatch_fails_closed_without_mutation():
    live = {"shard_number": 1, "replication_factor": 1, "write_consistency_factor": 1}
    client = RecordingClient(exists=True, vector_size=768, live_params=live)
    with pytest.raises(CollectionPolicyMismatchError, match="snapshot-gated"):
        ensure_collection(client, _settings(768, **_POLICY))
    assert client.created is None  # never recreated
    assert client.indexes == []  # refused before index reconciliation


def test_ensure_collection_existing_unreadable_policy_values_are_not_mismatches():
    """A live info without readable values is unknown, not a mismatch —
    absence of evidence must not fail a healthy collection."""
    client = RecordingClient(exists=True, vector_size=768, live_params=None)
    ensure_collection(client, _settings(768, **_POLICY))
    assert client.created is None


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



def test_physical_page_span_round_trips_payload_to_citation():
    """Issue #271 round trip: make_chunks -> upsert payload -> retrieval
    projection -> citation -> validator. An unlabeled spanning chunk cites
    its whole physical span; a labeled chunk keeps its printed range."""
    from mainframe_rag.agent.cites import valid_citations
    from mainframe_rag.ingest.chunk import make_chunks
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc
    from mainframe_rag.ingest.qdrant_io import upsert_chunks
    from mainframe_rag.retrieve.query import _to_hit

    class UpsertRecordingClient(RecordingClient):
        def __init__(self):
            super().__init__()
            self.upserted_points = []

        def upsert(self, collection_name, *, points, wait=True):
            self.upserted_points.extend(points)
            return True

    def cite_for(labels):
        parsed = ParsedDoc(
            path="WX10-0001-00.pdf", doc_id="WX10-0001-00", sha256="ab" * 32, vendor="unknown",
            title="Widget Guide", toc=[[1, "Chapter 1. Widgets", 1]], page_count=3,
        )
        chunks = make_chunks(parsed, ["Alpha widget text.", "Beta widget text.", "Gamma widget text."], labels)
        assert len(chunks) == 1
        client = UpsertRecordingClient()
        upsert_chunks(client, _settings(4), parsed, chunks, [([0.1] * 4, ([1], [1.0]))])
        (point,) = client.upserted_points
        assert (point.payload["page_start"], point.payload["page_end"]) == (0, 2)
        hit = _to_hit(models.ScoredPoint(id=str(point.id), version=1, score=1.0, payload=point.payload), 1.0)
        assert valid_citations(f"Answer.\n\nCitations:\n- {hit.cite}\n", {hit.cite}) == [hit.cite]
        return hit.cite

    assert cite_for(["", "", ""]) == "WX10-0001-00 Widget Guide, Chapter 1. Widgets, p. PDF 1–3"
    assert cite_for([None, "1", "2"]) == "WX10-0001-00 Widget Guide, Chapter 1. Widgets, p. PDF 1–3"
    assert cite_for(["7", "8", "9"]) == "WX10-0001-00 Widget Guide, Chapter 1. Widgets, p. 7–9"


def test_completion_code_round_trips_ingest_to_query_filter():
    """Issue #591 round trip: make_chunks -> upsert payload -> query filter.

    The first two revisions passed every unit test while the feature was
    dead: `system_codes` was added to `_KEYWORD_INDEXES` but never written
    into the point payload, so on a real collection the keyword index read
    `points: 0` and every code query's must-filter matched nothing and fell
    back. Only this hop — chunk to stored payload to filter value — proves
    the identifier is transported, so it is asserted as one round trip.
    """
    from mainframe_rag.ingest.chunk import make_chunks
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc
    from mainframe_rag.ingest.qdrant_io import upsert_chunks
    from mainframe_rag.retrieve.filters import build_filter, parse_query, query_kind

    class UpsertRecordingClient(RecordingClient):
        def __init__(self):
            super().__init__()
            self.upserted_points = []

        def upsert(self, collection_name, *, points, wait=True):
            self.upserted_points.extend(points)
            return True

    text = (
        "System completion codes\n\n"
        "0C4\nExplanation:\nA protection exception occurred during the operation.\n\n"
        "0C7\nA data exception occurred.\n"
    )
    parsed = ParsedDoc(
        path="SA99-0000-00.pdf", doc_id="SA99-0000-00", sha256="cd" * 32,
        vendor="unknown", title="Synthetic Code Manual",
        toc=((1, "System completion codes", 1),), page_count=1,
    )
    chunks = make_chunks(parsed, [text])
    client = UpsertRecordingClient()
    upsert_chunks(
        client, _settings(4), parsed, chunks, [([0.1] * 4, ([1], [1.0]))] * len(chunks)
    )

    # Stored: every entry the chunk carries, not just the first line.
    stored = {code for p in client.upserted_points for code in p.payload["system_codes"]}
    assert {"0C4", "0C7"} <= stored

    # The filter a code query builds must select exactly those stored values.
    for query in ("What does abend S0C4 mean?", "abend 0C4", "abend 0C7"):
        ids = parse_query(query)
        assert query_kind(ids) == "identifier"
        clause = next(c for c in build_filter(ids).must if c.key == "system_codes")
        assert set(clause.match.any) & stored, f"{query!r} filter cannot match stored codes"

    # Issue #621: the same code-shaped lines in an unlabelled section (an
    # index page, a return-code table) reach the payload as nothing, so the
    # prefilter cannot admit them.
    unlabelled = ParsedDoc(
        path="SA99-0001-00.pdf", doc_id="SA99-0001-00", sha256="ef" * 32,
        vendor="unknown", title="Synthetic Guide",
        toc=((1, "Return codes", 1),), page_count=1,
    )
    other = make_chunks(unlabelled, [text.replace("Explanation:\n", "")])
    client = UpsertRecordingClient()
    upsert_chunks(
        client, _settings(4), unlabelled, other, [([0.1] * 4, ([1], [1.0]))] * len(other)
    )
    assert client.upserted_points
    assert all(p.payload["system_codes"] == [] for p in client.upserted_points)


def test_completion_codes_key_present_even_without_codes():
    """The key is written unconditionally, so the indexed field exists on
    every point instead of only on code-bearing chunks."""
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

    parsed = ParsedDoc(
        path="manual.pdf", doc_id="SC14-7315-70", sha256="abc123", vendor="IBM",
        product="z/OS", version="3.2", title="Sample Manual", page_count=10,
    )
    chunk = Chunk(
        chunk_id="00000000-0000-0000-0000-000000000001", doc_id="SC14-7315-70",
        heading_path="Chapter 1 > Overview", page_start=1, page_label="1-1",
        chunk_type="narrative", text="No codes on this page.", message_ids=[],
        members=[], ordinal=0,
    )
    client = UpsertRecordingClient()
    upsert_chunks(client, _settings(4), parsed, [chunk], [([0.1] * 4, ([1], [1.0]))])
    assert client.upserted_points[0].payload["system_codes"] == []
