"""Shared fixtures: generate original test PDFs at runtime. Never commit PDFs."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from qdrant_client import models

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))



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
