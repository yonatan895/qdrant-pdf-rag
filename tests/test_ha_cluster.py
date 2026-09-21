"""Disposable three-peer Qdrant acceptance fixture (issue #360 Slice B).

Starts the pinned image as a real three-peer cluster and exercises the
placement verifier over actual placement: healthy, false-HA configuration,
peer loss (degraded, reads survive), and rejoin, plus the publication
cutover distribution gate over real staging pairs (healthy pass, false-HA
and control-only-mismatch refusal). Each scenario creates and
seeds its own physical corpus/control pair, so any test can run alone or in
any order; expected point ids/payloads are declared here, independent of the
verifier, and re-asserted through survivors after loss and on every peer
after rejoin.

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
from dataclasses import dataclass
from pathlib import Path

import pytest
from qdrant_client import QdrantClient, models
from scripts.qdrant_cluster import (
    QdrantCluster,
    QdrantClusterError,
    free_port,
    start_cluster,
    wait_cluster_ready,
    wait_collection_placement,
)
from scripts.verify_placement import perform_verification

from mainframe_rag.config import Settings
from mainframe_rag.ingest.completion import completion_collection_for
from mainframe_rag.ingest.publish import verify_staging_distribution
from mainframe_rag.ingest.qdrant_io import collection_vector_configs

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[1]
DIM = 64
SHARDS = 6
RF = 3
CORPUS_POINTS = tuple(range(1, 61))
CONTROL_POINTS = tuple(range(1001, 1007))


@dataclass(frozen=True)
class SeededPair:
    corpus: str
    control: str

    def expected(self, collection: str) -> dict[int, str]:
        points = CORPUS_POINTS if collection == self.corpus else CONTROL_POINTS
        prefix = "corpus" if collection == self.corpus else "control"
        return {point_id: f"{prefix}-{point_id}" for point_id in points}


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


@pytest.fixture
def seeded_pair(cluster: QdrantCluster) -> SeededPair:
    """A fresh 6/3/2 corpus+control pair, seeded and fully placed."""
    suffix = uuid.uuid4().hex[:8]
    pair = SeededPair(
        corpus=f"ha_corpus_{suffix}",
        control=f"ha_corpus_{suffix}__completions",
    )
    primary = QdrantClient(url=cluster.urls[0], timeout=60)
    try:
        _create_pair(primary, pair.corpus, replication_factor=RF)
        _seed(primary, pair.corpus, CORPUS_POINTS)
        _seed(primary, pair.control, CONTROL_POINTS)
    finally:
        primary.close()
    for collection in (pair.corpus, pair.control):
        wait_collection_placement(
            cluster.urls, collection, shard_number=SHARDS, replication_factor=RF
        )
    return pair


def _settings(url: str, collection: str, **overrides) -> Settings:
    base = {
        "qdrant_url": url,
        "qdrant_collection": collection,
        "dense_dim": DIM,
        "qdrant_shard_number": SHARDS,
        "qdrant_replication_factor": RF,
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
            shard_number=SHARDS,
            replication_factor=replication_factor,
            write_consistency_factor=min(2, replication_factor),
        )


def _seed(client: QdrantClient, name: str, point_ids: tuple[int, ...]) -> None:
    prefix = "corpus" if point_ids is CORPUS_POINTS else "control"
    points = [
        models.PointStruct(
            id=point_id,
            vector={"dense": [float(point_id)] * DIM},
            payload={"point": point_id, "tag": f"{prefix}-{point_id}"},
        )
        for point_id in point_ids
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


def _assert_exact_payloads(url: str, pair: SeededPair) -> None:
    reader = QdrantClient(url=url, timeout=30)
    try:
        for collection in (pair.corpus, pair.control):
            expected = pair.expected(collection)
            records = reader.retrieve(collection, ids=list(expected), with_payload=True)
            actual = {record.id: record.payload["tag"] for record in records}
            assert actual == expected, f"{url} {collection}"
    finally:
        reader.close()


def test_real_three_peer_healthy_fixture(
    cluster: QdrantCluster, seeded_pair: SeededPair, capsys
):
    settings = _settings(cluster.urls[0], seeded_pair.corpus)
    report, code = _verify(settings, cluster.urls)
    output = capsys.readouterr().out
    assert code == 0, output
    assert report is not None and report.state == "healthy"
    assert report.alias is not None
    assert report.alias.inventory == (seeded_pair.corpus, seeded_pair.control)
    assert {verdict.collection for verdict in report.collections} == {
        seeded_pair.corpus,
        seeded_pair.control,
    }
    for verdict in report.collections:
        assert len(verdict.shards) == SHARDS
        assert all(len(shard.active_peers) == RF for shard in verdict.shards)

    for url in cluster.urls:
        _assert_exact_payloads(url, seeded_pair)


def test_false_ha_configuration_refused(cluster: QdrantCluster, capsys):
    """Three Ready peers with an RF1 corpus and control collection must be
    rejected against the production tuple (the packet's false-HA case)."""
    name = f"ha_false_{uuid.uuid4().hex[:8]}"
    primary = QdrantClient(url=cluster.urls[0], timeout=60)
    try:
        _create_pair(primary, name, replication_factor=1)
    finally:
        primary.close()
    settings = _settings(cluster.urls[0], name)
    report, code = _verify(settings, cluster.urls)
    output = capsys.readouterr().out
    assert code == 1, output
    assert report is not None
    assert report.configured_problems
    assert any("replication_factor=1" in problem for problem in report.configured_problems)


def test_cutover_gate_passes_on_healthy_staging_pair(
    cluster: QdrantCluster, seeded_pair: SeededPair
):
    """The exact cutover helper wired into publication must pass a real
    healthy 6/3/2 staging pair through the same single-client surface the
    publisher uses — guarding against fake/real client drift."""
    settings = _settings(cluster.urls[0], seeded_pair.corpus)
    client = QdrantClient(url=cluster.urls[0], timeout=60)
    try:
        assert verify_staging_distribution(client, settings) == []
    finally:
        client.close()


def test_cutover_gate_refuses_false_ha_staging_pair(cluster: QdrantCluster):
    """An RF1 staging pair must be refused at the publication layer even
    though every collection exists and reads succeed."""
    name = f"ha_cutover_false_{uuid.uuid4().hex[:8]}"
    client = QdrantClient(url=cluster.urls[0], timeout=60)
    try:
        _create_pair(client, name, replication_factor=1)
        settings = _settings(cluster.urls[0], name)
        problems = verify_staging_distribution(client, settings)
    finally:
        client.close()
    assert problems
    assert any("replication_factor=1" in problem for problem in problems)
    assert any("snapshot-gated" in problem for problem in problems)


def test_cutover_gate_refuses_control_only_mismatch(cluster: QdrantCluster):
    """A healthy corpus with an RF1 control collection must still refuse:
    the gate covers the pair, not just the corpus."""
    name = f"ha_cutover_ctl_{uuid.uuid4().hex[:8]}"
    control = completion_collection_for(name)
    vectors, sparse = collection_vector_configs(DIM)
    client = QdrantClient(url=cluster.urls[0], timeout=60)
    try:
        client.create_collection(
            name,
            vectors_config=vectors,
            sparse_vectors_config=sparse,
            on_disk_payload=True,
            shard_number=SHARDS,
            replication_factor=RF,
            write_consistency_factor=2,
        )
        client.create_collection(
            control,
            vectors_config=vectors,
            sparse_vectors_config=sparse,
            on_disk_payload=True,
            shard_number=SHARDS,
            replication_factor=1,
            write_consistency_factor=1,
        )
        settings = _settings(cluster.urls[0], name)
        problems = verify_staging_distribution(client, settings)
    finally:
        client.close()
    assert problems
    assert any(control in problem for problem in problems)
    assert any("replication_factor=1" in problem for problem in problems)


def test_peer_loss_is_degraded_and_rejoins_healthy(
    cluster: QdrantCluster, seeded_pair: SeededPair, capsys
):
    settings = _settings(cluster.urls[0], seeded_pair.corpus)
    dropped = f"{cluster.prefix}-2"
    survivors = (cluster.urls[0], cluster.urls[2])

    try:
        subprocess.run(
            ["docker", "stop", dropped], check=True, capture_output=True, text=True
        )
        report, code = _verify(settings, cluster.urls)
        output = capsys.readouterr().out
        assert code == 1, output
        assert report is not None and report.state == "degraded"
        assert any("unreachable" in problem for problem in report.cluster.problems)

        # The published generation stays readable, with exact identity,
        # through the survivors.
        for url in survivors:
            _assert_exact_payloads(url, seeded_pair)
    finally:
        subprocess.run(
            ["docker", "start", dropped], check=True, capture_output=True, text=True
        )
        wait_cluster_ready(cluster.urls)

    # Membership alone is not catch-up: every shard must be ACTIVE on RF
    # distinct peers again before re-qualification.
    for collection in (seeded_pair.corpus, seeded_pair.control):
        wait_collection_placement(
            cluster.urls, collection, shard_number=SHARDS, replication_factor=RF
        )
    report, code = _verify(settings, cluster.urls)
    output = capsys.readouterr().out
    assert code == 0, output
    assert report is not None and report.state == "healthy"
    for url in cluster.urls:
        _assert_exact_payloads(url, seeded_pair)


# New points for the write path must not collide with the seeded identity
# (corpus 1..60, control 1001..1006).
WRITE_CORPUS_POINTS = tuple(range(2001, 2011))
WRITE_CONTROL_POINTS = tuple(range(3001, 3004))


def _write_points(client: QdrantClient, collection: str, point_ids: tuple[int, ...], prefix: str) -> None:
    """Upsert stable-ID points; returning under wait=True is the write
    acknowledgement this lane demonstrates."""
    points = [
        models.PointStruct(
            id=point_id,
            vector={"dense": [float(point_id)] * DIM},
            payload={"point": point_id, "tag": f"{prefix}-{point_id}"},
        )
        for point_id in point_ids
    ]
    client.upsert(collection, points=points, wait=True)


def _assert_points_exact(url: str, collection: str, point_ids: tuple[int, ...], prefix: str) -> None:
    """Exact ID set and payloads through one endpoint: a duplicate,
    divergent, or missing copy fails here."""
    reader = QdrantClient(url=url, timeout=30)
    try:
        records = reader.retrieve(collection, ids=list(point_ids), with_payload=True)
        actual = {record.id: record.payload["tag"] for record in records}
        assert actual == {point_id: f"{prefix}-{point_id}" for point_id in point_ids}, (
            f"{url} {collection}"
        )
    finally:
        reader.close()


def test_acknowledged_writes_survive_one_peer_loss(cluster: QdrantCluster, seeded_pair: SeededPair):
    """W=2 writes acknowledged while one peer is down must survive with
    exact identity: written via one survivor, read back via the other, and
    present on every peer after rejoin. Bounded synthetic RPO-0 target for
    acknowledged writes only — unacknowledged writes are out of scope and
    this is not a production SLA."""
    dropped = f"{cluster.prefix}-2"
    writer_url, reader_url = cluster.urls[0], cluster.urls[2]
    try:
        subprocess.run(
            ["docker", "stop", dropped], check=True, capture_output=True, text=True
        )
        writer = QdrantClient(url=writer_url, timeout=60)
        try:
            _write_points(writer, seeded_pair.corpus, WRITE_CORPUS_POINTS, "corpus")
            _write_points(writer, seeded_pair.control, WRITE_CONTROL_POINTS, "control")
        finally:
            writer.close()
        # Acknowledged writes must be visible beyond the written peer.
        _assert_points_exact(reader_url, seeded_pair.corpus, WRITE_CORPUS_POINTS, "corpus")
        _assert_points_exact(reader_url, seeded_pair.control, WRITE_CONTROL_POINTS, "control")
        # The previously published generation stays exact through survivors.
        _assert_exact_payloads(reader_url, seeded_pair)
    finally:
        subprocess.run(
            ["docker", "start", dropped], check=True, capture_output=True, text=True
        )
        wait_cluster_ready(cluster.urls)
    for collection in (seeded_pair.corpus, seeded_pair.control):
        wait_collection_placement(
            cluster.urls, collection, shard_number=SHARDS, replication_factor=RF
        )
    for url in cluster.urls:
        _assert_points_exact(url, seeded_pair.corpus, WRITE_CORPUS_POINTS, "corpus")
        _assert_points_exact(url, seeded_pair.control, WRITE_CONTROL_POINTS, "control")
        _assert_exact_payloads(url, seeded_pair)


def test_identical_id_retry_converges_without_duplicates(
    cluster: QdrantCluster, seeded_pair: SeededPair
):
    """Client-observable ambiguous-write retry: overlapping identical-ID
    batches with identical content (as if the first batch ambiguously failed
    mid-apply) must converge to the exact expected union on every peer, with
    no duplicates or divergent content. This exercises publisher stable-ID
    retry semantics, not replica-level fault injection."""
    client = QdrantClient(url=cluster.urls[0], timeout=60)
    try:
        _write_points(client, seeded_pair.corpus, WRITE_CORPUS_POINTS[:5], "corpus")
        _write_points(client, seeded_pair.corpus, WRITE_CORPUS_POINTS[3:], "corpus")
    finally:
        client.close()
    for url in cluster.urls:
        _assert_points_exact(url, seeded_pair.corpus, WRITE_CORPUS_POINTS, "corpus")
