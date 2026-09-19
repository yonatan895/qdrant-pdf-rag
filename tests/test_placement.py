"""Replica-placement evaluation tests (issue #360 Slice B).

Pure evaluation over synthetic observations plus script-level claim/exit
tests with fake clients — no Qdrant needed. Every refused state pins its
precise cause: a pending, under-replicated, stale, unreachable, remote-only,
or unknown placement is never healthy, and the 6/3/2 owner decision is
distinct from the explicit 1/1/1 non-HA profile.
"""

import argparse
from dataclasses import replace
from types import SimpleNamespace

import pytest
from qdrant_client import models
from scripts.verify_placement import perform_verification

from mainframe_rag.config import Settings
from mainframe_rag.ingest.placement import (
    PRODUCTION_POLICY,
    SINGLE_NODE_POLICY,
    CollectionObservation,
    PeerReachability,
    PlacementPolicy,
    PolicyClaimError,
    VerificationReport,
    configured_policy_problems,
    consensus_name,
    evaluate_cluster,
    evaluate_collection_placement,
    evidence_lines,
    member_peer_ids,
    observe_collection,
    resolve_alias_mapping,
    resolve_verification_inventory,
    selected_policy,
)

COLLECTION = "mainframe_manuals_g3"
CONTROL = "mainframe_manuals_g3__completions"
PRIMARY = "http://qdrant:6333"
PEERS = (101, 202, 303)


def _local(shard_id, state="ACTIVE"):
    return models.LocalShardInfo(
        shard_id=shard_id, points_count=10, state=models.ReplicaState[state]
    )


def _remote(shard_id, peer_id, state="ACTIVE"):
    return models.RemoteShardInfo(
        shard_id=shard_id, peer_id=peer_id, state=models.ReplicaState[state]
    )


def _transfer(shard_id, from_peer, to_peer):
    return models.ShardTransferInfo(
        shard_id=shard_id, **{"from": from_peer}, to=to_peer, sync=False
    )


def _info(peer_id, shard_count, local=(), remote=(), transfers=()):
    return models.CollectionClusterInfo(
        peer_id=peer_id,
        shard_count=shard_count,
        local_shards=list(local),
        remote_shards=list(remote),
        shard_transfers=list(transfers),
    )


def _obs(
    peer_id,
    shard_count,
    local=(),
    remote=(),
    transfers=(),
    *,
    collection=COLLECTION,
    endpoint=None,
    missing=False,
    reachable=True,
    error=None,
):
    if missing:
        return CollectionObservation(
            collection=collection,
            endpoint=endpoint or f"peer-{peer_id}",
            reachable=True,
            missing=True,
        )

    def local_pair(item):
        if isinstance(item, tuple):
            return (int(item[0]), str(item[1]).upper())
        return (item.shard_id, item.state.name.upper())

    def remote_triple(item):
        if isinstance(item, tuple):
            return (int(item[0]), int(item[1]), str(item[2]).upper())
        return (item.shard_id, item.peer_id, item.state.name.upper())

    return CollectionObservation(
        collection=collection,
        endpoint=endpoint or f"peer-{peer_id}",
        reachable=reachable,
        peer_id=peer_id,
        shard_count=shard_count,
        local_shards=tuple(local_pair(item) for item in local),
        remote_shards=tuple(remote_triple(item) for item in remote),
        transfers=tuple((item.shard_id, item.from_, item.to) for item in transfers),
        error=error,
    )


def _fleet(
    shard_count=6,
    peers=PEERS,
    copies=3,
    state="ACTIVE",
    collection=COLLECTION,
    unreachable=(),
):
    """Every shard on `copies` distinct peers; each peer's remote view names
    exactly the shards the other peers hold (deduplication must never inflate
    the observed copy count)."""
    assignment = {peer: set() for peer in peers}
    if copies == len(peers):
        for peer in peers:
            assignment[peer] = set(range(shard_count))
    else:
        for shard_id in range(shard_count):
            for offset in range(copies):
                assignment[peers[(shard_id + offset) % len(peers)]].add(shard_id)
    observations = []
    for peer in peers:
        if peer in unreachable:
            observations.append(
                _obs(peer, None, collection=collection, reachable=False, error="ConnectError")
            )
            continue
        local = tuple(_local(shard_id, state) for shard_id in sorted(assignment[peer]))
        remote = tuple(
            _remote(shard_id, other, state)
            for other in peers
            if other != peer
            for shard_id in sorted(assignment[other])
        )
        observations.append(
            _obs(peer, shard_count, local=local, remote=remote, collection=collection)
        )
    return observations


def _with_local_state(observations, peer_id, shard_id, state):
    """Rewrite one peer's local replica state consistently in every view
    (the simulating caller keeps remote reports in sync)."""
    updated = []
    for view in observations:
        if view.missing or not view.reachable:
            updated.append(view)
            continue
        local = tuple(
            (shard, state if (view.peer_id == peer_id and shard == shard_id) else current)
            for shard, current in view.local_shards
        )
        remote = tuple(
            (shard, peer, state if (peer == peer_id and shard == shard_id) else current)
            for shard, peer, current in view.remote_shards
        )
        updated.append(replace(view, local_shards=local, remote_shards=remote))
    return updated


# ------------------------------------------------------------------ claims


def _settings(**overrides):
    base = {
        "qdrant_url": PRIMARY,
        "qdrant_collection": "mainframe_manuals",
        "dense_dim": 8,
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


def test_production_requires_the_owner_decision():
    settings = _settings(
        qdrant_shard_number=6,
        qdrant_replication_factor=3,
        qdrant_write_consistency_factor=2,
    )
    policy = selected_policy(settings, production=True, single_node=False)
    assert policy is not None and policy.as_tuple == PRODUCTION_POLICY


def test_production_rejects_other_complete_tuples():
    settings = _settings(
        qdrant_shard_number=3,
        qdrant_replication_factor=2,
        qdrant_write_consistency_factor=1,
    )
    with pytest.raises(PolicyClaimError, match="owner decision"):
        selected_policy(settings, production=True, single_node=False)


def test_contradictory_claims_rejected():
    with pytest.raises(PolicyClaimError, match="contradictory"):
        selected_policy(_settings(), production=True, single_node=True)


def test_partial_policy_rejected():
    with pytest.raises(PolicyClaimError, match="incomplete"):
        selected_policy(
            _settings(qdrant_replication_factor=2), production=False, single_node=False
        )


def test_no_claim_is_none():
    assert selected_policy(_settings(), production=False, single_node=False) is None


def test_single_node_synthesizes_one_one_one():
    policy = selected_policy(_settings(), production=False, single_node=True)
    assert policy is not None and policy.as_tuple == SINGLE_NODE_POLICY


def test_single_node_declaration_cannot_hide_a_bigger_policy():
    settings = _settings(
        qdrant_shard_number=6,
        qdrant_replication_factor=3,
        qdrant_write_consistency_factor=2,
    )
    with pytest.raises(PolicyClaimError, match="1/1/1"):
        selected_policy(settings, production=False, single_node=True)


def test_write_above_replication_rejected():
    with pytest.raises(PolicyClaimError, match="exceeds"):
        selected_policy(
            _settings(
                qdrant_shard_number=6,
                qdrant_replication_factor=2,
                qdrant_write_consistency_factor=3,
            ),
            production=False,
            single_node=False,
        )


# ------------------------------------------------------- configured policy


def _params(shard_number=6, replication_factor=3, write_consistency_factor=2):
    return SimpleNamespace(
        shard_number=shard_number,
        replication_factor=replication_factor,
        write_consistency_factor=write_consistency_factor,
    )


def test_configured_policy_matches():
    assert configured_policy_problems(COLLECTION, _params(), PlacementPolicy(6, 3, 2)) == ()


def test_configured_policy_mismatch_names_migration():
    problems = configured_policy_problems(COLLECTION, _params(1, 1, 1), PlacementPolicy(6, 3, 2))
    assert len(problems) == 3
    assert all("migration" in problem for problem in problems)


def test_configured_policy_unknown_is_unverifiable():
    params = SimpleNamespace(
        shard_number=None, replication_factor=None, write_consistency_factor=None
    )
    problems = configured_policy_problems(COLLECTION, params, PlacementPolicy(6, 3, 2))
    assert len(problems) == 3
    assert all("unknown/unreadable" in problem for problem in problems)


# --------------------------------------------------------------- placement


def test_healthy_rf3_all_six_shards():
    verdict = evaluate_collection_placement(
        COLLECTION, _fleet(), PlacementPolicy(6, 3, 2)
    )
    assert verdict.state == "healthy"
    assert len(verdict.shards) == 6
    assert all(shard.active_peers == PEERS for shard in verdict.shards)


def test_three_ready_peers_one_copy_refused():
    """The packet counterexample: peers present, but one copy per shard must
    refuse an HA claim against RF=3."""
    observations = _fleet(copies=1)
    verdict = evaluate_collection_placement(COLLECTION, observations, PlacementPolicy(6, 3, 2))
    assert verdict.state == "degraded"
    assert any("only 1/3" in problem for problem in verdict.problems)


def test_remote_reports_never_count_as_copies():
    """A single peer's memory of remote replicas is not an observation of
    those copies: only local reports certify placement."""
    peer = PEERS[0]
    only = _obs(
        peer,
        6,
        local=[_local(shard) for shard in range(6)],
        remote=[
            _remote(shard, other)
            for other in PEERS
            if other != peer
            for shard in range(6)
        ],
    )
    assert len(only.remote_shards) == 12
    verdict = evaluate_collection_placement(COLLECTION, [only], PlacementPolicy(6, 3, 2))
    assert verdict.state == "degraded"
    assert all(shard.active_peers == (peer,) for shard in verdict.shards)


def test_remote_report_disagreement_is_unverifiable():
    """Another peer's memory contradicting a reachable peer's own report is
    an incomplete observation, not a copy."""
    observations = _fleet()
    peer = observations[0]
    local = tuple(
        (shard_id, "DEAD" if shard_id == 0 else state)
        for shard_id, state in peer.local_shards
    )
    observations[0] = replace(peer, local_shards=local)
    verdict = evaluate_collection_placement(COLLECTION, observations, PlacementPolicy(6, 3, 2))
    assert verdict.state == "unverifiable"
    assert any("disagree" in problem for problem in verdict.problems)


def test_remote_report_omission_is_unverifiable():
    observations = _fleet()
    peer = observations[2]
    local = tuple(
        (shard_id, state) for shard_id, state in peer.local_shards if shard_id != 0
    )
    observations[2] = _obs(peer.peer_id, 6, local=local, collection=COLLECTION)
    verdict = evaluate_collection_placement(COLLECTION, observations, PlacementPolicy(6, 3, 2))
    assert verdict.state == "unverifiable"
    assert any("omits" in problem for problem in verdict.problems)


def test_missing_collection_everywhere_is_unservable():
    observations = [_obs(peer, None, collection=COLLECTION, missing=True) for peer in PEERS]
    verdict = evaluate_collection_placement(COLLECTION, observations, PlacementPolicy(6, 3, 2))
    assert verdict.state == "unservable"


def test_missing_collection_on_one_peer_is_unverifiable():
    observations = _fleet() + [_obs(999, None, collection=COLLECTION, missing=True)]
    verdict = evaluate_collection_placement(COLLECTION, observations, PlacementPolicy(6, 3, 2))
    assert verdict.state == "unverifiable"
    assert any("absent" in problem for problem in verdict.problems)


def test_unreachable_peer_cannot_certify_healthy():
    observations = _fleet(peers=(101, 202, 303), copies=3, unreachable=(303,))
    verdict = evaluate_collection_placement(COLLECTION, observations, PlacementPolicy(6, 3, 2))
    assert verdict.state == "degraded"
    assert all(len(shard.active_peers) == 2 for shard in verdict.shards)


def test_no_copy_observed_with_reachable_peers_is_unservable():
    observations = _fleet(peers=(101, 202, 303), copies=3)
    trimmed = [
        _obs(
            view.peer_id,
            view.shard_count,
            local=tuple(copy for copy in view.local_shards if copy[0] != 0),
            collection=COLLECTION,
        )
        for view in observations
    ]
    verdict = evaluate_collection_placement(COLLECTION, trimmed, PlacementPolicy(6, 3, 2))
    assert verdict.state == "unservable"
    assert any("shard 0" in problem for problem in verdict.problems)


def test_transfer_in_progress_is_recovering():
    observations = _fleet()
    observations[0] = _obs(
        PEERS[0],
        6,
        local=observations[0].local_shards,
        transfers=(_transfer(0, PEERS[1], PEERS[2]),),
        collection=COLLECTION,
    )
    verdict = evaluate_collection_placement(COLLECTION, observations, PlacementPolicy(6, 3, 2))
    assert verdict.state == "recovering"
    assert any("transfer in progress" in problem for problem in verdict.problems)


def test_initializing_replica_is_recovering():
    observations = _with_local_state(_fleet(), PEERS[2], 0, "INITIALIZING")
    verdict = evaluate_collection_placement(COLLECTION, observations, PlacementPolicy(6, 3, 2))
    assert verdict.state == "recovering"
    assert any("initializing" in problem for problem in verdict.problems)


def test_dead_stale_copy_is_recovering():
    observations = _with_local_state(_fleet(), PEERS[0], 0, "DEAD")
    verdict = evaluate_collection_placement(COLLECTION, observations, PlacementPolicy(6, 3, 2))
    assert verdict.state == "recovering"
    assert any("dead" in problem for problem in verdict.problems)


def test_active_read_does_not_stand_in_for_an_active_copy():
    observations = _with_local_state(_fleet(), PEERS[0], 0, "ACTIVEREAD")
    verdict = evaluate_collection_placement(COLLECTION, observations, PlacementPolicy(6, 3, 2))
    assert verdict.state == "recovering"
    assert any("active-read" in problem for problem in verdict.problems)


def test_unknown_replica_state_is_unverifiable():
    observations = _with_local_state(_fleet(), PEERS[0], 0, "MYSTERY")
    verdict = evaluate_collection_placement(COLLECTION, observations, PlacementPolicy(6, 3, 2))
    assert verdict.state == "unverifiable"
    assert any("unrecognized" in problem for problem in verdict.problems)


def test_unknown_shard_count_is_unverifiable():
    observations = [_obs(peer, None, collection=COLLECTION) for peer in PEERS]
    verdict = evaluate_collection_placement(COLLECTION, observations, PlacementPolicy(6, 3, 2))
    assert verdict.state == "unverifiable"
    assert any("shard count unknown" in problem for problem in verdict.problems)


def test_peers_disagreeing_on_shard_count_is_unverifiable():
    observations = _fleet()
    observations[0] = _obs(PEERS[0], 5, local=observations[0].local_shards, collection=COLLECTION)
    verdict = evaluate_collection_placement(COLLECTION, observations, PlacementPolicy(6, 3, 2))
    assert verdict.state == "unverifiable"
    assert any("disagree" in problem for problem in verdict.problems)


def test_observed_shard_count_below_policy_is_unservable():
    observations = _fleet(shard_count=1)
    verdict = evaluate_collection_placement(COLLECTION, observations, PlacementPolicy(6, 3, 2))
    assert verdict.state == "unservable"


# ----------------------------------------------------------------- cluster


def _enabled_status(peer_ids=(101, 202, 303), thread="working"):
    return SimpleNamespace(
        status="enabled",
        peers={str(peer): SimpleNamespace(uri=f"http://peer{peer}:6335") for peer in peer_ids},
        consensus_thread_status=SimpleNamespace(consensus_thread_status=thread),
    )


def test_cluster_healthy_with_three_reachable_peers():
    verdict = evaluate_cluster(
        expected_peers=3,
        member_peer_ids=member_peer_ids(_enabled_status()),
        peer_reachability=tuple(PeerReachability(f"http://p{peer}", True, peer) for peer in PEERS),
        consensus=consensus_name(_enabled_status()),
        primary_endpoint=PRIMARY,
    )
    assert verdict.state == "healthy"


def test_cluster_degraded_when_expected_peer_unreachable():
    reachability = (
        PeerReachability("http://p1", True, 101),
        PeerReachability("http://p2", True, 202),
        PeerReachability("http://p3", False, None, "ConnectError"),
    )
    verdict = evaluate_cluster(
        expected_peers=3,
        member_peer_ids=member_peer_ids(_enabled_status()),
        peer_reachability=reachability,
        consensus=consensus_name(_enabled_status()),
        primary_endpoint=PRIMARY,
    )
    assert verdict.state == "degraded"
    assert any("unreachable" in problem for problem in verdict.problems)


def test_duplicate_peer_endpoints_are_not_independent_peers():
    verdict = evaluate_cluster(
        expected_peers=3,
        member_peer_ids=member_peer_ids(_enabled_status()),
        peer_reachability=(
            PeerReachability("http://p1", True, 101),
            PeerReachability("http://p1-alias", True, 101),
            PeerReachability("http://p3", True, 303),
        ),
        consensus=consensus_name(_enabled_status()),
        primary_endpoint=PRIMARY,
    )
    assert verdict.state == "degraded"
    assert any("distinct peer" in problem for problem in verdict.problems)


def test_cluster_membership_below_expectation_is_degraded():
    verdict = evaluate_cluster(
        expected_peers=3,
        member_peer_ids=(101, 202),
        peer_reachability=(
            PeerReachability("http://p1", True, 101),
            PeerReachability("http://p2", True, 202),
        ),
        consensus="working",
        primary_endpoint=PRIMARY,
    )
    assert verdict.state == "degraded"
    assert any("member peer" in problem for problem in verdict.problems)


def test_standalone_server_is_the_single_node_profile():
    disabled = SimpleNamespace(status="disabled")
    verdict = evaluate_cluster(
        expected_peers=1,
        member_peer_ids=member_peer_ids(disabled),
        peer_reachability=(PeerReachability(PRIMARY, True, 1),),
        consensus=consensus_name(disabled),
        primary_endpoint=PRIMARY,
    )
    assert verdict.state == "healthy"
    assert member_peer_ids(disabled) == ()


def test_standalone_server_is_not_three_peer_production():
    disabled = SimpleNamespace(status="disabled")
    verdict = evaluate_cluster(
        expected_peers=3,
        member_peer_ids=(),
        peer_reachability=(PeerReachability(PRIMARY, True, 1),),
        consensus=consensus_name(disabled),
        primary_endpoint=PRIMARY,
    )
    assert verdict.state == "degraded"
    assert any("distributed mode" in problem for problem in verdict.problems)


def test_stopped_consensus_is_not_healthy():
    status = _enabled_status(thread="stopped")
    verdict = evaluate_cluster(
        expected_peers=3,
        member_peer_ids=member_peer_ids(status),
        peer_reachability=tuple(
            PeerReachability(f"http://p{peer}", True, peer) for peer in PEERS
        ),
        consensus=consensus_name(status),
        primary_endpoint=PRIMARY,
    )
    assert verdict.state == "degraded"


# ------------------------------------------------------------------ report


def _report(*, cluster_state="healthy", collection_state="healthy", single_node=False,
            configured=(), aliases=()):
    return VerificationReport(
        policy=PlacementPolicy(6, 3, 2),
        production=not single_node,
        single_node=single_node,
        cluster=SimpleNamespace(
            state=cluster_state,
            expected_peers=3,
            member_peers=3,
            reachable_peer_urls=3,
            consensus="working",
            problems=(),
        ),
        collections=(
            SimpleNamespace(collection=COLLECTION, state=collection_state, shards=(), problems=()),
        ),
        configured_problems=configured,
        alias_conflicts=aliases,
    )


def test_report_healthy_verdict():
    lines = evidence_lines(_report())
    assert lines[-1].startswith("VERDICT: healthy")


def test_report_degraded_refused_by_default():
    lines = evidence_lines(_report(collection_state="degraded"))
    assert lines[-1] == "VERDICT: degraded (refused)"


def test_report_degraded_allowed_for_operations():
    lines = evidence_lines(_report(collection_state="degraded"), allow_degraded=True)
    assert "not production qualification" in lines[-1]


def test_report_non_ha_verdict():
    lines = evidence_lines(_report(single_node=True))
    assert lines[-1].startswith("VERDICT: non-ha")


def test_report_unknown_configuration_is_unverifiable():
    report = _report(configured=("c: configured RF unknown",))
    assert report.state == "unverifiable"


# --------------------------------------------------------------- inventory


class _AliasClient:
    def __init__(self, mapping):
        self.mapping = mapping

    def get_aliases(self):
        return SimpleNamespace(
            aliases=[
                SimpleNamespace(alias_name=alias, collection_name=collection)
                for alias, collection in self.mapping.items()
            ]
        )


def test_inventory_resolves_alias_to_physical_plus_control():
    client = _AliasClient({"mainframe_manuals": COLLECTION})
    assert resolve_verification_inventory(client, _settings()) == [COLLECTION, CONTROL]


def test_inventory_without_alias_uses_configured_name():
    client = _AliasClient({})
    assert resolve_verification_inventory(client, _settings()) == [
        "mainframe_manuals",
        "mainframe_manuals__completions",
    ]


def test_alias_mapping_reads_physical_target():
    client = _AliasClient({"mainframe_manuals": COLLECTION})
    assert resolve_alias_mapping(client, "mainframe_manuals") == COLLECTION
    assert resolve_alias_mapping(client, "other") is None


# ------------------------------------------------------------- normalization


def test_observe_collection_normalizes_payload():
    info = _info(101, 2, local=[_local(0)], remote=[_remote(1, 202)], transfers=[_transfer(0, 101, 202)])
    view = observe_collection(COLLECTION, PRIMARY, lambda _name: info)
    assert view.reachable and view.peer_id == 101 and view.shard_count == 2
    assert view.local_shards == ((0, "ACTIVE"),)
    assert view.remote_shards == ((1, 202, "ACTIVE"),)
    assert view.transfers == ((0, 101, 202),)


def test_observe_collection_reports_fetch_failure():
    def boom(_name):
        raise RuntimeError("connection refused")

    view = observe_collection(COLLECTION, PRIMARY, boom)
    assert not view.reachable
    assert view.error is not None and "RuntimeError" in view.error


# ------------------------------------------------------------ script claims


class _FakeClient:
    def __init__(self, peer_id, collections, params, status):
        self.peer_id = peer_id
        self.collections = collections
        self.params = params
        self.status = status

    def get_aliases(self):
        return SimpleNamespace(
            aliases=[SimpleNamespace(alias_name="mainframe_manuals", collection_name=COLLECTION)]
        )

    def get_collection(self, _name):
        return SimpleNamespace(config=SimpleNamespace(params=self.params))

    def collection_exists(self, name):
        return name in self.collections

    def collection_cluster_info(self, name):
        return self.collections[name]

    def cluster_status(self):
        return self.status

    def close(self):
        return None


def _args(**overrides):
    base = {
        "production": False,
        "expect_single_node": False,
        "peer_url": [],
        "expect_peers": None,
        "allow_degraded": False,
        "timeout": 1.0,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _fake_fleet(params=None):
    """Three peers holding 6/3/2 collections behind a primary endpoint."""
    status = _enabled_status()
    params = params or _params()
    collections = {
        peer: {
            COLLECTION: _info(peer, 6, local=[_local(shard) for shard in range(6)]),
            CONTROL: _info(peer, 6, local=[_local(shard) for shard in range(6)]),
        }
        for peer in PEERS
    }
    clients = {
        PRIMARY: _FakeClient(101, collections[101], params, status),
        "http://qdrant-1:6333": _FakeClient(202, collections[202], params, status),
        "http://qdrant-2:6333": _FakeClient(303, collections[303], params, status),
    }
    return clients


def _connect_factory(clients):
    def connect(url, _settings, _timeout):
        return clients[url]

    return connect


def _production_settings(**overrides):
    base = {
        "qdrant_url": PRIMARY,
        "qdrant_collection": "mainframe_manuals",
        "dense_dim": 8,
        "qdrant_shard_number": 6,
        "qdrant_replication_factor": 3,
        "qdrant_write_consistency_factor": 2,
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


def test_script_production_healthy_exit_zero(capsys):
    clients = _fake_fleet()
    report, code = perform_verification(
        _production_settings(),
        _args(production=True, peer_url=["http://qdrant-1:6333", "http://qdrant-2:6333"]),
        connect=_connect_factory(clients),
    )
    output = capsys.readouterr().out
    assert code == 0, output
    assert report is not None and report.state == "healthy"
    assert "VERDICT: healthy" in output


def test_script_production_requires_peer_urls(capsys):
    clients = _fake_fleet()
    report, code = perform_verification(
        _production_settings(), _args(production=True), connect=_connect_factory(clients)
    )
    assert code == 1
    assert report is not None and report.state != "healthy"
    assert "VERDICT:" in capsys.readouterr().out


def test_script_no_claim_refused():
    report, code = perform_verification(
        _settings(), _args(), connect=_connect_factory(_fake_fleet())
    )
    assert report is None and code == 1


def test_script_contradictory_claim_usage_error():
    _report_value, code = perform_verification(
        _settings(), _args(production=True, expect_single_node=True)
    )
    assert code == 2


def test_script_production_mismatch_refused(capsys):
    clients = _fake_fleet(params=_params(1, 1, 1))
    _report_value, code = perform_verification(
        _production_settings(), _args(production=True), connect=_connect_factory(clients)
    )
    assert code == 1


def test_script_single_node_profile(capsys):
    status = SimpleNamespace(status="disabled")
    client = _FakeClient(
        1,
        {COLLECTION: _info(1, 1, local=[_local(0)]), CONTROL: _info(1, 1, local=[_local(0)])},
        _params(1, 1, 1),
        status,
    )
    _report_value, code = perform_verification(
        _settings(qdrant_shard_number=1, qdrant_replication_factor=1,
                  qdrant_write_consistency_factor=1),
        _args(expect_single_node=True),
        connect=lambda url, _settings, _timeout: client,
    )
    assert code == 0
    assert "VERDICT: non-ha" in capsys.readouterr().out
