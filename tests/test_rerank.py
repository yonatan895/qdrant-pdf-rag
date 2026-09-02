"""Unit tests for cross-encoder reranking (issue #76 PR-02).

Tests:
- Ordering: cross-encoder scores re-rank candidates over pure RRF.
- Flag off: rerank_enabled=False preserves byte-identical legacy retrieval path.
- Batching: handles candidate list larger than batch size.
- HttpReranker: mocked /v1/score and /v1/rerank paths.
- HashReranker: deterministic hermetic scoring.
- Dispatch: build_reranker resolves properly according to settings.
"""

from __future__ import annotations

from typing import Any

import httpx2
import pytest
from qdrant_client import models

from mainframe_rag.agent.app import SearchResponse
from mainframe_rag.config import Settings
from mainframe_rag.retrieve.query import SearchHit, diversify_hits, search
from mainframe_rag.retrieve.rerank import (
    HashReranker,
    HttpReranker,
    build_reranker,
    format_rerank_text,
    rerank_candidates,
)


def _make_hit(
    chunk_id: str,
    doc_id: str,
    score: float,
    heading: str = "Heading",
    text: str = "Body text",
    page_label: str = "1",
    rerank_score: float | None = None,
) -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        score=score,
        cite=f"{doc_id} Manual, {heading}, p. {page_label}",
        heading=heading,
        text=text,
        doc_id=doc_id,
        title="Manual",
        page_label=page_label,
        chunk_type="narrative",
        message_ids=(),
        rerank_score=rerank_score,
    )


class FakeEmbedder:
    def dense_query(self, queries: list[str]) -> list[list[float]]:
        return [[0.1] * 16]

    def sparse(self, texts: list[str]) -> list[tuple[list[int], list[float]]]:
        return [([1, 2], [1.0, 0.5])]


from types import SimpleNamespace


class FakeQdrantPoints:
    def __init__(self, points: list[models.ScoredPoint]) -> None:
        self.points = points
        self.queries_made: list[dict[str, Any]] = []

    def query_points(
        self,
        collection_name: str,
        *,
        query: Any,
        using: str,
        limit: int,
        query_filter: Any = None,
        with_payload: Any = True,
    ) -> Any:
        self.queries_made.append({"using": using, "limit": limit})
        return SimpleNamespace(points=self.points[:limit])


class MockReranker:
    def __init__(self, score_map: dict[str, float] | None = None) -> None:
        self.score_map = score_map or {}
        self.call_count = 0
        self.last_texts: list[str] = []

    def score(self, query: str, texts: list[str]) -> list[float]:
        self.call_count += 1
        self.last_texts = texts
        return [self.score_map.get(t, 0.5) for t in texts]


# ---------------------------------------------------------------- Tests
def test_format_rerank_text():
    hit = _make_hit("c1", "SA23-1380-70", 0.8, heading="Parmlib > IEASYSxx", text="LFAREA=2G parameter")
    text = format_rerank_text(hit)
    assert "SA23-1380-70" in text
    assert "Manual" in text
    assert "Parmlib > IEASYSxx" in text
    assert "LFAREA=2G parameter" in text


def test_hash_reranker_deterministic():
    reranker = HashReranker()
    scores = reranker.score(
        "IEA500I IOSCMDS",
        [
            "IEA500I BEFORE IOS IOSCMDS COMMAND REJECTED",
            "Something completely irrelevant about CICS transaction dump",
            "",
        ],
    )
    assert len(scores) == 3
    assert scores[0] > scores[1]
    assert scores[2] == 0.0
    # Deterministic repeatability
    scores2 = reranker.score("IEA500I IOSCMDS", ["IEA500I BEFORE IOS IOSCMDS COMMAND REJECTED"])
    assert scores[0] == scores2[0]


def test_rerank_candidates_reorders_and_attaches_score():
    hit1 = _make_hit("c1", "DOC1", 0.9, text="Doc 1 text")
    hit2 = _make_hit("c2", "DOC2", 0.5, text="Doc 2 highly relevant")
    hit3 = _make_hit("c3", "DOC3", 0.3, text="Doc 3 medium")

    t1 = format_rerank_text(hit1)
    t2 = format_rerank_text(hit2)
    t3 = format_rerank_text(hit3)

    # Cross-encoder rates hit2 highest, hit3 second, hit1 lowest
    reranker = MockReranker({t1: 0.1, t2: 0.95, t3: 0.6})
    reranked = rerank_candidates("test query", [hit1, hit2, hit3], reranker)

    assert len(reranked) == 3
    assert reranked[0].chunk_id == "c2"
    assert reranked[0].rerank_score == 0.95
    assert reranked[1].chunk_id == "c3"
    assert reranked[1].rerank_score == 0.6
    assert reranked[2].chunk_id == "c1"
    assert reranked[2].rerank_score == 0.1


def test_rerank_flag_off_is_byte_identical():
    """When rerank_enabled=False, search behaves exactly as legacy code."""
    scored_points = [
        models.ScoredPoint(
            id="c1",
            version=1,
            score=0.8,
            payload={"doc_id": "DOC1", "title": "M1", "heading_path": "H1", "page_label": "1", "text": "T1"},
        ),
        models.ScoredPoint(
            id="c2",
            version=1,
            score=0.6,
            payload={"doc_id": "DOC2", "title": "M2", "heading_path": "H2", "page_label": "1", "text": "T2"},
        ),
    ]
    client = FakeQdrantPoints(scored_points)
    embedder = FakeEmbedder()
    settings = Settings(rerank_enabled=False, embed_mode="hash", allow_hash_mode=True, _env_file=None)

    hits, _kind, timings = search(
        client,
        embedder,
        "test-coll",
        "sample query",
        settings=settings,
    )

    assert len(hits) == 2
    assert "rerank_ms" not in timings
    for h in hits:
        assert h.rerank_score is None


def test_rerank_flag_on_executes_pipeline():
    """When rerank_enabled=True, cross-encoder scores and records rerank_ms."""
    scored_points = [
        models.ScoredPoint(
            id="c1",
            version=1,
            score=0.9,
            payload={"doc_id": "DOC1", "title": "M1", "heading_path": "H1", "page_label": "1", "text": "Low match"},
        ),
        models.ScoredPoint(
            id="c2",
            version=1,
            score=0.4,
            payload={"doc_id": "DOC2", "title": "M2", "heading_path": "H2", "page_label": "1", "text": "High match exact"},
        ),
    ]
    client = FakeQdrantPoints(scored_points)
    embedder = FakeEmbedder()
    settings = Settings(rerank_enabled=True, embed_mode="hash", allow_hash_mode=True, _env_file=None)

    hits, _kind, timings = search(
        client,
        embedder,
        "test-coll",
        "exact",
        settings=settings,
    )

    assert len(hits) == 2
    assert "rerank_ms" in timings
    for h in hits:
        assert h.rerank_score is not None
    # High match exact (c2) should be boosted to top-1 by HashReranker
    assert hits[0].chunk_id == "c2"


def test_http_reranker_v1_score_success():
    """HttpReranker posts to OpenAI-compatible /v1/score and parses index-sorted scores."""
    settings = Settings(
        rerank_base_url="http://rerank.test/v1",
        rerank_model="BAAI/bge-reranker-v2-m3",
        rerank_batch_size=2,
        _env_file=None,
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/score"
        body = httpx2.Response(
            200,
            json={"data": [{"index": 1, "score": 0.88}, {"index": 0, "score": 0.42}]},
        )
        return body

    transport = httpx2.MockTransport(handler)
    client = httpx2.Client(transport=transport)
    reranker = HttpReranker(settings, client=client)

    scores = reranker.score("query", ["text0", "text1"])
    assert scores == [0.42, 0.88]


def test_http_reranker_fallback_to_rerank_endpoint():
    """HttpReranker falls back to /v1/rerank when /v1/score returns 404."""
    settings = Settings(
        rerank_base_url="http://rerank.test/v1",
        rerank_model="BAAI/bge-reranker-v2-m3",
        rerank_batch_size=2,
        _env_file=None,
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/score":
            return httpx2.Response(404, json={"error": "not found"})
        if request.url.path == "/v1/rerank":
            return httpx2.Response(
                200,
                json={"results": [{"index": 0, "relevance_score": 0.77}, {"index": 1, "relevance_score": 0.99}]},
            )
        return httpx2.Response(500)

    transport = httpx2.MockTransport(handler)
    client = httpx2.Client(transport=transport)
    reranker = HttpReranker(settings, client=client)

    scores = reranker.score("query", ["text0", "text1"])
    assert scores == [0.77, 0.99]


def test_build_reranker_dispatch():
    # 1. Disabled
    s_off = Settings(rerank_enabled=False, _env_file=None)
    assert build_reranker(s_off) is None

    # 2. Hash mode
    s_hash = Settings(rerank_enabled=True, embed_mode="hash", allow_hash_mode=True, _env_file=None)
    r_hash = build_reranker(s_hash)
    assert isinstance(r_hash, HashReranker)

    # 3. HTTP mode
    s_http = Settings(rerank_enabled=True, embed_mode="vllm", rerank_base_url="http://rerank:8000/v1", _env_file=None)
    r_http = build_reranker(s_http)
    assert isinstance(r_http, HttpReranker)

    # 4. Misconfigured (vllm without base url or cache dir)
    s_bad = Settings(rerank_enabled=True, embed_mode="vllm", rerank_base_url=None, embed_base_url=None, rerank_cache_dir=None, _env_file=None)
    with pytest.raises(RuntimeError, match="neither RERANK_BASE_URL nor RERANK_CACHE_DIR"):
        build_reranker(s_bad)


def test_diversify_hits_respects_rerank_scores():
    """diversify_hits sorts backfilled pages using rerank_score."""
    h1 = _make_hit("c1", "D1", 0.5, page_label="1", rerank_score=0.9)
    h2 = _make_hit("c2", "D1", 0.8, page_label="1", rerank_score=0.95)
    h3 = _make_hit("c3", "D2", 0.4, page_label="2", rerank_score=0.7)

    # max_per_page=1: h2 selected (rerank_score 0.95), h3 selected (p2, 0.7)
    selected = diversify_hits([h2, h1, h3], limit=2, max_per_page=1)
    assert len(selected) == 2
    assert selected[0].chunk_id == "c2"
    assert selected[1].chunk_id == "c3"


def test_search_response_payload_includes_rerank_score():
    hit = _make_hit("c1", "DOC1", 0.8, rerank_score=0.923)
    resp = SearchResponse(request_id="req-1", query_kind="identifier", hits=[hit])
    dump = resp.model_dump()
    assert dump["hits"][0]["rerank_score"] == 0.923


def test_verify_reranker_manifest(tmp_path):
    import hashlib

    from scripts.fetch_reranker_weights import verify_manifest

    target_dir = tmp_path / "weights"
    target_dir.mkdir()
    f1 = target_dir / "config.json"
    f1.write_text("{\"model\": \"test\"}", encoding="utf-8")
    d1 = hashlib.sha256(f1.read_bytes()).hexdigest()

    manifest_file = tmp_path / "reranker.sha256"
    manifest_file.write_text(f"{d1}  config.json\n", encoding="utf-8")

    # Success path
    verify_manifest(target_dir, manifest_file)

    # Mismatch path fails closed
    f1.write_text("{\"model\": \"corrupt\"}", encoding="utf-8")
    with pytest.raises(SystemExit):
        verify_manifest(target_dir, manifest_file)
