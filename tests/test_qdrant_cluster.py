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
    monkeypatch.setattr(cluster, "require_docker", lambda _image: None)
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


def test_require_docker_pulls_when_absent(monkeypatch):
    calls: list[list[str]] = []
    outcomes = {("image", "inspect"): 1, ("pull",): 0}

    def fake_run(cmd, *, timeout=300.0, check=True):
        calls.append(list(cmd))
        if cmd[1] == "image":
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=outcomes.get(("pull",), 1), stdout="", stderr="")

    monkeypatch.setattr(cluster.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(cluster, "_run", fake_run)
    cluster.require_docker("qdrant/pinned:tag")
    assert ["docker", "pull", "qdrant/pinned:tag"] in calls


def test_require_docker_missing_without_network_fails(monkeypatch):
    def fake_run(cmd, *, timeout=300.0, check=True):
        return SimpleNamespace(returncode=1, stdout="", stderr="no network")

    monkeypatch.setattr(cluster.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(cluster, "_run", fake_run)
    with pytest.raises(cluster.QdrantClusterError, match="could not be pulled"):
        cluster.require_docker("qdrant/pinned:tag")


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
