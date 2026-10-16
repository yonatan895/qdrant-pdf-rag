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
    STATE_COMMITTED,
    STATE_PENDING,
    begin_manifest,
    build_manifest,
    check_ingest_compatible,
    commit_manifest,
    compare_manifests,
    manifest_digest,
    manifest_point_id,
    read_manifest_record,
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

    def scroll(self, name, *, scroll_filter=None, limit=10, with_payload=None, with_vectors=False, offset=None):
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
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("extraction_rules", "0" * 16),
        ("embed_model_revision", "rev-2"),
        ("dense_dim", 768),
    ],
)
def test_compare_each_reembed_field_rejects(field, value):
    s = _settings()
    stored = build_manifest(s, RULES).model_copy(update={field: value})
    outcome, fields = compare_manifests(stored, build_manifest(s, RULES))
    assert outcome == REEMBED_REQUIRED
    assert field in fields


def test_compare_manifests_compatible_and_record_only():
    s = _settings()
    wanted = build_manifest(s, RULES)
    assert compare_manifests(build_manifest(s, RULES), wanted) == (COMPATIBLE, [])
    # Issue #391 F1 follow-up: the ingest Job's empty-string CONTEXT_LLM_MODEL
    # and the agent's unset value are one contract value; without
    # normalization a published generation read reembed_required forever.
    assert compare_manifests(
        build_manifest(_settings(context_llm_model=""), RULES),
        build_manifest(_settings(context_llm_model=None), RULES),
    ) == (COMPATIBLE, [])
    # The query prefix is record-only, and a re-embed field wins over it.
    prefix_only = wanted.model_copy(update={"dense_query_prefix": "OTHER:"})
    assert compare_manifests(prefix_only, wanted) == (RECORD_ONLY_DRIFT, ["dense_query_prefix"])
    both = wanted.model_copy(update={"embed_model_revision": "rev-2", "dense_query_prefix": "OTHER:"})
    outcome, fields = compare_manifests(both, wanted)
    assert outcome == REEMBED_REQUIRED
    assert "embed_model_revision" in fields


# -------------------------------------------------------------- attestation --
@pytest.mark.parametrize("rev", ["", "   "])
def test_attestation_rejects_a_blank_revision_before_store_contact(rev):
    s = _settings(embed_mode="vllm", embed_model="m", dense_dim=64,
                  embed_base_url="http://x/v1", embed_model_revision=rev)
    with pytest.raises(RuntimeError, match="EMBED_MODEL_REVISION"):
        require_attested_revision(s)
    with pytest.raises(RuntimeError, match="EMBED_MODEL_REVISION"):
        check_ingest_compatible(ExplodingStore(), s, "coll__completions", RULES)


def test_attestation_accepts_a_declared_revision_and_exempts_hash_mode():
    require_attested_revision(
        _settings(embed_mode="vllm", embed_model="m", dense_dim=64,
                  embed_base_url="http://x/v1", embed_model_revision="rev-1")
    )
    require_attested_revision(_settings())


# --------------------------------------------------------------- preflight ---
def test_preflight_proceeds_for_empty_compatible_and_record_only_targets():
    s = _settings()
    digest, drift = check_ingest_compatible(SyncStore(), s, _completions(s), RULES)
    assert (digest, drift) == (manifest_digest(s, RULES), [])
    store = SyncStore()
    write_manifest(store, _completions(s), s, RULES)
    store._points.setdefault(s.qdrant_collection, []).append(
        SimpleNamespace(id="p1", payload={"doc_id": "D"}, vector=None)
    )
    _, drift = check_ingest_compatible(store, s, _completions(s), RULES)
    assert drift == []
    s2 = _settings(dense_query_prefix="OTHER:")
    _, drift = check_ingest_compatible(store, s2, _completions(s2), RULES)
    assert drift == ["dense_query_prefix"]


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
    with pytest.raises(RuntimeError, match="representation drift on embed_model_revision") as exc:
        check_ingest_compatible(store, s2, _completions(s2), RULES)
    assert "--reingest" in str(exc.value)


def test_preflight_legacy_or_corrupt_manifest_rejects_with_attest_path():
    s = _settings()
    store = SyncStore()
    store._points.setdefault(s.qdrant_collection, []).append(
        SimpleNamespace(id="p1", payload={"doc_id": "D"}, vector=None)
    )
    with pytest.raises(RuntimeError, match="predates the representation manifest"):
        check_ingest_compatible(store, s, _completions(s), RULES)
    # A corrupt manifest point reads as legacy.
    store._points.setdefault(_completions(s), []).append(
        SimpleNamespace(
            id=manifest_point_id(_completions(s)),
            payload={"record_type": _MANIFEST_KEY_PREFIX, "manifest": {"bogus": 1}},
            vector=None,
        )
    )
    with pytest.raises(RuntimeError, match="predates the representation manifest"):
        check_ingest_compatible(store, s, _completions(s), RULES)


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


def test_rekey_inherits_live_state_verbatim():
    """Staging inherits live's contract verbatim: nothing is recomputed from
    current settings (a drifted run must still fail its preflight, otherwise
    cloning would launder a migration into a pass), a pending live is not
    laundered into a committed staging, and an absent source writes nothing."""
    s = _settings()
    store = SyncStore()
    assert rekey_manifest(store, "live__completions", "staging__completions") is False
    assert store.upserts == 0

    live_comp, staging_comp = "live__completions", "staging__completions"
    write_manifest(store, live_comp, s, RULES)  # live attests rev ""
    assert rekey_manifest(store, live_comp, staging_comp) is True
    s2 = _settings(embed_model_revision="rev-2", qdrant_collection="staging")
    store._points.setdefault("staging", []).append(
        SimpleNamespace(id="p1", payload={"doc_id": "D"}, vector=None)
    )
    with pytest.raises(RuntimeError, match="representation drift on embed_model_revision"):
        check_ingest_compatible(store, s2, staging_comp, RULES)

    pending_live, pending_staging = "plive__completions", "pstaging__completions"
    write_manifest(store, pending_live, s, RULES, state=STATE_PENDING)
    assert rekey_manifest(store, pending_live, pending_staging) is True
    assert read_manifest_record(store, pending_staging).state == STATE_PENDING


# ----------------------------------------------------------------- serving ---
@pytest.mark.anyio
async def test_serving_outcome_per_stored_contract():
    s = _settings()
    comp = _completions(s)

    async def outcome(store, settings=s):
        return await serving_outcome(store, settings, comp, RULES)

    assert (await outcome(ServingManifestQdrant(manifest_envelope(s, RULES, comp))))[0] == "compatible"
    assert (await outcome(ServingManifestQdrant(None, points=False)))[0] == "empty"
    drifted = manifest_envelope(s, RULES, comp, embed_model_revision="rev-2")
    kind, fields = await outcome(ServingManifestQdrant(drifted))
    assert kind == "reembed_required" and "embed_model_revision" in fields
    assert (await outcome(ServingManifestQdrant(None, points=True)))[0] == "legacy"
    assert (await outcome(ServingManifestQdrant(explode=True)))[0] == "unknown"
    # A pending contract is never servable.
    pending = manifest_envelope(s, RULES, comp, state=STATE_PENDING)
    assert await outcome(ServingManifestQdrant(pending)) == ("pending", [])
    # An unattested vllm revision raises before the store is contacted.
    unattested = _settings(embed_mode="vllm", embed_model="m", dense_dim=64,
                           embed_base_url="http://x/v1", embed_model_revision="")
    with pytest.raises(RuntimeError, match="EMBED_MODEL_REVISION"):
        await outcome(ServingManifestQdrant(explode=True), unattested)


# --------------------------------------------- lifecycle (issue #391 F2) ---
def _point(doc_id: str = "D"):
    return SimpleNamespace(id=f"p-{doc_id}", payload={"doc_id": doc_id}, vector=None)


def test_begin_commit_lifecycle():
    """A contract change opens as pending BEFORE any document work and only
    the explicit commit flips it; a pending re-declaration stays pending
    (resume), and a committed same-contract begin is zero-write."""
    s = _settings()
    store = SyncStore()
    comp = _completions(s)
    digest, mode = begin_manifest(store, comp, s, RULES)
    assert (digest, mode) == (manifest_digest(s, RULES), "pending")
    record = read_manifest_record(store, comp)
    assert record is not None
    assert (record.state, record.manifest) == (STATE_PENDING, build_manifest(s, RULES))
    writes = store.upserts
    assert begin_manifest(store, comp, s, RULES) == (digest, "pending")
    commit_manifest(store, comp, s, RULES)
    assert read_manifest_record(store, comp).state == STATE_COMMITTED
    assert store.upserts == writes + 2  # re-declared pending once, committed once
    assert begin_manifest(store, comp, s, RULES) == (digest, "already_current")
    assert store.upserts == writes + 2, "steady state is zero-write"


def test_begin_record_only_drift_commits_inline():
    """Query-prefix drift touches no stored vector, so there is nothing to
    verify: it commits immediately, never via a pending window."""
    s = _settings()
    store = SyncStore()
    comp = _completions(s)
    write_manifest(store, comp, s, RULES)
    s2 = _settings(dense_query_prefix="OTHER:")
    _, mode = begin_manifest(store, comp, s2, RULES)
    assert mode == "committed"
    record = read_manifest_record(store, comp)
    assert (record.state, record.manifest) == (STATE_COMMITTED, build_manifest(s2, RULES))


def test_preflight_pending_contract_rejects_with_resume_path():
    """An interrupted A->B migration must never look compatible: the stored
    pending state refuses skips until `--reingest` resumes."""
    s = _settings()
    store = SyncStore()
    comp = _completions(s)
    write_manifest(store, comp, s, RULES, state=STATE_PENDING)
    store._points.setdefault(s.qdrant_collection, []).append(_point())
    with pytest.raises(RuntimeError, match="unfinished representation migration") as exc:
        check_ingest_compatible(store, s, comp, RULES)
    assert "--reingest" in str(exc.value)
