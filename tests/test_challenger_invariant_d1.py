"""Adversarial stress test suite challenging Invariant D1 (Retrieval Scope Retention).

Author: challenger_m1_1
Focus: Adversarially stress test async_search and fallback filter behavior under diverse conditions:
  - Mixed scope parameters (only product, only version, both, none, empty strings).
  - Scope parameters matching zero points vs matching candidate points.
  - Queries with multiple identifiers (doc_ids, message_ids, members, mixed, partial matches) vs queries without identifiers.
  - Out-of-scope record leakage resistance across dense/sparse score differences, asymmetric legs, reranking, and query splitting.
  - Parity across batch/legacy transport and sync/async search interfaces.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from qdrant_client import QdrantClient, models

from mainframe_rag.config import Settings
from mainframe_rag.retrieve.filters import (
    build_scope_filter,
)
from mainframe_rag.retrieve.query import (
    async_search,
    search,
)
from tests.fakes import EmbedderFake, PromotingRerankerFake

# ---------------------------------------------------------------------------
# Filter matching and adversarial fake Qdrant double
# ---------------------------------------------------------------------------


def _matches_filter(point: models.ScoredPoint, flt: models.Filter | None) -> bool:
    if flt is None:
        return True
    payload = point.payload or {}
    for cond in flt.must or []:
        val = payload.get(cond.key)
        if isinstance(cond.match, models.MatchValue):
            if val != cond.match.value:
                return False
        elif isinstance(cond.match, models.MatchAny):
            if isinstance(val, (list, tuple)):
                if not any(v in val for v in cond.match.any):
                    return False
            elif val not in cond.match.any:
                return False
    return True


class AdversarialScopeQdrant:
    """Rigorous mock Qdrant supporting batch & sequential queries, custom dense/sparse pools,
    and recording all requests and filters."""

    def __init__(
        self,
        dense_points: list[models.ScoredPoint] | None = None,
        sparse_points: list[models.ScoredPoint] | None = None,
        all_points: list[models.ScoredPoint] | None = None,
    ):
        if all_points is not None:
            self._dense = list(all_points)
            self._sparse = list(all_points)
        else:
            self._dense = list(dense_points or [])
            self._sparse = list(sparse_points or [])
        self.batch_calls = 0
        self.single_calls = 0
        self.queries: list[dict[str, Any]] = []
        self.batch_requests: list[models.QueryRequest] = []

    def query_batch_points(self, collection: str, requests: list[models.QueryRequest], **_):
        self.batch_calls += 1
        results = []
        for req in requests:
            self.queries.append(
                {"using": req.using, "filter": req.filter, "with_payload": req.with_payload}
            )
            self.batch_requests.append(req)
            pool = self._dense if req.using == "dense" else self._sparse
            matching = [p for p in pool if _matches_filter(p, req.filter)]
            # Sort by score descending and apply limit
            sorted_matching = sorted(matching, key=lambda p: p.score, reverse=True)[:req.limit]
            results.append(SimpleNamespace(points=sorted_matching))
        return results

    def query_points(
        self,
        collection: str,
        query: Any,
        using: str,
        limit: int,
        query_filter: models.Filter | None,
        with_payload: Any,
        **_,
    ):
        self.single_calls += 1
        self.queries.append({"using": using, "filter": query_filter, "with_payload": with_payload})
        pool = self._dense if using == "dense" else self._sparse
        matching = [p for p in pool if _matches_filter(p, query_filter)]
        sorted_matching = sorted(matching, key=lambda p: p.score, reverse=True)[:limit]
        return SimpleNamespace(points=sorted_matching)


class LegacyAdversarialScopeQdrant:
    """Method-less double: deliberately has NO query_batch_points so
    retrieve's hasattr dispatch pins the sequential query_points fallback path."""

    def __init__(
        self,
        dense_points: list[models.ScoredPoint] | None = None,
        sparse_points: list[models.ScoredPoint] | None = None,
        all_points: list[models.ScoredPoint] | None = None,
    ):
        if all_points is not None:
            self._dense = list(all_points)
            self._sparse = list(all_points)
        else:
            self._dense = list(dense_points or [])
            self._sparse = list(sparse_points or [])
        self.single_calls = 0
        self.queries: list[dict[str, Any]] = []

    def query_points(
        self,
        collection: str,
        query: Any,
        using: str,
        limit: int,
        query_filter: models.Filter | None,
        with_payload: Any,
        **_,
    ):
        self.single_calls += 1
        self.queries.append({"using": using, "filter": query_filter, "with_payload": with_payload})
        pool = self._dense if using == "dense" else self._sparse
        matching = [p for p in pool if _matches_filter(p, query_filter)]
        sorted_matching = sorted(matching, key=lambda p: p.score, reverse=True)[:limit]
        return SimpleNamespace(points=sorted_matching)


class AsyncAdversarialScopeQdrant(AdversarialScopeQdrant):
    """Async variant returning awaitables for query_batch_points and query_points."""

    async def query_batch_points(self, collection: str, requests: list[models.QueryRequest], **kwargs):
        return super().query_batch_points(collection, requests, **kwargs)

    async def query_points(self, *args, **kwargs):
        return super().query_points(*args, **kwargs)


def _make_point(
    pid: str,
    *,
    product: str | None = "z/OS",
    version: str | None = "3.1",
    doc_id: str = "SA22-7592-05",
    message_ids: list[str] | None = None,
    members: list[str] | None = None,
    score: float = 1.0,
    text: str = "sample text",
) -> models.ScoredPoint:
    payload = {
        "product": product,
        "version": version,
        "doc_id": doc_id,
        "title": f"Doc {doc_id}",
        "heading_path": "Chapter 1 > Section",
        "page_label": "1",
        "chunk_type": "narrative",
        "message_ids": message_ids or [],
        "members": members or [],
        "text": text,
    }
    return models.ScoredPoint(id=pid, version=1, score=score, payload=payload)


@pytest.fixture
def embedder():
    return EmbedderFake()


# ============================================================================
# Dimension 1: Mixed Scope Parameters (all 8 combinations, empty strings, special chars)
# ============================================================================


class TestMixedScopeParameters:
    """Stress test build_scope_filter and async_search under all permutations of scope parameters."""

    @pytest.mark.parametrize(
        ("product", "version", "expected_keys"),
        [
            (None, None, set()),
            ("z/OS", None, {"product"}),
            (None, "3.1", {"version"}),
            ("z/OS", "3.1", {"product", "version"}),
        ],
    )
    def test_build_scope_filter_exact_keys(self, product, version, expected_keys):
        """Invariant D1: build_scope_filter constructs filters with exactly the caller-specified keys."""
        flt = build_scope_filter(product=product, version=version)
        if not expected_keys:
            assert flt is None
        else:
            assert flt is not None
            assert flt.must is not None
            keys = {c.key for c in flt.must}
            assert keys == expected_keys

    def test_build_scope_filter_with_empty_strings_evaluates_to_none(self):
        """Falsy empty strings ('') do not generate invalid or unmatchable filter conditions."""
        flt = build_scope_filter(product="", version="")
        assert flt is None

    def test_build_scope_filter_special_characters_preserved(self):
        """Scope parameters with spaces, dashes, dots, and slashes are preserved verbatim."""
        flt = build_scope_filter(
            product="z/OS 2.5.0-SP1",
            version="v3.1-beta/rel",
        )
        assert flt is not None
        cond_map = {c.key: c.match.value for c in flt.must}
        assert cond_map["product"] == "z/OS 2.5.0-SP1"
        assert cond_map["version"] == "v3.1-beta/rel"

    @pytest.mark.parametrize(
        ("product", "version"),
        [
            ("z/OS", None),
            (None, "3.1"),
            ("z/OS", "3.1"),
        ],
    )
    def test_search_retains_each_scope_permutation_on_fallback(self, embedder, product, version):
        """When an identifier query matches 0 points, fallback preserves whatever scope subset was passed."""
        in_scope = _make_point("in-scope", product=product or "z/OS", version=version or "3.1")
        # Construct an out-of-scope point by flipping one of the active dimensions
        out_prod = "Linux" if product else "z/OS"
        out_ver = "1.0" if version else "3.1"
        out_scope = _make_point("out-scope", product=out_prod, version=out_ver)

        fake = AdversarialScopeQdrant(all_points=[in_scope, out_scope])
        # Query with non-existent doc_id triggers fallback
        hits, kind, _ = search(
            fake,
            embedder,
            "mainframe_manuals",
            "Identify SC99-9999",
            product=product,
            version=version,
            limit=5,
        )

        assert kind == "identifier"
        assert fake.batch_calls == 2
        # Check fallback filter keys
        fallback_flt = fake.batch_requests[2].filter
        assert fallback_flt is not None
        actual_keys = {c.key for c in fallback_flt.must}
        expected_keys = {k for k, v in [("product", product), ("version", version)] if v}
        assert actual_keys == expected_keys

        # Returned hits must only include in-scope
        assert len(hits) == 1
        assert hits[0].chunk_id == "in-scope"


# ============================================================================
# Dimension 2: Scope Matching Zero Points vs Candidate Points
# ============================================================================


class TestScopeMatchingZeroVsCandidatePoints:
    """Stress test boundary behavior when zero points in DB match scope constraints."""

    def test_zero_scope_matches_with_identifier_query_returns_empty_and_never_leaks(self, embedder):
        """Invariant D1: When DB contains points for other products, but 0 points match caller's scope,
        fallback relaxing identifiers MUST NOT relax scope to return out-of-scope points."""
        linux_p1 = _make_point("lx-1", product="Linux", version="1.0", score=10.0)
        linux_p2 = _make_point("lx-2", product="Linux", version="2.0", score=20.0)
        fake = AdversarialScopeQdrant(all_points=[linux_p1, linux_p2])

        hits, kind, _ = search(
            fake,
            embedder,
            "mainframe_manuals",
            "Identify SC23-6862",
            product="z/OS",
            version="3.1",
            limit=5,
        )

        assert kind == "identifier"
        assert fake.batch_calls == 2
        # Fallback executed with scope filter
        fallback_flt = fake.batch_requests[2].filter
        assert fallback_flt is not None
        assert {c.key for c in fallback_flt.must} == {"product", "version"}
        # Zero hits returned — Linux points NEVER leak
        assert hits == []

    def test_zero_scope_matches_with_natural_language_query_no_redundant_query(self, embedder):
        """When query has no identifiers and scope matches 0 points, _needs_filter_fallback returns False,
        issuing exactly 1 batch query and returning []."""
        linux_p = _make_point("lx-1", product="Linux", version="1.0")
        fake = AdversarialScopeQdrant(all_points=[linux_p])

        hits, kind, _ = search(
            fake,
            embedder,
            "mainframe_manuals",
            "how to configure catalog address space",
            product="z/OS",
            version="3.1",
            limit=5,
        )

        assert kind == "nl"
        assert fake.batch_calls == 1  # No redundant retry
        assert hits == []

    def test_partial_scope_mismatch_strictly_excluded(self, embedder):
        """If caller asks for product="z/OS" and version="3.1", but DB only has version="2.4",
        version 2.4 must be strictly excluded even if product matches."""
        v24_point = _make_point("zos-24", product="z/OS", version="2.4", score=5.0)
        fake = AdversarialScopeQdrant(all_points=[v24_point])

        hits, _, _ = search(
            fake,
            embedder,
            "mainframe_manuals",
            "lookaside facility tuning",
            product="z/OS",
            version="3.1",
            limit=5,
        )

        assert hits == []
        assert fake.batch_calls == 1


# ============================================================================
# Dimension 3: Multiple Identifiers vs No Identifiers
# ============================================================================


class TestMultipleIdentifiersVsNoIdentifiers:
    """Stress test behavior with multiple identifiers (doc_ids, message_ids, members, mixed)."""

    def test_multi_doc_id_query_all_match(self, embedder):
        """Query with two doc_ids extracts both; MatchAny matches points having either doc_id."""
        p1 = _make_point("p1", doc_id="SA22-7592-05", product="z/OS", version="3.1")
        p2 = _make_point("p2", doc_id="SC23-6862-02", product="z/OS", version="3.1")
        fake = AdversarialScopeQdrant(all_points=[p1, p2])

        hits, kind, _ = search(
            fake,
            embedder,
            "mainframe_manuals",
            "Compare SA22-7592-05 and SC23-6862-02",
            product="z/OS",
            version="3.1",
            limit=5,
        )

        assert kind == "identifier"
        assert fake.batch_calls == 1  # Matched on first attempt
        assert {h.chunk_id for h in hits} == {"p1", "p2"}

    def test_multi_doc_id_query_none_match_fallback_retains_scope(self, embedder):
        """Query with two doc_ids matching nothing in scope triggers fallback; scope is retained."""
        in_scope = _make_point("in-scope", doc_id="SA22-0000-00", product="z/OS", version="3.1")
        out_scope = _make_point("out-scope", doc_id="SC99-9999-99", product="Linux", version="1.0")
        fake = AdversarialScopeQdrant(all_points=[in_scope, out_scope])

        hits, kind, _ = search(
            fake,
            embedder,
            "mainframe_manuals",
            "Compare SC99-9999-99 and SC99-8888-88",
            product="z/OS",
            version="3.1",
            limit=5,
        )

        assert kind == "identifier"
        assert fake.batch_calls == 2
        assert len(hits) == 1
        assert hits[0].chunk_id == "in-scope"
        assert "out-scope" not in {h.chunk_id for h in hits}

    def test_mixed_identifier_types_doc_id_message_member_conjunction_fallback(self, embedder):
        """Query with doc_id, message_id, and member creates must conditions for all three.
        If point matches only doc_id and message_id but not member, initial query fails;
        fallback relaxes identifiers and returns the in-scope point."""
        point = _make_point(
            "p1",
            doc_id="SA22-7592-05",
            message_ids=["IEF403I"],
            members=["IEASYS00"],
            product="z/OS",
            version="3.1",
        )
        fake = AdversarialScopeQdrant(all_points=[point])

        # Query contains member LPALST00 which point doesn't have -> initial match fails
        hits, kind, _ = search(
            fake,
            embedder,
            "mainframe_manuals",
            "SA22-7592-05 IEF403I LPALST00",
            product="z/OS",
            version="3.1",
            limit=5,
        )

        assert kind == "identifier"
        assert fake.batch_calls == 2
        assert len(hits) == 1
        assert hits[0].chunk_id == "p1"

    def test_lowercase_identifier_query_matches_and_retains_scope(self, embedder):
        """Query with lowercase identifiers correctly normalizes and preserves scope."""
        point = _make_point(
            "p1",
            doc_id="SA22-7592-05",
            message_ids=["IEF403I"],
            members=["IEASYS00"],
            product="z/OS",
            version="3.1",
        )
        fake = AdversarialScopeQdrant(all_points=[point])

        hits, kind, _ = search(
            fake,
            embedder,
            "mainframe_manuals",
            "what does ief403i in sa22-7592-05 say",
            product="z/OS",
            version="3.1",
            limit=5,
        )

        assert kind == "identifier"
        assert fake.batch_calls == 1
        assert len(hits) == 1
        assert hits[0].chunk_id == "p1"


# ============================================================================
# Dimension 4: Dense/Sparse Score Differences and Out-of-Scope Leakage
# ============================================================================


class TestScoreDifferencesAndOutofScopeLeakage:
    """Stress test whether out-of-scope records could leak under extreme score differences,
    dense vs sparse dominance, reranking, or query splitting."""

    def test_extreme_dense_score_disparity_never_leaks_out_of_scope(self, embedder):
        """Out-of-scope points have massive score (1,000,000.0); in-scope point has tiny score (0.0001).
        Out-of-scope points must NEVER leak."""
        in_scope = _make_point("in-scope", product="z/OS", version="3.1", score=0.0001)
        out_prod = _make_point("out-prod", product="Linux", version="3.1", score=1_000_000.0)
        out_ver = _make_point("out-ver", product="z/OS", version="1.0", score=999_999.0)

        fake = AdversarialScopeQdrant(all_points=[in_scope, out_prod, out_ver])

        hits, _, _ = search(
            fake,
            embedder,
            "mainframe_manuals",
            "SC99-9999",  # Triggers fallback
            product="z/OS",
            version="3.1",
            limit=5,
        )

        assert len(hits) == 1
        assert hits[0].chunk_id == "in-scope"
        assert {h.chunk_id for h in hits} == {"in-scope"}

    def test_extreme_sparse_score_disparity_never_leaks_out_of_scope(self, embedder):
        """Sparse leg has massive score for out-of-scope point; dense leg has 0.
        Out-of-scope points must NEVER leak."""
        in_scope = _make_point("in-scope", product="z/OS", version="3.1", score=0.001)
        out_scope = _make_point("out-scope", product="Linux", version="3.1", score=500_000.0)

        # Separate dense and sparse pools
        fake = AdversarialScopeQdrant(
            dense_points=[in_scope],
            sparse_points=[in_scope, out_scope],
        )

        hits, _, _ = search(
            fake,
            embedder,
            "mainframe_manuals",
            "general question about paging",
            product="z/OS",
            version="3.1",
            limit=5,
        )

        assert len(hits) == 1
        assert hits[0].chunk_id == "in-scope"

    def test_asymmetric_leg_one_empty_does_not_trigger_fallback(self, embedder):
        """If dense leg matches an in-scope point but sparse leg matches 0,
        _needs_filter_fallback MUST NOT trigger (no fallback retry needed)."""
        in_scope = _make_point("in-scope", doc_id="SA22-7592-05", product="z/OS", version="3.1")

        fake = AdversarialScopeQdrant(
            dense_points=[in_scope],
            sparse_points=[],  # Empty sparse
        )

        hits, kind, _ = search(
            fake,
            embedder,
            "mainframe_manuals",
            "SA22-7592-05",
            product="z/OS",
            version="3.1",
            limit=5,
        )

        assert kind == "identifier"
        assert fake.batch_calls == 1  # Fallback was NOT triggered because dense had hits
        assert len(hits) == 1
        assert hits[0].chunk_id == "in-scope"

    def test_reranker_with_promoted_out_of_scope_points_never_receives_them(self, embedder):
        """A promoting reranker that assigns highest scores to later candidates
        CANNOT promote out-of-scope points because Qdrant filters prevent them from reaching reranker."""
        in_scope = _make_point("in-scope", product="z/OS", version="3.1", score=0.5)
        out_scope = _make_point("out-scope", product="Linux", version="3.1", score=100.0)

        fake = AdversarialScopeQdrant(all_points=[in_scope, out_scope])
        reranker = PromotingRerankerFake()

        settings = Settings(
            rerank_enabled=True,
            embed_mode="hash",
            allow_hash_mode=True,
            _env_file=None,
        )

        hits, _, timings = search(
            fake,
            embedder,
            "mainframe_manuals",
            "paging and swapping configuration",
            product="z/OS",
            version="3.1",
            limit=5,
            settings=settings,
            reranker=reranker,
        )

        assert len(hits) == 1
        assert hits[0].chunk_id == "in-scope"
        assert "out-scope" not in {h.chunk_id for h in hits}
        # Verify reranker ran on in-scope candidate only
        assert reranker.call_count == 1
        assert hits[0].rerank_score is not None
        assert "rerank_ms" in timings

    def test_query_splitting_comparative_preserves_scope_across_all_legs(self, embedder):
        """Comparative split query ('X vs Y') evaluates multiple legs; each leg strictly
        shares caller scope filter, preventing cross-leg leakage."""
        in_scope_1 = _make_point("p1", doc_id="SA22-7592-05", product="z/OS", version="3.1", text="vlf sizing")
        in_scope_2 = _make_point("p2", doc_id="SA22-7592-05", product="z/OS", version="3.1", text="dlf sizing")
        out_scope = _make_point("out", product="Linux", version="3.1", text="vlf sizing comparison")

        fake = AdversarialScopeQdrant(all_points=[in_scope_1, in_scope_2, out_scope])

        hits, _, _ = search(
            fake,
            embedder,
            "mainframe_manuals",
            "vlf sizing vs dlf sizing",
            product="z/OS",
            version="3.1",
            limit=5,
        )

        assert len(hits) > 0
        hit_ids = {h.chunk_id for h in hits}
        assert "out" not in hit_ids
        for h in hits:
            assert h.product == "z/OS"


# ============================================================================
# Dimension 5: Transport & Interface Parity (Batch vs Legacy, Sync vs Async)
# ============================================================================


class TestTransportAndInterfaceParity:
    """Stress test scope retention across transport variations:
    LegacyQdrant (sequential fallback) and AsyncQdrant client."""

    def test_legacy_sequential_qdrant_client_retains_scope(self, embedder):
        """A client without query_batch_points takes the sequential _async_prefetch_one path;
        it must retain scope filters on fallback and reject out-of-scope records."""
        in_scope = _make_point("in-scope", product="z/OS", version="3.1")
        out_scope = _make_point("out-scope", product="Linux", version="3.1")

        fake = LegacyAdversarialScopeQdrant(all_points=[in_scope, out_scope])
        assert not hasattr(fake, "query_batch_points")

        hits, kind, _ = search(
            fake,
            embedder,
            "mainframe_manuals",
            "Identify SC99-9999",  # Triggers fallback
            product="z/OS",
            version="3.1",
            limit=5,
        )

        assert kind == "identifier"
        # 2 legs (dense, bm25) * 2 attempts (initial, fallback) = 4 single calls
        assert fake.single_calls == 4
        assert len(hits) == 1
        assert hits[0].chunk_id == "in-scope"

    def test_async_search_direct_invocation_scope_retention(self, embedder):
        """Direct invocation of async_search with AsyncQdrantPoints strictly enforces Invariant D1."""
        in_scope = _make_point("in-scope", product="z/OS", version="3.1")
        out_scope = _make_point("out-scope", product="Linux", version="3.1")

        fake = AsyncAdversarialScopeQdrant(all_points=[in_scope, out_scope])

        hits, kind, _ = asyncio.run(
            async_search(
                fake,
                embedder,
                "mainframe_manuals",
                "Identify SC99-9999",
                product="z/OS",
                version="3.1",
                limit=5,
            )
        )

        assert kind == "identifier"
        assert fake.batch_calls == 2
        assert len(hits) == 1
        assert hits[0].chunk_id == "in-scope"

    def test_empty_results_when_all_points_out_of_scope_legacy_sequential(self, embedder):
        """Sequential client returns [] when no points match scope, never falling back to unfiltered."""
        out_scope = _make_point("out-scope", product="Linux", version="3.1")
        fake = LegacyAdversarialScopeQdrant(all_points=[out_scope])

        hits, _, _ = search(
            fake,
            embedder,
            "mainframe_manuals",
            "Identify SC99-9999",
            product="z/OS",
            version="3.1",
            limit=5,
        )

        assert fake.single_calls == 4
        assert hits == []


# ============================================================================
# Dimension 6: Indexed Source Behavior Across Mocked and Real Qdrant Configurations
# ============================================================================


def _make_real_qdrant_client(points: list[models.ScoredPoint], dim: int = 4) -> tuple[QdrantClient, str]:
    """Build a real in-memory QdrantClient with payload schema indexes mirroring production."""
    client = QdrantClient(":memory:")
    col_name = "test_col"
    client.create_collection(
        col_name,
        vectors_config={"dense": models.VectorParams(size=dim, distance=models.Distance.COSINE)},
        sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
    )
    for kw in (
        "vendor",
        "product",
        "version",
        "doc_id",
        "chunk_type",
        "message_ids",
        "members",
        "sha256",
        "source_rev",
    ):
        client.create_payload_index(col_name, field_name=kw, field_schema=models.PayloadSchemaType.KEYWORD)
    client.create_payload_index(col_name, field_name="page_start", field_schema=models.PayloadSchemaType.INTEGER)

    real_points = []
    for idx, p in enumerate(points, 1):
        point_id = int(p.id) if str(p.id).isdigit() else idx
        real_points.append(
            models.PointStruct(
                id=point_id,
                vector={
                    "dense": [0.1] * dim,
                    "bm25": models.SparseVector(indices=[3], values=[1.0]),
                },
                payload=p.payload,
            )
        )
    client.upsert(col_name, real_points)
    return client, col_name


async def _make_async_real_qdrant_client(points: list[models.ScoredPoint], dim: int = 4) -> tuple[Any, str]:
    """Build a real in-memory AsyncQdrantClient with payload schema indexes mirroring production."""
    from qdrant_client.async_qdrant_client import AsyncQdrantClient as RealAsyncQdrantClient

    client = RealAsyncQdrantClient(":memory:")
    col_name = "test_col_async"
    await client.create_collection(
        col_name,
        vectors_config={"dense": models.VectorParams(size=dim, distance=models.Distance.COSINE)},
        sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
    )
    for kw in (
        "vendor",
        "product",
        "version",
        "doc_id",
        "chunk_type",
        "message_ids",
        "members",
        "sha256",
        "source_rev",
    ):
        await client.create_payload_index(col_name, field_name=kw, field_schema=models.PayloadSchemaType.KEYWORD)
    await client.create_payload_index(col_name, field_name="page_start", field_schema=models.PayloadSchemaType.INTEGER)

    real_points = []
    for idx, p in enumerate(points, 1):
        point_id = int(p.id) if str(p.id).isdigit() else idx
        real_points.append(
            models.PointStruct(
                id=point_id,
                vector={
                    "dense": [0.1] * dim,
                    "bm25": models.SparseVector(indices=[3], values=[1.0]),
                },
                payload=p.payload,
            )
        )
    await client.upsert(col_name, real_points)
    return client, col_name


class TestScopedRetentionAcrossMockAndRealQdrant:
    """Invariant D1 scope preservation (product/version) across both mocked
    and real in-memory vector configurations in Qdrant."""

    def test_scope_matching_and_isolation_real_and_mock(self, embedder):
        """Exact identifier match and fallback retention match z/OS 3.1
        records while strictly isolating other products and versions."""
        p_zos = _make_point("1", product="z/OS", version="3.1", doc_id="SA22-7592-05")
        p_old = _make_point("2", product="z/OS", version="2.4", doc_id="SA22-8000-01")
        p_lx = _make_point("3", product="Linux", version="1.0", doc_id="LN-0001-00")

        points = [p_zos, p_old, p_lx]
        mock_client = AdversarialScopeQdrant(all_points=points)
        real_client, col = _make_real_qdrant_client(points)

        # 1. Exact identifier match within scope
        for client in (mock_client, real_client):
            hits, kind, _ = search(
                client,
                embedder,
                col,
                "SA22-7592-05",
                product="z/OS",
                version="3.1",
            )
            assert kind == "identifier"
            assert len(hits) == 1
            assert hits[0].chunk_id == "1"

        # 2. Fallback retry when identifier SC99-9999 is missing retains scope
        for client in (mock_client, real_client):
            hits, kind, _ = search(
                client,
                embedder,
                col,
                "Identify SC99-9999",
                product="z/OS",
                version="3.1",
            )
            assert kind == "identifier"
            assert len(hits) == 1
            assert hits[0].chunk_id == "1"
            assert {h.chunk_id for h in hits} == {"1"}

    def test_scope_zero_match_returns_empty_and_never_leaks(self, embedder):
        """When 0 points match caller scope, fallback returns [] on both real and mock Qdrant."""
        p_old = _make_point("1", product="z/OS", version="2.4")
        p_lx = _make_point("2", product="Linux", version="1.0")

        points = [p_old, p_lx]
        mock_client = AdversarialScopeQdrant(all_points=points)
        real_client, col = _make_real_qdrant_client(points)

        # Query asks for z/OS 3.1 -> 0 points match
        for client in (mock_client, real_client):
            hits, kind, _ = search(
                client,
                embedder,
                col,
                "SC99-9999",
                product="z/OS",
                version="3.1",
            )
            assert kind == "identifier"
            assert hits == []

    def test_scope_mismatch_on_matching_identifier_forces_fallback_isolation(self, embedder):
        """When a point matches the doc_id but its version is out-of-scope, the initial query
        fails, fallback relaxes the identifier but retains scope, returning in-scope points only."""
        # Hostile point: matches requested doc_id SA22-7592-05, but version is out of scope
        p_hostile = _make_point("1", product="z/OS", version="2.4", doc_id="SA22-7592-05")
        # In-scope point: different doc_id, matching product and version
        p_in_scope = _make_point("2", product="z/OS", version="3.1", doc_id="SA22-0000-00")

        points = [p_hostile, p_in_scope]
        mock_client = AdversarialScopeQdrant(all_points=points)
        real_client, col = _make_real_qdrant_client(points)

        for client in (mock_client, real_client):
            hits, kind, _ = search(
                client,
                embedder,
                col,
                "SA22-7592-05",
                product="z/OS",
                version="3.1",
            )
            assert kind == "identifier"
            assert len(hits) == 1
            assert hits[0].chunk_id == "2"
            assert "1" not in {h.chunk_id for h in hits}

    @pytest.mark.parametrize("scope", [{"product": "z/OS"}, {"version": "3.1"}, {"product": "z/OS", "version": "3.1"}])
    def test_parity_matrix_across_scope_subsets(self, embedder, scope):
        """Cross-configuration matrix: verify exact result parity between mock and real Qdrant."""
        p_zos = _make_point("1", product="z/OS", version="3.1", doc_id="SA22-7592-05")
        p_old = _make_point("2", product="z/OS", version="2.4", doc_id="SA22-8000-01")
        p_lx = _make_point("3", product="Linux", version="1.0", doc_id="LN-0001-00")

        points = [p_zos, p_old, p_lx]
        mock_client = AdversarialScopeQdrant(all_points=points)
        real_client, col = _make_real_qdrant_client(points)

        mock_hits, mock_kind, _ = search(
            mock_client, embedder, col, "SC99-9999", **scope
        )
        real_hits, real_kind, _ = search(
            real_client, embedder, col, "SC99-9999", **scope
        )

        assert mock_kind == real_kind == "identifier"
        assert {h.chunk_id for h in mock_hits} == {h.chunk_id for h in real_hits}

    def test_async_real_and_mock_scope_parity(self, embedder):
        """Async transport parity: verify async_search with real AsyncQdrantClient matches AsyncAdversarialScopeQdrant."""
        async def _run():
            p_zos = _make_point("1", product="z/OS", version="3.1", doc_id="SA22-7592-05")
            p_old = _make_point("2", product="z/OS", version="2.4", doc_id="SA22-8000-01")

            points = [p_zos, p_old]
            mock_async = AsyncAdversarialScopeQdrant(all_points=points)
            real_async, col = await _make_async_real_qdrant_client(points)

            mock_hits, mock_kind, _ = await async_search(
                mock_async, embedder, col, "SC99-9999", product="z/OS", version="3.1"
            )
            real_hits, real_kind, _ = await async_search(
                real_async, embedder, col, "SC99-9999", product="z/OS", version="3.1"
            )

            assert mock_kind == real_kind == "identifier"
            assert {h.chunk_id for h in mock_hits} == {h.chunk_id for h in real_hits} == {"1"}

        asyncio.run(_run())
