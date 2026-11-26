"""Retained-generation listing and snapshot-only disposal (issue #1373 PR 1).

Faithful fake of the Qdrant surfaces the tool reads: aliases, collections,
receipts and markers through the entry endpoint, and node-local snapshots per
peer (a peer lists and deletes only its own snapshots; a delete elsewhere is a
404, as observed on the pinned v1.19.2). Expected outcomes are declared here,
independent of the module's own ranking.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from qdrant_client import models
from qdrant_client.http.exceptions import UnexpectedResponse

from mainframe_rag.config import Settings
from mainframe_rag.ingest import generations as gen
from mainframe_rag.ingest.build import build_aliases
from mainframe_rag.ingest.completion import acquire_publish_lock, release_run_lock
from mainframe_rag.ingest.publish import (
    publication_metadata_point_id,
    publish_state_path,
    write_publish_state,
)
from mainframe_rag.ports import QdrantSnapshotAdmin

ALIAS = "corp"
G1, G2, G3 = (f"{c * 8}-0000-4000-8000-00000000000{n}" for c, n in (("a", 1), ("b", 2), ("c", 3)))
P1, P2, P3 = (f"{ALIAS}__gen{n}" for n in ("aaa", "bbb", "ccc"))


def _not_found() -> UnexpectedResponse:
    return UnexpectedResponse(404, "Not Found", b"{}", None)


class Store:
    """Cluster-wide state: consensus-replicated aliases/collections, node-local snapshots."""

    def __init__(self, peers: int = 1) -> None:
        self.aliases: dict[str, str] = {}
        self.collections: dict[str, dict[str, dict]] = {}
        self.snapshots: list[dict[str, list[models.SnapshotDescription]]] = [{} for _ in range(peers)]
        self.calls: list[tuple] = []
        self.peer_ids = [1000 + i for i in range(peers)]
        self.clustered = peers > 1
        self.unreachable: set[int] = set()
        self.on_delete = None
        self.on_aliases = None


class Peer:
    def __init__(self, store: Store, index: int) -> None:
        self.store, self.index = store, index

    # read surface -----------------------------------------------------------
    def _up(self):
        if self.index in self.store.unreachable:
            raise ConnectionError("peer down")

    def cluster_status(self):
        self._up()
        if not self.store.clustered:
            return models.ClusterStatusOneOf(status="disabled")
        return SimpleNamespace(status="enabled", peer_id=self.store.peer_ids[self.index],
                               peers={str(p): {} for p in self.store.peer_ids})

    def get_aliases(self):
        self._up()
        if self.store.on_aliases:
            self.store.on_aliases()
        return models.CollectionsAliasesResponse(aliases=[
            models.AliasDescription(alias_name=a, collection_name=c) for a, c in self.store.aliases.items()])

    def get_collections(self):
        self._up()
        return models.CollectionsResponse(collections=[
            models.CollectionDescription(name=n) for n in self.store.collections])

    def collection_exists(self, collection_name):
        self._up()
        name = self.store.aliases.get(collection_name, collection_name)
        return name in self.store.collections

    def get_collection(self, collection_name):
        self._up()
        return SimpleNamespace(points_count=len(self.store.collections[collection_name]))

    def scroll(self, collection_name, *, scroll_filter=None, limit=10, with_payload=True,
               with_vectors=False, offset=None):
        self._up()
        items = sorted(self.store.collections[collection_name].items())
        start = int(offset or 0)
        page = items[start:start + limit]
        nxt = start + limit if start + limit < len(items) else None
        return [models.Record(id=i, payload=p) for i, p in page], nxt

    def retrieve(self, collection_name, ids, *, with_payload=True, with_vectors=False):
        self._up()
        points = self.store.collections[collection_name]
        return [models.Record(id=i, payload=points[i]) for i in ids if i in points]

    def query_points(self, *a, **k):
        raise AssertionError("not used")

    def query_batch_points(self, *a, **k):
        raise AssertionError("not used")

    def collection_cluster_info(self, *a, **k):
        raise AssertionError("not used")

    # snapshots --------------------------------------------------------------
    def list_snapshots(self, collection_name):
        self._up()
        if collection_name not in self.store.collections:
            raise _not_found()
        return list(self.store.snapshots[self.index].get(collection_name, []))

    def delete_snapshot(self, collection_name, snapshot_name, *, wait=True):
        self._up()
        self.store.calls.append(("delete_snapshot", self.index, collection_name, snapshot_name))
        if self.store.on_delete:
            self.store.on_delete(self.index, collection_name, snapshot_name)
        mine = self.store.snapshots[self.index].get(collection_name, [])
        kept = [s for s in mine if s.name != snapshot_name]
        if len(kept) == len(mine):
            raise _not_found()
        self.store.snapshots[self.index][collection_name] = kept
        return True

    def close(self):
        pass


def _marker(control: str, finished_at: float, doc: str = "doc") -> dict:
    return {
        "doc_id": doc, "sha256": "0" * 64, "rules_v": "r", "target_collection": control.removesuffix("__completions"),
        "generation_id": "g", "expected_chunks": 1, "chunk_ids_digest": "d", "content_digest": "c",
        "manifest_digest": "m", "source_rev": f"v|p|1|{'0' * 64}", "finished_at": finished_at,
    }


def publish(store: Store, build_id: str, physical: str, finished_at: float | None, *, live: bool = False,
            aliases: bool = True) -> None:
    control = physical + "__completions"
    store.collections[physical] = {str(uuid.UUID(int=i + 1)): {"text": "secret manual text"} for i in range(3)}
    receipt = {
        "record_type": "publication-metadata", "target_collection": control, "build_schema": 1,
        "build_id": build_id, "logical_alias": ALIAS, "data_collection": physical, "gen_fp": "g", "corpus_fp": "c",
    }
    points = {publication_metadata_point_id(control): receipt}
    if finished_at is not None:
        points[str(uuid.uuid5(uuid.NAMESPACE_URL, control + "|marker"))] = _marker(control, finished_at)
    store.collections[control] = points
    if aliases:
        data, ctl = build_aliases(ALIAS, build_id)
        store.aliases[data] = physical
        store.aliases[ctl] = control
    if live:
        store.aliases[ALIAS] = physical


def snap(store: Store, peer: int, collection: str, name: str, size: int = 100) -> None:
    store.snapshots[peer].setdefault(collection, []).append(models.SnapshotDescription(
        name=name, creation_time="2026-11-26T00:00:00", size=size, checksum="a" * 64))


def three_generations(peers: int = 1) -> Store:
    store = Store(peers)
    publish(store, G1, P1, 100.0)
    publish(store, G2, P2, 200.0)
    publish(store, G3, P3, 300.0, live=True)
    for peer in range(peers):
        for physical in (P1, P2, P3):
            snap(store, peer, physical, f"{physical}-p{peer}-a.snapshot", 1000 + peer)
    snap(store, 0, P1, f"{P1}-p0-b.snapshot", 2000)
    return store


def run(store: Store, tmp_path, *argv: str, peer_urls: bool | None = None, api_key: str | None = None):
    peers = len(store.snapshots)
    urls = [f"http://q{i}:6333" for i in range(peers)]
    use_peers = peers > 1 if peer_urls is None else peer_urls
    settings = Settings(qdrant_url="http://qdrant:6333", qdrant_collection=ALIAS,
                        qdrant_peer_urls=",".join(urls) if use_peers else None,
                        qdrant_api_key=api_key, _env_file=None)
    clients = {u: Peer(store, i) for i, u in enumerate(urls)}
    clients["http://qdrant:6333"] = Peer(store, 0)
    lines: list[str] = []
    rc = gen.main(list(argv), settings=settings, connect=clients.__getitem__, out=lines.append)
    return rc, lines


def progress(tmp_path):
    path = tmp_path / "inventory.jsonl"
    path.touch()
    return path


def listing(store, tmp_path, *extra):
    rc, lines = run(store, tmp_path, "list", "--progress", str(progress(tmp_path)), "--format", "json", *extra)
    assert rc == 0
    rows = [json.loads(line) for line in lines]
    gens = {r["generation"]["build_id"]: r["generation"] for r in rows if "generation" in r}
    return gens, rows


def deletes(store):
    return [c for c in store.calls if c[0] == "delete_snapshot"]


def test_listing_reports_each_generation_with_snapshots_sizes_and_verdicts(tmp_path, capsys):
    store = three_generations()
    gens, rows = listing(store, tmp_path)
    assert set(gens) == {G1, G2, G3}
    assert gens[G3]["phase"] == "live" and gens[G3]["live"] and gens[G3]["reasons"] == ["LIVE"]
    assert gens[G2]["phase"] == "retained" and gens[G2]["rank"] == 1 and gens[G2]["reasons"] == ["KEEP_N"]
    assert gens[G1]["phase"] == "retained" and gens[G1]["rank"] == 2 and gens[G1]["disposable"]
    assert gens[G1]["finished_at"] == "1970-01-01T00:01:40Z"
    assert [s["name"] for s in gens[G1]["snapshots"]] == [f"{P1}-p0-a.snapshot", f"{P1}-p0-b.snapshot"]
    assert gens[G1]["size"] == {"data_bytes": None, "data_source": "not-reported", "snapshot_bytes": 3000,
                                "control_snapshot_bytes": 0}
    assert gens[G1]["aliases"] == sorted(build_aliases(ALIAS, G1))
    assert gens[G1]["points"] == 3 and gens[G1]["control_points"] == 2
    summary = rows[-1]["summary"]
    assert summary["sidecar"] == "absent" and summary["peers_complete"] and summary["live"] == P3
    assert "secret manual text" not in json.dumps(rows)
    assert deletes(store) == []


def test_listing_without_progress_never_reports_a_generation_disposable(tmp_path):
    store = three_generations()
    rc, lines = run(store, tmp_path, "list", "--format", "json")
    assert rc == 0
    gens = [json.loads(line)["generation"] for line in lines if "generation" in line]
    assert gens and all("SIDECAR_NOT_READ" in g["reasons"] and not g["disposable"] for g in gens)


def test_text_listing_names_unmanaged_collections_and_never_prints_keys(tmp_path):
    store = three_generations()
    store.collections[f"{ALIAS}__genzzz"] = {}  # abandoned staging, no control
    store.collections[f"{ALIAS}__genyyy__completions"] = {}  # control without receipt
    store.collections[f"{ALIAS}__genyyy"] = {}
    store.collections["other_corpus"] = {}
    rc, lines = run(store, tmp_path, "list", "--progress", str(progress(tmp_path)), api_key="sekret-key")
    text = "\n".join(lines)
    assert rc == 0
    assert f"NOT_MANAGED {ALIAS}__genzzz NO_BUILD_BINDING" in text
    assert f"NOT_MANAGED {ALIAS}__genyyy NO_RECEIPT" in text
    assert "other_corpus" not in text and "sekret" not in text and "secret manual text" not in text
    assert "data bytes not reported" in text


def test_dry_run_prints_the_plan_and_deletes_nothing(tmp_path):
    store = three_generations()
    before = json.dumps([{k: [s.name for s in v] for k, v in p.items()} for p in store.snapshots])
    rc, lines = run(store, tmp_path, "dispose-snapshots", "--progress", str(progress(tmp_path)), "--build-id", G1)
    assert rc == 0
    assert any(line.startswith("DRY RUN") for line in lines)
    assert sum(line.strip().startswith("DELETE peer 0") for line in lines) == 2
    assert deletes(store) == []
    assert json.dumps([{k: [s.name for s in v] for k, v in p.items()} for p in store.snapshots]) == before
    assert not gen.disposal_record_path(progress(tmp_path), ALIAS).exists()


def _execute(store, tmp_path, *builds, extra=()):
    args = ["dispose-snapshots", "--progress", str(progress(tmp_path))]
    for b in builds:
        args += ["--build-id", b]
    return run(store, tmp_path, *args, "--execute", "--recovery-ref", "CHG-1373", *extra)


def test_execute_deletes_only_the_named_retained_snapshots_on_every_peer(tmp_path):
    store = three_generations(peers=3)
    snap(store, 1, P1 + "__completions", "control-snap", 7)
    rc, lines = _execute(store, tmp_path, G1)
    assert rc == 0, lines
    assert sorted((c[1], c[3]) for c in deletes(store)) == sorted(
        [(0, f"{P1}-p0-a.snapshot"), (0, f"{P1}-p0-b.snapshot"),
         (1, f"{P1}-p1-a.snapshot"), (2, f"{P1}-p2-a.snapshot")])
    for peer in range(3):
        assert store.snapshots[peer][P1] == []
        assert [s.name for s in store.snapshots[peer][P2]] == [f"{P2}-p{peer}-a.snapshot"]
        assert [s.name for s in store.snapshots[peer][P3]] == [f"{P3}-p{peer}-a.snapshot"]
    assert [s.name for s in store.snapshots[1][P1 + "__completions"]] == ["control-snap"]
    assert set(store.collections) >= {P1, P1 + "__completions"}  # collections are kept
    assert build_aliases(ALIAS, G1)[0] in store.aliases
    events = [json.loads(line) for line in gen.disposal_record_path(progress(tmp_path), ALIAS).read_text().splitlines()]
    assert [e["event"] for e in events] == ["planned", *["deleting", "deleted"] * 4, "done"]
    assert events[0]["recovery_ref"] == "CHG-1373"


def test_rerun_after_success_and_after_a_crash_converges(tmp_path):
    store = three_generations(peers=3)
    state = {"n": 0}

    def crash(peer, collection, name):
        state["n"] += 1
        if state["n"] == 2:
            raise KeyboardInterrupt  # process stops between two deletes

    store.on_delete = crash
    with pytest.raises(KeyboardInterrupt):
        _execute(store, tmp_path, G1)
    assert len(deletes(store)) == 2
    remaining = sum(len(store.snapshots[p][P1]) for p in range(3))
    assert remaining == 3  # first delete done, second interrupted before it applied
    store.on_delete = None
    rc, lines = _execute(store, tmp_path, G1)
    assert rc == 0, lines
    assert all(store.snapshots[p][P1] == [] for p in range(3))
    store.calls.clear()
    rc, lines = _execute(store, tmp_path, G1)
    assert rc == 0 and deletes(store) == []
    assert any("DONE deleted 0" in line for line in lines)
    # The lock was released: an ordinary publisher can take it afterwards.
    release_run_lock(acquire_publish_lock(progress(tmp_path), ALIAS))


def test_a_404_delete_is_judged_by_relisting_the_peer(tmp_path):
    store = three_generations()

    def vanish(peer, collection, name):  # gone before our call lands: the server answers 404
        store.snapshots[peer][collection] = [s for s in store.snapshots[peer][collection] if s.name != name]

    store.on_delete = vanish
    rc, _ = _execute(store, tmp_path, G1)
    assert rc == 0 and store.snapshots[0][P1] == []


def test_a_failed_delete_that_is_still_listed_stops_with_exit_3(tmp_path, capsys):
    store = three_generations()

    def refuse(peer, collection, name):
        raise UnexpectedResponse(500, "boom", b"upstream detail", None)

    store.on_delete = refuse
    rc, _ = _execute(store, tmp_path, G1)
    err = capsys.readouterr().err
    assert rc == gen.EXIT_UNREADABLE
    assert "E_SNAPSHOT_NOT_DELETED" in err and "upstream detail" not in err and "boom" not in err
    assert len(deletes(store)) == 1  # stopped at the first failure


@pytest.mark.parametrize(("build", "code", "reason"), [
    (G3, "E_NOT_DISPOSABLE", "LIVE"),
    (G2, "E_NOT_DISPOSABLE", "KEEP_N"),
    (str(uuid.UUID(int=0x9999)), "E_UNKNOWN_BUILD", None),
    (P1, "E_BUILD_ID", None),
    (G1.upper(), "E_BUILD_ID", None),
])
def test_refusals_change_nothing(tmp_path, capsys, build, code, reason):
    store = three_generations()
    rc, _ = _execute(store, tmp_path, build)
    err = capsys.readouterr().err
    assert rc == gen.EXIT_REFUSED and code in err
    if reason:
        assert reason in err
    assert deletes(store) == []
    assert not gen.disposal_record_path(progress(tmp_path), ALIAS).exists()


def test_one_refused_target_refuses_the_whole_run(tmp_path, capsys):
    store = three_generations()
    rc, _ = _execute(store, tmp_path, G1, G2)
    assert rc == gen.EXIT_REFUSED and "KEEP_N" in capsys.readouterr().err
    assert deletes(store) == []


def test_keep_guard_floor_and_override(tmp_path, capsys):
    store = three_generations()
    rc, _ = _execute(store, tmp_path, G1, extra=("--keep", "2"))
    assert rc == gen.EXIT_REFUSED and "KEEP_N" in capsys.readouterr().err
    rc, _ = _execute(store, tmp_path, G1, extra=("--keep", "0"))
    assert rc == gen.EXIT_REFUSED and "E_KEEP_FLOOR" in capsys.readouterr().err
    # The override lowers the guard to the floor of 1, never to 0: G2 stays protected.
    rc, _ = _execute(store, tmp_path, G2, extra=("--keep", "2", "--allow-below-keep"))
    assert rc == gen.EXIT_REFUSED and "KEEP_N" in capsys.readouterr().err
    rc, _ = _execute(store, tmp_path, G1, extra=("--keep", "2", "--allow-below-keep"))
    assert rc == 0
    assert deletes(store) and all(c[2] == P1 for c in deletes(store))


def test_unordered_and_tied_generations_are_protected(tmp_path):
    store = Store()
    publish(store, G1, P1, None)  # no marker time
    publish(store, G2, P2, 200.0)
    g4 = str(uuid.UUID(int=0x4444))
    publish(store, g4, f"{ALIAS}__genddd", 200.0)  # ties G2 at the keep boundary
    publish(store, G3, P3, 300.0, live=True)
    gens, _ = listing(store, tmp_path)
    assert "UNORDERED" in gens[G1]["reasons"]
    assert {"KEEP_N"} <= set(gens[G2]["reasons"]) | set(gens[g4]["reasons"])
    assert not gens[G2]["disposable"] and not gens[g4]["disposable"]


@pytest.mark.parametrize("role", ["staging", "previous"])
def test_publish_sidecar_protects_what_it_names(tmp_path, capsys, role):
    store = three_generations()
    kwargs = {"staging": P1, "previous": P3} if role == "staging" else {"staging": f"{ALIAS}__genzzz", "previous": P1}
    write_publish_state(progress(tmp_path), ALIAS, gen_fp="g", corpus_fp="c", build_id=str(uuid.uuid4()),
                        **kwargs)
    rc, _ = _execute(store, tmp_path, G1, extra=("--allow-below-keep",))
    assert rc == gen.EXIT_REFUSED and f"SIDECAR_{role.upper()}" in capsys.readouterr().err
    assert deletes(store) == []


def test_unrelated_sidecar_does_not_block_and_corrupt_sidecar_refuses(tmp_path, capsys):
    store = three_generations()
    write_publish_state(progress(tmp_path), ALIAS, staging=f"{ALIAS}__genzzz", gen_fp="g", corpus_fp="c",
                        build_id=str(uuid.uuid4()), previous=P3)
    gens, _ = listing(store, tmp_path)
    assert gens[G1]["disposable"]
    publish_state_path(progress(tmp_path), ALIAS).write_text("{not json", encoding="utf-8")
    rc, _ = _execute(store, tmp_path, G1)
    assert rc == gen.EXIT_REFUSED and "SIDECAR_UNREADABLE" in capsys.readouterr().err
    assert deletes(store) == []


def test_foreign_alias_and_unpublished_candidates_are_refused(tmp_path):
    store = three_generations()
    store.aliases["operator_pin"] = P1
    g5 = str(uuid.UUID(int=0x5555))
    publish(store, g5, f"{ALIAS}__geneee", 50.0, aliases=False)  # sealed, never published
    gens, _ = listing(store, tmp_path)
    assert "FOREIGN_ALIAS" in gens[G1]["reasons"]
    assert gens[g5]["phase"] == "sealed" and "NOT_RETAINED" in gens[g5]["reasons"]


def test_held_publish_lock_refuses_and_disposal_excludes_a_publisher(tmp_path, capsys):
    store = three_generations()
    handle = acquire_publish_lock(progress(tmp_path), ALIAS)
    try:
        rc, _ = _execute(store, tmp_path, G1)
    finally:
        release_run_lock(handle)
    assert rc == gen.EXIT_REFUSED and "E_LOCK_REFUSED" in capsys.readouterr().err
    assert deletes(store) == []
    seen = []

    def competing_publisher(peer, collection, name):
        with pytest.raises(RuntimeError, match="concurrent publishers"):
            acquire_publish_lock(progress(tmp_path), ALIAS)
        seen.append(name)

    store.on_delete = competing_publisher
    rc, _ = _execute(store, tmp_path, G1)
    assert rc == 0 and len(seen) == 2


def test_alias_change_before_a_delete_stops_the_run(tmp_path, capsys):
    store = three_generations()
    reads = {"n": 0}

    def repoint():
        reads["n"] += 1
        if reads["n"] == 4:  # after planning: someone points the serving alias at G1
            store.aliases[ALIAS] = P1

    store.on_aliases = repoint
    rc, _ = _execute(store, tmp_path, G1)
    assert rc == gen.EXIT_REFUSED and "E_CHANGED" in capsys.readouterr().err
    assert len(deletes(store)) == 1


def test_execute_requires_a_recovery_reference(tmp_path, capsys):
    store = three_generations()
    rc, _ = run(store, tmp_path, "dispose-snapshots", "--progress", str(progress(tmp_path)),
                "--build-id", G1, "--execute")
    assert rc == gen.EXIT_REFUSED and "E_RECOVERY_REF" in capsys.readouterr().err
    assert deletes(store) == []


@pytest.mark.parametrize("case", ["peer-down", "no-peer-urls", "missing-member"])
def test_an_incomplete_peer_view_refuses_before_any_delete(tmp_path, capsys, case):
    store = three_generations(peers=3)
    peer_urls = None
    if case == "peer-down":
        store.unreachable.add(2)
    elif case == "no-peer-urls":
        peer_urls = False
    else:
        store.peer_ids[2] = store.peer_ids[1]  # two endpoints answer as one member
    rc, _ = run(store, tmp_path, "dispose-snapshots", "--progress", str(progress(tmp_path)), "--build-id", G1,
                "--execute", "--recovery-ref", "CHG-1", peer_urls=peer_urls)
    assert rc == gen.EXIT_REFUSED and "PEERS_INCOMPLETE" in capsys.readouterr().err
    assert deletes(store) == []


def test_peer_url_with_credentials_is_refused_without_echo(tmp_path, capsys):
    store = three_generations()
    settings = Settings(qdrant_url="http://qdrant:6333", qdrant_collection=ALIAS,
                        qdrant_peer_urls="http://user:pw@q0:6333", _env_file=None)
    rc = gen.main(["list"], settings=settings, connect=lambda url: Peer(store, 0), out=lambda _l: None)
    err = capsys.readouterr().err
    assert rc == gen.EXIT_REFUSED and "E_PEER_URL" in err and "pw" not in err


@pytest.mark.parametrize("command", ["list", "dispose-snapshots"])
def test_a_missing_inventory_file_refuses(tmp_path, capsys, command):
    store = three_generations()
    args = [command, "--progress", str(tmp_path / "elsewhere" / "inventory.jsonl")]
    if command == "dispose-snapshots":
        args += ["--build-id", G1, "--execute", "--recovery-ref", "CHG-1"]
    rc, _ = run(store, tmp_path, *args)
    assert rc == gen.EXIT_REFUSED and "E_PROGRESS" in capsys.readouterr().err
    assert deletes(store) == []


def test_fake_satisfies_the_admin_port():
    assert isinstance(Peer(Store(), 0), QdrantSnapshotAdmin)
