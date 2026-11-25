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


def test_ensure_collection_creates_schema_and_indexes_before_load():
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
    # Unset policy reproduces server-default creation exactly (issue #360):
    # no shard/replication/consistency keys travel.
    for key in ("shard_number", "replication_factor", "write_consistency_factor"):
        assert key not in client.created
    by_name = dict(client.indexes)
    for kw in ("vendor", "product", "version", "doc_id", "chunk_type",
               "message_ids", "members", "system_codes", "sha256", "source_rev"):
        assert by_name[kw] == models.PayloadSchemaType.KEYWORD, kw
    assert by_name["page_start"] == models.PayloadSchemaType.INTEGER
    assert len(client.indexes) == 11
    # Hash mode pins the dimension regardless of DENSE_DIM.
    hashed = RecordingClient(exists=False)
    ensure_collection(hashed, Settings(embed_mode="hash", dense_dim=None, _env_file=None))
    assert hashed.created["vectors_config"]["dense"].size == 256


def test_ensure_collection_refuses_dimension_problems():
    with pytest.raises(DimMismatchError):
        ensure_collection(RecordingClient(exists=True, vector_size=384), _settings(768))
    with pytest.raises(RuntimeError, match="DENSE_DIM"):
        ensure_collection(RecordingClient(exists=False), _settings(dim=None))


def test_ensure_collection_forwards_selected_policy_verbatim():
    client = RecordingClient(exists=False)
    ensure_collection(client, _settings(768, **_POLICY))
    assert client.created["shard_number"] == 6
    assert client.created["replication_factor"] == 2
    assert client.created["write_consistency_factor"] == 1


def test_ensure_collection_existing_policy_is_examined_never_recreated():
    live = {"shard_number": 6, "replication_factor": 2, "write_consistency_factor": 1}
    matching = RecordingClient(exists=True, vector_size=768, live_params=live)
    ensure_collection(matching, _settings(768, **_POLICY))
    assert matching.created is None
    # Unset keys are not examined: selecting only replication does not fail on
    # the shard count the server chose.
    partial = RecordingClient(
        exists=True, vector_size=768, live_params={**live, "shard_number": 99}
    )
    ensure_collection(partial, _settings(768, qdrant_replication_factor=2))
    assert partial.created is None
    # A live info without readable values is unknown, not a mismatch.
    unreadable = RecordingClient(exists=True, vector_size=768, live_params=None)
    ensure_collection(unreadable, _settings(768, **_POLICY))
    assert unreadable.created is None


def test_ensure_collection_existing_mismatch_fails_closed_without_mutation():
    live = {"shard_number": 1, "replication_factor": 1, "write_consistency_factor": 1}
    client = RecordingClient(exists=True, vector_size=768, live_params=live)
    with pytest.raises(CollectionPolicyMismatchError, match="snapshot-gated"):
        ensure_collection(client, _settings(768, **_POLICY))
    assert client.created is None  # never recreated
    assert client.indexes == []  # refused before index reconciliation


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
    # Revision identity travels with the point (normalized labels + content hash);
    # the indexed code field exists on every point, code-bearing or not.
    assert payload["source_rev"] == "ibm|z/os|3.2|abc123"
    assert payload["system_codes"] == []



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


# --- bulk copy request sizing (issue #1379) ---------------------------------

# Full-precision, negative and exponent floats: the longest JSON number forms.
_WORST_FLOATS = (-2.2250738585072014e-308, -1.2345678901234567e-05, -0.12345678901234568, 1.7976931348623157e308)
# Non-ASCII text: the transport sends UTF-8, the estimate counts ASCII escapes.
_MIXED_TEXT = "Z" + chr(0xFC) + "rich " + "".join(map(chr, (0x5E9, 0x5DC, 0x5D5, 0x5DD))) + " " + chr(0x1F600) + " "


def _worst_point(i: int, dim: int, nnz: int = 64) -> models.PointStruct:
    dense = [_WORST_FLOATS[(i + k) % len(_WORST_FLOATS)] for k in range(dim)]
    return models.PointStruct(
        id=f"00000000-0000-4000-8000-{i:012d}",
        vector={
            "dense": dense,
            "bm25": models.SparseVector(
                indices=[4294967295 - k for k in range(nnz)], values=[_WORST_FLOATS[1]] * nnz
            ),
        },
        payload={"doc_id": f"D{i}", "text": _MIXED_TEXT * 20, "page_start": i},
    )


def _request_bytes(batch: list[models.PointStruct]) -> int:
    """The client's own REST body for an upsert of batch (qdrant-client's
    encoder for PointsList), as UTF-8 bytes on the wire."""
    from qdrant_client.http.api.points_api import jsonable_encoder

    return len(jsonable_encoder(models.PointsList(points=batch)).encode("utf-8"))


@pytest.mark.parametrize("dim", [32, 1024, 4096])
def test_point_estimate_bounds_the_client_request_body(dim):
    from mainframe_rag.ingest.qdrant_io import estimate_point_bytes

    points = [_worst_point(i, dim) for i in range(3)]
    for point in points:
        assert estimate_point_bytes(point) >= _request_bytes([point])
    assert sum(estimate_point_bytes(p) for p in points) >= _request_bytes(points)


@pytest.mark.parametrize("dim", [32, 1024, 4096])
def test_byte_budget_batches_stay_under_budget_in_order(dim):
    from mainframe_rag.ingest.qdrant_io import byte_bounded_batches, estimate_point_bytes

    budget = 1024 * 1024
    points = [_worst_point(i, dim) for i in range(40)]
    batches = byte_bounded_batches(points, budget)
    assert [p.id for batch in batches for p in batch] == [p.id for p in points]
    for index, batch in enumerate(batches):
        assert _request_bytes(batch) <= budget
        if index < len(batches) - 1:  # greedy: the next point did not fit
            nxt = batches[index + 1][0]
            assert sum(estimate_point_bytes(p) for p in batch) + estimate_point_bytes(nxt) > budget - 1024
    # At 32 dimensions the whole page is one request; at 1024 and 4096 it splits.
    assert (len(batches) == 1) == (dim == 32)


def test_default_copy_budget_splits_a_4096_dim_scan_page_under_the_server_limit():
    """The reported failure: one 1000-point scan page at 4096 dimensions is
    one request of about 52 MB. Under the default budget every request of
    that page stays below Qdrant's default 32 MiB limit with a margin."""
    from mainframe_rag.ingest.qdrant_io import byte_bounded_batches, estimate_point_bytes

    settings = _settings(dim=4096)
    assert settings.ingest_copy_request_bytes == 24 * 1024 * 1024
    point = _worst_point(0, 4096, nnz=200)
    assert settings.ingest_scan_page_size * estimate_point_bytes(point) > 32 * 1024 * 1024
    page = [point] * settings.ingest_scan_page_size
    batches = byte_bounded_batches(page, settings.ingest_copy_request_bytes)
    assert sum(len(b) for b in batches) == len(page)
    assert len(batches) > 1
    # Every batch of a uniform page is no larger than the first.
    assert _request_bytes(batches[0]) <= settings.ingest_copy_request_bytes


def test_a_point_over_the_copy_budget_fails_closed_before_any_batch():
    from mainframe_rag.ingest.qdrant_io import PointTooLargeError, byte_bounded_batches

    small = _worst_point(0, 32)
    huge = models.PointStruct(id="00000000-0000-4000-8000-999999999999", vector={"dense": [0.0] * 32},
                              payload={"text": "x" * (1024 * 1024)})
    with pytest.raises(PointTooLargeError, match="copy request budget"):
        byte_bounded_batches([small, huge], 1024 * 1024)


class _CopyClient:
    """Scroll by offset over src; records every dst upsert (size, wait)."""

    def __init__(self, points, dim):
        self.points = points
        self.dim = dim
        self.dst: dict[str, models.PointStruct] = {}
        self.upserts: list[tuple[int, bool]] = []
        self.created = False

    def collection_exists(self, name):
        return name == "src" or (name == "dst" and self.created)

    def get_collection(self, _name):
        return SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(
            vectors={"dense": SimpleNamespace(size=self.dim)})), payload_schema={})

    def create_collection(self, _name, **_kwargs):
        self.created = True

    def scroll(self, name, *, limit, offset=None, scroll_filter=None, with_payload=True, with_vectors=False):
        stored = self.points if name == "src" else list(self.dst.values())
        start = int(offset or 0)
        page = stored[start:start + limit]
        return page, (str(start + limit) if start + limit < len(stored) else None)

    def upsert(self, _name, *, points, wait=True):
        self.upserts.append((len(points), wait))
        self.dst.update({p.id: p for p in points})


@pytest.mark.parametrize("policy", [(1, 1, 1), (6, 3, 2)])
def test_clone_sizes_each_request_by_bytes_and_keeps_the_barrier(policy):
    """Pages are read at ingest_scan_page_size and written in byte-bounded
    requests; 1/1/1 pipelines all but the final request (#1298), any other
    policy waits on every request; the id-set check still covers the copy."""
    from mainframe_rag.ingest.qdrant_io import clone_collection, estimate_point_bytes

    dim = 4096
    points = [
        models.PointStruct(id=f"00000000-0000-4000-8000-{i:012d}", vector={"dense": [0.0] * dim},
                           payload={"doc_id": "D"})
        for i in range(250)
    ]
    client = _CopyClient(points, dim)
    settings = _settings(dim=dim, ingest_scan_page_size=100, ingest_copy_request_bytes=1024 * 1024,
                         qdrant_shard_number=policy[0], qdrant_replication_factor=policy[1],
                         qdrant_write_consistency_factor=policy[2])
    clone_collection(client, settings, "src", "dst")

    assert set(client.dst) == {p.id for p in points}
    per_point = estimate_point_bytes(points[0])
    assert all(n * per_point <= 1024 * 1024 for n, _ in client.upserts)
    assert len(client.upserts) > 3  # more requests than the three scan pages
    waits = [w for _, w in client.upserts]
    if policy == (1, 1, 1):
        assert waits[-1] is True and not any(waits[:-1])
    else:
        assert all(waits)
