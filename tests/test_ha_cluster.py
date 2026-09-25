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
Requires docker and the explicitly prepared pinned image (artifacts:qdrant on a connected
host); a missing prerequisite fails rather than silently skipping.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException
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
from mainframe_rag.ingest.publish import verify_staging_distribution, verify_staging_placement
from mainframe_rag.ingest.qdrant_io import collection_vector_configs
from mainframe_rag.ingest.run_ingest import _placement_clients
from tests import test_airgap_ingest_sh as ingest_shell
from tests.helpers_airgap import rendered_env

pytestmark = pytest.mark.integration
ingest_tree = ingest_shell.ingest_tree

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


def test_active_copy_gate_blocks_cutover_while_degraded(
    cluster: QdrantCluster, seeded_pair: SeededPair
):
    """The in-process cutover gate must refuse while one peer is down and
    pass again after rejoin: degraded placement blocks the swap while the
    old generation keeps serving. Same per-endpoint client surface the
    publisher builds from QDRANT_PEER_URLS."""
    settings = _settings(cluster.urls[0], seeded_pair.corpus)
    dropped = f"{cluster.prefix}-2"
    clients = {url: QdrantClient(url=url, timeout=30) for url in cluster.urls}
    try:
        assert verify_staging_placement(clients, settings) == []
        subprocess.run(
            ["docker", "stop", dropped], check=True, capture_output=True, text=True
        )
        problems = verify_staging_placement(clients, settings)
        assert problems
        assert any(
            word in problems[0] for word in ("degraded", "unreachable", "unverifiable")
        )
        subprocess.run(
            ["docker", "start", dropped], check=True, capture_output=True, text=True
        )
        wait_cluster_ready(cluster.urls)
        for collection in (seeded_pair.corpus, seeded_pair.control):
            wait_collection_placement(
                cluster.urls, collection, shard_number=SHARDS, replication_factor=RF
            )
        # Post-rejoin convergence is not instant: ACTIVE placement returns
        # before catch-up finishes, so the gate is waited for, not asserted
        # once. A permanent loss times the wait out instead of passing.
        deadline = time.monotonic() + 120.0
        problems = ["no attempt"]
        while time.monotonic() < deadline:
            problems = verify_staging_placement(clients, settings)
            if not problems:
                break
            time.sleep(2.0)
        assert problems == [], problems
    finally:
        for client in clients.values():
            client.close()


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


# Partition points must not collide with seeded or write-path identity.
PART_CORPUS_POINTS = tuple(range(4001, 4006))
PART_CONTROL_POINTS = tuple(range(5001, 5003))
PART_MINORITY_POINTS = tuple(range(6001, 6003))


def _wait_points_exact(
    urls: tuple[str, ...],
    collection: str,
    point_ids: tuple[int, ...],
    prefix: str,
    *,
    timeout_s: float = 60.0,
) -> None:
    """Wait until every peer serves the exact expected ID set and payloads.
    Permanent loss or divergence times out with the last mismatch instead of
    passing silently."""
    import time

    deadline = time.monotonic() + timeout_s
    last: str = "no attempt"
    while time.monotonic() < deadline:
        try:
            for url in urls:
                _assert_points_exact(url, collection, point_ids, prefix)
        except AssertionError as exc:
            last = str(exc)
            time.sleep(1.0)
            continue
        return
    raise AssertionError(f"exact reads never converged on {collection}: {last}")


def _wait_pair_exact(urls: tuple[str, ...], pair: SeededPair, *, timeout_s: float = 60.0) -> None:
    """Same convergence wait for a seeded corpus/control pair."""
    import time

    deadline = time.monotonic() + timeout_s
    last: str = "no attempt"
    while time.monotonic() < deadline:
        try:
            for url in urls:
                _assert_exact_payloads(url, pair)
        except AssertionError as exc:
            last = str(exc)
            time.sleep(1.0)
            continue
        return
    raise AssertionError(f"seeded pair never converged: {last}")


def _wait_points_agree(
    urls: tuple[str, ...],
    collection: str,
    point_ids: tuple[int, ...],
    prefix: str,
    *,
    timeout_s: float = 60.0,
) -> dict[int, str]:
    """Wait until every peer reports the same state for the points and every
    present record carries the exact expected payload. Which Raft winner
    commits a timed-out minority write is timing-dependent; permanent
    divergence or corrupt content is not — that is what fails here."""
    import time

    deadline = time.monotonic() + timeout_s
    last: list[dict[int, str]] = []
    while time.monotonic() < deadline:
        states: list[dict[int, str]] = []
        for url in urls:
            reader = QdrantClient(url=url, timeout=30)
            try:
                records = reader.retrieve(collection, ids=list(point_ids), with_payload=True)
                states.append({record.id: record.payload["tag"] for record in records})
            finally:
                reader.close()
        last = states
        if states[0] == states[1] == states[2] and all(
            tag == f"{prefix}-{point_id}" for point_id, tag in states[0].items()
        ):
            return states[0]
        time.sleep(1.0)
    raise AssertionError(f"peers never converged on {collection} {point_ids}: {last}")


def test_partition_minority_writes_unacknowledged_and_majority_durable(
    cluster: QdrantCluster, seeded_pair: SeededPair
):
    """2+1 partition: writes through the isolated minority peer must never
    acknowledge — any transport refusal (client timeout or reset, never
    asserted which) with no false W2 success — while majority-acked writes
    stay exact across survivors. After heal every peer converges to one exact
    state: the majority points exact everywhere, and the timed-out minority
    points either all absent or all exact — a timed-out write may still commit
    later via re-election, so retries must reuse identical IDs *and* identical
    content. No publication is attempted from the minority side. Timing is
    bounded by client timeouts, never asserted."""
    isolated = f"{cluster.prefix}-3"
    majority_writer, majority_reader = cluster.urls[0], cluster.urls[1]
    minority_url = cluster.urls[2]
    try:
        subprocess.run(
            ["docker", "network", "disconnect", cluster.network, isolated],
            check=True,
            capture_output=True,
            text=True,
        )
        majority = QdrantClient(url=majority_writer, timeout=60)
        try:
            _write_points(majority, seeded_pair.corpus, PART_CORPUS_POINTS, "corpus")
            _write_points(majority, seeded_pair.control, PART_CONTROL_POINTS, "control")
        finally:
            majority.close()
        _assert_points_exact(majority_reader, seeded_pair.corpus, PART_CORPUS_POINTS, "corpus")
        _assert_points_exact(majority_reader, seeded_pair.control, PART_CONTROL_POINTS, "control")
        _assert_exact_payloads(majority_reader, seeded_pair)
        minority = QdrantClient(url=minority_url, timeout=15)
        try:
            # Every transport failure funnels through ResponseHandlingException
            # (observed: client timeout or connection reset); the invariant is
            # no acknowledgement, never the refusal flavor.
            with pytest.raises(ResponseHandlingException):
                _write_points(minority, seeded_pair.corpus, PART_MINORITY_POINTS, "corpus")
        finally:
            minority.close()
    finally:
        subprocess.run(
            ["docker", "network", "connect", cluster.network, isolated],
            check=True,
            capture_output=True,
            text=True,
        )
        wait_cluster_ready(cluster.urls)
    for collection in (seeded_pair.corpus, seeded_pair.control):
        wait_collection_placement(
            cluster.urls, collection, shard_number=SHARDS, replication_factor=RF
        )
    # ACTIVE placement is not read convergence: a rejoined peer reports
    # ACTIVE replicas before finishing data sync, so exact reads must be
    # waited for, never asserted once. A permanent loss times the wait out
    # instead of passing silently.
    _wait_points_exact(
        cluster.urls, seeded_pair.corpus, PART_CORPUS_POINTS, "corpus"
    )
    _wait_points_exact(
        cluster.urls, seeded_pair.control, PART_CONTROL_POINTS, "control"
    )
    _wait_pair_exact(cluster.urls, seeded_pair)
    _wait_points_agree(
        cluster.urls, seeded_pair.corpus, PART_MINORITY_POINTS, "corpus"
    )


def test_operator_job_peers_reach_real_placement_gate(cluster, seeded_pair, ingest_tree):
    """Rendered operator input drives real corpus/control placement observations."""
    main_client = QdrantClient(url=cluster.urls[0], timeout=30)
    healthy = " " + ",\n\t".join(cluster.urls) + " "
    unreachable = f"http://127.0.0.1:{free_port(7900)}"
    selections = [
        (healthy, "healthy"),
        ("", "missing"),
        (",".join([cluster.urls[0]] * 3), "duplicate"),
        (",".join([*cluster.urls[:2], unreachable]), "unreachable"),
    ]
    try:
        for raw, outcome in selections:
            result = ingest_shell._run_ingest(
                ingest_tree, ("QDRANT_PEER_URLS", raw),
                ("INGEST_ALIAS_PUBLISH", "true"), policy=("6", "3", "2"),
            )
            assert result.returncode == 0, result.stderr
            rendered = (ingest_tree[0] / "dist/ingest-rendered.yaml").read_text()
            job_env = rendered_env(rendered, "ingest")
            settings = _settings(
                cluster.urls[0], seeded_pair.corpus,
                qdrant_peer_urls=job_env.get("QDRANT_PEER_URLS", ""),
                qdrant_shard_number=int(job_env["QDRANT_SHARD_NUMBER"]),
                qdrant_replication_factor=int(job_env["QDRANT_REPLICATION_FACTOR"]),
                qdrant_write_consistency_factor=int(job_env["QDRANT_WRITE_CONSISTENCY_FACTOR"]),
                qdrant_ingest_timeout_s=2,
            )
            if outcome == "missing":
                with (
                    pytest.raises(RuntimeError, match="no direct Qdrant peer endpoints"),
                    _placement_clients(settings, main_client),
                ):
                    pytest.fail("missing peers must refuse")
                continue
            with _placement_clients(settings, main_client) as clients:
                if outcome == "healthy":
                    assert job_env["QDRANT_PEER_URLS"] == healthy
                    assert tuple(clients) == cluster.urls
                    assert verify_staging_placement(clients, settings) == []
                else:
                    assert verify_staging_placement(clients, settings)
            # Closing the per-peer clients must leave the shared client usable.
            assert main_client.count(seeded_pair.corpus, exact=True).count == len(CORPUS_POINTS)
        _assert_exact_payloads(cluster.urls[0], seeded_pair)
    finally:
        main_client.close()


@pytest.mark.parametrize("refuse_first", [False, True], ids=["fresh", "refuse-recover"])
def test_operator_job_publishes_exact_corpus_on_three_peers(cluster, ingest_tree, tmp_path, refuse_first):
    """Operator render -> image entrypoint CLI -> real 6/3/2 publication."""
    import json
    import os
    import signal
    import sys

    from mainframe_rag.ingest.publish import publish_state_path
    from tests.helpers_airgap import rendered_container
    from tests.helpers_publication_lifecycle import TEXTS, _records, _write_source

    tree = ingest_tree[0]
    shutil.copy(REPO / "Taskfile.yml", tree)
    shutil.copytree(REPO / "taskfiles", tree / "taskfiles")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for name, text in TEXTS.items():
        _write_source(corpus, name, text)
    progress = tmp_path / "inventory.jsonl"
    alias = "mainframe_manuals"  # actual canonical Job target, on an owned cluster
    clients = [QdrantClient(url=url, timeout=30) for url in cluster.urls]

    def task_runner(script, env, cwd):
        return subprocess.run(
            [str(REPO / ".tools/bin/task"), "airgap:ingest"],
            env=env, cwd=cwd, capture_output=True, text=True, check=False,
        )

    launched = 0

    def run_job(peers):
        nonlocal launched
        result = ingest_shell._run_ingest(
            ingest_tree, ("QDRANT_PEER_URLS", peers), ("INGEST_ALIAS_PUBLISH", "true"),
            ("INGEST_WORKERS", "1"), ("DENSE_DIM", str(DIM)),
            ("OTEL_EXPORTER_OTLP_ENDPOINT", "off"),
            policy=("6", "3", "2"), runner=task_runner,
        )
        assert result.returncode == 0, result.stderr
        rendered = (tree / "dist/ingest-rendered.yaml").read_text()
        container = rendered_container(rendered, "ingest")
        job_env = rendered_env(rendered, "ingest")
        assert job_env["QDRANT_COLLECTION"] == alias
        assert job_env.get("QDRANT_PEER_URLS", "") == peers
        assert [job_env[key] for key in ("QDRANT_SHARD_NUMBER", "QDRANT_REPLICATION_FACTOR",
                                         "QDRANT_WRITE_CONSISTENCY_FACTOR")] == ["6", "3", "2"]
        assert "EMBED_MODE" not in job_env and "ALLOW_HASH_MODE" not in job_env
        # Execute the checked-in image's entrypoint with the rendered arguments.
        entrypoint_line = next(line for line in (REPO / "images/Containerfile.ingest").read_text().splitlines()
                               if line.startswith("ENTRYPOINT "))
        entrypoint = json.loads(entrypoint_line.removeprefix("ENTRYPOINT "))
        assert entrypoint == ["python3", "-m", "mainframe_rag.ingest.run_ingest"]
        args = [str(corpus) if arg == "/corpus" else str(progress)
                if arg == "/work/inventory.jsonl" else arg for arg in container["args"]]
        # Lab adaptations only: Kubernetes service/mounts and deterministic compute.
        # Peer/policy inputs stay exactly as emitted by the operator launcher.
        env = {key: value for key, value in os.environ.items()
               if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}}
        env.update({key: value for key, value in job_env.items() if isinstance(value, str)})
        env.update(QDRANT_URL=cluster.urls[0], EMBED_MODE="hash", ALLOW_HASH_MODE="true")
        launched += 1
        log = tmp_path / f"ingest-{launched}.log"
        command = [sys.executable, *entrypoint[1:], *args]
        with log.open("w") as output:
            process = subprocess.Popen(
                command, env=env, cwd=REPO, stdout=output,
                stderr=subprocess.STDOUT, start_new_session=True,
            )
            try:
                code = process.wait(timeout=120)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=10)
        return subprocess.CompletedProcess(command, code, log.read_text(), "")

    try:
        state_path = publish_state_path(progress, alias)
        recorded = None
        if refuse_first:
            # Missing peers cannot be repaired by replica counts or the healthy server.
            refused = run_job("")
            assert refused.returncode != 0
            assert "no direct Qdrant peer endpoints" in refused.stdout
            assert not any(a.alias_name == alias for a in clients[0].get_aliases().aliases)
            recorded = json.loads(state_path.read_text())["staging"]
            duplicate = run_job(",".join([cluster.urls[0]] * 3))
            assert duplicate.returncode != 0
            assert not any(a.alias_name == alias for a in clients[0].get_aliases().aliases)
            assert json.loads(state_path.read_text())["staging"] == recorded
        healthy = " " + ",\n\t".join(cluster.urls) + " "
        published = run_job(healthy)
        assert published.returncode == 0, published.stdout + published.stderr
        actual = next(a.collection_name for a in clients[0].get_aliases().aliases if a.alias_name == alias)
        if recorded is not None:
            assert actual == recorded
        recorded = actual
        assert not state_path.exists()
        frozen = None
        for client in clients:
            assert next(a.collection_name for a in client.get_aliases().aliases
                        if a.alias_name == alias) == recorded
            pair = (_records(client, recorded), _records(client, recorded + "__completions"))
            for collection in (recorded, recorded + "__completions"):
                params = client.get_collection(collection).config.params
                assert (params.shard_number, params.replication_factor,
                        params.write_consistency_factor) == (6, 3, 2)
            manifests = [p for p, _ in pair[1].values() if p.get("record_type") == "representation-manifest"]
            assert len(manifests) == 1 and manifests[0]["state"] == "committed"
            assert manifests[0]["target_collection"] == recorded + "__completions"
            receipts = [p for p, _ in pair[1].values() if p.get("record_type") == "publication-metadata"]
            assert len(receipts) == 1 and receipts[0]["target_collection"] == recorded + "__completions"
            by_doc = {}
            for payload, _ in pair[0].values():
                by_doc.setdefault(payload["doc_id"], []).append(payload["text"])
            assert by_doc == {name: [text] for name, text in TEXTS.items()}
            markers = [p for p, _ in pair[1].values() if "expected_chunks" in p]
            assert {p["doc_id"] for p in markers} == set(TEXTS)
            assert all(p["expected_chunks"] == 1 and p["target_collection"] == recorded for p in markers)
            if frozen is None:
                frozen = pair
            assert pair == frozen
        for _ in range(2):
            repeated = run_job(healthy)
            assert repeated.returncode == 0, repeated.stdout + repeated.stderr
            for client in clients:
                assert next(a.collection_name for a in client.get_aliases().aliases
                            if a.alias_name == alias) == recorded
                assert (_records(client, recorded), _records(client, recorded + "__completions")) == frozen
            assert not state_path.exists()
    finally:
        try:
            if any(a.alias_name == alias for a in clients[0].get_aliases().aliases):
                clients[0].update_collection_aliases([
                    models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias))
                ])
            for collection in clients[0].get_collections().collections:
                if collection.name.startswith(alias + "__gen"):
                    clients[0].delete_collection(collection.name)
        finally:
            for client in clients:
                client.close()
