"""Shared test doubles: share builders, pin behavior.

Pure builders (hits, points, settings, HTTP envelopes) live here and are
safe to reuse everywhere. Behavior-pinning knobs (batch vs fallback,
str vs ChatResult, digest chars) stay explicit via arguments — each call
site names the path it locks so a shared-helper change cannot silently
flip a fallback pin into a success pin.

Test-only: never imported by src/.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from qdrant_client import models

from mainframe_rag.retrieve.query import SearchHit

# ---------------------------------------------------------------------------
# Golden-query sweep + adversarial wrap battery (docs/testing.md matrices)
# ---------------------------------------------------------------------------

GOLDEN_FILES = ("evals/golden.jsonl", "evals/paraphrase.jsonl", "evals/holdout.jsonl")


def iter_golden_queries():
    """Yield (file, query) across the golden sets."""
    import json

    for name in GOLDEN_FILES:
        p = REPO_ROOT / name
        for line in p.read_text().splitlines():
            line = line.strip()
            if line:
                yield name, json.loads(line)["query"]


# ---------------------------------------------------------------------------
# Points / hits
# ---------------------------------------------------------------------------


def make_point(pid: str, score: float = 1.0) -> models.ScoredPoint:
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


def make_hit(
    chunk_id: str = "abc123",
    doc_id: str = "SA22-0000-00",
    score: float = 0.42,
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


# ---------------------------------------------------------------------------
# Qdrant / embedder / reranker
# ---------------------------------------------------------------------------


class QdrantFake:
    """Retrieval-leg double with batched prefetch support."""

    def __init__(self, dense, sparse):
        self._dense, self._sparse = dense, sparse
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
            self.queries.append(
                {"using": req.using, "filter": req.filter, "with_payload": req.with_payload}
            )
            points = self._dense if req.using == "dense" else self._sparse
            results.append(SimpleNamespace(points=list(points)))
        return results


class LegacyQdrantFake:
    """Method-less double: deliberately has NO query_batch_points so
    retrieve's hasattr dispatch pins the sequential query_points fallback
    path. Do not add batch support here — that is the pin."""

    def __init__(self, dense, sparse):
        self._dense, self._sparse = dense, sparse
        self.queries = []

    def query_points(self, collection, query, using, limit, query_filter, with_payload, **_):
        self.queries.append({"using": using, "filter": query_filter, "with_payload": with_payload})
        points = self._dense if using == "dense" else self._sparse
        return SimpleNamespace(points=list(points))


class EmbedderFake:
    """Embedder protocol double: deterministic vectors, no network."""

    def dense(self, texts):
        return [[0.1] * 4 for _ in texts]

    def dense_query(self, queries):
        return self.dense(queries)

    def sparse(self, texts):
        return [([3], [1.0]) for _ in texts]


class RerankerFake:
    def __init__(self, score_map: dict[str, float] | None = None) -> None:
        self.score_map = score_map or {}
        self.call_count = 0
        self.last_texts: list[str] = []

    def score(self, query: str, texts: list[str]) -> list[float]:
        self.call_count += 1
        self.last_texts = texts
        return [self.score_map.get(t, 0.5) for t in texts]


class PromotingRerankerFake(RerankerFake):
    """Scores later candidates highest: without the trap gate it would
    promote the trap doc to top-1."""

    def score(self, query: str, texts: list[str]) -> list[float]:
        self.call_count += 1
        return [float(i) for i in range(len(texts))]


# ---------------------------------------------------------------------------
# HTTP envelopes (tokenizer POST, vLLM stream/post)
# ---------------------------------------------------------------------------


class PostResp:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if callable(self._payload):
            return self._payload()
        return self._payload

    def __await__(self):
        # Dual-mode: HttpxLLMClient awaits .post() on the async path and
        # calls it plainly on the sync path. Awaiting a PostResp returns
        # itself so one fake serves both legs.
        async def _self():
            return self

        return _self().__await__()


class StreamResp:
    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    def __iter__(self):
        return iter(self._lines)

    def iter_lines(self):
        return iter(self._lines)


class TokenizerPostFake:
    """Capturing POST double for VllmTokenizer tests.

    count: returned {"count": N}; status/exc/payload override for
    downgrade pins; capture dict records url/json/headers.
    """

    def __init__(
        self,
        count: int | None = 7,
        status_code: int = 200,
        extra: dict | None = None,
        raises: BaseException | None = None,
        capture: dict | None = None,
    ):
        self.count = count
        self.status_code = status_code
        self.extra = extra or {}
        self.raises = raises
        self.capture = capture if capture is not None else {}
        self.calls: list[str] = []

    def post(self, url, json, timeout=5.0, headers=None):
        if self.raises is not None:
            self.calls.append(url)
            raise self.raises
        self.calls.append(url)
        self.capture["url"] = url
        self.capture["json"] = json
        self.capture["headers"] = headers
        payload = dict(self.extra)
        if self.count is not None:
            payload = {"count": self.count, **payload}
        return SimpleNamespace(status_code=self.status_code, json=lambda: payload)


class HttpxStreamFake:
    """Async+sync stream/post double for HttpxLLMClient tests."""

    def __init__(self, lines=None, payload=None, capture: dict | None = None):
        self.lines = lines or []
        self.payload = payload or {}
        self.capture = capture if capture is not None else {}
        self.stream_bodies: list = []
        self.post_bodies: list = []

    @asynccontextmanager
    async def stream(self, method, url, json=None, headers=None):
        self.capture["method"] = method
        self.capture["url"] = url
        self.capture["json"] = json
        self.capture["headers"] = headers
        self.stream_bodies.append(json)
        yield StreamResp(self.lines)

    @contextmanager
    def stream_sync(self, method, url, json=None, headers=None):
        self.capture["method"] = method
        self.capture["url"] = url
        self.capture["json"] = json
        self.capture["headers"] = headers
        self.stream_bodies.append(json)
        yield StreamResp(self.lines)

    def post(self, url, json=None, timeout=None, headers=None):
        self.capture["url"] = url
        self.capture["json"] = json
        self.capture["headers"] = headers
        self.post_bodies.append(json)
        return PostResp(self.payload)


# ---------------------------------------------------------------------------
# Settings helper
# ---------------------------------------------------------------------------


def settings_kw(**overrides) -> dict:
    base = {
        "llm_base_url": "http://llm.internal:8000/v1",
        "llm_model_reasoning": "trial-reasoning-model",
        "_env_file": None,
    }
    base.update(overrides)
    return base


def vllm_models_mock(routes: dict[str, list[str]]):
    """httpx2.get double for /models discovery: match a url substring to a
    list of served ids. Unmatched urls (e.g. the OTEL collector probe the
    resolver also fires) answer 404, which probes tolerate."""

    def mock_get(url, timeout=None, headers=None):
        from unittest.mock import MagicMock

        for substr, ids in routes.items():
            if substr in url:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {"data": [{"id": i} for i in ids]}
                return mock_resp
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        return mock_resp

    return mock_get


def embedding_mock(dim: int = 1024):
    """httpx2.post double for the embeddings probe (dim discovery)."""

    def mock_post(url, json=None, timeout=None, headers=None):
        from unittest.mock import MagicMock

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"embedding": [0.1] * dim}]}
        return mock_resp

    return mock_post
