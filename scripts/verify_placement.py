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

Observations come from the primary endpoint plus every `--peer-url`
(repeatable; use each peer's direct endpoint, e.g. the per-pod headless
Service names). Only a peer's own local-shard report counts as an observed
copy; remote views are deduplicated by `(collection, shard_id, peer_id)` and
never counted as copies. Pass `--peer-url` for all expected peers:
production qualification requires every expected peer to answer.

Exit codes: 0 verified (healthy, or the declared non-HA profile; degraded
only with `--allow-degraded`), 1 refused/unverifiable/under-replicated, 2
usage or contradictory/incomplete claim. Read-only: no collection, replica
or snapshot is created, moved, or dropped (moving replicas is the later
migration slice).

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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mainframe_rag.config import Settings, load_settings
from mainframe_rag.ingest.placement import (
    PRODUCTION_PEERS,
    CollectionObservation,
    CollectionVerdict,
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


def _connect(url: str, settings: Settings, timeout: float):
    from qdrant_client import QdrantClient

    return QdrantClient(
        url=url,
        api_key=settings.qdrant_api_key,
        timeout=int(timeout),
        prefer_grpc=False,
    )


def _endpoints(primary: str, peer_urls: list[str]) -> list[str]:
    endpoints = [primary.rstrip("/")]
    for url in peer_urls:
        normalized = url.rstrip("/")
        if normalized and normalized not in endpoints:
            endpoints.append(normalized)
    return endpoints


def _expected_peers(args, policy: PlacementPolicy, endpoints: list[str]) -> int:
    if args.expect_peers is not None:
        return args.expect_peers
    if args.production:
        return PRODUCTION_PEERS
    if args.expect_single_node:
        return 1
    return max(policy.replication_factor, len(endpoints))


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

    endpoints = _endpoints(settings.qdrant_url, args.peer_url)
    expected_peers = _expected_peers(args, policy, endpoints)
    clients: dict[str, object] = {}
    configured_problems: list[str] = []
    alias_conflicts: list[str] = []
    try:
        for endpoint in endpoints:
            clients[endpoint] = connector(endpoint, settings, args.timeout)
        primary = clients[endpoints[0]]
        try:
            inventory = resolve_verification_inventory(primary, settings)
        except Exception as exc:  # noqa: BLE001 - read failure is a refusal
            print(f"FAIL: store unreachable or unreadable: {type(exc).__name__}: {exc}")
            return None, 1

        for collection in inventory:
            try:
                params = primary.get_collection(collection).config.params
            except Exception as exc:  # noqa: BLE001 - read failure is a refusal
                configured_problems.append(
                    f"{collection}: configured policy unreadable "
                    f"({type(exc).__name__}: {exc})"
                )
                continue
            configured_problems.extend(
                configured_policy_problems(collection, params, policy)
            )

        try:
            cluster_status = primary.cluster_status()
            members = member_peer_ids(cluster_status)
            consensus = consensus_name(cluster_status)
        except Exception as exc:  # noqa: BLE001 - read failure is a refusal
            members = ()
            consensus = ""
            configured_problems.append(
                f"cluster status unreadable ({type(exc).__name__}: {exc})"
            )

        observations = _observe_all(clients, endpoints, inventory)
        reachable_endpoints = {
            view.endpoint for view in observations if view.reachable
        }
        primary_mapping = resolve_alias_mapping(primary, settings.qdrant_collection)
        alias_conflicts.extend(
            _alias_conflicts(
                [url for url in endpoints[1:] if url in reachable_endpoints],
                clients,
                settings,
                primary_mapping,
            )
        )
        verdicts: list[CollectionVerdict] = [
            evaluate_collection_placement(collection, observations, policy)
            for collection in inventory
        ]
        reachability = _peer_reachability(clients, endpoints, observations)
    finally:
        for client in clients.values():
            close = getattr(client, "close", None)
            if callable(close):
                close()

    report = VerificationReport(
        policy=policy,
        production=bool(args.production),
        single_node=bool(args.expect_single_node),
        cluster=evaluate_cluster(
            expected_peers=expected_peers,
            member_peer_ids=members,
            peer_reachability=reachability,
            consensus=consensus,
            primary_endpoint=endpoints[0],
        ),
        collections=tuple(verdicts),
        configured_problems=tuple(configured_problems),
        alias_conflicts=tuple(alias_conflicts),
    )
    if len(endpoints) < expected_peers:
        report = _with_note(
            report,
            f"only {len(endpoints)} endpoint(s) probed; pass --peer-url for every expected peer",
        )
    lines = evidence_lines(report, allow_degraded=bool(args.allow_degraded))
    print("\n".join(lines))
    state = report.state
    if state in ("healthy", "non-ha"):
        return report, 0
    if state == "degraded" and args.allow_degraded:
        return report, 0
    return report, 1


def _with_note(report: VerificationReport, note: str) -> VerificationReport:
    from dataclasses import replace

    return replace(report, notes=report.notes + (note,))


def _alias_conflicts(
    extra_endpoints: list[str],
    clients: dict[str, object],
    settings: Settings,
    primary_mapping: str | None,
) -> list[str]:
    conflicts: list[str] = []
    for endpoint in extra_endpoints:
        try:
            mapping = resolve_alias_mapping(clients[endpoint], settings.qdrant_collection)
        except Exception as exc:  # noqa: BLE001 - unreachable endpoint reported separately
            conflicts.append(
                f"{endpoint}: alias mapping unreadable ({type(exc).__name__}: {exc})"
            )
            continue
        if mapping != primary_mapping:
            conflicts.append(
                f"{endpoint}: alias {settings.qdrant_collection!r} resolves to "
                f"{mapping!r} but the primary reports {primary_mapping!r}"
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


def _peer_reachability(
    clients: dict[str, object],
    endpoints: list[str],
    observations: list[CollectionObservation],
) -> tuple[PeerReachability, ...]:
    reachability: list[PeerReachability] = []
    for endpoint in endpoints:
        views = [view for view in observations if view.endpoint == endpoint]
        ok = next((view for view in views if view.reachable), None)
        if ok is None:
            error = next(
                (view.error for view in views if view.error), "no readable collection"
            )
            reachability.append(PeerReachability(endpoint, False, None, error))
        else:
            reachability.append(PeerReachability(endpoint, True, ok.peer_id, None))
    return tuple(reachability)


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
        help="direct REST endpoint of one peer (repeat for every expected peer; "
        "the primary QDRANT_URL is always probed as well)",
    )
    parser.add_argument(
        "--expect-peers",
        type=int,
        default=None,
        help="expected cluster member count (default: 3 for --production, 1 for "
        "--expect-single-node, otherwise the replication factor)",
    )
    parser.add_argument(
        "--allow-degraded",
        action="store_true",
        help="operational check: a readable but under-replicated topology exits 0 "
        "with a degraded verdict; never production qualification",
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
