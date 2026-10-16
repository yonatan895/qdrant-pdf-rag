"""Hermetic check of the one fixture behavior the `qa:ha` verdict depends on.

The disposable multi-peer fixture is test infrastructure; its behavior is
proven by `tests/test_ha_cluster.py` (integration, `qa:ha`). Only the pinned
image attestation stays here: without it `qa:ha` could pass against an
unpinned or substituted Qdrant image.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from scripts import qdrant_cluster as cluster
from scripts import qdrant_pin


def test_require_docker_rejects_digest_mismatch_without_pull(monkeypatch):
    payload = json.dumps([{
        "Id": "sha256:" + "b" * 64,
        "RepoDigests": ["qdrant/qdrant@sha256:" + "c" * 64],
    }])
    calls = []

    def inspect(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=payload)

    monkeypatch.setattr(qdrant_pin.subprocess, "run", inspect)
    with pytest.raises(cluster.QdrantClusterError):
        cluster.require_docker("docker.io/qdrant/qdrant@sha256:" + "a" * 64)
    assert len(calls) == 1
    assert calls[0][:3] == ["docker", "image", "inspect"]


def test_require_docker_rejects_an_empty_inspection_record_without_pull(monkeypatch):
    calls = []

    def inspect(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="[]")

    monkeypatch.setattr(qdrant_pin.subprocess, "run", inspect)
    with pytest.raises(cluster.QdrantClusterError):
        cluster.require_docker("docker.io/qdrant/qdrant@sha256:" + "a" * 64)
    assert len(calls) == 1 and calls[0][:3] == ["docker", "image", "inspect"]


def test_qdrant_image_pin_ignores_commented_and_blank_lines(tmp_path):
    images = tmp_path / "images.txt"
    images.write_text("#docker.io/qdrant/qdrant:v0.0.1-unprivileged sha256:old\n\n   \n"
                      "docker.io/other/tool:1 sha256:x\n"
                      "docker.io/qdrant/qdrant:v1.19.0-unprivileged sha256:" + "a" * 64 + "\n", encoding="utf-8")
    assert qdrant_pin.qdrant_image_pin(images) == "docker.io/qdrant/qdrant:v1.19.0-unprivileged"


def test_require_docker_returns_immutable_id_after_digest_attestation(monkeypatch):
    reference = "docker.io/qdrant/qdrant@sha256:" + "a" * 64
    image_id = "sha256:" + "b" * 64
    monkeypatch.setattr(qdrant_pin.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0,
        stdout=json.dumps([{"Id": image_id, "RepoDigests": [reference.removeprefix("docker.io/")]}])))
    assert cluster.require_docker(reference) == image_id


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
