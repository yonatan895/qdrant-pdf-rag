"""Replica-placement evaluation for the HA acceptance command (issue #360).

Read-only judgement over actual cluster observations: configured
shard/replication/write-consistency policy, per-shard authoritative active
copies on reachable peers, transfers/recovery states, and cluster
membership. Pure evaluation (no I/O) except `resolve_alias_binding`, which
captures the configured alias -> physical generation binding together with
the physical corpus collection and its paired control collection through the
existing naming owner.

Authoritative placement observations come only from direct peer endpoints
(`--peer-url`), each of which is bound to the member set the cluster reports
about itself. The entry endpoint (`QDRANT_URL`, possibly a load-balanced
Service) is used for inventory/alias/binding checks, never as a peer
identity: a Service that alternates backends must not become an extra
replica or combine changing identities. Replicas named outside the accepted
membership are refused, never counted.

The owner decision (issue #360 comment, 19 September 2026) is 3 Qdrant
peers, 6 logical shards, replication factor 3, write consistency 2, applied
to corpus and completion collections. `1/1/1` is the explicit non-HA
single-node profile and never production qualification. Unknown live
metadata is unverifiable — never green. Moving replicas is a later
migration slice; nothing here mutates.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from qdrant_client import models

from mainframe_rag.config import Settings
from mainframe_rag.ingest.completion import completion_collection_for
from mainframe_rag.ports import QdrantPoints

POLICY_KEYS = (
    "QDRANT_SHARD_NUMBER",
    "QDRANT_REPLICATION_FACTOR",
    "QDRANT_WRITE_CONSISTENCY_FACTOR",
)

# Owner decision for the air-gapped production topology and the explicit
# single-node profile (both checked in: overlays/openshift/collection-policy.env
# and the explicit rehearsal profiles in .github/workflows/e2e.yml).
PRODUCTION_POLICY = (6, 3, 2)
SINGLE_NODE_POLICY = (1, 1, 1)
PRODUCTION_PEERS = 3

# Outcomes, worst first for aggregation.
_STATE_RANK = {
    "healthy": 0,
    "degraded": 1,
    "recovering": 2,
    "unservable": 3,
    "unverifiable": 4,
}

# Replica states that are not serving reads. Anything but ACTIVE is a reason
# to refuse a healthy verdict with a precise cause: a transfer in progress, a
# stale/dead copy, or a deliberately non-serving topology (ActiveRead serves
# reads but not writes, so it cannot stand in for an active copy).
_NON_SERVING_DETAIL = {
    "INITIALIZING": "initializing",
    "RECOVERY": "recovering",
    "PARTIAL": "partial",
    "PARTIALSNAPSHOT": "partial snapshot",
    "RESHARDING": "resharding",
    "RESHARDINGSCALEDOWN": "resharding scale-down",
    "MANUALRECOVERY": "manual recovery",
    "ACTIVEREAD": "active-read (read-only)",
    "DEAD": "dead",
    "LISTENER": "listener (not serving reads)",
}


class PolicyClaimError(ValueError):
    """The requested qualification claim is incomplete or contradictory."""


@dataclass(frozen=True)
class PlacementPolicy:
    """One complete expected collection-distribution tuple."""

    shard_number: int
    replication_factor: int
    write_consistency_factor: int

    @property
    def as_tuple(self) -> tuple[int, int, int]:
        return (
            self.shard_number,
            self.replication_factor,
            self.write_consistency_factor,
        )

    def problems(self) -> tuple[str, ...]:
        problems: list[str] = []
        if self.shard_number < 1:
            problems.append(f"shard_number={self.shard_number} must be >= 1")
        if self.replication_factor < 1:
            problems.append(f"replication_factor={self.replication_factor} must be >= 1")
        if self.write_consistency_factor < 1:
            problems.append(
                f"write_consistency_factor={self.write_consistency_factor} must be >= 1"
            )
        if self.write_consistency_factor > self.replication_factor:
            problems.append(
                f"write_consistency_factor={self.write_consistency_factor} exceeds "
                f"replication_factor={self.replication_factor}: writes can never acknowledge"
            )
        return tuple(problems)


def _parse_policy(values: tuple[int | None, int | None, int | None]) -> PlacementPolicy:
    missing = [
        key for key, value in zip(POLICY_KEYS, values, strict=True) if value is None
    ]
    if missing:
        raise PolicyClaimError(
            "collection distribution policy is incomplete (missing: "
            + " ".join(missing)
            + ") — select all three keys; production is 6/3/2 and the one-node "
            "profile is 1/1/1 (see overlays/openshift/collection-policy.env)"
        )
    shard_number, replication_factor, write_consistency_factor = values
    assert shard_number is not None
    assert replication_factor is not None
    assert write_consistency_factor is not None
    policy = PlacementPolicy(
        shard_number=shard_number,
        replication_factor=replication_factor,
        write_consistency_factor=write_consistency_factor,
    )
    problems = policy.problems()
    if problems:
        raise PolicyClaimError("; ".join(problems))
    return policy


def selected_policy(
    settings: Settings, *, production: bool, single_node: bool
) -> PlacementPolicy | None:
    """Resolve the operator's explicit claim.

    `--production` pins the owner decision 6/3/2 and refuses anything else.
    `--expect-single-node` declares the explicit 1/1/1 non-HA profile; live
    configured values must still match it. With neither flag, only a complete
    explicit tuple establishes a claim; no tuple means no claim (the caller
    refuses rather than assuming the live configuration is the claim).
    """
    if production and single_node:
        raise PolicyClaimError(
            "--production and --expect-single-node are contradictory claims"
        )
    values = (
        settings.qdrant_shard_number,
        settings.qdrant_replication_factor,
        settings.qdrant_write_consistency_factor,
    )
    if production:
        policy = _parse_policy(values)
        if policy.as_tuple != PRODUCTION_POLICY:
            raise PolicyClaimError(
                f"production qualification requires the owner decision "
                f"{PRODUCTION_POLICY[0]}/{PRODUCTION_POLICY[1]}/{PRODUCTION_POLICY[2]} "
                f"(shards/RF/W), got {policy.shard_number}/"
                f"{policy.replication_factor}/{policy.write_consistency_factor}"
            )
        return policy
    if single_node:
        if all(value is None for value in values):
            return PlacementPolicy(*SINGLE_NODE_POLICY)
        policy = _parse_policy(values)
        if policy.as_tuple != SINGLE_NODE_POLICY:
            raise PolicyClaimError(
                f"--expect-single-node declares 1/1/1, but the selected policy is "
                f"{policy.shard_number}/{policy.replication_factor}/"
                f"{policy.write_consistency_factor}: labels must not disagree with "
                "the configured topology"
            )
        return policy
    if all(value is None for value in values):
        return None
    return _parse_policy(values)


def configured_policy_problems(
    collection: str, params: models.CollectionParams, expected: PlacementPolicy
) -> tuple[str, ...]:
    """Configured S/RF/W must match the expected tuple exactly. Unknown
    (unreadable/None) live values are unverifiable, never green; a mismatch
    names the migration requirement instead of suggesting a downgrade."""
    problems: list[str] = []
    for attr, want in (
        ("shard_number", expected.shard_number),
        ("replication_factor", expected.replication_factor),
        ("write_consistency_factor", expected.write_consistency_factor),
    ):
        live = getattr(params, attr, None)
        if live is None:
            problems.append(
                f"{collection}: configured {attr} is unknown/unreadable; "
                "cannot certify an unknown policy"
            )
        elif live != want:
            problems.append(
                f"{collection}: configured {attr}={live} != selected {want} "
                "(snapshot-gated replica/rebuild migration, issue #360) — "
                "never lower the production policy to hide it"
            )
    return tuple(problems)


def _replica_state_name(state: object) -> str:
    name = getattr(state, "name", state)
    return str(name).upper()


@dataclass(frozen=True)
class CollectionObservation:
    """One peer endpoint's authoritative view of one collection.

    `local_shards` is what this peer says about its own copies; only these
    count as observed copies. `remote_shards` is other peers' topology as
    this peer remembers it (informational; never counted).
    """

    collection: str
    endpoint: str
    reachable: bool
    peer_id: int | None = None
    shard_count: int | None = None
    local_shards: tuple[tuple[int, str], ...] = ()
    remote_shards: tuple[tuple[int, int, str], ...] = ()
    transfers: tuple[tuple[int, int, int], ...] = ()
    missing: bool = False
    error: str | None = None


@dataclass(frozen=True)
class ShardVerdict:
    shard_id: int
    state: str
    active_peers: tuple[int, ...]
    observed_peers: tuple[int, ...]
    detail: str


@dataclass(frozen=True)
class CollectionVerdict:
    collection: str
    state: str
    problems: tuple[str, ...]
    shards: tuple[ShardVerdict, ...]


@dataclass(frozen=True)
class ClusterVerdict:
    state: str
    expected_peers: int
    member_peers: int
    reachable_peer_urls: int
    consensus: str
    problems: tuple[str, ...]
    member_ids: tuple[int, ...] = ()
    observed_peer_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class PeerClusterView:
    """One authoritative direct peer endpoint's own control-plane view.

    `reachable` means the endpoint answered a read. A reachable endpoint whose
    own membership/consensus is missing or empty is unverifiable, never a
    supported lost member: only an endpoint that failed every read can
    positively establish a single-peer loss. Membership is what the endpoint
    reports about the whole cluster, not about itself alone.
    """

    endpoint: str
    reachable: bool
    peer_id: int | None = None
    member_peer_ids: tuple[int, ...] = ()
    consensus: str = ""
    error: str | None = None


@dataclass(frozen=True)
class AliasBinding:
    """The configured alias and the physical generation it resolved to when
    the inventory was captured. `target is None` means no alias exists and
    the configured name is the physical collection itself (in-place layout);
    such a verification covers that physical pair, not a current-alias
    certification."""

    alias: str
    target: str | None
    inventory: tuple[str, ...]


def _state_from_rank(rank: int) -> str:
    for state, value in _STATE_RANK.items():
        if value == rank:
            return state
    return "unverifiable"


def evaluate_collection_placement(
    collection: str,
    observations: Iterable[CollectionObservation],
    expected: PlacementPolicy,
    *,
    accepted_peers: tuple[int, ...],
) -> CollectionVerdict:
    """Judge actual placement from authoritative local-shard reports.

    Every shard needs `expected.replication_factor` ACTIVE copies on distinct
    peers. Deduplication is by `(collection, shard_id, peer_id)` — a replica
    reported by several peers is still one copy, and a remote report is not an
    observation of that copy's state. Only peer ids inside the cluster's own
    reported member set count: an endpoint or remote reference outside
    `accepted_peers` is a foreign topology and makes the verdict
    unverifiable. Local shards outside the declared shard set are refused
    rather than silently ignored. A pending, stale, or under-replicated state
    is never healthy; an unknown shard count is unverifiable.
    """
    if expected.shard_number < 1:
        raise ValueError(f"shard_number must be >= 1, got {expected.shard_number}")
    if not accepted_peers:
        return CollectionVerdict(
            collection,
            "unverifiable",
            (
                (
                    f"{collection}: accepted cluster membership is not established; "
                    "placement cannot be judged"
                ),
            ),
            (),
        )
    accepted = frozenset(accepted_peers)
    views = [view for view in observations if view.collection == collection]
    reachable = [view for view in views if view.reachable]
    unreachable = [view for view in views if not view.reachable]
    if not reachable:
        return CollectionVerdict(
            collection,
            "unverifiable",
            (
                (
                    f"{collection}: no peer endpoint reachable; topology cannot be observed "
                    f"({', '.join(view.endpoint + ': ' + (view.error or 'unreachable') for view in unreachable) or 'no endpoints'})"
                ),
            ),
            (),
        )
    absent = [view for view in reachable if view.missing]
    present = [view for view in reachable if not view.missing]
    if absent and not present:
        return CollectionVerdict(
            collection,
            "unservable",
            (f"{collection}: collection absent on every reachable peer",),
            (),
        )
    if absent:
        return CollectionVerdict(
            collection,
            "unverifiable",
            (
                (
                    f"{collection}: present on {', '.join(view.endpoint for view in present)} "
                    f"but absent on {', '.join(view.endpoint for view in absent)} — "
                    "peers disagree on the required collection"
                ),
            ),
            (),
        )
    anonymous = [view for view in present if view.peer_id is None]
    if anonymous:
        return CollectionVerdict(
            collection,
            "unverifiable",
            (
                (
                    f"{collection}: peer id unreadable from "
                    f"{', '.join(view.endpoint for view in anonymous)}"
                ),
            ),
            (),
        )
    foreign = [view for view in present if view.peer_id not in accepted]
    if foreign:
        return CollectionVerdict(
            collection,
            "unverifiable",
            (
                (
                    f"{collection}: endpoint(s) "
                    f"{', '.join(view.endpoint for view in foreign)} report peer id(s) "
                    f"{sorted(peer for peer in (view.peer_id for view in foreign) if peer is not None)} "
                    f"outside the accepted cluster membership {sorted(accepted)} — "
                    "refusing a foreign topology"
                ),
            ),
            (),
        )

    # (shard_id, peer_id) -> state, from each peer's own local report.
    local: dict[int, dict[int, str]] = {}
    shard_count_votes: list[int] = []
    problems: list[str] = []
    transfers: list[tuple[int, int, int]] = []
    for view in present:
        assert view.peer_id is not None  # anonymous views returned above
        if view.shard_count is None:
            problems.append(f"{view.endpoint}: shard count unknown/unreadable")
            continue
        shard_count_votes.append(view.shard_count)
        for shard_id, state in view.local_shards:
            local.setdefault(shard_id, {})[view.peer_id] = state
        transfers.extend(view.transfers)
    # Cross-check remote reports against the peers' own reports: a copy one
    # peer believes is on another reachable peer, but that peer's own report
    # omits or contradicts, is an incomplete/contradictory observation —
    # never a copy and never healthy.
    reached = {view.peer_id for view in present if view.peer_id is not None}
    for view in present:
        for shard_id, peer_id, state in view.remote_shards:
            if peer_id not in accepted:
                problems.append(
                    f"shard {shard_id}: {view.endpoint} names peer {peer_id} outside "
                    f"the accepted cluster membership {sorted(accepted)} — refusing a "
                    "foreign topology"
                )
                continue
            if peer_id not in reached:
                continue
            local_state = local.get(shard_id, {}).get(peer_id)
            if local_state is None:
                problems.append(
                    f"shard {shard_id}: {view.endpoint} reports a copy on peer "
                    f"{peer_id} that peer's own report omits — observations are incomplete"
                )
            elif local_state != state:
                problems.append(
                    f"shard {shard_id}: peer {peer_id} reports {local_state} about "
                    f"itself while {view.endpoint} reports {state} — observations disagree"
                )
    if problems:
        return CollectionVerdict(collection, "unverifiable", tuple(problems), ())
    shard_count = shard_count_votes[0] if shard_count_votes else None
    if shard_count is None:
        return CollectionVerdict(
            collection, "unverifiable", tuple(problems) or (f"{collection}: shard count unknown",), ()
        )
    if len(set(shard_count_votes)) != 1:
        return CollectionVerdict(
            collection,
            "unverifiable",
            (
                (
                    f"{collection}: peers disagree on shard_count "
                    f"({sorted(set(shard_count_votes))}); observations are incomplete"
                ),
            ),
            (),
        )
    if shard_count != expected.shard_number:
        return CollectionVerdict(
            collection,
            "unverifiable" if shard_count > expected.shard_number else "unservable",
            (
                (
                    f"{collection}: observed shard_count={shard_count} != expected "
                    f"{expected.shard_number}"
                ),
            ),
            (),
        )
    declared = range(shard_count)
    unexpected = sorted(
        {
            shard_id
            for view in present
            for shard_id, _state in view.local_shards
            if shard_id not in declared
        }
        | {
            shard_id
            for view in present
            for shard_id, _peer_id, _state in view.remote_shards
            if shard_id not in declared
        }
    )
    if unexpected:
        return CollectionVerdict(
            collection,
            "unverifiable",
            (
                (
                    f"{collection}: observed shard id(s) {unexpected} outside the "
                    f"declared shard set 0..{shard_count - 1} — refusing an "
                    "unsupported layout"
                ),
            ),
            (),
        )

    shards: list[ShardVerdict] = []
    ranks: list[int] = [_STATE_RANK["healthy"]]
    shard_problems: list[str] = []
    for shard_id in range(shard_count):
        copies = local.get(shard_id, {})
        observed = tuple(sorted(copies))
        active = tuple(sorted(peer for peer, state in copies.items() if state == "ACTIVE"))
        idle = sorted({state for state in copies.values() if state != "ACTIVE"})
        if not observed:
            detail = (
                f"no copy observed on any reachable peer"
                f" ({len(unreachable)} peer endpoint(s) unreachable)"
                if unreachable
                else "no copy observed on any peer"
            )
            state = "unverifiable" if unreachable else "unservable"
            shards.append(ShardVerdict(shard_id, state, (), (), detail))
            shard_problems.append(f"shard {shard_id}: {detail}")
            ranks.append(_STATE_RANK[state])
            continue
        unknown_states = sorted(
            state for state in copies.values() if state not in ("ACTIVE",) and state not in _NON_SERVING_DETAIL
        )
        if unknown_states:
            detail = f"unrecognized replica state(s): {', '.join(unknown_states)}"
            shards.append(ShardVerdict(shard_id, "unverifiable", active, observed, detail))
            shard_problems.append(f"shard {shard_id}: {detail}")
            ranks.append(_STATE_RANK["unverifiable"])
            continue
        if not active:
            detail = "no ACTIVE copy on any reachable peer"
            if idle:
                detail += "; non-serving states: " + ", ".join(
                    _NON_SERVING_DETAIL[state] for state in idle
                )
            state = "recovering" if idle else ("unverifiable" if unreachable else "unservable")
            shards.append(ShardVerdict(shard_id, state, active, observed, detail))
            shard_problems.append(f"shard {shard_id}: {detail}")
            ranks.append(_STATE_RANK[state])
            continue
        if len(active) < expected.replication_factor:
            detail = f"only {len(active)}/{expected.replication_factor} ACTIVE copies"
            if unreachable:
                detail += f"; {len(unreachable)} peer endpoint(s) unreachable"
            if idle:
                detail += "; non-serving states: " + ", ".join(
                    _NON_SERVING_DETAIL[state] for state in idle
                )
            state = "recovering" if idle or transfers else "degraded"
            shards.append(ShardVerdict(shard_id, state, active, observed, detail))
            shard_problems.append(f"shard {shard_id}: {detail}")
            ranks.append(_STATE_RANK[state])
            continue
        if idle:
            detail = "ACTIVE copies meet policy but stale copies remain: " + ", ".join(
                _NON_SERVING_DETAIL[state] for state in idle
            )
            shards.append(ShardVerdict(shard_id, "recovering", active, observed, detail))
            shard_problems.append(f"shard {shard_id}: {detail}")
            ranks.append(_STATE_RANK["recovering"])
            continue
        shards.append(
            ShardVerdict(
                shard_id,
                "healthy",
                active,
                observed,
                f"{len(active)}/{expected.replication_factor} ACTIVE copies on distinct peers",
            )
        )
    if transfers:
        detail = "; ".join(
            f"shard {shard_id}: replica transfer in progress (peer {src} -> peer {dst})"
            for shard_id, src, dst in transfers
        )
        shard_problems.append(detail)
        ranks.append(_STATE_RANK["recovering"])
    if unreachable and not any(rank >= _STATE_RANK["unverifiable"] for rank in ranks):
        # Unreachable expected peers make the copy count unverifiable even
        # when other peers still list the missing replicas as ACTIVE.
        ranks.append(_STATE_RANK["degraded"])
    state = _state_from_rank(max(ranks))
    return CollectionVerdict(collection, state, tuple(shard_problems), tuple(shards))


def _cluster_refusal(
    expected_peers: int,
    views: tuple[PeerClusterView, ...],
    problem: str,
    *,
    members: frozenset[int] = frozenset(),
) -> ClusterVerdict:
    observed = tuple(sorted({v.peer_id for v in views if v.reachable and v.peer_id is not None}))
    consensus = {v.consensus for v in views if v.reachable}
    return ClusterVerdict(
        state="unverifiable",
        expected_peers=expected_peers,
        member_peers=len(members),
        reachable_peer_urls=sum(1 for v in views if v.reachable),
        consensus=consensus.pop() if len(consensus) == 1 else "unknown",
        problems=(problem,),
        member_ids=tuple(sorted(members)),
        observed_peer_ids=observed,
    )


def evaluate_cluster(
    *,
    expected_peers: int,
    views: Iterable[PeerClusterView],
) -> ClusterVerdict:
    """Cluster membership bound to the observed endpoint identities.

    Every expected peer must be provided as its own direct endpoint; each one
    must report its own peer id, the same cluster membership, and a working
    consensus thread. Observed identities must be exactly the reported member
    set. Only a single known member missing with every other check coherent is
    positively established degraded (the supported one-peer-loss state);
    missing/contradictory control-plane evidence is unverifiable, never
    degraded.
    """
    if expected_peers < 1:
        raise ValueError(f"expected_peers must be >= 1, got {expected_peers}")
    provided = tuple(views)
    if len(provided) != expected_peers:
        return _cluster_refusal(
            expected_peers,
            provided,
            f"{len(provided)} authoritative peer endpoint(s) provided, expected "
            f"{expected_peers} — pass --peer-url for every expected peer (the entry "
            "endpoint is not a peer)",
        )
    reachable = tuple(view for view in provided if view.reachable)
    unreachable = tuple(view for view in provided if not view.reachable)
    if not reachable:
        return _cluster_refusal(
            expected_peers, provided, "no authoritative peer endpoint reachable"
        )
    if any(view.peer_id is None for view in reachable):
        endpoints = ", ".join(view.endpoint for view in reachable if view.peer_id is None)
        return _cluster_refusal(
            expected_peers, provided, f"reachable peer endpoint(s) {endpoints} did not report a peer id"
        )
    observed = [view.peer_id for view in reachable if view.peer_id is not None]
    if len(set(observed)) != len(observed):
        return _cluster_refusal(
            expected_peers,
            provided,
            f"only {len(set(observed))} distinct peer id(s) among {len(reachable)} "
            "reachable endpoint(s) — endpoints must address different peers",
        )
    if expected_peers == 1:
        view = reachable[0]
        assert view.peer_id is not None
        members = frozenset(view.member_peer_ids)
        if members and members != frozenset({view.peer_id}):
            return _cluster_refusal(
                expected_peers,
                provided,
                f"single peer reports cluster membership {sorted(members)}, which is "
                f"not itself (peer {view.peer_id})",
            )
        if view.consensus not in ("disabled", "working"):
            return _cluster_refusal(
                expected_peers,
                provided,
                f"peer {view.peer_id} consensus thread is "
                f"{view.consensus or 'unknown/unreadable'}",
                members=frozenset({view.peer_id}),
            )
        if not members and view.consensus != "disabled":
            return _cluster_refusal(
                expected_peers,
                provided,
                f"peer {view.peer_id} reports no membership but consensus is "
                f"{view.consensus!r}, not a standalone server",
            )
        return ClusterVerdict(
            state="healthy",
            expected_peers=1,
            member_peers=1,
            reachable_peer_urls=1,
            consensus=view.consensus,
            problems=(),
            member_ids=(view.peer_id,),
            observed_peer_ids=(view.peer_id,),
        )

    member_sets = {frozenset(view.member_peer_ids) for view in reachable}
    if len(member_sets) != 1:
        described = "; ".join(
            f"{view.endpoint} reports {sorted(view.member_peer_ids)}" for view in reachable
        )
        return _cluster_refusal(
            expected_peers,
            provided,
            f"peer endpoints disagree on cluster membership: {described}",
        )
    members = member_sets.pop()
    if not members:
        return _cluster_refusal(
            expected_peers,
            provided,
            f"reachable peer endpoints report no cluster membership; expected "
            f"{expected_peers} members",
        )
    if len(members) != expected_peers:
        return _cluster_refusal(
            expected_peers,
            provided,
            f"cluster reports {len(members)} member peer(s) {sorted(members)}, "
            f"expected {expected_peers}",
            members=members,
        )
    foreign = sorted(set(observed) - members)
    if foreign:
        return _cluster_refusal(
            expected_peers,
            provided,
            f"observed peer id(s) {foreign} are not in the cluster's own membership "
            f"{sorted(members)}",
            members=members,
        )
    consensus_values = {view.consensus for view in reachable}
    if len(consensus_values) != 1:
        return _cluster_refusal(
            expected_peers,
            provided,
            "peer endpoints disagree on consensus status: "
            + ", ".join(
                f"{view.endpoint}={view.consensus or 'unknown'}" for view in reachable
            ),
            members=members,
        )
    consensus = consensus_values.pop()
    if consensus != "working":
        return _cluster_refusal(
            expected_peers,
            provided,
            f"cluster consensus thread is {consensus or 'unknown/unreadable'}; "
            f"expected a working {expected_peers}-member cluster",
            members=members,
        )
    missing = sorted(members - set(observed))
    if not missing:
        return ClusterVerdict(
            state="healthy",
            expected_peers=expected_peers,
            member_peers=len(members),
            reachable_peer_urls=len(reachable),
            consensus=consensus,
            problems=(),
            member_ids=tuple(sorted(members)),
            observed_peer_ids=tuple(sorted(observed)),
        )
    if len(missing) == 1 and len(unreachable) == 1:
        return ClusterVerdict(
            state="degraded",
            expected_peers=expected_peers,
            member_peers=len(members),
            reachable_peer_urls=len(reachable),
            consensus=consensus,
            problems=(
                (
                    f"member peer {missing[0]} unreachable via "
                    f"{unreachable[0].endpoint} "
                    f"({unreachable[0].error or 'no response'}); its replica and "
                    "configured policy states are unknown in this run"
                ),
            ),
            member_ids=tuple(sorted(members)),
            observed_peer_ids=tuple(sorted(observed)),
        )
    return _cluster_refusal(
        expected_peers,
        provided,
        f"member peer(s) {missing} not observed (reachable peer ids {sorted(observed)}) "
        "— insufficient surviving topology to establish a supported degraded state",
        members=members,
    )


@dataclass(frozen=True)
class VerificationReport:
    policy: PlacementPolicy
    production: bool
    single_node: bool
    cluster: ClusterVerdict
    collections: tuple[CollectionVerdict, ...]
    configured_problems: tuple[str, ...] = ()
    alias_conflicts: tuple[str, ...] = ()
    alias: AliasBinding | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def state(self) -> str:
        if self.single_node:
            # The explicit non-HA profile verifies only when every observed
            # shard has exactly one ACTIVE copy and nothing is unknown.
            ranks = [_STATE_RANK[self.cluster.state]]
            ranks.extend(_STATE_RANK[verdict.state] for verdict in self.collections)
            if self.configured_problems or self.alias_conflicts:
                return "refused"
            if ranks and max(ranks) != _STATE_RANK["healthy"]:
                return "refused"
            return "non-ha"
        if self.configured_problems or self.alias_conflicts:
            return "unverifiable"
        ranks = [0]
        ranks.extend(_STATE_RANK[verdict.state] for verdict in self.collections)
        ranks.append(_STATE_RANK[self.cluster.state])
        return _state_from_rank(max(ranks))


def evidence_lines(
    report: VerificationReport, *, allow_degraded: bool = False
) -> list[str]:
    """Human-readable report lines, ending with the machine `VERDICT:` line."""
    lines: list[str] = []
    if report.configured_problems:
        lines.extend(f"configured: {problem}" for problem in report.configured_problems)
    if report.alias is not None:
        if report.alias.target is None:
            lines.append(
                f"alias: {report.alias.alias!r} has no alias mapping; verified the "
                f"configured physical pair {report.alias.inventory[0]!r} — this is not "
                "a current-alias certification"
            )
        else:
            lines.append(
                f"alias: {report.alias.alias!r} -> {report.alias.target!r} (binding "
                "captured with the inventory and unchanged across observation; it may "
                "change after this command returns)"
            )
    for conflict in report.alias_conflicts:
        lines.append(f"alias: {conflict}")
    cluster = report.cluster
    lines.append(
        f"cluster: {cluster.state} — members={cluster.member_peers}/"
        f"{cluster.expected_peers} reachable_peer_urls={cluster.reachable_peer_urls} "
        f"observed_peer_ids={list(cluster.observed_peer_ids)} "
        f"consensus={cluster.consensus}"
    )
    for problem in cluster.problems:
        lines.append(f"cluster: {problem}")
    for verdict in report.collections:
        lines.append(f"collection {verdict.collection}: {verdict.state}")
        for shard in verdict.shards:
            peers = ",".join(str(peer) for peer in shard.active_peers) or "-"
            lines.append(
                f"  shard {shard.shard_id}: {shard.state} active=[{peers}] ({shard.detail})"
            )
        for problem in verdict.problems:
            lines.append(f"  {problem}")
    for note in report.notes:
        lines.append(f"note: {note}")
    if report.state == "non-ha":
        lines.append(
            "VERDICT: non-ha (explicit 1/1/1 profile verified; no peer loss survivable)"
        )
    elif report.state == "healthy":
        lines.append(
            f"VERDICT: healthy (observed {report.policy.replication_factor} distinct "
            "ACTIVE copies per shard on every required collection — placement evidence "
            "only; reads/writes were not exercised)"
        )
    elif report.state == "degraded" and allow_degraded:
        lines.append(
            "VERDICT: degraded (observed placement with one known member missing — "
            "operational continuation only, never production qualification; "
            "reads/writes were not exercised)"
        )
    else:
        lines.append(f"VERDICT: {report.state} (refused)")
    return lines


def resolve_alias_binding(client: QdrantPoints, configured: str) -> AliasBinding:
    """Capture the alias -> physical generation binding together with the
    durable pair the active generation needs: the resolved physical corpus
    collection plus its paired control collection (the same pair the serving
    path reads).

    Read-only. The caller re-reads this binding after observing placement and
    refuses (or retries) when it moved: resolving the alias and inspecting the
    collections must be one consistent observation, never a stale generation
    certified while publication already cut over. `target is None` means the
    configured name is the physical collection itself.
    """
    target: str | None = None
    for desc in client.get_aliases().aliases:
        if desc.alias_name == configured:
            target = desc.collection_name
            break
    candidate = target if target is not None else configured
    return AliasBinding(
        alias=configured,
        target=target,
        inventory=(candidate, completion_collection_for(candidate)),
    )


def observe_collection(
    collection: str,
    endpoint: str,
    fetch: Callable[[str], models.CollectionClusterInfo],
) -> CollectionObservation:
    """Call a peer's collection_cluster_info and normalize the payload.

    `fetch` performs the I/O (per-endpoint client) so tests can inject
    synthetic observations; an exception becomes an unreachable observation
    with the exact error text, never a false zero-copy report.
    """
    try:
        info = fetch(collection)
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return CollectionObservation(
            collection=collection,
            endpoint=endpoint,
            reachable=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    return CollectionObservation(
        collection=collection,
        endpoint=endpoint,
        reachable=True,
        peer_id=info.peer_id,
        shard_count=info.shard_count,
        local_shards=tuple(
            (shard.shard_id, _replica_state_name(shard.state)) for shard in info.local_shards
        ),
        remote_shards=tuple(
            (shard.shard_id, shard.peer_id, _replica_state_name(shard.state))
            for shard in info.remote_shards
        ),
        transfers=tuple(
            (transfer.shard_id, transfer.from_, transfer.to)
            for transfer in info.shard_transfers
        ),
    )


def consensus_name(status: models.ClusterStatus) -> str:
    """Textual consensus state; 'disabled' when the server is not clustered."""
    if getattr(status, "status", None) != "enabled":
        return "disabled"
    thread = getattr(status, "consensus_thread_status", None)
    return str(getattr(thread, "consensus_thread_status", "unknown"))


def member_peer_ids(status: models.ClusterStatus) -> tuple[int, ...]:
    peers: Mapping[str, object] = getattr(status, "peers", None) or {}
    ids: list[int] = []
    for key in peers:
        try:
            ids.append(int(key))
        except (TypeError, ValueError):
            continue
    return tuple(ids)
