"""Hermetic checks for the disposable multi-peer fixture plumbing.

The real three-peer behavior lives in `tests/test_ha_cluster.py`
(integration, `qa:ha`); these tests pin the command shape and teardown
semantics without docker.
"""

from __future__ import annotations

import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import qdrant_cluster as cluster


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_free_port_returns_bindable_port():
    port = cluster.free_port(_free_port())
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))


def test_start_cluster_command_shape(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_run(cmd, *, timeout=300.0, check=True):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cluster, "qdrant_image", lambda _root: "qdrant/pinned:tag")
    monkeypatch.setattr(cluster, "require_docker", lambda _image: "sha256:" + "b" * 64)
    monkeypatch.setattr(cluster, "_run", fake_run)
    monkeypatch.setattr(cluster, "wait_cluster_ready", lambda *_a, **_k: None)

    started = cluster.start_cluster(
        tmp_path, prefix="qdrant-ha-x", base_port=7500, peers=3
    )
    assert started.urls == (
        "http://127.0.0.1:7500",
        "http://127.0.0.1:7510",
        "http://127.0.0.1:7520",
    )
    assert calls[0][:3] == ["docker", "network", "create"]
    runs = [call for call in calls if call[:2] == ["docker", "run"]]
    assert len(runs) == 3
    assert all("--pull=never" in call and "sha256:" + "b" * 64 in call for call in runs)
    first, second, third = runs
    assert "--uri" in first and first[first.index("--uri") + 1] == "http://qdrant-ha-x-1:6335"
    for peer in (second, third):
        assert "--bootstrap" in peer
        assert peer[peer.index("--bootstrap") + 1] == "http://qdrant-ha-x-1:6335"
    for command in runs:
        assert command[command.index("--entrypoint") + 1] == "/qdrant/qdrant"
        assert "QDRANT__CLUSTER__ENABLED=true" in command
        assert "QDRANT__CLUSTER__P2P__PORT=6335" in command
        # Only the HTTP API is published; replication stays on the network.
        published = [item for item in command if item.startswith("127.0.0.1:")]
        assert published and all(item.endswith(":6333") for item in published)


def test_stop_removes_containers_and_network(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, *, timeout=300.0, check=True):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cluster, "_run", fake_run)
    cluster.QdrantCluster(prefix="ha", network="ha-net", urls=("u1", "u2", "u3")).stop()
    assert ["docker", "rm", "-f", "-v", "ha-3"] in calls
    assert ["docker", "network", "rm", "ha-net"] in calls


@pytest.mark.parametrize('returncode, payload', [
    (1, ''), (0, '[]'),
    (0, '[{"Id": "sha256:' + 'b' * 64 + '", "RepoDigests": ["qdrant/qdrant@sha256:' + 'c' * 64 + '"]}]'),
])
def test_require_docker_rejects_missing_or_wrong_identity_without_pull(monkeypatch, returncode, payload):
    from scripts import qdrant_pin

    calls = []
    def inspect(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=returncode, stdout=payload)
    monkeypatch.setattr(qdrant_pin.subprocess, 'run', inspect)
    with pytest.raises(cluster.QdrantClusterError):
        cluster.require_docker('docker.io/qdrant/qdrant@sha256:' + 'a' * 64)
    assert len(calls) == 1
    assert calls[0][:3] == ['docker', 'image', 'inspect']


def test_require_docker_returns_immutable_id_after_digest_attestation(monkeypatch):
    import json

    from scripts import qdrant_pin

    reference = 'docker.io/qdrant/qdrant@sha256:' + 'a' * 64
    image_id = 'sha256:' + 'b' * 64
    monkeypatch.setattr(qdrant_pin.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=json.dumps([{'Id': image_id, 'RepoDigests': [reference.removeprefix('docker.io/')]}])))
    assert cluster.require_docker(reference) == image_id


def test_wait_cluster_ready_requires_all_peers(monkeypatch):
    answers = {"u1": 3, "u2": 2}

    def fake_count(url, **_kwargs):
        return answers[url]

    monkeypatch.setattr(cluster, "cluster_peer_count", fake_count)
    with pytest.raises(cluster.QdrantClusterError, match="not ready"):
        cluster.wait_cluster_ready(("u1", "u2"), timeout_s=0.1)


def test_active_copies_by_shard_counts_only_active_local_reports(monkeypatch):
    payloads = {
        "u1": {
            "peer_id": 101,
            "local_shards": [
                {"shard_id": 0, "state": "Active"},
                {"shard_id": 1, "state": "Recovery"},
            ],
        },
        "u2": {"peer_id": 202, "local_shards": [{"shard_id": 0, "state": "Active"}]},
    }

    def fake_get(url, timeout):
        endpoint = url.split("/", 1)[0]
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"result": payloads[endpoint]},
        )

    monkeypatch.setattr(cluster.httpx2, "get", fake_get)
    copies = cluster.active_copies_by_shard(("u1", "u2"), "c")
    assert copies[0] == {101, 202}
    assert 1 not in copies


def test_wait_collection_placement_waits_for_rf_copies(monkeypatch):
    calls = {"n": 0}

    def fake_copies(_urls, _collection, *, timeout=5.0):
        calls["n"] += 1
        if calls["n"] == 1:
            return {0: {101, 202}}
        return {0: {101, 202, 303}}

    monkeypatch.setattr(cluster, "active_copies_by_shard", fake_copies)
    cluster.wait_collection_placement(
        ("u1",), "c", shard_number=1, replication_factor=3, timeout_s=10
    )
    assert calls["n"] >= 2


def test_wait_collection_placement_times_out(monkeypatch):
    monkeypatch.setattr(cluster, "active_copies_by_shard", lambda *_a, **_k: {0: {101}})
    with pytest.raises(cluster.QdrantClusterError, match="placement not ready"):
        cluster.wait_collection_placement(
            ("u1",), "c", shard_number=1, replication_factor=3, timeout_s=0.1
        )


def test_main_up_reports_failure(monkeypatch, capsys):
    def boom(*_args, **_kwargs):
        raise cluster.QdrantClusterError("docker is required")

    monkeypatch.setattr(cluster, "start_cluster", boom)
    assert cluster.main(["up"]) == 1
    assert "docker is required" in capsys.readouterr().err


def test_qdrant_image_reads_the_images_pin():
    repo = Path(__file__).resolve().parents[1]
    assert "qdrant" in cluster.qdrant_image(repo)


@pytest.fixture
def rejoining_pair(monkeypatch):
    """Exercise the HA scenario's actual writes/reads with delayed replica data.

    Placement is already ACTIVE while one peer still serves its old records.
    The virtual clock makes delayed recovery and permanent loss deterministic.
    """
    from copy import deepcopy

    from tests import test_ha_cluster as ha

    fixture = cluster.QdrantCluster("ha-unit", "ha-unit-net", ("u1", "u2", "u3"))
    pair = ha.SeededPair("corpus", "control")
    state = SimpleNamespace(
        now=0.0, rejoined=False, recover_at=2.0, bad_peer="u2",
        bad_collection="corpus", corruption=False, survivor_missing=False,
        closed=[], placement=[],
    )
    initial = {
        "corpus": {i: {"tag": f"corpus-{i}", "point": i} for i in range(1, 61)},
        "control": {i: {"tag": f"control-{i}", "point": i} for i in range(1001, 1007)},
    }
    data = {url: deepcopy(initial) for url in fixture.urls}

    class Reader:
        def __init__(self, *, url, timeout):
            self.url = url

        def upsert(self, collection, *, points, wait):
            assert wait is True
            for url in fixture.urls:
                if url != "u2" or state.rejoined:
                    data[url][collection].update({p.id: deepcopy(p.payload) for p in points})

        def retrieve(self, collection, *, ids, with_payload):
            assert with_payload is True
            records = data[self.url][collection]
            if state.survivor_missing and self.url == "u3" and not state.rejoined:
                records = initial[collection]
            if (state.rejoined and self.url == state.bad_peer
                    and collection == state.bad_collection and state.now < state.recover_at):
                records = deepcopy(data["u1"][collection]) if state.corruption else initial[collection]
                if state.corruption:
                    records[ids[0]]["tag"] = "wrong-content"
            elif state.rejoined:
                # Replica recovery copies persisted records from a surviving peer.
                records = data["u1"][collection]
            return [SimpleNamespace(id=i, payload=deepcopy(records[i])) for i in ids if i in records]

        def close(self):
            state.closed.append(self.url)

    def docker(cmd, **kwargs):
        assert cmd[:2] in (["docker", "stop"], ["docker", "start"])
        assert cmd[2] == "ha-unit-2"
        if cmd[1] == "start":
            state.rejoined = True
        return SimpleNamespace(returncode=0)

    def placement(urls, collection, **kwargs):
        assert urls == fixture.urls
        assert kwargs == {"shard_number": 6, "replication_factor": 3}
        state.placement.append(collection)

    monkeypatch.setattr(ha, "QdrantClient", Reader)
    monkeypatch.setattr(ha.subprocess, "run", docker)
    monkeypatch.setattr(ha, "wait_cluster_ready", lambda urls: None)
    monkeypatch.setattr(ha, "wait_collection_placement", placement)
    monkeypatch.setattr(ha.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(ha.time, "sleep", lambda seconds: setattr(state, "now", state.now + seconds))
    return ha, fixture, pair, state


@pytest.mark.parametrize("peer,collection", [("u1", "corpus"), ("u2", "corpus"), ("u3", "control")])
def test_acknowledged_write_rejoin_waits_for_actual_records(rejoining_pair, peer, collection):
    ha, fixture, pair, state = rejoining_pair
    state.bad_peer, state.bad_collection = peer, collection
    ha.test_acknowledged_writes_survive_one_peer_loss(fixture, pair)
    assert state.now == 2.0
    assert state.placement == ["corpus", "control"]
    # The next ordinary read must see exact acknowledged corpus/control data.
    for url in fixture.urls:
        ha._assert_points_exact(url, "corpus", tuple(range(2001, 2011)), "corpus")
        ha._assert_points_exact(url, "control", tuple(range(3001, 3004)), "control")
        ha._assert_exact_payloads(url, pair)
    assert set(state.closed) == set(fixture.urls)


@pytest.mark.parametrize("collection,corruption", [("control", False), ("corpus", True)])
def test_acknowledged_write_rejoin_refuses_permanent_loss_or_corruption(
    rejoining_pair, collection, corruption,
):
    ha, fixture, pair, state = rejoining_pair
    state.bad_collection, state.corruption = collection, corruption
    state.recover_at = float("inf")
    with pytest.raises(AssertionError, match="exact reads never converged") as failure:
        ha.test_acknowledged_writes_survive_one_peer_loss(fixture, pair)
    assert state.now == 60.0
    assert f"u2 {collection}: expected" in str(failure.value)
    assert "retrieved" in str(failure.value)
    if corruption:
        assert "wrong-content" in str(failure.value)
    assert "u2" in state.closed


def test_acknowledged_write_survivor_loss_still_fails_immediately(rejoining_pair):
    ha, fixture, pair, state = rejoining_pair
    state.survivor_missing = True
    with pytest.raises(AssertionError, match="u3 corpus"):
        ha.test_acknowledged_writes_survive_one_peer_loss(fixture, pair)
    assert state.now == 0.0
