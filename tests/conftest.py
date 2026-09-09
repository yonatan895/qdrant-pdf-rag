"""Shared fixtures: generate original test PDFs at runtime. Never commit PDFs."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from qdrant_client import models

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from mainframe_rag.retrieve.query import SearchHit


@pytest.fixture(scope="session")
def synthetic_pdf(tmp_path_factory) -> Path:
    from scripts.make_synthetic_pdf import build

    out = tmp_path_factory.mktemp("ibm_shape") / "SA22-0000-00_outline.pdf"
    build(out)
    return out


@pytest.fixture(scope="session")
def plain_pdf(tmp_path_factory) -> Path:
    from scripts.make_synthetic_pdf import build_plain

    out = tmp_path_factory.mktemp("plain") / "widget-guide.pdf"
    build_plain(out)
    return out


@pytest.fixture(scope="session")
def jcl_pdf(tmp_path_factory) -> Path:
    from scripts.make_synthetic_pdf import build_jcl

    out = tmp_path_factory.mktemp("jcl") / "SA22-8004-00_smpjcl.pdf"
    build_jcl(out)
    return out


@pytest.fixture(scope="session")
def rexx_pdf(tmp_path_factory) -> Path:
    from scripts.make_synthetic_pdf import build_rexx

    out = tmp_path_factory.mktemp("rexx") / "SA22-8005-00_smprexx.pdf"
    build_rexx(out)
    return out


# ---------------------------------------------------------------------------
# Shared retrieval doubles (single definition; were duplicated across
# test_query_filters.py and test_rerank.py). Plain helpers, not fixtures.
# NOT moved (behaviorally distinct, documented at each site):
# FakeQdrantPoints (no batch support — pins the fallback path),
# _FakePoints/_RecordingEmbedder (different shapes/recording),
# SplitAware* and the httpx doubles (per-test transport behavior).


def _point(pid: str, score: float = 1.0) -> models.ScoredPoint:
    return models.ScoredPoint(
        id=pid,
        version=1,
        score=score,
        payload={
            "doc_id": "SA22-0000-00",
            "title": "Synthetic Reference",
            "heading_path": "Chapter 2 > IEA500I",
            "page_label": "1-6",
            "page_start": 5,
            "chunk_type": "message",
            "product": "z/OS",
            "version": "9.9",
            "message_ids": ["IEA500I"],
            "text": "IEA500I synthetic text",
        },
    )


def _typed_point(pid: str, chunk_type: str, page: str, score: float = 1.0) -> models.ScoredPoint:
    """_point with an overridden payload chunk_type/page so per-type BM25
    boosts have something to read without diversify collapsing the pool."""
    base = _point(pid, score)
    payload = dict(base.payload or {})
    payload["chunk_type"] = chunk_type
    payload["page_label"] = page
    return base.model_copy(update={"payload": payload})


class FakeQdrant:
    def __init__(self, dense, sparse, support_batch: bool = True):
        self._dense, self._sparse = dense, sparse
        self.support_batch = support_batch
        self.queries = []
        self.batch_requests = []

    def query_points(self, collection, query, using, limit, query_filter, with_payload, **_):
        self.queries.append({"using": using, "filter": query_filter, "with_payload": with_payload})
        points = self._dense if using == "dense" else self._sparse
        return SimpleNamespace(points=list(points))

    def query_batch_points(self, collection, requests, **_):
        self.batch_requests.extend(requests)
        results = []
        for req in requests:
            self.queries.append({"using": req.using, "filter": req.filter, "with_payload": req.with_payload})
            points = self._dense if req.using == "dense" else self._sparse
            results.append(SimpleNamespace(points=list(points)))
        return results


class FakeEmbedder:
    """Embedder protocol double: deterministic vectors, no network."""

    def dense(self, texts):
        return [[0.1] * 4 for _ in texts]

    def dense_query(self, queries):
        return self.dense(queries)

    def sparse(self, texts):
        return [([3], [1.0]) for _ in texts]


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


class MockReranker:
    def __init__(self, score_map: dict[str, float] | None = None) -> None:
        self.score_map = score_map or {}
        self.call_count = 0
        self.last_texts: list[str] = []

    def score(self, query: str, texts: list[str]) -> list[float]:
        self.call_count += 1
        self.last_texts = texts
        return [self.score_map.get(t, 0.5) for t in texts]


class PromotingReranker:
    """Double that scores later candidates highest: without the #113 gate it
    would promote the trap doc (c2) to top-1."""

    def __init__(self) -> None:
        self.call_count = 0

    def score(self, query: str, texts: list[str]) -> list[float]:
        self.call_count += 1
        return [float(i) for i in range(len(texts))]
