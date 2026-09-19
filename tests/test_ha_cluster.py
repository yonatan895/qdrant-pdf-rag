"""Disposable three-peer Qdrant acceptance fixture (issue #360 Slice B).

Starts the pinned image as a real three-peer cluster, creates the corpus and
control collections with the checked-in production tuple (6 shards / RF 3 /
W 2), and exercises the placement verifier over actual placement: healthy,
false-HA configuration, peer loss (degraded, reads survive), and rejoin.

Local disposable fixture only: three containers on one host prove
distributed software behavior, not independent-worker or site tolerance.
Requires docker and the pinned image (qa:ha downloads it on a connected
host); a missing prerequisite fails rather than silently skipping.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from qdrant_client import QdrantClient, models
from scripts.qdrant_cluster import (
    QdrantCluster,
    QdrantClusterError,
    free_port,
    start_cluster,
    wait_cluster_ready,
)
from scripts.verify_placement import perform_verification

from mainframe_rag.config import Settings
from mainframe_rag.ingest.completion import completion_collection_for
from mainframe_rag.ingest.qdrant_io import collection_vector_configs

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[1]
CORPUS = "ha_corpus"
FALSE_HA = "ha_false"
DIM = 64
POINTS = tuple(range(1, 61))


@pytest.fixture(scope="module")
def cluster() -> QdrantCluster:
    if shutil.which("docker") is None:
        pytest.fail("docker is required for the three-peer HA fixture")
    prefix = f"qdrant-ha-test-{uuid.uuid4().hex[:8]}"
    try:
        started = start_cluster(REPO, prefix=prefix, base_port=free_port(7600))
    except QdrantClusterError as exc:
        pytest.fail(str(exc))
    try:
        yield started
    finally:
        started.stop()


def _settings(url: str, collection: str, **overrides) -> Settings:
    base = {
        "qdrant_url": url,
        "qdrant_collection": collection,
        "dense_dim": DIM,
        "qdrant_shard_number": 6,
        "qdrant_replication_factor": 3,
        "qdrant_write_consistency_factor": 2,
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


def _create_pair(client: QdrantClient, name: str, replication_factor: int) -> None:
    vectors, sparse = collection_vector_configs(DIM)
    for collection in (name, completion_collection_for(name)):
        client.create_collection(
            collection,
            vectors_config=vectors,
            sparse_vectors_config=sparse,
            on_disk_payload=True,
            shard_number=6,
            replication_factor=replication_factor,
            write_consistency_factor=min(2, replication_factor),
        )


def _seed(client: QdrantClient, name: str) -> None:
    points = [
        models.PointStruct(
            id=point_id,
            vector={"dense": [float(point_id)] * DIM},
            payload={"point": point_id, "tag": f"synthetic-{point_id}"},
        )
        for point_id in POINTS
    ]
    client.upsert(name, points=points, wait=True)


def _verify(settings: Settings, urls: tuple[str, ...]):
    args = argparse.Namespace(
        production=True,
        expect_single_node=False,
        peer_url=list(urls),
        expect_peers=None,
        allow_degraded=False,
        timeout=10.0,
    )
    return perform_verification(settings, args)


def test_real_three_peer_healthy_fixture(cluster: QdrantCluster, capsys):
    primary = QdrantClient(url=cluster.urls[0], timeout=60)
    _create_pair(primary, CORPUS, replication_factor=3)
    _seed(primary, CORPUS)

    settings = _settings(cluster.urls[0], CORPUS)
    report, code = _verify(settings, cluster.urls)
    output = capsys.readouterr().out
    assert code == 0, output
    assert report is not None and report.state == "healthy"
    assert {verdict.collection for verdict in report.collections} == {
        CORPUS,
        completion_collection_for(CORPUS),
    }
    for verdict in report.collections:
        assert len(verdict.shards) == 6
        assert all(len(shard.active_peers) == 3 for shard in verdict.shards)

    # Exact producer-to-consumer round-trip through a surviving peer entry.
    for url in cluster.urls:
        reader = QdrantClient(url=url, timeout=30)
        count = reader.count(CORPUS, exact=True).count
        assert count == len(POINTS), url
        records = reader.retrieve(CORPUS, ids=list(POINTS), with_payload=True)
        assert {
            record.id: record.payload["tag"] for record in records
        } == {point_id: f"synthetic-{point_id}" for point_id in POINTS}


def test_false_ha_configuration_refused(cluster: QdrantCluster, capsys):
    """Three Ready peers with an RF1 corpus and control collection must be
    rejected against the production tuple (the packet's false-HA case)."""
    primary = QdrantClient(url=cluster.urls[0], timeout=60)
    _create_pair(primary, FALSE_HA, replication_factor=1)
    settings = _settings(cluster.urls[0], FALSE_HA)
    report, code = _verify(settings, cluster.urls)
    output = capsys.readouterr().out
    assert code == 1, output
    assert report is not None
    assert report.configured_problems
    assert any("replication_factor=1" in problem for problem in report.configured_problems)


def test_peer_loss_is_degraded_and_rejoins_healthy(cluster: QdrantCluster, capsys):
    settings = _settings(cluster.urls[0], CORPUS)
    dropped = f"{cluster.prefix}-2"
    containers = ["docker", "stop", dropped]

    try:
        subprocess.run(containers, check=True, capture_output=True, text=True)
        report, code = _verify(settings, cluster.urls)
        output = capsys.readouterr().out
        assert code == 1, output
        assert report is not None and report.state == "degraded"
        assert any("unreachable" in problem for problem in report.cluster.problems)

        # The published generation stays readable through a survivor.
        reader = QdrantClient(url=cluster.urls[0], timeout=30)
        assert reader.count(CORPUS, exact=True).count == len(POINTS)
    finally:
        subprocess.run(
            ["docker", "start", dropped], check=True, capture_output=True, text=True
        )
        wait_cluster_ready(cluster.urls)

    report, code = _verify(settings, cluster.urls)
    output = capsys.readouterr().out
    assert code == 0, output
    assert report is not None and report.state == "healthy"
