"""Retained publication generations: a read-only listing and snapshot-only
disposal (issue #1373, PR 1).

Every corpus-changing alias publication keeps the superseded data/control
pair, its two immutable build aliases and a safety snapshot of the old data
collection (`swap_alias_to`). This module lists those generations with their
snapshots and sizes, and deletes the snapshots of named retained generations.
It never deletes a collection or an alias and never writes to Qdrant other
than a snapshot delete; data disposal (tombstone, reader stop) is later work
under the same issue.

  list               read-only and lock-free: a point-in-time view. Without
                     `--progress` the publish sidecar is not read, so no
                     generation is reported disposable.
  dispose-snapshots  dry run by default. `--execute` takes the publish lock
                     (`publish-<alias>.lock`, the one an ingest holds),
                     recomputes the plan under it, and deletes on each peer
                     exactly the snapshots that peer lists under the named
                     builds' data collections. A rerun recomputes and
                     converges: a deleted snapshot is no longer listed.

Targets are named by full build UUID only, never by collection name, pattern,
count, size or age. A named build is refused unless it is retained (both
immutable aliases present and consistent, the serving alias names another
physical), no other alias targets it, the publish sidecar does not name it,
every peer's snapshot listing was read, and it is outside the newest
`--keep` retained generations ordered by their newest completion-marker
`finished_at` (a missing time or a tie at the boundary is protected).

Snapshots are node-local (Qdrant v1.19.2): a peer lists only the snapshots it
stores, and a delete sent to another peer is a 404 that changes nothing. So
the listing reads every direct peer (`QDRANT_PEER_URLS`) and a delete goes to
the peer that listed the snapshot; whether it is gone is decided by listing
that peer again, never by the delete's status. Collection disk bytes are not
reported: v1.19.2 telemetry returns `disk_usage_bytes` 0 for populated
segments, so the listing prints snapshot bytes exactly and the data size as
not reported.

Output is fixed text plus build ids, collection and snapshot names, peer
host:port, counts, sizes and times: never payloads, keys or upstream error
text. Owner contract: docs/ingest.md (Rollback / disposal).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from mainframe_rag.config import Settings, qdrant_pool_limits
from mainframe_rag.ingest.build import (
    build_aliases,
    build_phase,
    canonical_build_id,
    decode_build_binding,
    is_build_alias,
)
from mainframe_rag.ingest.completion import (
    CompletionRecord,
    acquire_publish_lock,
    completion_collection_for,
    publish_lock_path,
    release_run_lock,
)
from mainframe_rag.ingest.placement import member_peer_ids
from mainframe_rag.ingest.publish import read_publication_record, read_publish_state
from mainframe_rag.ingest.qdrant_io import resolve_live_collection, scroll_all_points
from mainframe_rag.ingest.retirement import InvalidTombstone, read_tombstone, tombstone_matches
from mainframe_rag.ports import QdrantSnapshotAdmin, QdrantSnapshotReader

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNREADABLE = 3
EXIT_REFUSED = 4

KEEP_FLOOR = 1
DEFAULT_KEEP = 1
_COMPLETIONS = "__completions"
_RECOVERY_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#-]{0,127}")
_OPERATOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}")


def default_operator() -> str:
    """Who runs the disposal when `--operator` is not given: the login name
    from the environment, else the numeric uid (an OpenShift pod's arbitrary
    uid has no passwd entry, so no lookup that can fail is made)."""
    for name in ("USER", "LOGNAME"):
        value = os.environ.get(name, "")
        if _OPERATOR.fullmatch(value):
            return value
    return f"uid:{os.getuid()}"
DATA_SIZE_SOURCE = "not-reported"


class Refused(Exception):
    """A fixed, operator-facing refusal (exit 4). Nothing further changes."""


class Unreadable(Exception):
    """Qdrant or a peer could not be read (exit 3). Fixed text only."""


@dataclass(frozen=True)
class Peer:
    index: int
    label: str
    client: QdrantSnapshotReader
    reachable: bool
    peer_id: int | None


@dataclass(frozen=True)
class PeerView:
    """Every direct peer the snapshot listing reads. `complete` holds only
    when each configured endpoint answered as a distinct member and together
    they are the whole cluster (or the server is a single node)."""

    peers: tuple[Peer, ...]
    complete: bool
    problem: str | None


@dataclass(frozen=True)
class SnapshotEntry:
    peer: int
    collection: str
    name: str
    size_bytes: int | None
    creation_time: str | None
    checksum: str | None

    def as_json(self, labels: dict[int, str]) -> dict[str, Any]:
        return {
            "peer": self.peer, "peer_endpoint": labels.get(self.peer), "collection": self.collection,
            "name": self.name, "size_bytes": self.size_bytes,
            "creation_time": self.creation_time, "checksum": self.checksum,
        }


@dataclass
class Generation:
    build_id: str
    physical: str
    control: str
    # live | retained | sealed | inconsistent | data-missing, or from the
    # retirement tombstone, which overrides the alias-derived phase:
    # retiring | retired | tombstone-invalid
    phase: str
    live: bool
    build_schema: int | None
    finished_at: float | None
    points: int | None
    control_points: int
    aliases: tuple[str, ...]
    foreign_aliases: tuple[str, ...]
    sidecar_role: str | None = None
    snapshots: list[SnapshotEntry] = field(default_factory=list)
    control_snapshots: list[SnapshotEntry] = field(default_factory=list)
    rank: int | None = None
    reasons: list[str] = field(default_factory=list)
    sealed: bool = False
    data_present: bool = True
    tombstone: dict[str, Any] | None = None

    @property
    def snapshot_bytes(self) -> int:
        return sum(s.size_bytes or 0 for s in self.snapshots)

    @property
    def disposable(self) -> bool:
        return not self.reasons


@dataclass(frozen=True)
class Unmanaged:
    collection: str
    reason: str
    points: int | None


@dataclass
class Registry:
    alias: str
    live: str | None
    live_resolved: bool
    generations: list[Generation]
    unmanaged: list[Unmanaged]
    peers: PeerView
    sidecar_state: str  # absent | present | unreadable | not-read
    sidecar: dict | None
    unreadable_listings: set[int] = field(default_factory=set)

    @property
    def snapshots_complete(self) -> bool:
        return self.peers.complete and not self.unreadable_listings


# --- peers -------------------------------------------------------------------


def endpoint_label(url: str) -> str:
    """host:port of a peer endpoint for output. Credentials, a query or a
    fragment in a configured URL are refused, never printed."""
    parts = urlsplit(url)
    if parts.username or parts.password or parts.query or parts.fragment or not parts.hostname:
        raise Refused("E_PEER_URL: a configured Qdrant endpoint is not a plain http(s) URL")
    port = parts.port or (443 if parts.scheme == "https" else 6333)
    return f"{parts.hostname}:{port}"


def _status(client: QdrantSnapshotReader) -> tuple[bool, int | None, tuple[int, ...]]:
    status = client.cluster_status()
    enabled = getattr(status, "status", None) == "enabled"
    return enabled, getattr(status, "peer_id", None), member_peer_ids(status)


def read_peer_view(
    settings: Settings,
    entry: QdrantSnapshotReader,
    connect: Callable[[str], QdrantSnapshotReader],
) -> PeerView:
    """The direct peers to list snapshots on, from `QDRANT_PEER_URLS` like
    the placement gate. Without configured endpoints only a single-node
    server (or a one-member cluster) is complete: through a Service, each
    call lands on some peer and other peers' snapshots stay unseen."""
    try:
        enabled, _own, members = _status(entry)
    except Exception:  # noqa: BLE001 - reported with fixed text
        raise Unreadable("E_QDRANT_UNREADABLE: the cluster status could not be read") from None
    endpoints = settings.qdrant_peer_endpoints()
    if not endpoints:
        peer = Peer(0, endpoint_label(settings.qdrant_url), entry, True, None)
        if not enabled or len(members) <= 1:
            return PeerView((peer,), True, None)
        return PeerView((peer,), False, "PEERS_NOT_CONFIGURED")
    peers: list[Peer] = []
    problem: str | None = None
    for index, url in enumerate(endpoints):
        label = endpoint_label(url)
        client = connect(url)
        try:
            peer_enabled, peer_id, _ = _status(client)
        except Exception:  # noqa: BLE001 - an unreachable peer is reported, not raised
            peers.append(Peer(index, label, client, False, None))
            problem = problem or "PEER_UNREACHABLE"
            continue
        peers.append(Peer(index, label, client, True, peer_id if peer_enabled else None))
    if problem is None:
        if not enabled:
            if len(peers) != 1:
                problem = "PEERS_MISMATCH"
        else:
            ids = [p.peer_id for p in peers]
            if None in ids or len(set(ids)) != len(ids) or set(ids) != set(members):
                problem = "PEERS_MISMATCH"
    return PeerView(tuple(peers), problem is None, problem)


# --- registry ----------------------------------------------------------------


def read_sidecar(progress: Path | None, alias: str) -> tuple[str, dict | None]:
    if progress is None:
        return "not-read", None
    try:
        state = read_publish_state(progress, alias)
    except RuntimeError:
        return "unreadable", None
    return ("absent", None) if state is None else ("present", state)


def _marker_summary(client: QdrantSnapshotReader, control: str, page_size: int) -> tuple[float | None, int]:
    """Newest completion-marker `finished_at` in a control collection and its
    point count. Only payloads that validate as completion markers count;
    the receipt, manifest and catalog entries carry no such time."""
    records = scroll_all_points(client, control, scroll_filter=None, with_payload=True, page_size=page_size)
    newest: float | None = None
    for record in records:
        try:
            marker = CompletionRecord.model_validate(record.payload or {})
        except ValidationError, ValueError:
            continue
        if newest is None or marker.finished_at > newest:
            newest = marker.finished_at
    return newest, len(records)


def _points(client: QdrantSnapshotReader, name: str) -> int | None:
    try:
        return client.get_collection(name).points_count
    except Exception:  # noqa: BLE001 - a count is display only
        return None


def _snapshots(registry: Registry, collection: str) -> list[SnapshotEntry]:
    entries: list[SnapshotEntry] = []
    for peer in registry.peers.peers:
        if not peer.reachable:
            registry.unreadable_listings.add(peer.index)
            continue
        try:
            listed = peer.client.list_snapshots(collection)
        except Exception:  # noqa: BLE001 - an unreadable listing blocks disposal
            registry.unreadable_listings.add(peer.index)
            continue
        for item in listed:
            entries.append(SnapshotEntry(
                peer=peer.index, collection=collection, name=item.name,
                size_bytes=item.size, creation_time=item.creation_time, checksum=item.checksum,
            ))
    return sorted(entries, key=lambda s: (s.peer, s.creation_time or "", s.name))


def read_registry(
    entry: QdrantSnapshotReader,
    settings: Settings,
    peers: PeerView,
    sidecar_state: str,
    sidecar: dict | None,
) -> Registry:
    """Every publication generation of the configured logical alias, read
    through the entry endpoint; snapshots through each direct peer."""
    alias = settings.qdrant_collection
    aliases = {d.alias_name: d.collection_name for d in entry.get_aliases().aliases}
    live, legacy = resolve_live_collection(entry, settings)
    names = {c.name for c in entry.get_collections().collections}
    registry = Registry(alias, live, live is not None and not legacy, [], [], peers, sidecar_state, sidecar)
    controls: set[str] = set()
    for name, target in aliases.items():
        if name.startswith(alias + "__build_") and is_build_alias(name) and name.endswith(_COMPLETIONS):
            controls.add(target)
    controls |= {n for n in names if n.startswith(alias + "__") and n.endswith(_COMPLETIONS)}
    if registry.live_resolved and live is not None:
        controls.add(completion_collection_for(live))
    managed: set[str] = set()
    for control in sorted(controls):
        physical = control.removesuffix(_COMPLETIONS)
        if control not in names:
            registry.unmanaged.append(Unmanaged(physical, "NO_CONTROL", _points(entry, physical)))
            managed.add(physical)
            continue
        try:
            payload = read_publication_record(entry, control)
        except RuntimeError:
            reason = "INVALID_RECEIPT"
            payload = None
        else:
            reason = "NO_RECEIPT"
        binding = decode_build_binding(payload, control) if payload is not None else None
        if binding is None or binding.alias != alias:
            reason = reason if binding is None else "OTHER_ALIAS"
            managed |= {physical, control}
            registry.unmanaged.append(Unmanaged(physical, reason, _points(entry, physical)))
            continue
        try:
            phase = build_phase(binding, aliases)
        except ValueError:
            phase = "inconsistent"
        is_live = registry.live_resolved and live == physical
        if phase == "published":
            phase = "live"
        if physical not in names:
            phase = "data-missing"
        # A tombstone decides the phase whatever the aliases say: between the
        # tombstone write and the alias delete both immutable aliases still
        # resolve, and such a build is no rollback point and no kept generation.
        tombstone: dict[str, Any] | None = None
        try:
            tombstone = read_tombstone(entry, control)
        except InvalidTombstone:
            phase = "tombstone-invalid"
        if tombstone is not None:
            if tombstone_matches(tombstone, binding):
                phase = tombstone["state"]
            else:
                phase, tombstone = "tombstone-invalid", None
        own = set(build_aliases(alias, binding.build_id))
        targeting = tuple(sorted(n for n, t in aliases.items() if t in (physical, control)))
        foreign = tuple(n for n in targeting if n not in own and not (n == alias and is_live))
        finished_at, control_points = _marker_summary(entry, control, settings.ingest_scan_page_size)
        generation = Generation(
            build_id=binding.build_id, physical=physical, control=control, phase=phase,
            live=is_live, build_schema=payload.get("build_schema") if payload else None,
            finished_at=finished_at,
            points=_points(entry, physical) if physical in names else None,
            control_points=control_points, aliases=targeting, foreign_aliases=foreign,
            sealed=payload is not None and "content_seal" in payload, tombstone=tombstone,
            data_present=physical in names,
        )
        if physical in names:
            generation.snapshots = _snapshots(registry, physical)
        generation.control_snapshots = _snapshots(registry, control)
        if sidecar is not None:
            if sidecar.get("staging") == physical:
                generation.sidecar_role = "staging"
            elif sidecar.get("previous") == physical:
                generation.sidecar_role = "previous"
        registry.generations.append(generation)
        managed |= {physical, control}
    for name in sorted(names - managed):
        if name == alias or name.startswith(alias + "_"):
            registry.unmanaged.append(Unmanaged(name, "NO_BUILD_BINDING", _points(entry, name)))
    return registry


def assess(registry: Registry, keep: int) -> None:
    """Rank retained generations newest first and set each generation's
    refusal reasons. Keep-N is a guard, never a selector."""
    retained = [g for g in registry.generations if g.phase == "retained"]
    ordered = sorted((g for g in retained if g.finished_at is not None),
                     key=lambda g: g.finished_at or 0.0, reverse=True)
    for rank, generation in enumerate(ordered, start=1):
        generation.rank = rank
    kept = {id(g) for g in ordered[:keep]}
    unordered = {id(g) for g in retained if g.finished_at is None}
    if ordered and len(ordered) > keep:
        boundary = ordered[keep - 1].finished_at
        unordered |= {id(g) for g in ordered[keep:] if g.finished_at == boundary}
    for g in registry.generations:
        reasons: list[str] = []
        if g.live:
            reasons.append("LIVE")
        elif g.phase != "retained":
            reasons.append("NOT_RETAINED")
        if not registry.live_resolved:
            reasons.append("LIVE_UNRESOLVED")
        if g.foreign_aliases:
            reasons.append("FOREIGN_ALIAS")
        if registry.sidecar_state == "not-read":
            reasons.append("SIDECAR_NOT_READ")
        elif registry.sidecar_state == "unreadable":
            reasons.append("SIDECAR_UNREADABLE")
        elif g.sidecar_role == "staging":
            reasons.append("SIDECAR_STAGING")
        elif g.sidecar_role == "previous":
            reasons.append("SIDECAR_PREVIOUS")
        if id(g) in kept:
            reasons.append("KEEP_N")
        if id(g) in unordered:
            reasons.append("UNORDERED")
        if not registry.snapshots_complete:
            reasons.append("PEERS_INCOMPLETE")
        g.reasons = reasons


# --- output ------------------------------------------------------------------


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def generation_json(g: Generation, labels: dict[int, str]) -> dict[str, Any]:
    return {
        "build_id": g.build_id, "physical": g.physical, "control": g.control, "phase": g.phase,
        "live": g.live, "build_schema": g.build_schema, "finished_at": _iso(g.finished_at),
        "rank": g.rank, "points": g.points, "control_points": g.control_points,
        "aliases": list(g.aliases), "sidecar": g.sidecar_role,
        "snapshots": [s.as_json(labels) for s in g.snapshots],
        "control_snapshots": [s.as_json(labels) for s in g.control_snapshots],
        "size": {
            "data_bytes": None, "data_source": DATA_SIZE_SOURCE,
            "snapshot_bytes": g.snapshot_bytes,
            "control_snapshot_bytes": sum(s.size_bytes or 0 for s in g.control_snapshots),
        },
        "disposable": g.disposable, "reasons": list(g.reasons),
        "tombstone": None if g.tombstone is None else {
            key: g.tombstone[key] for key in ("state", "created_at", "retired_at", "plan_digest",
                                              "recovery_ref", "operator", "readers_stopped_at")
        },
    }


def _labels(registry: Registry) -> dict[int, str]:
    return {p.index: p.label for p in registry.peers.peers}


def summary_json(registry: Registry, keep: int) -> dict[str, Any]:
    return {
        "alias": registry.alias, "live": registry.live if registry.live_resolved else None,
        "peers": [{"peer": p.index, "endpoint": p.label, "reachable": p.reachable}
                  for p in registry.peers.peers],
        "peers_complete": registry.snapshots_complete,
        "peers_problem": registry.peers.problem or ("LISTING_UNREADABLE" if registry.unreadable_listings else None),
        "sidecar": registry.sidecar_state, "keep": keep,
        "snapshot_bytes": sum(g.snapshot_bytes for g in registry.generations),
        "data_size": "Qdrant does not report collection disk bytes; snapshot sizes are exact",
        "view": "point-in-time; dispose-snapshots --execute recomputes under the publish lock",
    }


def emit_listing(registry: Registry, keep: int, fmt: str, out: Callable[[str], None]) -> None:
    labels = _labels(registry)
    order = sorted(registry.generations, key=lambda g: (not g.live, g.phase != "retained",
                                                         g.rank or 10**9, g.physical))
    if fmt == "json":
        for g in order:
            out(json.dumps({"generation": generation_json(g, labels)}, sort_keys=True))
        out(json.dumps({"not_managed": [{"collection": u.collection, "reason": u.reason, "points": u.points}
                                        for u in registry.unmanaged]}, sort_keys=True))
        out(json.dumps({"summary": summary_json(registry, keep)}, sort_keys=True))
        return
    summary = summary_json(registry, keep)
    peers = ", ".join(f"{p['peer']}={p['endpoint']}{'' if p['reachable'] else ' (unreachable)'}"
                      for p in summary["peers"])
    out(f"GENERATIONS alias {registry.alias}; live {summary['live']}; keep {keep}; "
        f"sidecar {registry.sidecar_state}; peers {peers}; "
        f"snapshot view {'complete' if registry.snapshots_complete else 'INCOMPLETE (' + str(summary['peers_problem']) + ')'}")
    for g in order:
        verdict = "disposable" if g.disposable else "protected " + ",".join(g.reasons)
        out(f"{g.phase.upper()} build {g.build_id} physical {g.physical} finished {_iso(g.finished_at)} "
            f"rank {g.rank} points {g.points} control_points {g.control_points} "
            f"snapshots {len(g.snapshots)} ({g.snapshot_bytes} bytes) data bytes not reported; {verdict}")
        if g.tombstone is not None:
            out(f"  TOMBSTONE {g.tombstone['state']} created {g.tombstone['created_at']} "
                f"retired {g.tombstone['retired_at']} plan {g.tombstone['plan_digest']}")
        for s in g.snapshots + g.control_snapshots:
            out(f"  SNAPSHOT peer {s.peer} ({labels.get(s.peer)}) {s.collection} {s.name} "
                f"{s.size_bytes} bytes created {s.creation_time}")
    for u in registry.unmanaged:
        out(f"NOT_MANAGED {u.collection} {u.reason} points {u.points} (never disposed by this tool)")
    out(f"TOTAL snapshot bytes of listed generations: {summary['snapshot_bytes']}. "
        "Collection disk bytes are not reported by Qdrant. Point-in-time view.")


# --- snapshot-only disposal --------------------------------------------------


def disposal_record_path(progress: Path, alias: str) -> Path:
    """Append-only audit record beside the publish lock. Audit only: a rerun
    converges by recomputing the plan and re-listing, never from this file."""
    lock = publish_lock_path(progress, alias)
    stem = lock.name.removeprefix("publish-").removesuffix(".lock")
    return lock.with_name(f"snapshot-disposal-{stem}.jsonl")


def _record(path: Path, event: dict[str, Any]) -> None:
    line = json.dumps({"at": _iso(datetime.now(UTC).timestamp()), **event}, sort_keys=True)
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise Unreadable("E_STATE_IO: the audit record beside the publish lock could not be "
                         "written; nothing more deleted") from None


def _listed_names(peer: Peer, collection: str) -> set[str]:
    try:
        return {s.name for s in peer.client.list_snapshots(collection)}
    except Exception:  # noqa: BLE001 - fixed text
        raise Unreadable(f"E_PEER_UNREADABLE: peer {peer.index} ({peer.label}) snapshot listing of "
                         f"{collection} could not be read; rerun when every peer answers") from None


def _recheck(entry: QdrantSnapshotReader, alias: str, g: Generation) -> None:
    """Immediately before a delete: the build is still retained under its own
    immutable aliases and nothing else names its collections."""
    try:
        aliases = {d.alias_name: d.collection_name for d in entry.get_aliases().aliases}
    except Exception:  # noqa: BLE001 - fixed text
        raise Unreadable("E_QDRANT_UNREADABLE: aliases could not be re-read; nothing more deleted") from None
    data_alias, control_alias = build_aliases(alias, g.build_id)
    targeting = {n for n, t in aliases.items() if t in (g.physical, g.control)}
    if (aliases.get(data_alias) != g.physical or aliases.get(control_alias) != g.control
            or aliases.get(alias) == g.physical or targeting - {data_alias, control_alias}):
        raise Refused(f"E_CHANGED: build {g.build_id} is no longer a retained generation with only its own "
                      "aliases; nothing more deleted, list again")


def delete_snapshot_on_peer(peer: Peer, collection: str, name: str) -> None:
    """Delete one snapshot through the peer that stores it. The outcome is
    decided by listing that peer again: a 404 also means 'not on this peer'
    or 'no such collection', so the status alone proves nothing."""
    admin = peer.client
    if not isinstance(admin, QdrantSnapshotAdmin):
        raise Refused("E_NO_WRITER: this client cannot delete snapshots")
    with suppress(Exception):  # the outcome is decided by the re-listing below
        admin.delete_snapshot(collection, name, wait=True)
    if name in _listed_names(peer, collection):
        raise Unreadable(f"E_SNAPSHOT_NOT_DELETED: {name} is still listed on peer {peer.index} "
                         f"({peer.label}); a delete needs the writer key; rerun to retry")


@contextmanager
def _publish_lock(progress: Path, alias: str) -> Iterator[None]:
    try:
        handle: IO[Any] = acquire_publish_lock(progress, alias)
    except RuntimeError:
        raise Refused(f"E_LOCK_REFUSED: the publish lock for {alias} was not taken: an ingest or "
                      "disposal holds it, or a legacy publish state must be resolved first; "
                      "nothing deleted") from None
    except OSError:
        raise Refused("E_STATE_IO: the publish lock beside the ingest inventory could not be "
                      "opened; nothing deleted") from None
    try:
        yield
    finally:
        release_run_lock(handle)


def _targets(registry: Registry, build_ids: Sequence[str]) -> list[Generation]:
    by_id: dict[str, list[Generation]] = {}
    for g in registry.generations:
        by_id.setdefault(g.build_id, []).append(g)
    chosen: list[Generation] = []
    refusals: list[str] = []
    for build_id in build_ids:
        found = by_id.get(build_id, [])
        if len(found) != 1:
            raise Refused(f"E_UNKNOWN_BUILD: build {build_id} is not exactly one publication generation "
                          f"of {registry.alias}; nothing deleted")
        if found[0].reasons:
            refusals.append(f"{build_id} ({','.join(found[0].reasons)})")
        chosen.append(found[0])
    if refusals:
        raise Refused("E_NOT_DISPOSABLE: " + "; ".join(refusals) + "; nothing deleted")
    return chosen


def dispose_snapshots(
    entry: QdrantSnapshotReader,
    settings: Settings,
    connect: Callable[[str], QdrantSnapshotReader],
    *,
    progress: Path,
    build_ids: Sequence[str],
    keep: int,
    execute: bool,
    recovery_ref: str | None,
    out: Callable[[str], None],
    operator: str | None = None,
) -> None:
    alias = settings.qdrant_collection
    with ExitStack() as stack:
        if execute:
            stack.enter_context(_publish_lock(progress, alias))
        sidecar_state, sidecar = read_sidecar(progress, alias)
        registry = read_registry(entry, settings, read_peer_view(settings, entry, connect),
                                 sidecar_state, sidecar)
        assess(registry, keep)
        targets = _targets(registry, build_ids)
        peers = {p.index: p for p in registry.peers.peers}
        labels = _labels(registry)
        total = 0
        for g in targets:
            out(f"PLAN build {g.build_id} physical {g.physical} finished {_iso(g.finished_at)} "
                f"rank {g.rank}: {len(g.snapshots)} snapshot(s), {g.snapshot_bytes} bytes; "
                "collections and aliases are kept")
            for s in g.snapshots:
                out(f"  DELETE peer {s.peer} ({labels.get(s.peer)}) {s.collection} {s.name} "
                    f"{s.size_bytes} bytes created {s.creation_time}")
            total += g.snapshot_bytes
        out(f"PLAN total {sum(len(g.snapshots) for g in targets)} snapshot(s), {total} bytes, keep {keep}")
        if not execute:
            out("DRY RUN: nothing deleted. Rerun with --execute (optionally --recovery-ref <ticket>) to "
                "delete exactly what each peer lists then.")
            return
        record = disposal_record_path(progress, alias)
        planned: dict[str, Any] = {
            "event": "planned", "alias": alias, "keep": keep, "operator": operator or default_operator(),
            "builds": [{"build_id": g.build_id, "physical": g.physical,
                        "snapshots": [s.as_json(labels) for s in g.snapshots]} for g in targets],
        }
        if recovery_ref is not None:
            planned["recovery_ref"] = recovery_ref
        _record(record, planned)
        deleted = 0
        for g in targets:
            for s in g.snapshots:
                _recheck(entry, alias, g)
                _record(record, {"event": "deleting", "build_id": g.build_id, "peer": s.peer,
                                 "collection": s.collection, "snapshot": s.name})
                delete_snapshot_on_peer(peers[s.peer], s.collection, s.name)
                deleted += 1
                _record(record, {"event": "deleted", "build_id": g.build_id, "peer": s.peer,
                                 "collection": s.collection, "snapshot": s.name, "size_bytes": s.size_bytes})
                out(f"DELETED peer {s.peer} ({labels.get(s.peer)}) {s.collection} {s.name}")
        for g in targets:
            for peer in registry.peers.peers:
                left = _listed_names(peer, g.physical)
                if left:
                    raise Refused(f"E_NOT_CONVERGED: peer {peer.index} ({peer.label}) still lists "
                                  f"{len(left)} snapshot(s) of {g.physical}; list again and rerun")
        _record(record, {"event": "done", "deleted": deleted})
        out(f"DONE deleted {deleted} snapshot(s); no snapshot of the named builds remains on any peer. "
            f"Record: {record.name} beside the publish lock")


# --- entry -------------------------------------------------------------------


def _client(url: str, settings: Settings) -> QdrantSnapshotAdmin:
    from qdrant_client import QdrantClient

    return QdrantClient(url=url, api_key=settings.qdrant_api_key, timeout=settings.qdrant_ingest_timeout_s,
                        prefer_grpc=False, limits=qdrant_pool_limits(url))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mainframe_rag.ingest.generations",
        description="List retained publication generations; delete snapshots of named retained builds.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                        help="Newest retained generations whose snapshots are protected (>= 1).")
    listing = sub.add_parser("list", parents=[common], help="Read-only, lock-free listing.")
    listing.add_argument("--format", choices=("text", "json"), default="text")
    listing.add_argument("--progress", type=Path, default=None,
                         help="Ingest inventory path; its directory holds the publish sidecar.")
    dispose = sub.add_parser("dispose-snapshots", parents=[common],
                             help="Delete snapshots of named retained builds (dry run by default).")
    dispose.add_argument("--progress", type=Path, required=True)
    dispose.add_argument("--build-id", action="append", required=True, dest="build_ids")
    dispose.add_argument("--allow-below-keep", action="store_true",
                         help="Lower the keep guard to the floor of 1 for this run.")
    dispose.add_argument("--execute", action="store_true")
    dispose.add_argument("--recovery-ref", default=None,
                         help="Optional ticket or decision id recorded in the audit record with --execute.")
    dispose.add_argument("--operator", default=None,
                         help="Who runs it, for the audit record (default: the login name, else the uid).")
    data = sub.add_parser("dispose", parents=[common],
                          help="Dispose of one superseded build's data (dry run by default; "
                               "--execute is refused until readers refuse tombstoned builds).")
    data.add_argument("--progress", type=Path, required=True)
    data.add_argument("--build-id", action="append", required=True, dest="build_ids",
                      help="Exactly one full build UUID per run.")
    data.add_argument("--allow-below-keep", action="store_true",
                      help="Lower the keep guard to the floor of 1 for this run.")
    data.add_argument("--execute", action="store_true")
    data.add_argument("--plan-digest", default=None,
                      help="The digest the dry run printed; required with --execute.")
    data.add_argument("--readers-stopped", default=None,
                      help="UTC time (YYYY-MM-DDTHH:MM:SSZ) at which every reader was confirmed stopped; "
                           "required with --execute and recorded in the tombstone.")
    data.add_argument("--recovery-ref", default=None)
    data.add_argument("--operator", default=None)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    connect: Callable[[str], QdrantSnapshotReader] | None = None,
    out: Callable[[str], None] = print,
) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "dispose" and args.execute:
            # Before the settings, any connection and the lock: this release cannot execute.
            from mainframe_rag.ingest.disposal import require_reader_awareness

            require_reader_awareness()
        if args.keep < KEEP_FLOOR:
            raise Refused(f"E_KEEP_FLOOR: --keep must be at least {KEEP_FLOOR}; keeping 0 is not supported")
        if settings is None:
            try:
                settings = Settings()
            except Exception:  # noqa: BLE001 - configuration text may carry values
                raise Refused("E_CONFIG: the Qdrant settings in the environment are invalid") from None
        if args.progress is not None and not args.progress.is_file():
            raise Refused("E_PROGRESS: --progress must name the ingest inventory file that exists; "
                          "its directory holds the publish lock and sidecar")
        connector = connect or (lambda url: _client(url, settings))
        with ExitStack() as stack:
            made: list[Any] = []

            def tracked(url: str) -> QdrantSnapshotReader:
                client = connector(url)
                made.append(client)
                return client

            stack.callback(lambda: [getattr(c, "close", lambda: None)() for c in made])
            entry = tracked(settings.qdrant_url)
            if args.command == "list":
                sidecar_state, sidecar = read_sidecar(args.progress, settings.qdrant_collection)
                registry = read_registry(entry, settings, read_peer_view(settings, entry, tracked),
                                         sidecar_state, sidecar)
                assess(registry, args.keep)
                emit_listing(registry, args.keep, args.format, out)
                return EXIT_OK
            build_ids: list[str] = []
            if args.command == "dispose" and len(args.build_ids) != 1:
                raise Refused("E_BUILD_ID: a data disposal takes exactly one --build-id per run")
            for value in args.build_ids:
                try:
                    canonical = canonical_build_id(value)
                except (TypeError, ValueError):
                    raise Refused("E_BUILD_ID: --build-id takes a full canonical build UUID") from None
                if canonical in build_ids:
                    raise Refused(f"E_BUILD_ID: build {canonical} is named twice")
                build_ids.append(canonical)
            if args.recovery_ref is not None and not _RECOVERY_REF.fullmatch(args.recovery_ref):
                raise Refused("E_RECOVERY_REF: --recovery-ref takes a ticket or decision id "
                              "([A-Za-z0-9._:/#-], at most 128 characters)")
            if args.operator is not None and not _OPERATOR.fullmatch(args.operator):
                raise Refused("E_OPERATOR: --operator takes a login name ([A-Za-z0-9._@-], at most 64 characters)")
            keep = KEEP_FLOOR if args.allow_below_keep else args.keep
            if args.command == "dispose":
                from mainframe_rag.ingest.disposal import dispose_data, parse_dispose_inputs

                digest, stopped = parse_dispose_inputs(args.execute, args.plan_digest, args.readers_stopped)
                dispose_data(entry, settings, tracked, progress=args.progress, build_id=build_ids[0],
                             keep=keep, below_keep=args.allow_below_keep, execute=args.execute,
                             plan_digest=digest, readers_stopped=stopped,
                             recovery_ref=args.recovery_ref, operator=args.operator, out=out)
                return EXIT_OK
            dispose_snapshots(entry, settings, tracked, progress=args.progress, build_ids=build_ids,
                              keep=keep, execute=args.execute, recovery_ref=args.recovery_ref, out=out,
                              operator=args.operator)
            return EXIT_OK
    except Refused as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except Unreadable as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    except Exception as exc:  # noqa: BLE001 - never echo upstream text
        print(f"FAIL: E_QDRANT_UNREADABLE: Qdrant could not be read ({type(exc).__name__}); "
              "nothing more deleted", file=sys.stderr)
        return EXIT_UNREADABLE


if __name__ == "__main__":
    sys.exit(main())
