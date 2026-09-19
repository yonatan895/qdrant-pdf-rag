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
    AliasBinding,
    CollectionObservation,
    PeerClusterView,
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
    resolve_alias_binding,
    selected_policy,
)

COLLECTION = "mainframe_manuals_g3"
CONTROL = "mainframe_manuals_g3__completions"
SERVICE = "http://qdrant:6333"
PEER_URLS = (
    "http://qdrant-0.qdrant-headless:6333",
    "http://qdrant-1.qdrant-headless:6333",
    "http://qdrant-2.qdrant-headless:6333",
)
PEERS = (101, 202, 303)


def _judge(observations, *, collection=COLLECTION, expected=None, accepted=PEERS):
    expected = expected or PlacementPolicy(6, 3, 2)
    return evaluate_collection_placement(
        collection, observations, expected, accepted_peers=accepted
    )


def _view(
    peer_id,
    *,
    endpoint=None,
    reachable=True,
    members=PEERS,
    consensus="working",
    error=None,
):
    return PeerClusterView(
        endpoint=endpoint or f"peer-{peer_id}",
        reachable=reachable,
        peer_id=peer_id,
        member_peer_ids=tuple(members),
        consensus=consensus,
        error=error,
    )


def _enabled_status(peer_ids=(101, 202, 303), thread="working"):
    return SimpleNamespace(
        status="enabled",
        peer_id=None,
        peers={
            str(peer): SimpleNamespace(uri=f"http://peer{peer}:6335")
            for peer in peer_ids
        },
        consensus_thread_status=SimpleNamespace(consensus_thread_status=thread),
    )


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
        "qdrant_url": SERVICE,
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
    verdict = _judge(_fleet())
    assert verdict.state == "healthy"
    assert len(verdict.shards) == 6
    assert all(shard.active_peers == PEERS for shard in verdict.shards)


def test_three_ready_peers_one_copy_refused():
    """The packet counterexample: peers present, but one copy per shard must
    refuse an HA claim against RF=3."""
    observations = _fleet(copies=1)
    verdict = _judge(observations)
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
    verdict = _judge([only])
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
    verdict = _judge(observations)
    assert verdict.state == "unverifiable"
    assert any("disagree" in problem for problem in verdict.problems)


def test_remote_report_omission_is_unverifiable():
    observations = _fleet()
    peer = observations[2]
    local = tuple(
        (shard_id, state) for shard_id, state in peer.local_shards if shard_id != 0
    )
    observations[2] = _obs(peer.peer_id, 6, local=local, collection=COLLECTION)
    verdict = _judge(observations)
    assert verdict.state == "unverifiable"
    assert any("omits" in problem for problem in verdict.problems)


def test_missing_collection_everywhere_is_unservable():
    observations = [_obs(peer, None, collection=COLLECTION, missing=True) for peer in PEERS]
    verdict = _judge(observations)
    assert verdict.state == "unservable"


def test_missing_collection_on_one_peer_is_unverifiable():
    observations = _fleet() + [_obs(999, None, collection=COLLECTION, missing=True)]
    verdict = _judge(observations)
    assert verdict.state == "unverifiable"
    assert any("absent" in problem for problem in verdict.problems)


def test_unreachable_peer_cannot_certify_healthy():
    observations = _fleet(peers=(101, 202, 303), copies=3, unreachable=(303,))
    verdict = _judge(observations)
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
    verdict = _judge(trimmed)
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
    verdict = _judge(observations)
    assert verdict.state == "recovering"
    assert any("transfer in progress" in problem for problem in verdict.problems)


def test_initializing_replica_is_recovering():
    observations = _with_local_state(_fleet(), PEERS[2], 0, "INITIALIZING")
    verdict = _judge(observations)
    assert verdict.state == "recovering"
    assert any("initializing" in problem for problem in verdict.problems)


def test_dead_stale_copy_is_recovering():
    observations = _with_local_state(_fleet(), PEERS[0], 0, "DEAD")
    verdict = _judge(observations)
    assert verdict.state == "recovering"
    assert any("dead" in problem for problem in verdict.problems)


def test_active_read_does_not_stand_in_for_an_active_copy():
    observations = _with_local_state(_fleet(), PEERS[0], 0, "ACTIVEREAD")
    verdict = _judge(observations)
    assert verdict.state == "recovering"
    assert any("active-read" in problem for problem in verdict.problems)


def test_unknown_replica_state_is_unverifiable():
    observations = _with_local_state(_fleet(), PEERS[0], 0, "MYSTERY")
    verdict = _judge(observations)
    assert verdict.state == "unverifiable"
    assert any("unrecognized" in problem for problem in verdict.problems)


def test_unknown_shard_count_is_unverifiable():
    observations = [_obs(peer, None, collection=COLLECTION) for peer in PEERS]
    verdict = _judge(observations)
    assert verdict.state == "unverifiable"
    assert any("shard count unknown" in problem for problem in verdict.problems)


def test_peers_disagreeing_on_shard_count_is_unverifiable():
    observations = _fleet()
    observations[0] = _obs(PEERS[0], 5, local=observations[0].local_shards, collection=COLLECTION)
    verdict = _judge(observations)
    assert verdict.state == "unverifiable"
    assert any("disagree" in problem for problem in verdict.problems)


def test_observed_shard_count_below_policy_is_unservable():
    observations = _fleet(shard_count=1)
    verdict = _judge(observations)
    assert verdict.state == "unservable"


def test_peer_view_outside_accepted_membership_is_unverifiable():
    """F2: a reachable endpoint whose own peer id is outside the accepted
    member set must never contribute replicas."""
    observations = _fleet()
    observations[2] = _obs(888, 6, local=observations[2].local_shards, collection=COLLECTION)
    verdict = _judge(observations)
    assert verdict.state == "unverifiable"
    assert any("outside the accepted cluster membership" in problem for problem in verdict.problems)


def test_remote_reference_outside_membership_is_unverifiable():
    observations = _fleet()
    observations[0] = _obs(
        PEERS[0],
        6,
        local=observations[0].local_shards,
        remote=observations[0].remote_shards + ((0, 999, "ACTIVE"),),
        collection=COLLECTION,
    )
    verdict = _judge(observations)
    assert verdict.state == "unverifiable"
    assert any("foreign topology" in problem for problem in verdict.problems)


def test_unexpected_shard_id_is_unverifiable():
    """F2: an unsupported layout must not be silently ignored by iterating
    only range(shard_count)."""
    observations = _fleet()
    peer = observations[0]
    observations[0] = _obs(
        peer.peer_id,
        peer.shard_count,
        local=peer.local_shards + ((6, "ACTIVE"),),
        remote=peer.remote_shards,
        collection=COLLECTION,
    )
    verdict = _judge(observations)
    assert verdict.state == "unverifiable"
    assert any("outside the declared shard set" in problem for problem in verdict.problems)


def test_empty_accepted_membership_refuses_placement():
    verdict = _judge(_fleet(), accepted=())
    assert verdict.state == "unverifiable"


# ----------------------------------------------------------------- cluster


def test_cluster_healthy_with_three_reachable_peers():
    verdict = evaluate_cluster(
        expected_peers=3, views=tuple(_view(peer) for peer in PEERS)
    )
    assert verdict.state == "healthy"
    assert verdict.member_ids == PEERS


def test_single_member_loss_is_positively_degraded():
    views = (
        _view(101),
        _view(202),
        _view(
            None,
            endpoint=PEER_URLS[2],
            reachable=False,
            members=(),
            consensus="",
            error="ConnectError",
        ),
    )
    verdict = evaluate_cluster(expected_peers=3, views=views)
    assert verdict.state == "degraded"
    assert any("unreachable" in problem for problem in verdict.problems)


def test_two_member_losses_are_unverifiable():
    views = (
        _view(101),
        _view(None, endpoint=PEER_URLS[1], reachable=False, members=(), consensus="", error="x"),
        _view(None, endpoint=PEER_URLS[2], reachable=False, members=(), consensus="", error="x"),
    )
    verdict = evaluate_cluster(expected_peers=3, views=views)
    assert verdict.state == "unverifiable"
    assert any("insufficient surviving topology" in problem for problem in verdict.problems)


def test_duplicate_peer_ids_are_not_independent_peers():
    views = (
        _view(101),
        _view(101, endpoint="http://p1-alias"),
        _view(303),
    )
    verdict = evaluate_cluster(expected_peers=3, views=views)
    assert verdict.state == "unverifiable"
    assert any("distinct peer" in problem for problem in verdict.problems)


def test_membership_below_expectation_is_unverifiable():
    views = (
        _view(101, members=(101,)),
        _view(202, members=(101,)),
        _view(303, members=(101,)),
    )
    verdict = evaluate_cluster(expected_peers=3, views=views)
    assert verdict.state == "unverifiable"
    assert any("member peer" in problem for problem in verdict.problems)


def test_observed_identity_outside_membership_is_unverifiable():
    """F2: observed endpoint identities must belong to the cluster's own
    reported member set."""
    views = (_view(101), _view(202), _view(888, members=(101, 202, 303)))
    verdict = evaluate_cluster(expected_peers=3, views=views)
    assert verdict.state == "unverifiable"
    assert any("not in the cluster's own membership" in problem for problem in verdict.problems)


def test_membership_disagreement_is_unverifiable():
    views = (
        _view(101),
        _view(202),
        _view(303, members=(101, 202, 999)),
    )
    verdict = evaluate_cluster(expected_peers=3, views=views)
    assert verdict.state == "unverifiable"
    assert any("disagree on cluster membership" in problem for problem in verdict.problems)


def test_missing_membership_on_reachable_peer_is_unverifiable():
    views = (_view(101), _view(202), _view(303, members=(), consensus=""))
    verdict = evaluate_cluster(expected_peers=3, views=views)
    assert verdict.state == "unverifiable"
    assert any("membership" in problem for problem in verdict.problems)


def test_stopped_consensus_is_unverifiable():
    views = (_view(101), _view(202), _view(303, consensus="stopped"))
    verdict = evaluate_cluster(expected_peers=3, views=views)
    assert verdict.state == "unverifiable"
    assert any("consensus" in problem for problem in verdict.problems)


def test_fewer_endpoints_than_expected_is_unverifiable():
    verdict = evaluate_cluster(expected_peers=3, views=(_view(101), _view(202)))
    assert verdict.state == "unverifiable"
    assert any("authoritative peer endpoint" in problem for problem in verdict.problems)


def test_standalone_server_is_the_single_node_profile():
    disabled = SimpleNamespace(status="disabled", peer_id=1)
    verdict = evaluate_cluster(
        expected_peers=1,
        views=(
            _view(
                1,
                endpoint=SERVICE,
                members=(),
                consensus=consensus_name(disabled),
            ),
        ),
    )
    assert verdict.state == "healthy"
    assert member_peer_ids(disabled) == ()


def test_standalone_server_is_not_three_peer_production():
    disabled = SimpleNamespace(status="disabled", peer_id=1)
    views = tuple(
        _view(
            peer,
            endpoint=url,
            members=(),
            consensus=consensus_name(disabled),
        )
        for peer, url in zip(PEERS, PEER_URLS, strict=True)
    )
    verdict = evaluate_cluster(expected_peers=3, views=views)
    assert verdict.state == "unverifiable"
    assert any("no cluster membership" in problem for problem in verdict.problems)


# ------------------------------------------------------------------ report


def _report(*, cluster_state="healthy", collection_state="healthy", single_node=False,
            configured=(), aliases=(), alias=None):
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
            member_ids=PEERS,
            observed_peer_ids=PEERS,
        ),
        collections=(
            SimpleNamespace(collection=COLLECTION, state=collection_state, shards=(), problems=()),
        ),
        configured_problems=configured,
        alias_conflicts=aliases,
        alias=alias,
    )


def test_report_healthy_verdict():
    lines = evidence_lines(_report())
    assert lines[-1].startswith("VERDICT: healthy")
    assert "placement evidence only" in lines[-1]


def test_report_degraded_refused_by_default():
    lines = evidence_lines(_report(collection_state="degraded"))
    assert lines[-1] == "VERDICT: degraded (refused)"


def test_report_degraded_allowed_for_operations():
    lines = evidence_lines(_report(collection_state="degraded"), allow_degraded=True)
    assert "never production qualification" in lines[-1]


def test_report_unverifiable_is_never_allowed_degraded():
    lines = evidence_lines(
        _report(cluster_state="unverifiable", collection_state="degraded"),
        allow_degraded=True,
    )
    assert lines[-1].startswith("VERDICT: unverifiable")


def test_report_non_ha_verdict():
    lines = evidence_lines(_report(single_node=True))
    assert lines[-1].startswith("VERDICT: non-ha")


def test_report_unknown_configuration_is_unverifiable():
    report = _report(configured=("c: configured RF unknown",))
    assert report.state == "unverifiable"


def test_report_alias_binding_is_evidence():
    binding = AliasBinding(alias="mainframe_manuals", target=COLLECTION, inventory=(COLLECTION, CONTROL))
    lines = evidence_lines(_report(alias=binding))
    assert any("binding" in line and COLLECTION in line for line in lines)


def test_report_physical_pair_is_not_alias_certification():
    binding = AliasBinding(alias="mainframe_manuals", target=None, inventory=("mainframe_manuals", "mainframe_manuals__completions"))
    lines = evidence_lines(_report(alias=binding))
    assert any("not a current-alias certification" in line for line in lines)


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


def test_binding_resolves_alias_to_physical_plus_control():
    client = _AliasClient({"mainframe_manuals": COLLECTION})
    binding = resolve_alias_binding(client, "mainframe_manuals")
    assert binding.target == COLLECTION
    assert binding.inventory == (COLLECTION, CONTROL)


def test_binding_without_alias_names_the_physical_pair():
    client = _AliasClient({})
    binding = resolve_alias_binding(client, "mainframe_manuals")
    assert binding.target is None
    assert binding.inventory == ("mainframe_manuals", "mainframe_manuals__completions")


# ------------------------------------------------------------- normalization


def test_observe_collection_normalizes_payload():
    info = _info(101, 2, local=[_local(0)], remote=[_remote(1, 202)], transfers=[_transfer(0, 101, 202)])
    view = observe_collection(COLLECTION, SERVICE, lambda _name: info)
    assert view.reachable and view.peer_id == 101 and view.shard_count == 2
    assert view.local_shards == ((0, "ACTIVE"),)
    assert view.remote_shards == ((1, 202, "ACTIVE"),)
    assert view.transfers == ((0, 101, 202),)


def test_observe_collection_reports_fetch_failure():
    def boom(_name):
        raise RuntimeError("connection refused")

    view = observe_collection(COLLECTION, SERVICE, boom)
    assert not view.reachable
    assert view.error is not None and "RuntimeError" in view.error


# ------------------------------------------------------------ script claims


class _FakeClient:
    """One endpoint face: identity, cluster view, collections, policy."""

    def __init__(
        self,
        *,
        peer_id,
        collections,
        params,
        status,
        aliases=(),
        status_error=None,
    ):
        self.peer_id = peer_id
        self.collections = collections
        self.params = params
        self.status = status
        self.aliases = dict(aliases)
        self.status_error = status_error
        self.closed = False

    def get_aliases(self):
        return SimpleNamespace(
            aliases=[
                SimpleNamespace(alias_name=alias, collection_name=collection)
                for alias, collection in self.aliases.items()
            ]
        )

    def get_collection(self, name):
        if name not in self.collections:
            raise RuntimeError(f"collection {name} not found")
        return SimpleNamespace(config=SimpleNamespace(params=self.params))

    def collection_exists(self, name):
        return name in self.collections

    def collection_cluster_info(self, name):
        return self.collections[name]

    def cluster_status(self):
        if self.status_error is not None:
            raise ConnectionError(self.status_error)
        return self.status

    def close(self):
        self.closed = True


class _UnreachableClient:
    """Every read fails: the positively established lost-peer candidate."""

    def __init__(self):
        self.closed = False

    def __getattr__(self, _name):
        def refuse(*_args, **_kwargs):
            raise ConnectionError("connection refused")

        return refuse

    def close(self):
        self.closed = True


def _client(
    peer_id,
    *,
    params=None,
    status=None,
    collections=None,
    alias_name="mainframe_manuals",
    alias_target=COLLECTION,
    status_error=None,
):
    if collections is None:
        collections = {
            COLLECTION: _info(peer_id, 6, local=[_local(shard) for shard in range(6)]),
            CONTROL: _info(peer_id, 6, local=[_local(shard) for shard in range(6)]),
        }
    aliases = {alias_name: alias_target} if alias_target else {}
    return _FakeClient(
        peer_id=peer_id,
        collections=collections,
        params=params or _params(),
        status=status or _enabled_status(),
        aliases=aliases,
        status_error=status_error,
    )


def _fleet_clients(*, params=None, status=None, unreachable=()):
    """Entry Service face plus three direct peer faces (RF3 placement)."""
    clients: dict[str, _FakeClient | _UnreachableClient] = {}
    for index, peer in enumerate(PEERS):
        url = PEER_URLS[index]
        if peer in unreachable:
            clients[url] = _UnreachableClient()
        else:
            clients[url] = _client(peer, params=params, status=status)
    clients[SERVICE] = _client(PEERS[0], params=params, status=status)
    return clients


def _connect_factory(clients):
    def connect(url, _settings, _timeout):
        return clients[url]

    return connect


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


def _production_settings(**overrides):
    base = {
        "qdrant_url": SERVICE,
        "qdrant_collection": "mainframe_manuals",
        "dense_dim": 8,
        "qdrant_shard_number": 6,
        "qdrant_replication_factor": 3,
        "qdrant_write_consistency_factor": 2,
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


def _verify(clients, settings, args):
    return perform_verification(settings, args, connect=_connect_factory(clients))


def test_documented_service_plus_direct_peers_is_healthy(capsys):
    """F1: QDRANT_URL is a load-balanced Service; only --peer-url endpoints
    are authoritative peers."""
    clients = _fleet_clients()
    report, code = _verify(
        clients,
        _production_settings(),
        _args(production=True, peer_url=list(PEER_URLS)),
    )
    output = capsys.readouterr().out
    assert code == 0, output
    assert report is not None and report.state == "healthy"
    assert report.alias is not None and report.alias.target == COLLECTION
    assert all(client.closed for client in clients.values())


def test_service_endpoint_identity_is_never_a_peer(capsys):
    """F1: a Service face with a foreign/unstable identity must not become a
    fourth replica or change the peer identity set."""
    clients = _fleet_clients()
    clients[SERVICE].peer_id = 999
    _report_value, code = _verify(
        clients,
        _production_settings(),
        _args(production=True, peer_url=list(PEER_URLS)),
    )
    assert code == 0, capsys.readouterr().out


def test_three_urls_reaching_one_peer_cannot_satisfy_rf3(capsys):
    clients = _fleet_clients()
    same_peer = clients[PEER_URLS[0]]
    for url in PEER_URLS:
        clients[url] = same_peer
    report, code = _verify(
        clients,
        _production_settings(),
        _args(production=True, peer_url=list(PEER_URLS)),
    )
    output = capsys.readouterr().out
    assert code == 1, output
    assert report is not None and report.state == "unverifiable"
    assert "distinct peer" in output


def test_failed_member_cannot_be_replaced_by_a_foreign_endpoint(capsys):
    """F2: the lost member cannot be substituted by an endpoint outside the
    cluster's own membership."""
    clients = _fleet_clients(unreachable=(303,))
    clients[PEER_URLS[2]] = _client(888)
    report, code = _verify(
        clients,
        _production_settings(),
        _args(production=True, peer_url=list(PEER_URLS)),
    )
    output = capsys.readouterr().out
    assert code == 1, output
    assert report is not None and report.state == "unverifiable"
    assert "not in the cluster's own membership" in output


def test_entry_membership_mismatch_is_refused(capsys):
    """F2: a Service routed to a different cluster (even with same-named
    collections) must not certify the direct peers' placement."""
    clients = _fleet_clients()
    clients[SERVICE].status = _enabled_status(peer_ids=(101, 202, 999))
    report, code = _verify(
        clients,
        _production_settings(),
        _args(production=True, peer_url=list(PEER_URLS)),
    )
    output = capsys.readouterr().out
    assert code == 1, output
    assert report is not None and report.state == "unverifiable"
    assert "different clusters" in output


def test_entry_membership_unreadable_is_refused_even_degraded(capsys):
    clients = _fleet_clients()
    clients[SERVICE].status_error = "cluster API down"
    report, code = _verify(
        clients,
        _production_settings(),
        _args(peer_url=list(PEER_URLS), allow_degraded=True),
    )
    output = capsys.readouterr().out
    assert code == 1, output
    assert report is not None and report.state == "unverifiable"
    assert "entry endpoint" in output


def test_one_peer_loss_is_degraded_only_with_the_operational_flag(capsys):
    clients = _fleet_clients(unreachable=(303,))
    report, code = _verify(
        clients,
        _production_settings(),
        _args(production=True, peer_url=list(PEER_URLS)),
    )
    output = capsys.readouterr().out
    assert code == 1, output
    assert report is not None and report.state == "degraded"

    with_flag = _fleet_clients(unreachable=(303,))
    report, code = _verify(
        with_flag,
        _production_settings(),
        _args(peer_url=list(PEER_URLS), allow_degraded=True),
    )
    output = capsys.readouterr().out
    assert code == 0, output
    assert report is not None and report.state == "degraded"


def test_allow_degraded_does_not_accept_missing_membership(capsys):
    clients = _fleet_clients()
    clients[PEER_URLS[2]].status_error = "cluster API down"
    report, code = _verify(
        clients,
        _production_settings(),
        _args(peer_url=list(PEER_URLS), allow_degraded=True),
    )
    output = capsys.readouterr().out
    assert code == 1, output
    assert report is not None and report.state == "unverifiable"


def test_allow_degraded_does_not_accept_stopped_consensus(capsys):
    clients = _fleet_clients(status=_enabled_status(thread="stopped"))
    report, code = _verify(
        clients,
        _production_settings(),
        _args(peer_url=list(PEER_URLS), allow_degraded=True),
    )
    output = capsys.readouterr().out
    assert code == 1, output
    assert report is not None and report.state == "unverifiable"


def test_allow_degraded_does_not_accept_missing_control_collection(capsys):
    clients = _fleet_clients()
    del clients[PEER_URLS[1]].collections[CONTROL]
    report, code = _verify(
        clients,
        _production_settings(),
        _args(peer_url=list(PEER_URLS), allow_degraded=True),
    )
    output = capsys.readouterr().out
    assert code == 1, output
    assert report is not None and report.state == "unverifiable"
    assert "required collection absent" in output


def test_production_with_allow_degraded_is_usage_error():
    _report_value, code = _verify(
        _fleet_clients(),
        _production_settings(),
        _args(production=True, peer_url=list(PEER_URLS), allow_degraded=True),
    )
    assert code == 2


def test_expect_peers_cannot_redefine_production():
    _report_value, code = _verify(
        _fleet_clients(),
        _production_settings(),
        _args(production=True, peer_url=list(PEER_URLS), expect_peers=5),
    )
    assert code == 2


def test_expect_peers_below_replication_is_usage_error():
    _report_value, code = _verify(
        _fleet_clients(),
        _production_settings(),
        _args(peer_url=list(PEER_URLS[:2]), expect_peers=2),
    )
    assert code == 2


def test_production_without_peer_urls_refused(capsys):
    report, code = _verify(
        _fleet_clients(), _production_settings(), _args(production=True)
    )
    assert code == 1
    assert report is None
    assert "no authoritative peer endpoints" in capsys.readouterr().out


def test_peer_policy_drift_refused(capsys):
    clients = _fleet_clients()
    clients[PEER_URLS[1]].params = _params(1, 1, 1)
    _report_value, code = _verify(
        clients,
        _production_settings(),
        _args(production=True, peer_url=list(PEER_URLS)),
    )
    output = capsys.readouterr().out
    assert code == 1
    assert "replication_factor=1" in output


def test_peer_policy_unreadable_refused(capsys):
    clients = _fleet_clients()

    def boom(_name):
        raise RuntimeError("config read failed")

    clients[PEER_URLS[2]].get_collection = boom
    _report_value, code = _verify(
        clients,
        _production_settings(),
        _args(production=True, peer_url=list(PEER_URLS)),
    )
    output = capsys.readouterr().out
    assert code == 1
    assert "configured policy unreadable" in output


def test_peer_identity_contradiction_across_collections_refused(capsys):
    clients = _fleet_clients()
    clients[PEER_URLS[1]].collections[CONTROL] = _info(
        999, 6, local=[_local(shard) for shard in range(6)]
    )
    report, code = _verify(
        clients,
        _production_settings(),
        _args(production=True, peer_url=list(PEER_URLS)),
    )
    output = capsys.readouterr().out
    assert code == 1
    assert report is not None and report.state == "unverifiable"
    assert "contradictory peer id" in output


def test_unexpected_shard_layout_refused(capsys):
    clients = _fleet_clients()
    clients[PEER_URLS[2]].collections[COLLECTION] = _info(
        303, 6, local=[_local(shard) for shard in range(7)]
    )
    report, code = _verify(
        clients,
        _production_settings(),
        _args(production=True, peer_url=list(PEER_URLS)),
    )
    output = capsys.readouterr().out
    assert code == 1
    assert report is not None and report.state == "unverifiable"
    assert "outside the declared shard set" in output


def test_script_no_claim_refused():
    report, code = _verify(_fleet_clients(), _settings(), _args())
    assert report is None and code == 1


def test_script_contradictory_claim_usage_error():
    _report_value, code = _verify(
        _fleet_clients(), _settings(), _args(production=True, expect_single_node=True)
    )
    assert code == 2


def test_script_production_mismatch_refused(capsys):
    _report_value, code = _verify(
        _fleet_clients(params=_params(1, 1, 1)),
        _production_settings(),
        _args(production=True, peer_url=list(PEER_URLS)),
    )
    assert code == 1


def test_script_single_node_profile(capsys):
    disabled = SimpleNamespace(status="disabled", peer_id=None)
    client = _client(
        1,
        params=_params(1, 1, 1),
        status=disabled,
        collections={
            "mainframe_manuals": _info(1, 1, local=[_local(0)]),
            "mainframe_manuals__completions": _info(1, 1, local=[_local(0)]),
        },
        alias_target=None,
    )
    _report_value, code = perform_verification(
        _settings(
            qdrant_shard_number=1,
            qdrant_replication_factor=1,
            qdrant_write_consistency_factor=1,
        ),
        _args(expect_single_node=True),
        connect=lambda url, _settings, _timeout: client,
    )
    output = capsys.readouterr().out
    assert code == 0, output
    assert "VERDICT: non-ha" in output


# ------------------------------------------------------------------ alias


class _ScriptedAliasClient(_FakeClient):
    """Alias target sequence per read (last value repeats)."""

    def __init__(self, *args, targets, **kwargs):
        super().__init__(*args, **kwargs)
        self.targets = list(targets)
        self.alias_reads = 0

    def get_aliases(self):
        target = self.targets[min(self.alias_reads, len(self.targets) - 1)]
        self.alias_reads += 1
        return SimpleNamespace(
            aliases=[] if target is None else [SimpleNamespace(
                alias_name="mainframe_manuals", collection_name=target
            )]
        )


def _switching_clients(
    targets: dict[str, list[str]],
    *,
    second: str,
    second_degraded: bool = False,
) -> dict[str, _FakeClient | _UnreachableClient]:
    clients: dict[str, _FakeClient | _UnreachableClient] = {}
    for index, peer in enumerate(PEERS):
        full = [_local(shard) for shard in range(6)]
        second_local = [] if (second_degraded and peer == PEERS[2]) else full
        collections = {
            COLLECTION: _info(peer, 6, local=full),
            CONTROL: _info(peer, 6, local=full),
            second: _info(peer, 6, local=second_local),
            f"{second}__completions": _info(peer, 6, local=second_local),
        }
        clients[PEER_URLS[index]] = _ScriptedAliasClient(
            peer_id=peer,
            collections=collections,
            params=_params(),
            status=_enabled_status(),
            targets=targets[PEER_URLS[index]],
        )
    entry_targets = targets.get(SERVICE, targets[PEER_URLS[0]])
    clients[SERVICE] = _ScriptedAliasClient(
        peer_id=PEERS[0],
        collections={
            COLLECTION: _info(PEERS[0], 6, local=[_local(s) for s in range(6)]),
            CONTROL: _info(PEERS[0], 6, local=[_local(s) for s in range(6)]),
            second: _info(PEERS[0], 6, local=[_local(s) for s in range(6)]),
            f"{second}__completions": _info(PEERS[0], 6, local=[_local(s) for s in range(6)]),
        },
        params=_params(),
        status=_enabled_status(),
        targets=entry_targets,
    )
    return clients


def test_alias_change_is_retried_against_the_new_generation(capsys):
    """F4: A healthy but stale generation must not be certified when the
    alias moved to B during observation."""
    second = "mainframe_manuals_g4"
    clients = _switching_clients(
        {
            SERVICE: [COLLECTION, second],
            PEER_URLS[0]: [COLLECTION, second],
            PEER_URLS[1]: [COLLECTION, second],
            PEER_URLS[2]: [COLLECTION, second],
        },
        second=second,
        second_degraded=True,
    )
    report, code = _verify(
        clients,
        _production_settings(),
        _args(production=True, peer_url=list(PEER_URLS)),
    )
    output = capsys.readouterr().out
    assert code == 1, output
    assert report is not None
    assert report.alias.inventory[0] == second
    assert report.state != "healthy"
    assert second in output


def test_alias_that_keeps_moving_is_refused(capsys):
    calls = {"n": 0}

    class _Alternating(_FakeClient):
        def get_aliases(self):
            calls["n"] += 1
            target = COLLECTION if calls["n"] % 2 else "mainframe_manuals_g4"
            return SimpleNamespace(
                aliases=[SimpleNamespace(alias_name="mainframe_manuals", collection_name=target)]
            )

    clients = _fleet_clients()
    for url in list(clients):
        original = clients[url]
        clients[url] = _Alternating(
            peer_id=original.peer_id,
            collections=original.collections,
            params=original.params,
            status=original.status,
        )
    report, code = _verify(
        clients,
        _production_settings(),
        _args(production=True, peer_url=list(PEER_URLS)),
    )
    output = capsys.readouterr().out
    assert code == 1, output
    assert report is not None and report.state == "unverifiable"
    assert "still changing" in output


def test_peer_alias_disagreement_is_refused(capsys):
    clients = _fleet_clients()
    peer = clients[PEER_URLS[1]]
    peer.aliases = {"mainframe_manuals": "mainframe_manuals_g4"}
    report, code = _verify(
        clients,
        _production_settings(),
        _args(production=True, peer_url=list(PEER_URLS)),
    )
    output = capsys.readouterr().out
    assert code == 1, output
    assert report is not None and report.state == "unverifiable"
    assert "generation changed during observation" in output


def test_physical_pair_without_alias_reports_the_physical_target(capsys):
    clients = _fleet_clients()
    for client in clients.values():
        if isinstance(client, _FakeClient):
            client.aliases = {}
            client.collections = {
                "mainframe_manuals": client.collections[COLLECTION],
                "mainframe_manuals__completions": client.collections[CONTROL],
            }
    report, code = _verify(
        clients,
        _production_settings(),
        _args(production=True, peer_url=list(PEER_URLS)),
    )
    output = capsys.readouterr().out
    assert code == 0, output
    assert report is not None and report.alias is not None
    assert report.alias.target is None
    assert "not a current-alias certification" in output
