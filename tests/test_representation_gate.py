"""Issue #362 step 2: representation enforcement (ingest preflight + serving).

Same-dimension-but-different-representation vectors must never silently
mix. Every test forces the claimed path: drift fixtures that the old
record-only code would have sailed through must now fail closed, and
record-only drift must still proceed. Hash mode throughout (the gate is
mode-agnostic; vllm attestation is pinned separately).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mainframe_rag.config import Settings
from mainframe_rag.ingest.completion import completion_collection_name
from mainframe_rag.ingest.representation import (
    _MANIFEST_KEY_PREFIX,
    COMPATIBLE,
    RECORD_ONLY_DRIFT,
    REEMBED_REQUIRED,
    build_manifest,
    check_ingest_compatible,
    compare_manifests,
    manifest_digest,
    manifest_point_id,
    rekey_manifest,
    require_attested_revision,
    serving_outcome,
    write_manifest,
)
from mainframe_rag.ingest.rules_version import extraction_rules_version
from tests.fakes import ServingManifestQdrant, manifest_envelope

RULES = extraction_rules_version()


def _settings(**overrides):
    base = {
        "_env_file": None,
        "embed_mode": "hash",
        "embed_model": None,
        "embed_model_revision": "",
        "dense_dim": None,
        "contextual_embed_enabled": False,
        "context_llm_model": None,
        "context_max_chars": 500,
        "bm25_model": "Qdrant/bm25",
        "bm25_weights_revision": "22b8d2af71a76161e18dd432d2cee0eefa66e412",
        "dense_query_prefix": "Q:",
    }
    base.update(overrides)
    return Settings(**base)


class SyncStore:
    """Minimal sync QdrantPoints double: dict-backed points + collections."""

    def __init__(self):
        self._points: dict[str, list] = {}
        self.upserts = 0

    def collection_exists(self, name):
        return True

    def scroll(self, name, *, scroll_filter=None, limit=10, with_payload=None, offset=None):
        return self._points.get(name, [])[:limit], None

    def retrieve(self, name, ids, *, with_payload=True, with_vectors=False):
        wanted = {str(i) for i in ids}
        return [
            SimpleNamespace(
                id=p.id,
                payload=p.payload,
                # Real client default: no vector unless explicitly requested
                # (issue #391 F5 — a fake that always returns one hides the
                # projection bug).
                vector=p.vector if with_vectors else None,
            )
            for p in self._points.get(name, [])
            if str(p.id) in wanted
        ]

    def upsert(self, name, *, points, wait=True):
        # Production upsert overwrites same-id points (manifest recommit);
        # reads must never see a stale first.
        self.upserts += 1
        stored = self._points.setdefault(name, [])
        ids = {str(p.id) for p in points}
        stored[:] = [p for p in stored if str(p.id) not in ids]
        stored.extend(points)
        return SimpleNamespace()


class ExplodingStore:
    """Proves pre-contact failure: any store touch raises."""

    def collection_exists(self, name):
        raise ConnectionError("must not touch the store")

    def scroll(self, *a, **k):
        raise ConnectionError("must not touch the store")

    def retrieve(self, *a, **k):
        raise ConnectionError("must not touch the store")

    def upsert(self, *a, **k):
        raise ConnectionError("must not touch the store")


def _completions(settings):
    return completion_collection_name(settings)


# ---------------------------------------------------------------- compare ----
def test_compare_identical_is_compatible():
    s = _settings()
    outcome, fields = compare_manifests(build_manifest(s, RULES), build_manifest(s, RULES))
    assert (outcome, fields) == (COMPATIBLE, [])


@pytest.mark.parametrize(
    "field,value",
    [
        ("extraction_rules", "0" * 16),
        ("identity_schema", "doc_id"),
        ("embed_mode", "vllm"),
        ("embed_model", "other-model"),
        ("embed_model_revision", "rev-2"),
        ("dense_dim", 768),
        ("contextual_enabled", True),
        ("context_llm_model", "other-llm"),
        ("context_prompt_version", "v9"),
        ("context_max_chars", 50),
        ("sparse_model", "other/bm25"),
        ("sparse_weights_revision", "0" * 40),
        ("schema_version", 999),
    ],
)
def test_compare_each_reembed_field_rejects(field, value):
    s = _settings()
    stored = build_manifest(s, RULES).model_copy(update={field: value})
    outcome, fields = compare_manifests(stored, build_manifest(s, RULES))
    assert outcome == REEMBED_REQUIRED
    assert field in fields


def test_compare_blank_context_model_is_compatible():
    """Issue #391 F1 follow-up: the ingest Job's empty-string
    CONTEXT_LLM_MODEL and the agent's unset value are one contract value;
    without normalization the published Kind generation read
    reembed_required on context_llm_model forever."""
    stored = build_manifest(_settings(context_llm_model=""), RULES)
    wanted = build_manifest(_settings(context_llm_model=None), RULES)
    assert compare_manifests(stored, wanted) == (COMPATIBLE, [])


def test_compare_prefix_only_is_record_only():
    s = _settings()
    stored = build_manifest(s, RULES).model_copy(update={"dense_query_prefix": "OTHER:"})
    outcome, fields = compare_manifests(stored, build_manifest(s, RULES))
    assert (outcome, fields) == (RECORD_ONLY_DRIFT, ["dense_query_prefix"])


def test_compare_reembed_wins_over_record_drift():
    s = _settings()
    stored = build_manifest(s, RULES).model_copy(
        update={"embed_model_revision": "rev-2", "dense_query_prefix": "OTHER:"}
    )
    outcome, fields = compare_manifests(stored, build_manifest(s, RULES))
    assert outcome == REEMBED_REQUIRED
    assert "embed_model_revision" in fields


# -------------------------------------------------------------- attestation --
def test_attestation_vllm_blank_and_whitespace_reject():
    for rev in ("", "   "):
        s = _settings(embed_mode="vllm", embed_model="m", dense_dim=64,
                      embed_base_url="http://x/v1", embed_model_revision=rev)
        with pytest.raises(RuntimeError, match="EMBED_MODEL_REVISION"):
            require_attested_revision(s)


def test_attestation_vllm_declared_and_hash_blank_pass():
    require_attested_revision(
        _settings(embed_mode="vllm", embed_model="m", dense_dim=64,
                  embed_base_url="http://x/v1", embed_model_revision="rev-1")
    )
    require_attested_revision(_settings())  # hash mode exempt


def test_attestation_fires_before_store_contact():
    s = _settings(embed_mode="vllm", embed_model="m", dense_dim=64,
                  embed_base_url="http://x/v1", embed_model_revision="")
    with pytest.raises(RuntimeError, match="EMBED_MODEL_REVISION"):
        check_ingest_compatible(ExplodingStore(), s, "coll__completions", RULES)


# --------------------------------------------------------------- preflight ---
def test_preflight_empty_target_proceeds():
    s = _settings()
    digest, drift = check_ingest_compatible(SyncStore(), s, _completions(s), RULES)
    assert (digest, drift) == (manifest_digest(s, RULES), [])


def test_preflight_compatible_proceeds():
    s = _settings()
    store = SyncStore()
    write_manifest(store, _completions(s), s, RULES)
    store._points.setdefault(s.qdrant_collection, []).append(
        SimpleNamespace(id="p1", payload={"doc_id": "D"}, vector=None)
    )
    _, drift = check_ingest_compatible(store, s, _completions(s), RULES)
    assert drift == []


def test_preflight_same_dim_new_revision_rejects_with_migration_path():
    s = _settings()
    store = SyncStore()
    write_manifest(store, _completions(s), s, RULES)  # stored: unattested hash contract
    store._points.setdefault(s.qdrant_collection, []).append(
        SimpleNamespace(id="p1", payload={"doc_id": "D"}, vector=None)
    )
    # Same dimension, operator attests a new revision mid-stream: the stored
    # vectors predate the attestation and must never be skipped against.
    s2 = _settings(embed_model_revision="rev-2")
    with pytest.raises(RuntimeError, match="representation drift on embed_model_revision"):
        check_ingest_compatible(store, s2, _completions(s2), RULES)
    try:
        check_ingest_compatible(store, s2, _completions(s2), RULES)
    except RuntimeError as exc:
        assert "--reingest" in str(exc)


def test_preflight_legacy_unversioned_rejects_with_attest_path():
    s = _settings()
    store = SyncStore()
    store._points.setdefault(s.qdrant_collection, []).append(
        SimpleNamespace(id="p1", payload={"doc_id": "D"}, vector=None)
    )
    with pytest.raises(RuntimeError, match="predates the representation manifest"):
        check_ingest_compatible(store, s, _completions(s), RULES)


def test_preflight_corrupt_manifest_reads_as_legacy():
    s = _settings()
    store = SyncStore()
    store._points.setdefault(_completions(s), []).append(
        SimpleNamespace(
            id=manifest_point_id(_completions(s)),
            payload={"record_type": _MANIFEST_KEY_PREFIX, "manifest": {"bogus": 1}},
            vector=None,
        )
    )
    store._points.setdefault(s.qdrant_collection, []).append(
        SimpleNamespace(id="p1", payload={"doc_id": "D"}, vector=None)
    )
    with pytest.raises(RuntimeError, match="predates the representation manifest"):
        check_ingest_compatible(store, s, _completions(s), RULES)


def test_preflight_record_only_drift_proceeds_with_fields():
    s = _settings()
    store = SyncStore()
    write_manifest(store, _completions(s), s, RULES)
    store._points.setdefault(s.qdrant_collection, []).append(
        SimpleNamespace(id="p1", payload={"doc_id": "D"}, vector=None)
    )
    s2 = _settings(dense_query_prefix="OTHER:")
    _, drift = check_ingest_compatible(store, s2, _completions(s2), RULES)
    assert drift == ["dense_query_prefix"]


# ------------------------------------------------------------------- rekey ---
def test_rekey_carries_contract_to_staging_id():
    from qdrant_client import models

    s = _settings()
    store = SyncStore()
    live_comp, staging_comp = "live__completions", "staging__completions"
    manifest = build_manifest(s, RULES)
    vector = {"dense": [0.0] * 256, "bm25": models.SparseVector(indices=[0], values=[1.0])}
    store._points.setdefault(live_comp, []).append(
        SimpleNamespace(
            id=manifest_point_id(live_comp),
            payload={
                "record_type": _MANIFEST_KEY_PREFIX,
                "target_collection": live_comp,
                "manifest_digest": manifest_digest(s, RULES),
                "manifest": manifest.model_dump(mode="json"),
            },
            vector=vector,
        )
    )
    assert rekey_manifest(store, live_comp, staging_comp) is True
    # The fake honors the real projection default (no vector unless asked):
    # rekey passes only because the production call requests vectors.
    assert store.retrieve(live_comp, [manifest_point_id(live_comp)])[0].vector is None
    got = store.retrieve(staging_comp, [manifest_point_id(staging_comp)], with_vectors=True)
    assert len(got) == 1
    assert got[0].payload["manifest"] == manifest.model_dump(mode="json")
    assert got[0].payload["target_collection"] == staging_comp
    assert got[0].vector == vector


def test_rekey_absent_source_writes_nothing():
    store = SyncStore()
    assert rekey_manifest(store, "live__completions", "staging__completions") is False
    assert store.upserts == 0


def test_rekey_never_recomputes_from_current_settings():
    """Staging inherits live's contract verbatim: a drifted run must still
    see the inherited contract and fail its preflight (otherwise cloning
    would launder a migration into a pass)."""
    s = _settings()
    store = SyncStore()
    live_comp, staging_comp = "live__completions", "staging__completions"
    write_manifest(store, live_comp, s, RULES)  # live attests rev ""
    assert rekey_manifest(store, live_comp, staging_comp) is True
    s2 = _settings(embed_model_revision="rev-2", qdrant_collection="staging")
    store._points.setdefault("staging", []).append(
        SimpleNamespace(id="p1", payload={"doc_id": "D"}, vector=None)
    )
    with pytest.raises(RuntimeError, match="representation drift on embed_model_revision"):
        check_ingest_compatible(store, s2, staging_comp, RULES)


# ----------------------------------------------------------------- serving ---
@pytest.mark.anyio
async def test_serving_compatible_and_empty():
    s = _settings()
    outcome, _ = await serving_outcome(
        ServingManifestQdrant(manifest_envelope(s, RULES, _completions(s))),
        s, _completions(s), RULES,
    )
    assert outcome == "compatible"
    outcome, _ = await serving_outcome(
        ServingManifestQdrant(None, points=False), s, _completions(s), RULES
    )
    assert outcome == "empty"


@pytest.mark.anyio
async def test_serving_drift_legacy_unknown():
    s = _settings()
    drifted = manifest_envelope(s, RULES, _completions(s), embed_model_revision="rev-2")
    outcome, fields = await serving_outcome(
        ServingManifestQdrant(drifted), s, _completions(s), RULES
    )
    assert outcome == "reembed_required" and "embed_model_revision" in fields
    outcome, _ = await serving_outcome(
        ServingManifestQdrant(None, points=True), s, _completions(s), RULES
    )
    assert outcome == "legacy"
    outcome, _ = await serving_outcome(
        ServingManifestQdrant(explode=True), s, _completions(s), RULES
    )
    assert outcome == "unknown"


@pytest.mark.anyio
async def test_serving_attestation_raises_before_contact():
    s = _settings(embed_mode="vllm", embed_model="m", dense_dim=64,
                  embed_base_url="http://x/v1", embed_model_revision="")
    with pytest.raises(RuntimeError, match="EMBED_MODEL_REVISION"):
        await serving_outcome(ServingManifestQdrant(explode=True), s, _completions(s), RULES)
