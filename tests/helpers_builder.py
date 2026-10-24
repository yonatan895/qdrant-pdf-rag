"""Builder seam for the ingest suites (in-place ingest retired, #360).

Every real run is an alias publication. Suites that cover the builder —
planning, completions, preflight gates, representation, tracing — assert on
a fixed collection, so this fixture keeps `main()`, `_dispatch` and
`_run_publish` real (its --limit and empty-corpus refusals, the hashed
prewalk, the identity gate and the publish lock) and replaces only
`_run_publish_locked`: the production builder `_run_impl` then builds the
configured collection through a `_StagingBuild`, exactly as publication
builds a staging generation. Staging resolution, verification and the alias
cutover are publication's contract and are covered in
tests/test_ingest_publish.py, never through this seam.
"""

from __future__ import annotations

import pytest

from mainframe_rag.ingest import run_ingest


def use_staging_builder(monkeypatch) -> None:
    """Route real runs through the builder seam (see the module docstring)."""

    def build_configured_collection(
        src, progress, workers, settings, tracer, root, *, vendor, product, version,
        force_reingest, prewalked, retire_docs, rules_v, authorize_from_catalog=False,
    ):
        assert not retire_docs, "retirements are a publication contract: use test_ingest_publish"
        assert not authorize_from_catalog, "a retirement authority: use test_ingest_publish"
        return run_ingest._run_impl(
            src, progress, workers, None, False, settings, tracer, root,
            vendor=vendor, product=product, version=version, force_reingest=force_reingest,
            staging=run_ingest._StagingBuild(
                staging=settings.qdrant_collection,
                live=None,
                prewalked=prewalked,
                pending_removals=frozenset(),
                retire_plan={},
                resume_verified=False,
            ),
        )

    monkeypatch.setattr(run_ingest, "_run_publish_locked", build_configured_collection)


@pytest.fixture
def staging_builder(monkeypatch):
    use_staging_builder(monkeypatch)
