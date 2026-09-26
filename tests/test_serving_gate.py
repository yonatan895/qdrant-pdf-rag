"""Serving-generation gate (issues #391 F3/F4): alias->physical resolution,
contract validation, TTL binding, and refusal.

Hermetic: scripted Qdrant doubles (`AliasQdrant`) and a fake clock; no
network. The endpoint-level behavior lives in test_agent_api/test_webui,
the real-server case in test_integration_sim.
"""

from __future__ import annotations

import pytest

from mainframe_rag.config import Settings
from mainframe_rag.ingest.representation import (
    read_manifest_record_async,
    resolve_serving_generation,
)
from mainframe_rag.ingest.rules_version import extraction_rules_version
from tests.fakes import AliasQdrant, manifest_envelope

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


@pytest.mark.anyio
async def test_resolve_binds_physical_and_reads_its_own_metadata():
    """F4: validation reads `<physical>__completions`; a stale collection
    under the alias-derived name must not certify or contaminate it."""
    s = _settings()
    alias, physical = s.qdrant_collection, f"{s.qdrant_collection}__genabc"
    qd = AliasQdrant(
        aliases={alias: physical},
        manifests={
            f"{physical}__completions": manifest_envelope(
                s, RULES, f"{physical}__completions"
            ),
            f"{alias}__completions": manifest_envelope(
                s, RULES, f"{alias}__completions", embed_model_revision="stale"
            ),
        },
        points={physical},
    )
    got, outcome, details = await resolve_serving_generation(qd, s, RULES)
    assert (got, outcome, details) == (physical, "compatible", [])
    assert qd.retrieved == [f"{physical}__completions"], "alias-derived metadata was read"
    assert qd.writes == [], "resolution and validation must stay read-only"


@pytest.mark.anyio
async def test_resolve_refuses_physical_drift_even_when_alias_metadata_is_compatible():
    """The alias-derived collection cannot vouch for another physical."""
    s = _settings()
    alias, physical = s.qdrant_collection, f"{s.qdrant_collection}__gendrift"
    qd = AliasQdrant(
        aliases={alias: physical},
        manifests={
            f"{physical}__completions": manifest_envelope(
                s, RULES, f"{physical}__completions", embed_model_revision="other"
            ),
            f"{alias}__completions": manifest_envelope(
                s, RULES, f"{alias}__completions"
            ),
        },
        points={physical},
    )
    got, outcome, details = await resolve_serving_generation(qd, s, RULES)
    assert (got, outcome) == (physical, "reembed_required")
    assert "embed_model_revision" in details


@pytest.mark.anyio
async def test_resolve_flat_legacy_empty_and_dangling():
    s = _settings()
    alias = s.qdrant_collection

    flat = AliasQdrant(
        manifests={f"{alias}__completions": manifest_envelope(s, RULES, f"{alias}__completions")},
        points={alias},
    )
    assert await resolve_serving_generation(flat, s, RULES) == (alias, "compatible", [])

    legacy = AliasQdrant(points={alias})
    assert await resolve_serving_generation(legacy, s, RULES) == (alias, "legacy", [])

    fresh = AliasQdrant()
    assert await resolve_serving_generation(fresh, s, RULES) == (None, "empty", [])

    dangling = AliasQdrant(aliases={alias: "gone"}, points={alias})
    assert await resolve_serving_generation(dangling, s, RULES) == (None, "empty", [])


@pytest.mark.anyio
async def test_async_manifest_read_missing_collection_is_absent_without_retrieve():
    """A fresh install reads as absent (bootstrap `empty`), never as an
    unreadable store — and never pays a metadata read for it."""
    s = _settings()
    qd = AliasQdrant()
    assert await read_manifest_record_async(qd, f"{s.qdrant_collection}__completions") is None
    assert qd.retrieved == []


@pytest.mark.anyio
async def test_gate_caches_within_ttl_and_revalidates(monkeypatch):
    """One validation serves many requests; a new resolution happens only
    after the TTL or when explicitly requested (fresh=True)."""
    import mainframe_rag.agent.serving as serving_mod

    clock = {"t": 1000.0}
    monkeypatch.setattr(serving_mod.time, "monotonic", lambda: clock["t"])
    calls: list[str] = []

    async def fake_resolve(client, settings, rules_v):
        calls.append(settings.qdrant_collection)
        return "physical-1", "compatible", []

    monkeypatch.setattr(serving_mod, "resolve_published_generation", fake_resolve)
    s = _settings()
    gate = serving_mod.ServingGate(ttl_s=5.0)

    first = await gate.generation(object(), s, RULES)
    again = await gate.generation(object(), s, RULES)
    assert calls == [s.qdrant_collection]
    assert first is again

    clock["t"] += 6.0
    refreshed = await gate.generation(object(), s, RULES)
    assert calls == [s.qdrant_collection, s.qdrant_collection]
    assert refreshed is not first

    forced = await gate.generation(object(), s, RULES, fresh=True)
    assert calls == [s.qdrant_collection] * 3
    assert forced is not refreshed
    assert refreshed.physical == "physical-1" and refreshed.servable


@pytest.mark.anyio
async def test_gate_zero_ttl_validates_every_request_and_caches_refusals(monkeypatch):
    """TTL=0 is the paranoid mode: every request revalidates. A refusal is a
    generation too (negative caching is what TTL>0 caches), and `servable`
    is False for every non-compatible outcome."""
    import mainframe_rag.agent.serving as serving_mod

    calls: list[int] = []

    async def fake_resolve(client, settings, rules_v):
        calls.append(1)
        return "physical-1", "pending", []

    monkeypatch.setattr(serving_mod, "resolve_published_generation", fake_resolve)
    s = _settings()
    gate = serving_mod.ServingGate(ttl_s=0.0)
    generation = await gate.generation(object(), s, RULES)
    await gate.generation(object(), s, RULES)
    assert len(calls) == 2
    assert generation.outcome == "pending" and not generation.servable


@pytest.mark.anyio
@pytest.mark.parametrize("damage", [None, "version", "uuid", "pair", "logical", "missing-record",
                                    "missing-data-alias", "missing-control-alias", "redirect-control", "seal-missing", "seal-schema", "seal-count"])
@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("schema", [1, 2])
async def test_serving_requires_full_published_build_pair(damage, direct, schema):
    s = _settings()
    alias = s.qdrant_collection
    physical = alias + "__gen_build_test"
    control = physical + "__completions"
    build_id = "12345678-1234-4234-8234-123456789abc"
    data_alias = alias + "__build_" + build_id
    control_alias = data_alias + "__completions"
    payload = {"record_type": "publication-metadata", "target_collection": control,
               "build_schema": schema, "build_id": build_id, "logical_alias": alias,
               "data_collection": physical, "gen_fp": "recipe", "corpus_fp": "corpus"}
    if schema == 2 or (damage and damage.startswith("seal-")):
        payload["build_schema"] = 2
        payload["content_seal"] = {"schema": 1, "data": {"count": 1, "sha256": "a" * 64},
                                   "control": {"count": 2, "sha256": "b" * 64}}
    if damage == "seal-missing":
        del payload["content_seal"]
    elif damage == "seal-schema":
        payload["content_seal"]["schema"] = 999
    elif damage == "seal-count":
        payload["content_seal"]["data"]["count"] = True
    qd = AliasQdrant(
        aliases={alias: physical, data_alias: physical, control_alias: control},
        manifests={control: manifest_envelope(s, RULES, control)},
        publications={control: payload}, points={physical},
    )
    if damage == "version":
        payload["build_schema"] = 99
    elif damage == "uuid":
        payload["build_id"] = build_id.upper()
    elif damage == "pair":
        payload["data_collection"] = "another-generation"
    elif damage == "logical":
        payload["logical_alias"] = "another-corpus"
    elif damage == "missing-record":
        qd.publications.clear()
    elif damage == "missing-data-alias":
        del qd.aliases[data_alias]
    elif damage == "missing-control-alias":
        del qd.aliases[control_alias]
    elif damage == "redirect-control":
        qd.aliases[control_alias] = "wrong__completions"
    configured = s.model_copy(update={"qdrant_collection": physical}) if direct else s
    from mainframe_rag.agent.serving import ServingGate

    generation = await ServingGate(ttl_s=0).generation(qd, configured, RULES)
    assert generation.physical == physical
    assert generation.outcome == ("compatible" if damage is None else "unknown")
    assert generation.details == (() if damage is None else ("build_control",))
    assert not qd.writes


@pytest.mark.anyio
async def test_publication_during_validation_preserves_resolved_reader(monkeypatch):
    from mainframe_rag.agent.serving import ServingGate

    settings = _settings()
    alias = settings.qdrant_collection
    old, new = alias + "__old", alias + "__new"
    aliases = {alias: old}
    records = {}
    for physical, build_id in ((old, "12345678-1234-4234-8234-123456789abc"),
                               (new, "22345678-1234-4234-8234-123456789abc")):
        controls = physical + "__completions"
        private = alias + "__build_" + build_id
        aliases[private] = physical
        aliases[private + "__completions"] = controls
        records[controls] = {
            "record_type": "publication-metadata", "target_collection": controls,
            "build_schema": 1, "build_id": build_id, "logical_alias": alias,
            "data_collection": physical, "gen_fp": "recipe", "corpus_fp": physical,
        }
    client = AliasQdrant(aliases=aliases, publications=records, points={old, new},
                        manifests={physical + "__completions": manifest_envelope(
                            settings, RULES, physical + "__completions") for physical in (old, new)})
    get_aliases = client.get_aliases
    def advance_after_resolution():
        observed = get_aliases()
        client.aliases[alias] = new
        return observed
    monkeypatch.setattr(client, "get_aliases", advance_after_resolution)
    gate = ServingGate(ttl_s=0)
    admitted = await gate.generation(client, settings, RULES)
    assert admitted.physical == old and admitted.servable
    following = await gate.generation(client, settings, RULES)
    assert following.physical == new and following.servable
    assert not client.writes


@pytest.mark.anyio
@pytest.mark.parametrize("alias", ["corpus__build_notes", "corpus__build_notes__completions"])
async def test_legacy_corpus_name_with_build_substring_remains_readable(alias):
    from mainframe_rag.agent.serving import ServingGate

    settings = _settings(qdrant_collection=alias)
    physical = alias + "__legacy"
    controls = physical + "__completions"
    client = AliasQdrant(aliases={alias: physical}, points={physical},
                        manifests={controls: manifest_envelope(settings, RULES, controls)})
    generation = await ServingGate(ttl_s=0).generation(client, settings, RULES)
    assert generation.physical == physical and generation.servable
    assert not client.writes
