#!/usr/bin/env python3
"""Replica-placement acceptance command (issue #360).

Read-only verification of ACTUAL per-shard replica placement for every
durable collection the active generation needs — the alias-resolved
physical corpus collection plus its paired completion/control collection —
instead of trusting pod counts or configured values. Three Ready pods
serving one copy fail here even when the collection config nominally
requests replication.

The expected policy comes only from an explicit claim: `--production` pins
the owner decision 6/3/2 (three peers), `--expect-single-node` declares the
1/1/1 non-HA profile, otherwise a complete QDRANT_* tuple selects the
policy. No claim means refusal, never an assumption. Unknown live metadata
is unverifiable, never green.

Authoritative placement observations come only from the direct peer
endpoints passed with `--peer-url`; each one must report its own peer id,
the same cluster membership, and a working consensus thread, and the
observed ids must be exactly that member set. `QDRANT_URL` (often a
load-balanced Service) is the entry endpoint: it supplies inventory, the
alias binding, configured-policy and reachability checks, and is never
counted as a peer identity — an alternating Service cannot become an extra
replica. Only a peer's own local-shard report counts as an observed copy;
remote views are deduplicated by `(collection, shard_id, peer_id)` and never
counted. Pass `--peer-url` for all expected peers: production qualification
requires every expected peer to answer at its own direct endpoint.

The alias -> physical-generation binding is captured together with the
inventory and re-read across every reachable endpoint after placement
observation. If publication moves the alias in that interval the command
retries from the inventory; a binding still moving after the bounded retries
is refused rather than certifying a stale generation.

Exit codes: 0 verified (healthy, or the declared non-HA profile; degraded
only with `--allow-degraded`, which requires a positively established loss
of exactly one known member — missing/contradictory control-plane evidence
is never accepted), 1 refused/unverifiable/under-replicated, 2 usage or
contradictory/incomplete claim. `--production` never combines with
`--allow-degraded`. Read-only: no collection, replica or snapshot is
created, moved, or dropped (moving replicas is the later migration slice).

    QDRANT_URL=http://qdrant:6333 QDRANT_SHARD_NUMBER=6 \
    QDRANT_REPLICATION_FACTOR=3 QDRANT_WRITE_CONSISTENCY_FACTOR=2 \
        python3 scripts/verify_placement.py --production \
            --peer-url http://qdrant-0.qdrant-headless:6333 \
            --peer-url http://qdrant-1.qdrant-headless:6333 \
            --peer-url http://qdrant-2.qdrant-headless:6333
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mainframe_rag.config import Settings, load_settings
from mainframe_rag.ingest.placement import (
    PRODUCTION_PEERS,
    SINGLE_NODE_POLICY,
    AliasBinding,
    CollectionObservation,
    CollectionVerdict,
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

ALIAS_RETRY_ATTEMPTS = 3


class _EntryUnreadable(Exception):
    """The entry endpoint could not resolve the inventory/alias binding."""


@dataclass
class _Generation:
    binding: AliasBinding
    observations: list[CollectionObservation]
    views: tuple[PeerClusterView, ...]
    entry_view: PeerClusterView | None = None
    configured_problems: list[str] = field(default_factory=list)
    alias_conflicts: list[str] = field(default_factory=list)

    @property
    def alias_changed(self) -> bool:
        return bool(self.alias_conflicts)


def _connect(url: str, settings: Settings, timeout: float):
    from qdrant_client import QdrantClient

    return QdrantClient(
        url=url,
        api_key=settings.qdrant_api_key,
        timeout=int(timeout),
        prefer_grpc=False,
    )


def _normalized(url: str) -> str:
    return url.rstrip("/")


def _dedupe(urls: list[str]) -> list[str]:
    seen: list[str] = []
    for url in urls:
        normalized = _normalized(url)
        if normalized and normalized not in seen:
            seen.append(normalized)
    return seen


def _expected_peers(args, policy: PlacementPolicy, authoritative_count: int) -> int:
    """Validate the expected peer count against the selected claim.

    `--production` pins the owner decision's three peers and
    `--expect-single-node` pins one; neither can be redefined by
    `--expect-peers`. Any other claim needs at least as many peers as the
    replication factor, otherwise the claim can never be satisfied.
    """
    override = args.expect_peers
    if args.production:
        if override is not None and override != PRODUCTION_PEERS:
            raise PolicyClaimError(
                f"--production pins {PRODUCTION_PEERS} peers; "
                f"--expect-peers={override} would redefine the contract"
            )
        return PRODUCTION_PEERS
    if args.expect_single_node:
        if override is not None and override != 1:
            raise PolicyClaimError(
                "--expect-single-node declares one peer; --expect-peers cannot override it"
            )
        return 1
    if override is not None:
        if override < 1:
            raise PolicyClaimError(f"--expect-peers must be >= 1, got {override}")
        expected = override
    else:
        expected = authoritative_count
    if expected < 1:
        raise PolicyClaimError("at least one authoritative peer endpoint is required")
    if expected < policy.replication_factor:
        raise PolicyClaimError(
            f"claim cannot be satisfied: {expected} peer(s) cannot hold "
            f"replication_factor={policy.replication_factor} copies"
        )
    return expected


def perform_verification(
    settings: Settings,
    args: argparse.Namespace,
    connect: Callable[[str, Settings, float], object] | None = None,
) -> tuple[VerificationReport | None, int]:
    """Build the report; return (report, exit_code). None means no claim."""
    connector = connect or _connect
    try:
        policy = selected_policy(
            settings, production=args.production, single_node=args.expect_single_node
        )
    except PolicyClaimError as exc:
        print(f"FAIL: {exc}")
        return None, 2
    if policy is None:
        print(
            "FAIL: no established claim — set a complete QDRANT_SHARD_NUMBER /"
            " QDRANT_REPLICATION_FACTOR / QDRANT_WRITE_CONSISTENCY_FACTOR tuple,"
            " or declare --expect-single-node (1/1/1 non-HA), or --production (6/3/2)"
        )
        return None, 1
    if args.production and args.allow_degraded:
        print(
            "FAIL: --production is strict qualification and cannot combine with "
            "--allow-degraded; run the operational degraded check as a separate "
            "non-production invocation"
        )
        return None, 2

    entry_url = _normalized(settings.qdrant_url)
    peer_urls = _dedupe(args.peer_url)
    if peer_urls:
        authoritative = peer_urls
    elif args.expect_single_node or policy.as_tuple == SINGLE_NODE_POLICY:
        authoritative = [entry_url]
    else:
        print(
            "FAIL: no authoritative peer endpoints — pass --peer-url for every "
            "expected peer (QDRANT_URL is an entry endpoint and cannot certify "
            "peer-local placement); the explicit single-server profile is declared "
            "with --expect-single-node"
        )
        return None, 1

    try:
        expected_peers = _expected_peers(args, policy, len(authoritative))
    except PolicyClaimError as exc:
        print(f"FAIL: {exc}")
        return None, 2

    probe_urls = list(authoritative)
    if entry_url not in authoritative:
        probe_urls.insert(0, entry_url)
    clients: dict[str, object] = {
        url: connector(url, settings, args.timeout) for url in probe_urls
    }
    notes: list[str] = [
        (
            "placement observation only: this command performs no read or write "
            "request, so it is not availability, acknowledgement or RPO/RTO evidence"
        ),
    ]
    if entry_url not in authoritative:
        notes.append(
            f"entry endpoint {entry_url} supplies inventory/alias binding only; "
            "peer identities and copies come from the --peer-url endpoints"
        )
    try:
        generation = _observe_with_retry(
            clients=clients,
            entry_url=entry_url,
            authoritative=authoritative,
            settings=settings,
            policy=policy,
        )
    except _EntryUnreadable as exc:
        print(f"FAIL: store unreachable or unreadable: {exc}")
        return None, 1
    finally:
        for client in clients.values():
            close = getattr(client, "close", None)
            if callable(close):
                close()

    cluster = evaluate_cluster(expected_peers=expected_peers, views=generation.views)
    configured_problems = list(generation.configured_problems) + _entry_problems(
        cluster, generation.entry_view
    )
    verdicts: list[CollectionVerdict] = [
        evaluate_collection_placement(
            collection,
            generation.observations,
            policy,
            accepted_peers=cluster.member_ids,
        )
        for collection in generation.binding.inventory
    ]
    report = VerificationReport(
        policy=policy,
        production=bool(args.production),
        single_node=bool(args.expect_single_node),
        cluster=cluster,
        collections=tuple(verdicts),
        configured_problems=tuple(configured_problems),
        alias_conflicts=tuple(generation.alias_conflicts),
        alias=generation.binding,
        notes=tuple(notes),
    )
    lines = evidence_lines(report, allow_degraded=bool(args.allow_degraded))
    print("\n".join(lines))
    state = report.state
    if state in ("healthy", "non-ha"):
        return report, 0
    if state == "degraded" and args.allow_degraded:
        return report, 0
    return report, 1


def _observe_with_retry(
    *,
    clients: dict[str, object],
    entry_url: str,
    authoritative: list[str],
    settings: Settings,
    policy: PlacementPolicy,
) -> _Generation:
    """One consistent generation observation, retried when the alias moved.

    The binding is resolved with the inventory, placement is observed, then
    every reachable endpoint re-reads the binding. A change means publication
    cut over during the inspection: observation restarts from the inventory
    (bounded), and a binding that keeps moving is refused rather than
    certifying the stale pair.
    """
    entry_client = clients[entry_url]
    last: _Generation | None = None
    for _attempt in range(ALIAS_RETRY_ATTEMPTS):
        try:
            binding = resolve_alias_binding(entry_client, settings.qdrant_collection)
        except Exception as exc:  # read failure is a refusal
            raise _EntryUnreadable(f"{type(exc).__name__}: {exc}") from exc
        generation = _observe_once(
            clients=clients,
            entry_url=entry_url,
            authoritative=authoritative,
            settings=settings,
            policy=policy,
            binding=binding,
        )
        if not generation.alias_changed:
            return generation
        last = generation
    assert last is not None
    last.alias_conflicts.append(
        f"alias binding still changing after {ALIAS_RETRY_ATTEMPTS} observations; "
        "refusing to certify a moving generation"
    )
    return last


def _observe_once(
    *,
    clients: dict[str, object],
    entry_url: str,
    authoritative: list[str],
    settings: Settings,
    policy: PlacementPolicy,
    binding: AliasBinding,
) -> _Generation:
    inventory = list(binding.inventory)
    observations = _observe_all(clients, authoritative, inventory)
    configured_problems = _configured_problems(
        clients[entry_url], entry_url, inventory, policy
    )
    views: list[PeerClusterView] = []
    for endpoint in authoritative:
        view, problems = _read_peer(
            endpoint, clients[endpoint], observations, inventory, policy
        )
        views.append(view)
        configured_problems.extend(problems)
    reachable = {view.endpoint for view in views if view.reachable}
    reachable.add(entry_url)
    alias_conflicts = _binding_recheck(clients, settings, binding, reachable)
    entry_view = None
    if entry_url not in authoritative:
        entry_view = _cluster_view(clients[entry_url], entry_url)
    return _Generation(
        binding=binding,
        observations=observations,
        views=tuple(views),
        entry_view=entry_view,
        configured_problems=configured_problems,
        alias_conflicts=alias_conflicts,
    )


def _cluster_view(client: object, endpoint: str) -> PeerClusterView:
    try:
        status = client.cluster_status()
    except Exception as exc:  # noqa: BLE001 - reported as unreadable membership
        return PeerClusterView(
            endpoint, True, None, (), "", f"{type(exc).__name__}: {exc}"
        )
    return PeerClusterView(
        endpoint=endpoint,
        reachable=True,
        peer_id=getattr(status, "peer_id", None),
        member_peer_ids=member_peer_ids(status),
        consensus=consensus_name(status),
    )


def _entry_problems(cluster, entry_view: PeerClusterView | None) -> list[str]:
    """The entry endpoint must see the same cluster as the direct peers: a
    Service routed to a foreign cluster with same-named collections must not
    be certified."""
    if entry_view is None or not cluster.member_ids:
        return []
    if entry_view.error:
        return [
            (
                f"entry endpoint {entry_view.endpoint}: cluster membership unreadable "
                f"({entry_view.error})"
            )
        ]
    entry_members = set(entry_view.member_peer_ids)
    if entry_members != set(cluster.member_ids):
        return [
            (
                f"entry endpoint {entry_view.endpoint} reports cluster membership "
                f"{sorted(entry_members)} while the direct peers report "
                f"{sorted(cluster.member_ids)} — entry and peers address different clusters"
            )
        ]
    if cluster.expected_peers > 1 and entry_view.consensus != cluster.consensus:
        return [
            (
                f"entry endpoint {entry_view.endpoint} reports consensus "
                f"{entry_view.consensus or 'unknown'} while the direct peers report "
                f"{cluster.consensus} — incompatible control-plane views"
            )
        ]
    return []


def _configured_problems(
    client: object,
    endpoint: str,
    inventory: list[str],
    policy: PlacementPolicy,
) -> list[str]:
    problems: list[str] = []
    for collection in inventory:
        try:
            exists = client.collection_exists(collection)
        except Exception as exc:  # noqa: BLE001 - read failure is a refusal
            problems.append(
                f"{endpoint}: {collection}: existence unreadable "
                f"({type(exc).__name__}: {exc})"
            )
            continue
        if not exists:
            problems.append(f"{endpoint}: {collection}: required collection absent")
            continue
        try:
            params = client.get_collection(collection).config.params
        except Exception as exc:  # noqa: BLE001 - read failure is a refusal
            problems.append(
                f"{endpoint}: {collection}: configured policy unreadable "
                f"({type(exc).__name__}: {exc})"
            )
            continue
        problems.extend(
            f"{endpoint}: {problem}"
            for problem in configured_policy_problems(collection, params, policy)
        )
    return problems


def _read_peer(
    endpoint: str,
    client: object,
    observations: list[CollectionObservation],
    inventory: list[str],
    policy: PlacementPolicy,
) -> tuple[PeerClusterView, list[str]]:
    """One direct peer's own identity, membership, consensus and policy.

    A peer that answered no read at all is the only absence eligible for the
    supported single-member-loss state. A reachable peer with unreadable
    membership/consensus (or contradictory identity across the required
    collections) yields an unverifiable view, never a lost member.
    """
    endpoint_views = [view for view in observations if view.endpoint == endpoint]
    problems: list[str] = []
    status_error: str | None = None
    members: tuple[int, ...] = ()
    consensus = ""
    status_peer_id: int | None = None
    try:
        status = client.cluster_status()
    except Exception as exc:  # noqa: BLE001 - reachability decides the outcome
        status_error = f"{type(exc).__name__}: {exc}"
    else:
        members = member_peer_ids(status)
        consensus = consensus_name(status)
        status_peer_id = getattr(status, "peer_id", None)
        if not consensus:
            status_error = "cluster consensus status unreadable"
    collection_ids = {
        view.peer_id
        for view in endpoint_views
        if view.reachable and not view.missing and view.peer_id is not None
    }
    if len(collection_ids) > 1:
        problems.append(
            f"{endpoint}: contradictory peer id(s) {sorted(collection_ids)} "
            "across the required collections"
        )
    peer_id = next(iter(collection_ids)) if len(collection_ids) == 1 else None
    if peer_id is None:
        peer_id = status_peer_id
    answered = any(view.reachable for view in endpoint_views) or status_error is None
    if not answered:
        error = next(
            (view.error for view in endpoint_views if view.error),
            status_error or "no response",
        )
        return PeerClusterView(endpoint, False, None, (), "", error), []
    if status_error is not None:
        problems.append(f"{endpoint}: cluster membership unreadable ({status_error})")
    view = PeerClusterView(
        endpoint=endpoint,
        reachable=True,
        peer_id=peer_id,
        member_peer_ids=tuple(members),
        consensus=consensus,
        error=status_error,
    )
    problems.extend(_configured_problems(client, endpoint, inventory, policy))
    return view, problems


def _binding_recheck(
    clients: dict[str, object],
    settings: Settings,
    binding: AliasBinding,
    reachable: set[str],
) -> list[str]:
    conflicts: list[str] = []
    for endpoint, client in clients.items():
        if endpoint not in reachable:
            continue
        try:
            resolved = resolve_alias_binding(client, settings.qdrant_collection).target
        except Exception as exc:  # noqa: BLE001 - read failure is a refusal
            conflicts.append(
                f"{endpoint}: alias binding unreadable on recheck "
                f"({type(exc).__name__}: {exc})"
            )
            continue
        if resolved != binding.target:
            conflicts.append(
                f"{endpoint}: alias {binding.alias!r} now resolves to {resolved!r}, "
                f"but resolved to {binding.target!r} when the inventory was captured "
                "— the generation changed during observation"
            )
    return conflicts


def _observe_all(
    clients: dict[str, object], endpoints: list[str], inventory: list[str]
) -> list[CollectionObservation]:
    observations: list[CollectionObservation] = []
    for endpoint in endpoints:
        client = clients[endpoint]
        for collection in inventory:
            try:
                exists = client.collection_exists(collection)
            except Exception as exc:  # noqa: BLE001 - reported as unreachable
                observations.append(
                    CollectionObservation(
                        collection=collection,
                        endpoint=endpoint,
                        reachable=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            if not exists:
                observations.append(
                    CollectionObservation(
                        collection=collection,
                        endpoint=endpoint,
                        reachable=True,
                        missing=True,
                    )
                )
                continue
            observations.append(
                observe_collection(
                    collection,
                    endpoint,
                    fetch=lambda name, c=client: c.collection_cluster_info(name),
                )
            )
    return observations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--production",
        action="store_true",
        help="strict production qualification: the selected policy must be the "
        "owner decision 6 shards / RF 3 / W 2 across 3 peers",
    )
    parser.add_argument(
        "--expect-single-node",
        action="store_true",
        help="declare the explicit 1/1/1 non-HA profile; verified single-copy "
        "placement is labeled non-ha instead of refused",
    )
    parser.add_argument(
        "--peer-url",
        action="append",
        default=[],
        metavar="URL",
        help="direct REST endpoint of one peer (repeat for every expected peer). "
        "QDRANT_URL is the entry/inventory endpoint and is never counted as a "
        "peer identity; with a 1/1/1 claim and no --peer-url the entry endpoint "
        "is treated as the single direct server",
    )
    parser.add_argument(
        "--expect-peers",
        type=int,
        default=None,
        help="expected cluster member count (must be 3 with --production and 1 "
        "with --expect-single-node; otherwise at least the replication factor; "
        "default: the number of --peer-url endpoints)",
    )
    parser.add_argument(
        "--allow-degraded",
        action="store_true",
        help="operational check: exit 0 only for a positively established loss of "
        "exactly one known member (observed placement with reduced redundancy); "
        "never available with --production and never production qualification",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="per-endpoint Qdrant timeout in seconds (default: 10)",
    )
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001 - configuration is the operator's to fix
        print(f"FAIL: cannot load settings: {type(exc).__name__}: {exc}")
        return 2
    _report, code = perform_verification(settings, args)
    return code


if __name__ == "__main__":
    sys.exit(main())
