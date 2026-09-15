"""Shared fixtures: generate original test PDFs at runtime. Never commit PDFs."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from qdrant_client import models

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))


class _HermeticAsyncQdrant:
    """Empty-store lifespan double (issue #362): the agent lifespan reads
    the representation manifest at startup, so unit tests must never
    depend on an ambient sim — refused, stale, or drifted. Empty reports
    the `empty` outcome (proceed, no warnings); tests that need a verdict
    patch `qdrant_client.AsyncQdrantClient` themselves (their patch wins:
    fixtures apply first). Integration-marked tests keep the real class."""

    def __init__(self, *a, **k):
        pass

    async def retrieve(self, *a, **k):
        return []

    async def scroll(self, *a, **k):
        return ([], None)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _hermetic_qdrant_client(monkeypatch, request):
    if request.node.get_closest_marker("integration"):
        return
    monkeypatch.setattr("qdrant_client.AsyncQdrantClient", _HermeticAsyncQdrant)



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
# Shared retrieval doubles — single-sourced from tests.fakes (share
# builders, pin behavior). LegacyFakeQdrant is method-less on purpose:
# retrieve dispatches on hasattr(client, "query_batch_points"), so the
# sequential-fallback pin needs the method ABSENT, not raising.
# Names here are kept as backwards-compatible aliases so existing imports
# keep working.
# ---------------------------------------------------------------------------

from tests.fakes import (  # noqa: F401
    EmbedderFake as FakeEmbedder,
)
from tests.fakes import (  # noqa: F401
    LegacyQdrantFake as LegacyFakeQdrant,
)
from tests.fakes import (  # noqa: F401
    PromotingRerankerFake as PromotingReranker,
)
from tests.fakes import (  # noqa: F401
    QdrantFake as FakeQdrant,
)
from tests.fakes import (  # noqa: F401
    RerankerFake as MockReranker,
)
from tests.fakes import (  # noqa: F401
    make_hit as _make_hit,
)
from tests.fakes import (
    make_point as _point,
)


def _typed_point(pid: str, chunk_type: str, page: str, score: float = 1.0) -> models.ScoredPoint:
    """_point with an overridden payload chunk_type/page so per-type BM25
    boosts have something to read without diversify collapsing the pool."""
    base = _point(pid, score)
    payload = dict(base.payload or {})
    payload["chunk_type"] = chunk_type
    payload["page_label"] = page
    return base.model_copy(update={"payload": payload})


@pytest.fixture(autouse=True)
def _servable_representation_gate(monkeypatch, request):
    """Default serving gate for the hermetic suite (issues #391 F3/F4).

    Endpoint tests monkeypatch `retrieve_search`/LLM seams directly, so the
    gate would otherwise try to resolve the configured alias against an
    unreachable Qdrant and turn every request into a 503. This fixture
    installs a servable gate bound to the configured collection; gate and
    refusal tests override `app_mod.serving_gate` themselves, and the
    integration tier keeps the real gate (real Qdrant)."""
    if request.node.get_closest_marker("integration"):
        yield None
        return
    from mainframe_rag.agent import app as app_mod
    from tests.fakes import ServingGateFake

    monkeypatch.setattr(app_mod, "serving_gate", ServingGateFake())
    yield
