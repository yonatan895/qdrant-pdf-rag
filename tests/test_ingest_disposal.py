"""Data disposal of a superseded generation (issue #1373 PR 2), hermetic lane.

The fake extends the listing fake (tests/test_ingest_generations.py) with the
writer surface the disposal uses and the pinned server's observed behaviour:
deleting a collection removes the aliases that point at it and orphans its
snapshot files (not listed while the name is absent, listed again when the
name is recreated); deleting a missing alias succeeds. Expected end states
are declared here from the issue's contract, not derived from the module.
"""

from __future__ import annotations

import copy
import json
import re

import pytest
from qdrant_client import models

from mainframe_rag.config import Settings
from mainframe_rag.ingest import build, disposal
from mainframe_rag.ingest import generations as gen
from mainframe_rag.ingest.build import build_aliases
from mainframe_rag.ingest.completion import acquire_publish_lock, release_run_lock
from mainframe_rag.ingest.publish import publication_metadata_point_id, write_publish_state
from mainframe_rag.ingest.retirement import InvalidTombstone, decode_tombstone, tombstone_point_id
from mainframe_rag.ingest.seal import capture_content_seal
from tests.test_ingest_generations import (
    ALIAS,
    G1,
    G2,
    G3,
    P1,
    P2,
    P3,
    Peer,
    Store,
    _not_found,
    publish,
    snap,
)

C1, C2, C3 = (p + "__completions" for p in (P1, P2, P3))
G0, P0 = "dddddddd-0000-4000-8000-000000000004", f"{ALIAS}__genddd"
STOPPED = "2026-11-28T10:00:00Z"
STEPS = ("step_tombstone", "step_alias", "step_snapshots", "step_collection", "step_retired")


class AdminPeer(Peer):
    def retrieve(self, collection_name, ids, *, with_payload=True, with_vectors=False):
        self._up()
        if collection_name not in self.store.collections:
            raise _not_found()
        points = self.store.collections[collection_name]
        return [models.Record(id=i, payload=copy.deepcopy(points[i]) if with_payload else None,
                              vector={"dense": [0.0]} if with_vectors else None)
                for i in ids if i in points]

    def _call(self, *call):
        self._up()
        self.store.calls.append(call)
        hook = getattr(self.store, "on_call", None)
        if hook:
            hook(*call)

    def upsert(self, collection_name, *, points, wait=True):
        for point in points:
            self._call("upsert", collection_name, (point.payload or {}).get("state"))
            assert wait is True and point.vector == {"dense": [0.0]}
            self.store.collections[collection_name][str(point.id)] = copy.deepcopy(point.payload)
        return True

    def update_collection_aliases(self, change_aliases_operations):
        for op in change_aliases_operations:
            name = op.delete_alias.alias_name
            self._call("delete_alias", name)
            self.store.aliases.pop(name, None)  # a missing alias succeeds (Q7)
        return True

    def delete_collection(self, collection_name):
        self._call("delete_collection", collection_name)
        self.store.collections.pop(collection_name, None)
        for name in [n for n, t in self.store.aliases.items() if t == collection_name]:
            del self.store.aliases[name]  # Q1
        return True  # snapshot files stay in store.snapshots: orphaned (Q2)


def sealed(store, build_id, physical, finished_at, **kw):
    publish(store, build_id, physical, finished_at, **kw)
    control = physical + "__completions"
    rid = publication_metadata_point_id(control)
    receipt = store.collections[control][rid]
    receipt["build_schema"] = 2
    receipt["content_seal"] = capture_content_seal(
        AdminPeer(store, 0), build_id=build_id, alias=ALIAS, physical=physical, gen_fp="g", corpus_fp="c",
        receipt_id=rid)


def world(peers: int = 1) -> Store:
    """G1 < G2 retained and sealed, G3 live; P1 has two snapshots on peer 0
    and one on every other peer."""
    store = Store(peers)
    sealed(store, G1, P1, 100.0)
    sealed(store, G2, P2, 200.0)
    sealed(store, G3, P3, 300.0, live=True)
    for peer in range(peers):
        for physical in (P1, P2, P3):
            snap(store, peer, physical, f"{physical}-p{peer}-a.snapshot", 1000 + peer)
    snap(store, 0, P1, f"{P1}-p0-b.snapshot", 2000)
    return store


@pytest.fixture
def aware(monkeypatch):
    """Only the reader-awareness change may set this; tests of the execute
    path stand in for it explicitly."""
    monkeypatch.setattr(build, "READERS_REFUSE_TOMBSTONED_BUILDS", True)


def progress(tmp_path):
    path = tmp_path / "inventory.jsonl"
    path.touch()
    return path


def drun(store, tmp_path, *argv):
    peers = len(store.snapshots)
    urls = [f"http://q{i}:6333" for i in range(peers)]
    settings = Settings(qdrant_url="http://qdrant:6333", qdrant_collection=ALIAS,
                        qdrant_peer_urls=",".join(urls) if peers > 1 else None, _env_file=None)
    clients = {u: AdminPeer(store, i) for i, u in enumerate(urls)}
    clients["http://qdrant:6333"] = AdminPeer(store, 0)
    lines: list[str] = []
    rc = gen.main(list(argv), settings=settings, connect=clients.__getitem__, out=lines.append)
    return rc, lines


def dry(store, tmp_path, build_id, *extra):
    return drun(store, tmp_path, "dispose", "--progress", str(progress(tmp_path)), "--build-id", build_id, *extra)


def digest_of(lines):
    found = [line.split()[-1] for line in lines if line.startswith("PLAN DIGEST ")]
    assert len(found) == 1 and re.fullmatch(r"[0-9a-f]{64}", found[0])
    return found[0]


def execute(store, tmp_path, build_id, digest, *extra):
    return dry(store, tmp_path, build_id, "--execute", "--plan-digest", digest,
               "--readers-stopped", STOPPED, "--recovery-ref", "CHG-1373", "--operator", "site-op", *extra)


def dispose(store, tmp_path, build_id=G1, *extra):
    rc, lines = dry(store, tmp_path, build_id, *extra)
    assert rc == 0, lines
    return execute(store, tmp_path, build_id, digest_of(lines), *extra)


def writes(store):
    return [c for c in store.calls if c[0] in ("upsert", "delete_alias", "delete_snapshot", "delete_collection")]


def state(store):
    """Everything stored, with the tombstone's two clock values normalised."""
    collections = copy.deepcopy(store.collections)
    for points in collections.values():
        for payload in points.values():
            if payload.get("record_type") == "retirement-tombstone":
                payload["created_at"] = "T"
                payload["retired_at"] = payload["retired_at"] and "T"
    listed = [{k: sorted(s.name for s in v) for k, v in peer.items() if v} for peer in store.snapshots]
    return dict(store.aliases), collections, listed


def tombstone(store, control=C1):
    return store.collections[control].get(tombstone_point_id(control))


def listing(store, tmp_path):
    rc, lines = drun(store, tmp_path, "list", "--progress", str(progress(tmp_path)), "--format", "json")
    assert rc == 0
    return {r["generation"]["build_id"]: r["generation"] for r in map(json.loads, lines) if "generation" in r}


# --- the interim refusal -------------------------------------------------------


def test_this_release_refuses_execute_before_reading_or_writing_anything(tmp_path, capsys, monkeypatch):
    store = world()
    before = state(store)
    monkeypatch.setenv("READERS_REFUSE_TOMBSTONED_BUILDS", "true")  # no environment switch exists
    assert build.READERS_REFUSE_TOMBSTONED_BUILDS is False
    connected: list[str] = []

    def connect(url):
        connected.append(url)
        return AdminPeer(store, 0)

    settings = Settings(qdrant_url="http://qdrant:6333", qdrant_collection=ALIAS, _env_file=None)
    rc = gen.main(["dispose", "--progress", str(progress(tmp_path)), "--build-id", G1, "--execute",
                   "--plan-digest", "0" * 64, "--readers-stopped", STOPPED],
                  settings=settings, connect=connect, out=lambda line: None)
    assert rc == gen.EXIT_REFUSED and "E_READERS_UNAWARE" in capsys.readouterr().err
    assert connected == [] and store.calls == [] and state(store) == before
    assert not list(tmp_path.glob("publish-*.lock"))


def test_the_core_refuses_execute_too_when_called_without_the_cli(tmp_path):
    store = world()
    settings = Settings(qdrant_url="http://qdrant:6333", qdrant_collection=ALIAS, _env_file=None)
    with pytest.raises(gen.Refused, match="E_READERS_UNAWARE"):
        disposal.dispose_data(AdminPeer(store, 0), settings, lambda url: AdminPeer(store, 0),
                              progress=progress(tmp_path), build_id=G1, keep=1, below_keep=False, execute=True,
                              plan_digest="0" * 64, readers_stopped=STOPPED, recovery_ref=None, operator=None,
                              out=lambda line: None)
    assert store.calls == []


def test_dry_run_prints_the_plan_and_digest_and_changes_nothing_in_this_release(tmp_path):
    store = world()
    before = state(store)
    rc, lines = dry(store, tmp_path, G1)
    text = "\n".join(lines)
    assert rc == 0 and digest_of(lines)
    assert f"PLAN dispose build {G1} physical {P1}" in text and f"kept and verified: {G2}" in text
    assert sum(line.strip().startswith("DELETE snapshot") for line in lines) == 2
    assert "DRY RUN: nothing changed. This release cannot execute" in text
    assert writes(store) == [] and state(store) == before
    assert digest_of(dry(store, tmp_path, G1)[1]) == digest_of(lines)  # stable for an unchanged plan


# --- the disposal --------------------------------------------------------------


def test_disposal_removes_the_data_unit_and_keeps_the_controls(tmp_path, aware):
    store = world(peers=3)
    kept_control = {i: copy.deepcopy(p) for i, p in store.collections[C1].items()}
    others = copy.deepcopy({n: store.collections[n] for n in (P2, C2, P3, C3)})
    data_alias, control_alias = build_aliases(ALIAS, G1)
    rc, lines = dispose(store, tmp_path)
    assert rc == 0, lines

    calls = writes(store)
    assert calls[0] == ("upsert", C1, "retiring")  # the tombstone precedes every delete
    assert calls[1] == ("delete_alias", data_alias)
    assert sorted(c[1:] for c in calls[2:6]) == sorted(
        [(0, P1, f"{P1}-p0-a.snapshot"), (0, P1, f"{P1}-p0-b.snapshot"),
         (1, P1, f"{P1}-p1-a.snapshot"), (2, P1, f"{P1}-p2-a.snapshot")])
    assert calls[6:] == [("delete_collection", P1), ("upsert", C1, "retired")]

    assert P1 not in store.collections and data_alias not in store.aliases
    assert store.aliases[control_alias] == C1 and store.aliases[ALIAS] == P3
    stored = dict(store.collections[C1])
    record = stored.pop(tombstone_point_id(C1))
    assert stored == kept_control  # receipt and marker byte-equal
    assert {n: store.collections[n] for n in (P2, C2, P3, C3)} == others
    for peer in range(3):
        assert [s.name for s in store.snapshots[peer][P2]] == [f"{P2}-p{peer}-a.snapshot"]
        assert [s.name for s in store.snapshots[peer][P3]] == [f"{P3}-p{peer}-a.snapshot"]

    assert decode_tombstone(record, C1) is record
    assert (record["state"], record["build_id"], record["data_collection"]) == ("retired", G1, P1)
    assert record["keep"] == {"n": 1, "kept_build_ids": [G2], "below_keep_override": False}
    assert (record["readers_stopped_at"], record["recovery_ref"], record["operator"]) == (STOPPED, "CHG-1373", "site-op")
    assert sorted(s["name"] for s in record["snapshots"]) == sorted(c[3] for c in calls[2:6])
    assert record["content_seal"] == kept_control[publication_metadata_point_id(C1)]["content_seal"]
    assert "doc_id" not in record and "secret manual text" not in json.dumps(record)

    # Q2, directly: were the name ever recreated, no peer would list a snapshot of it.
    store.collections[P1] = {}
    assert all(AdminPeer(store, i).list_snapshots(P1) == [] for i in range(3))
    del store.collections[P1]

    gens = listing(store, tmp_path)
    assert gens[G1]["phase"] == "retired" and gens[G1]["tombstone"]["state"] == "retired"
    assert gens[G1]["rank"] is None and not gens[G1]["disposable"]
    assert gens[G2]["phase"] == "retained" and gens[G2]["rank"] == 1 and gens[G3]["live"]


def test_a_rerun_of_a_retired_build_reports_it_and_writes_nothing(tmp_path, aware):
    store = world()
    rc, lines = dry(store, tmp_path, G1)
    digest = digest_of(lines)
    assert execute(store, tmp_path, G1, digest)[0] == 0
    done = state(store)
    store.calls.clear()
    rc, lines = execute(store, tmp_path, G1, digest)
    assert rc == 0 and lines[0].startswith(f"ALREADY RETIRED build {G1}")
    assert writes(store) == [] and state(store) == done
    assert dry(store, tmp_path, G1)[0] == 0 and writes(store) == []


def _refused(store, tmp_path, capsys, code, *argv, build_id=G1):
    before = state(store)
    rc, lines = dry(store, tmp_path, build_id, *argv)
    err = capsys.readouterr().err
    assert rc == gen.EXIT_REFUSED and code in err, (lines, err)
    assert writes(store) == [] and state(store) == before
    return err


def test_live_target_is_refused(tmp_path, capsys, aware):
    _refused(world(), tmp_path, capsys, "LIVE", "--execute", "--plan-digest", "0" * 64,
             "--readers-stopped", STOPPED, build_id=G3)


def test_keep_n_is_a_guard_computed_without_the_target(tmp_path, capsys, aware):
    store = world()
    # keep 1: the newest retained build is refused, it does not slide out of the kept set.
    _refused(store, tmp_path, capsys, "KEEP_N", build_id=G2)
    _refused(store, tmp_path, capsys, "KEEP_N", "--keep", "2")  # G1 is the second newest
    assert _refused(store, tmp_path, capsys, "E_KEEP_FLOOR", "--keep", "0")
    rc, lines = dry(store, tmp_path, G1, "--keep", "2", "--allow-below-keep")
    assert rc == 0 and "lowered by --allow-below-keep" in "\n".join(lines)


def test_an_unsealed_build_does_not_count_as_a_kept_rollback_point(tmp_path, capsys, aware):
    store = Store()
    sealed(store, G1, P1, 100.0)
    publish(store, G2, P2, 200.0)  # schema 1: bound, no content seal
    sealed(store, G3, P3, 300.0, live=True)
    _refused(store, tmp_path, capsys, "KEEP_N")
    sealed(store, G0, P0, 50.0)  # with an older sealed build, G1 becomes the kept one, G0 disposable
    _refused(store, tmp_path, capsys, "KEEP_N")
    rc, lines = dry(store, tmp_path, G0)
    assert rc == 0 and f"kept and verified: {G1}" in "\n".join(lines)


@pytest.mark.parametrize("damage", ["data", "control"])
def test_a_kept_build_that_fails_its_seal_refuses_the_disposal(tmp_path, capsys, aware, damage):
    store = world()
    if damage == "data":
        next(iter(store.collections[P2].values()))["text"] = "changed"
    else:
        store.collections[C2]["extra-point"] = {"doc_id": "x"}
    _refused(store, tmp_path, capsys, "E_KEPT_UNVERIFIED")


def test_a_tie_or_a_missing_time_at_the_keep_boundary_is_protected(tmp_path, capsys, aware):
    store = Store()
    sealed(store, G1, P1, 200.0)
    sealed(store, G2, P2, 200.0)
    sealed(store, G3, P3, 300.0, live=True)
    codes = [re.search(r"\((\w+)\)", _refused(store, tmp_path, capsys, "E_NOT_DISPOSABLE", build_id=b)).group(1)
             for b in (G1, G2)]
    assert sorted(codes) == ["KEEP_N", "UNORDERED"]  # neither of two equal times is disposable
    store = Store()
    sealed(store, G1, P1, None)
    sealed(store, G2, P2, 200.0)
    sealed(store, G3, P3, 300.0, live=True)
    _refused(store, tmp_path, capsys, "UNORDERED")


@pytest.mark.parametrize("role", ["staging", "previous"])
def test_a_collection_the_publish_sidecar_names_is_refused(tmp_path, capsys, aware, role):
    store = world()
    names = {"staging": f"{ALIAS}__genzzz", "previous": P3}
    names[role] = P1
    write_publish_state(progress(tmp_path), ALIAS, staging=names["staging"], gen_fp="g", corpus_fp="c",
                        previous=names["previous"])
    _refused(store, tmp_path, capsys, f"SIDECAR_{role.upper()}")


def test_a_foreign_alias_an_unpublished_build_and_an_unknown_build_are_refused(tmp_path, capsys, aware):
    store = world()
    store.aliases["someone-else"] = P1
    _refused(store, tmp_path, capsys, "FOREIGN_ALIAS")
    del store.aliases["someone-else"]
    sealed(store, G0, P0, 50.0, aliases=False)  # sealed, never published
    _refused(store, tmp_path, capsys, "NOT_RETAINED", build_id=G0)
    _refused(store, tmp_path, capsys, "E_UNKNOWN_BUILD", build_id="eeeeeeee-0000-4000-8000-000000000009")


def test_a_collection_name_is_never_accepted_in_place_of_the_build_id(tmp_path, capsys, aware):
    _refused(world(), tmp_path, capsys, "E_BUILD_ID", build_id=P1)


def test_one_run_takes_exactly_one_build(tmp_path, capsys, aware):
    store = world()
    sealed(store, G0, P0, 50.0)
    _refused(store, tmp_path, capsys, "E_BUILD_ID", "--build-id", G0)  # a second name never wins silently


def test_a_stopped_peer_refuses_before_the_tombstone(tmp_path, capsys, aware):
    store = world(peers=3)
    rc, lines = dry(store, tmp_path, G1)
    digest = digest_of(lines)
    store.unreachable = {2}
    before = state(store)
    rc, _ = execute(store, tmp_path, G1, digest)
    assert rc == gen.EXIT_REFUSED and "PEERS_INCOMPLETE" in capsys.readouterr().err
    assert writes(store) == [] and state(store) == before and tombstone(store) is None


def test_execute_needs_the_digest_the_attestation_and_a_matching_plan(tmp_path, capsys, aware):
    store = world()
    digest = digest_of(dry(store, tmp_path, G1)[1])
    _refused(store, tmp_path, capsys, "E_PLAN_DIGEST", "--execute", "--readers-stopped", STOPPED)
    _refused(store, tmp_path, capsys, "E_READERS_STOPPED", "--execute", "--plan-digest", digest)
    _refused(store, tmp_path, capsys, "E_READERS_STOPPED", "--execute", "--plan-digest", digest,
             "--readers-stopped", "2999-01-01T00:00:00Z")
    _refused(store, tmp_path, capsys, "E_READERS_STOPPED", "--execute", "--plan-digest", digest,
             "--readers-stopped", "yesterday")
    _refused(store, tmp_path, capsys, "E_PLAN_CHANGED", "--execute", "--plan-digest", "0" * 64,
             "--readers-stopped", STOPPED)
    # The plan changed after it was read: a snapshot nobody was shown.
    snap(store, 0, P1, f"{P1}-p0-late.snapshot", 5)
    _refused(store, tmp_path, capsys, "E_PLAN_CHANGED", "--execute", "--plan-digest", digest,
             "--readers-stopped", STOPPED)
    assert tombstone(store) is None


def test_the_plan_digest_binds_the_kept_set_and_the_keep_override(tmp_path, aware):
    store = world()
    sealed(store, G0, P0, 50.0)
    plain = digest_of(dry(store, tmp_path, G0)[1])
    assert digest_of(dry(store, tmp_path, G0, "--allow-below-keep")[1]) != plain
    assert digest_of(dry(store, tmp_path, G0, "--keep", "2")[1]) != plain


def test_a_held_publish_lock_refuses_and_the_disposal_holds_it_for_the_whole_run(tmp_path, capsys, aware):
    store = world()
    path = progress(tmp_path)
    digest = digest_of(dry(store, tmp_path, G1)[1])
    handle = acquire_publish_lock(path, ALIAS)
    try:
        _refused(store, tmp_path, capsys, "E_LOCK_REFUSED", "--execute", "--plan-digest", digest,
                 "--readers-stopped", STOPPED)
    finally:
        release_run_lock(handle)
    held: list[str] = []

    def publisher_tries(*call):
        with pytest.raises(RuntimeError):
            acquire_publish_lock(path, ALIAS)
        held.append(call[0])

    store.on_call = publisher_tries
    assert execute(store, tmp_path, G1, digest)[0] == 0
    assert held == ["upsert", "delete_alias", "delete_collection", "upsert"]
    release_run_lock(acquire_publish_lock(path, ALIAS))  # released afterwards


def test_the_serving_alias_is_reread_before_the_first_delete(tmp_path, capsys, aware):
    store = world()
    digest = digest_of(dry(store, tmp_path, G1)[1])

    def repoint(*call):
        if call == ("upsert", C1, "retiring"):
            store.aliases[ALIAS] = P1  # the build became the serving target after the plan

    store.on_call = repoint
    rc, _ = execute(store, tmp_path, G1, digest)
    assert rc == gen.EXIT_REFUSED and "E_LIVE" in capsys.readouterr().err
    assert [c[0] for c in writes(store)] == ["upsert"] and P1 in store.collections
    assert build_aliases(ALIAS, G1)[0] in store.aliases and len(store.snapshots[0][P1]) == 2


def test_a_snapshot_that_is_not_gone_keeps_the_collection(tmp_path, capsys, aware):
    store = world(peers=3)
    digest = digest_of(dry(store, tmp_path, G1)[1])

    def reappear(*call):  # a snapshot made between the snapshot step and the collection delete
        if call[0] == "delete_snapshot" and call[1] == 2:
            snap(store, 1, P1, "made-meanwhile.snapshot", 3)

    store.on_delete = lambda *a: reappear("delete_snapshot", *a)
    rc, _ = execute(store, tmp_path, G1, digest)
    assert rc == gen.EXIT_REFUSED and "E_NOT_CONVERGED" in capsys.readouterr().err
    assert P1 in store.collections and ("delete_collection", P1) not in store.calls
    assert tombstone(store)["state"] == "retiring"


def test_a_sidecar_that_names_the_collection_after_the_plan_stops_the_deletes(tmp_path, capsys, aware):
    store = world()
    digest = digest_of(dry(store, tmp_path, G1)[1])

    def a_build_records_it(*call):
        if call == ("upsert", C1, "retiring"):
            write_publish_state(progress(tmp_path), ALIAS, staging=f"{ALIAS}__genzzz", gen_fp="g", corpus_fp="c",
                                previous=P1)

    store.on_call = a_build_records_it
    rc, _ = execute(store, tmp_path, G1, digest)
    assert rc == gen.EXIT_REFUSED and "E_SIDECAR" in capsys.readouterr().err
    assert [c[0] for c in writes(store)] == ["upsert"] and P1 in store.collections


@pytest.mark.parametrize("fault", ["removed", "altered"])
def test_nothing_is_deleted_without_the_stored_tombstone(tmp_path, capsys, aware, fault):
    store = world()
    digest = digest_of(dry(store, tmp_path, G1)[1])

    def interfere(*call):
        if call == ("upsert", C1, "retiring"):
            store.on_call = None
            if fault == "removed":
                # The write is acknowledged but the point is not there afterwards.
                store.pending_removal = tombstone_point_id(C1)
            else:
                store.pending_change = tombstone_point_id(C1)

    original = AdminPeer.upsert

    def upsert(self, collection_name, *, points, wait=True):
        result = original(self, collection_name, points=points, wait=wait)
        if getattr(store, "pending_removal", None):
            del store.collections[C1][store.pending_removal]
        if getattr(store, "pending_change", None):
            store.collections[C1][store.pending_change]["operator"] = "someone-else"
        return result

    store.on_call = interfere
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(AdminPeer, "upsert", upsert)
        rc, _ = execute(store, tmp_path, G1, digest)
    assert rc == gen.EXIT_UNREADABLE and "E_TOMBSTONE_WRITE" in capsys.readouterr().err
    assert [c[0] for c in writes(store)] == ["upsert"] and P1 in store.collections
    assert build_aliases(ALIAS, G1)[0] in store.aliases


def test_a_tombstone_that_disappears_between_steps_stops_the_deletes(tmp_path, monkeypatch, capsys, aware):
    store = world()
    digest = digest_of(dry(store, tmp_path, G1)[1])
    original = disposal.step_tombstone

    def then_lost(run):
        original(run)
        del store.collections[C1][tombstone_point_id(C1)]

    monkeypatch.setattr(disposal, "step_tombstone", then_lost)
    rc, _ = execute(store, tmp_path, G1, digest)
    assert rc == gen.EXIT_REFUSED and "E_TOMBSTONE" in capsys.readouterr().err
    assert [c[0] for c in writes(store)] == ["upsert"] and P1 in store.collections


def test_the_core_requires_the_attestation_and_the_digest(tmp_path, aware):
    store = world()
    settings = Settings(qdrant_url="http://qdrant:6333", qdrant_collection=ALIAS, _env_file=None)
    real = digest_of(dry(store, tmp_path, G1)[1])
    for digest, stopped in ((real, None), (None, STOPPED)):
        with pytest.raises(gen.Refused, match="E_PLAN_DIGEST"):
            disposal.dispose_data(AdminPeer(store, 0), settings, lambda url: AdminPeer(store, 0),
                                  progress=progress(tmp_path), build_id=G1, keep=1, below_keep=False,
                                  execute=True, plan_digest=digest, readers_stopped=stopped, recovery_ref=None,
                                  operator=None, out=lambda line: None)
    assert writes(store) == [] and tombstone(store) is None


def test_the_collection_delete_itself_requires_every_peer_to_list_no_snapshot(tmp_path, monkeypatch, capsys, aware):
    store = world(peers=3)
    digest = digest_of(dry(store, tmp_path, G1)[1])
    original = disposal.step_snapshots

    def then_one_more(run):
        original(run)
        snap(store, 2, P1, "made-after-the-snapshot-step.snapshot", 3)

    monkeypatch.setattr(disposal, "step_snapshots", then_one_more)
    rc, _ = execute(store, tmp_path, G1, digest)
    assert rc == gen.EXIT_REFUSED and "E_NOT_CONVERGED" in capsys.readouterr().err
    assert P1 in store.collections and ("delete_collection", P1) not in store.calls


# --- crash and resume ------------------------------------------------------------


def _reference(tmp_path, peers):
    store = world(peers)
    assert dispose(store, tmp_path / "ref")[0] == 0
    return state(store)


@pytest.mark.parametrize("peers", [1, 3])
@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("step", STEPS)
def test_a_stop_at_any_step_boundary_converges_on_rerun(tmp_path, monkeypatch, aware, step, when, peers):
    (tmp_path / "ref").mkdir()
    expected = _reference(tmp_path, peers)
    store = world(peers)
    digest = digest_of(dry(store, tmp_path, G1)[1])
    original = getattr(disposal, step)

    def stopped(run):
        if when == "after":
            original(run)
        raise KeyboardInterrupt  # the process dies here

    with monkeypatch.context() as patch:
        patch.setattr(disposal, step, stopped)
        with pytest.raises(KeyboardInterrupt):
            execute(store, tmp_path, G1, digest)
    partial = writes(store)
    if partial:
        assert partial[0] == ("upsert", C1, "retiring")
        assert listing(store, tmp_path)[G1]["phase"] in ("retiring", "retired")
    store.calls.clear()
    rc, lines = execute(store, tmp_path, G1, digest)  # the same command again
    assert rc == 0, lines
    assert state(store) == expected
    assert [c for c in writes(store) if c == ("upsert", C1, "retiring")] == ([] if partial else [("upsert", C1, "retiring")])
    store.calls.clear()
    assert execute(store, tmp_path, G1, digest)[0] == 0 and writes(store) == []


def test_a_stop_between_two_snapshot_deletes_converges(tmp_path, aware):
    (tmp_path / "ref").mkdir()
    expected = _reference(tmp_path, 3)
    store = world(peers=3)
    digest = digest_of(dry(store, tmp_path, G1)[1])
    seen = {"n": 0}

    def crash(peer, collection, name):
        seen["n"] += 1
        if seen["n"] == 3:
            raise KeyboardInterrupt

    store.on_delete = crash
    with pytest.raises(KeyboardInterrupt):
        execute(store, tmp_path, G1, digest)
    assert P1 in store.collections and sum(len(store.snapshots[p][P1]) for p in range(3)) == 2
    store.on_delete = None
    assert execute(store, tmp_path, G1, digest)[0] == 0
    assert state(store) == expected


def _stopped_after(monkeypatch, store, tmp_path, step, digest=None):
    digest = digest or digest_of(dry(store, tmp_path, G1)[1])
    original = getattr(disposal, step)

    def stopped(run):
        original(run)
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(disposal, step, stopped)
        with pytest.raises(KeyboardInterrupt):
            execute(store, tmp_path, G1, digest)
    store.calls.clear()
    return digest


@pytest.mark.parametrize("change", ["kept-seal-broken", "newer-publication", "no-kept-build-left"])
def test_a_started_disposal_finishes_even_when_plan_time_checks_would_now_refuse(tmp_path, monkeypatch, aware, change):
    """Keep-N and the kept seal are plan-time checks. After the tombstone the
    build is no rollback point whatever happens, so the rerun must finish."""
    store = world()
    digest = _stopped_after(monkeypatch, store, tmp_path, "step_alias")
    if change == "kept-seal-broken":
        next(iter(store.collections[P2].values()))["text"] = "changed"  # the kept build no longer verifies
    elif change == "newer-publication":
        sealed(store, G0, P0, 400.0)
        store.aliases[ALIAS] = P0
    else:
        for name in build_aliases(ALIAS, G2):  # the only kept build is no longer a retained generation
            del store.aliases[name]
    assert execute(store, tmp_path, G1, digest)[0] == 0
    assert P1 not in store.collections and tombstone(store)["state"] == "retired"
    assert set(store.collections) >= {C1, P2, C2, P3, C3}


@pytest.mark.parametrize("hazard", ["live", "foreign-alias", "sidecar", "peer-down", "lock"])
def test_a_resume_is_still_gated_by_delete_safety(tmp_path, monkeypatch, capsys, aware, hazard):
    store = world(peers=3)
    digest = _stopped_after(monkeypatch, store, tmp_path, "step_tombstone")
    handle = None
    if hazard == "live":
        store.aliases[ALIAS] = P1
    elif hazard == "foreign-alias":
        store.aliases["someone-else"] = P1
    elif hazard == "sidecar":
        write_publish_state(progress(tmp_path), ALIAS, staging=f"{ALIAS}__genzzz", gen_fp="g", corpus_fp="c",
                            previous=P1)
    elif hazard == "peer-down":
        store.unreachable = {1}
    else:
        handle = acquire_publish_lock(progress(tmp_path), ALIAS)
    before = state(store)
    try:
        rc, _ = execute(store, tmp_path, G1, digest)
    finally:
        if handle is not None:
            release_run_lock(handle)
    assert rc == gen.EXIT_REFUSED, capsys.readouterr().err
    assert writes(store) == [] and state(store) == before


def test_no_other_build_is_disposed_while_one_is_retiring(tmp_path, monkeypatch, capsys, aware):
    store = world()
    sealed(store, G0, P0, 50.0)
    digest = _stopped_after(monkeypatch, store, tmp_path, "step_tombstone")
    before = state(store)
    rc, _ = dry(store, tmp_path, G0)
    assert rc == gen.EXIT_REFUSED and f"E_RETIRING_PENDING: build {G1}" in capsys.readouterr().err
    assert state(store) == before
    assert execute(store, tmp_path, G1, digest)[0] == 0
    assert dispose(store, tmp_path, G0)[0] == 0
    assert P0 not in store.collections and tombstone(store, P0 + "__completions")["state"] == "retired"


def test_a_snapshot_made_after_the_plan_needs_its_own_confirmation_on_resume(tmp_path, monkeypatch, capsys, aware):
    store = world()
    digest = _stopped_after(monkeypatch, store, tmp_path, "step_alias")
    snap(store, 0, P1, "operator-made.snapshot", 9)
    before = state(store)
    rc, _ = execute(store, tmp_path, G1, digest)
    assert rc == gen.EXIT_REFUSED and "E_PLAN_CHANGED" in capsys.readouterr().err
    assert writes(store) == [] and state(store) == before
    rc, lines = dry(store, tmp_path, G1)
    assert rc == 0 and any("UNRECORDED snapshot" in line and "operator-made.snapshot" in line for line in lines)
    resume = digest_of(lines)
    assert resume != digest
    assert execute(store, tmp_path, G1, resume)[0] == 0
    assert P1 not in store.collections and all(not v for v in store.snapshots[0].get(P1, []))
    assert tombstone(store)["state"] == "retired"


# --- the tombstone and the listing -----------------------------------------------


def test_a_retiring_build_is_no_kept_generation_and_no_snapshot_target(tmp_path, monkeypatch, capsys, aware):
    """Between the tombstone and the alias delete both immutable aliases still
    resolve: the tombstone, not the aliases, decides the phase."""
    store = world()
    sealed(store, G0, P0, 50.0)
    rc, lines = dry(store, tmp_path, G1, "--keep", "1")
    _stopped_after(monkeypatch, store, tmp_path, "step_tombstone", digest_of(lines))
    assert set(build_aliases(ALIAS, G1)) <= set(store.aliases)
    gens = listing(store, tmp_path)
    assert gens[G1]["phase"] == "retiring" and gens[G1]["rank"] is None and not gens[G1]["disposable"]
    assert (gens[G2]["rank"], gens[G0]["rank"]) == (1, 2)
    rc, _ = drun(store, tmp_path, "dispose-snapshots", "--progress", str(progress(tmp_path)), "--build-id", G1)
    assert rc == gen.EXIT_REFUSED and "NOT_RETAINED" in capsys.readouterr().err
    marker_time = gens[G1]["finished_at"]
    assert marker_time == "1970-01-01T00:01:40Z"  # the tombstone is not read as a completion marker


def test_a_tombstone_of_another_build_or_a_damaged_one_is_never_resume_evidence(tmp_path, monkeypatch, capsys, aware):
    store = world()
    digest = _stopped_after(monkeypatch, store, tmp_path, "step_tombstone")
    record = tombstone(store)
    assert decode_tombstone(record, C1) == record
    for change in ({"build_id": G2}, {"state": "gone"}, {"extra": 1}):
        damaged = world()
        damaged.collections[C1][tombstone_point_id(C1)] = {**record, **change}
        if "build_id" not in change:
            with pytest.raises(InvalidTombstone):
                decode_tombstone({**record, **change}, C1)
        assert listing(damaged, tmp_path)[G1]["phase"] == "tombstone-invalid"
        before = state(damaged)
        rc, _ = execute(damaged, tmp_path, G1, digest)
        assert rc == gen.EXIT_REFUSED and "E_TOMBSTONE" in capsys.readouterr().err
        assert writes(damaged) == [] and state(damaged) == before
    with pytest.raises(InvalidTombstone):
        decode_tombstone(record, C2)  # bound to its own control collection
