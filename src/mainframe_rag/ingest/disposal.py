"""Data disposal of one superseded publication generation (issue #1373, PR 2).

The unit is the data collection of a named build, its data build alias and
every snapshot of that collection on every peer. The `__completions`
control collection (markers, manifest, receipt, catalog entries), the control
build alias and a retirement tombstone stay. The serving alias, the live
pair, the publish sidecar, the lock and the inventory are never touched.

Not usable from this release: `--execute` is refused while
`build.READERS_REFUSE_TOMBSTONED_BUILDS` is False (set only in code, by the
change that makes the agent refuse a tombstoned build), and the site
launcher refuses the subcommand by name. The dry run works and prints the
plan and its digest.

Steps, all under the publish lock; the only resume evidence is the tombstone:

  S0  read-only: sidecar, every peer, registry, eligibility, the kept set's
      seal and catalog checks, plan digest recomputed and compared
  S1  tombstone `retiring` written into the control collection, read back
  S2  aliases re-read, the data build alias deleted, absence read back
  S3  every snapshot of the data collection deleted through the peer that
      stores it; then every peer must list none (a collection delete would
      orphan the files: they are listed again if the name is ever recreated)
  S4  the data collection deleted; absence confirmed through every peer
  S5  tombstone `retired`, read back

A rerun after a stop at any point reads the tombstone and continues: each
step first reads the stored state it would produce. Once a valid `retiring`
tombstone exists the rerun is gated only by delete-safety (live target,
foreign alias, sidecar, every peer answering, the lock): keep-N and the
kept set's seal are plan-time checks and never strand a started disposal.
While one build is `retiring` no other build can be disposed of.

Output is fixed text plus ids, names, peer host:port, counts, sizes, times
and digests. Owner contract: docs/ingest.md (Rollback / disposal).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from qdrant_client import models

from mainframe_rag.config import Settings
from mainframe_rag.ingest import build
from mainframe_rag.ingest.build import build_aliases
from mainframe_rag.ingest.generation_errors import Refused, Unreadable
from mainframe_rag.ingest.generations import (
    Generation,
    Peer,
    Registry,
    SnapshotEntry,
    _iso,
    _labels,
    _publish_lock,
    _record,
    assess,
    default_operator,
    delete_snapshot_on_peer,
    publish_lock_path,
    read_peer_view,
    read_registry,
    read_sidecar,
    require_bound_inventory,
)
from mainframe_rag.ingest.publish import (
    publication_metadata_point_id,
    verify_build_catalog,
    verify_publication_seal,
)
from mainframe_rag.ingest.retirement import (
    STATE_RETIRED,
    STATE_RETIRING,
    TOMBSTONE_RECORD_TYPE,
    TOMBSTONE_SCHEMA,
    InvalidTombstone,
    read_tombstone,
    tombstone_point_id,
)
from mainframe_rag.ports import QdrantGenerationAdmin, QdrantSnapshotReader

PLAN_SCHEMA = 1
_DIGEST = re.compile(r"[0-9a-f]{64}")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_CLOCK_SKEW = timedelta(minutes=5)
_ABSENCE_WAIT_S = 30.0


def require_reader_awareness() -> None:
    """Executing a data disposal needs readers that refuse a tombstoned build
    (see `build.READERS_REFUSE_TOMBSTONED_BUILDS`). Read at call time; no
    flag, environment variable or file changes the answer."""
    if build.READERS_REFUSE_TOMBSTONED_BUILDS is not True:
        raise Refused("E_READERS_UNAWARE: data disposal cannot be executed by this release: the agent "
                      "does not yet refuse a build that is being retired; nothing read, nothing changed")


def parse_dispose_inputs(execute: bool, plan_digest: str | None, readers_stopped: str | None) -> tuple[
        str | None, str | None]:
    if plan_digest is not None and not _DIGEST.fullmatch(plan_digest):
        raise Refused("E_PLAN_DIGEST: --plan-digest takes the 64-character digest the dry run printed")
    if readers_stopped is not None:
        if not _UTC.fullmatch(readers_stopped):
            raise Refused("E_READERS_STOPPED: --readers-stopped takes a UTC time YYYY-MM-DDTHH:MM:SSZ")
        try:
            when = datetime.strptime(readers_stopped, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except ValueError:
            raise Refused("E_READERS_STOPPED: --readers-stopped takes a UTC time YYYY-MM-DDTHH:MM:SSZ") from None
        if when > datetime.now(UTC) + _CLOCK_SKEW:
            raise Refused("E_READERS_STOPPED: --readers-stopped is in the future")
    if execute and plan_digest is None:
        raise Refused("E_PLAN_DIGEST: --execute needs --plan-digest, the digest the dry run printed")
    if execute and readers_stopped is None:
        raise Refused("E_READERS_STOPPED: --execute needs --readers-stopped, the UTC time at which every "
                      "reader (the agent) was confirmed stopped; nothing changed")
    return plan_digest, readers_stopped


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _snapshot_record(s: SnapshotEntry, labels: dict[int, str]) -> dict[str, Any]:
    return {"peer": labels.get(s.peer), "name": s.name, "size": s.size_bytes,
            "checksum": s.checksum, "creation_time": s.creation_time}


def _sorted_snapshots(snapshots: list[SnapshotEntry], labels: dict[int, str]) -> list[dict[str, Any]]:
    return sorted((_snapshot_record(s, labels) for s in snapshots),
                  key=lambda r: (r["peer"] or "", r["name"]))


# --- keep guard --------------------------------------------------------------


@dataclass(frozen=True)
class KeepGuard:
    """The newest retained generations a data disposal must leave in place.
    Computed from the registry alone, never from the named target: keep-N
    refuses, it never selects. `protected` is the newest-first prefix of the
    ordered retained generations up to and including the Nth sealed one; a
    build without a content seal (schema 1) is protected inside that prefix
    but does not count toward N. `kept` are the N sealed ones (fewer when
    there are not N: then every retained generation is protected)."""

    kept: tuple[Generation, ...]
    protected: frozenset[str]
    boundary: float | None
    satisfied: bool


def keep_guard(registry: Registry, keep: int) -> KeepGuard:
    ordered = sorted((g for g in registry.generations if g.phase == "retained" and g.finished_at is not None),
                     key=lambda g: g.finished_at or 0.0, reverse=True)
    kept: list[Generation] = []
    protected: list[Generation] = []
    for g in ordered:
        if len(kept) == keep:
            break
        protected.append(g)
        if g.sealed:
            kept.append(g)
    return KeepGuard(tuple(kept), frozenset(g.build_id for g in protected),
                     protected[-1].finished_at if protected else None, len(kept) == keep)


def _target_reasons(registry: Registry, g: Generation, guard: KeepGuard) -> list[str]:
    reasons = [r for r in g.reasons if r not in ("KEEP_N", "UNORDERED")]
    if g.phase == "retained":
        if g.build_id in guard.protected or not guard.satisfied:
            reasons.append("KEEP_N")
        elif g.finished_at is None or g.finished_at == guard.boundary:
            reasons.append("UNORDERED")
    return reasons


def _verify_kept(entry: QdrantSnapshotReader, settings: Settings, guard: KeepGuard) -> None:
    """Each kept generation must still hold exactly the content its receipt
    sealed, and a schema-3 build its source catalog, or the guard protects
    nothing. One full scan with vectors per kept generation."""
    for g in guard.kept:
        try:
            sealed = verify_publication_seal(entry, g.control, page_size=settings.ingest_scan_page_size)
            catalog = (g.build_schema or 0) < 3 or verify_build_catalog(
                entry, g.control, page_size=settings.ingest_scan_page_size)
        except Exception:  # noqa: BLE001 - fixed text; a kept point that cannot be verified refuses
            sealed = catalog = False
        if not (sealed and catalog):
            raise Refused(f"E_KEPT_UNVERIFIED: kept build {g.build_id} does not verify against its content "
                          "seal or source catalog; it is no rollback point, nothing changed")


# --- the run -----------------------------------------------------------------


@dataclass
class Run:
    entry: QdrantGenerationAdmin
    alias: str
    progress: Path
    registry: Registry
    target: Generation
    snapshots: list[SnapshotEntry]
    tombstone: dict[str, Any]
    out: Callable[[str], None]
    completion: dict[str, Any]

    @property
    def peers(self) -> tuple[Peer, ...]:
        return self.registry.peers.peers


def _aliases(entry: QdrantSnapshotReader) -> dict[str, str]:
    try:
        return {d.alias_name: d.collection_name for d in entry.get_aliases().aliases}
    except Exception:  # noqa: BLE001 - fixed text
        raise Unreadable("E_QDRANT_UNREADABLE: aliases could not be re-read; nothing more changed") from None


def delete_safe(run: Run) -> dict[str, str]:
    """Immediately before each destructive call: the data collection is not
    the serving target, the serving alias resolves elsewhere, nothing but the
    build's own immutable aliases names the pair, the control alias still
    names the control collection, the sidecar does not name the collection,
    and the stored tombstone is this build's `retiring` record."""
    g = run.target
    aliases = _aliases(run.entry)
    data_alias, control_alias = build_aliases(run.alias, g.build_id)
    live = aliases.get(run.alias)
    if live is None or live == g.physical:
        raise Refused(f"E_LIVE: {g.physical} is the serving target or the serving alias does not resolve; "
                      "nothing more changed")
    targeting = {n for n, t in aliases.items() if t in (g.physical, g.control)}
    if (aliases.get(control_alias) != g.control or aliases.get(data_alias, g.physical) != g.physical
            or targeting - {data_alias, control_alias}):
        raise Refused(f"E_CHANGED: the aliases of build {g.build_id} changed; nothing more changed, list again")
    state, sidecar = read_sidecar(run.progress, run.alias)
    if state == "unreadable" or (sidecar is not None and g.physical in (sidecar.get("staging"),
                                                                       sidecar.get("previous"))):
        raise Refused(f"E_SIDECAR: the publish sidecar is unreadable or names {g.physical}; nothing more changed")
    try:
        stored = read_tombstone(run.entry, g.control)
    except InvalidTombstone:
        stored = None
    if stored is None or stored["state"] != STATE_RETIRING or stored["build_id"] != g.build_id:
        raise Refused(f"E_TOMBSTONE: no retiring tombstone of build {g.build_id} is stored in {g.control}; "
                      "nothing deleted without it")
    return aliases


def _put_tombstone(run: Run, payload: dict[str, Any]) -> None:
    control = run.target.control
    try:
        receipt = run.entry.retrieve(control, ids=[publication_metadata_point_id(control)],
                                     with_payload=False, with_vectors=True)
        vector: Any = receipt[0].vector if receipt else None
        if not isinstance(vector, dict) or not vector:
            raise ValueError("receipt vector")
        run.entry.upsert(control, points=[models.PointStruct(
            id=tombstone_point_id(control), vector=vector, payload=payload)], wait=True)
        stored = read_tombstone(run.entry, control)
    except Exception:  # noqa: BLE001 - fixed text
        raise Unreadable(f"E_TOMBSTONE_WRITE: the tombstone in {control} could not be written and read back; "
                         "nothing deleted after this point, rerun") from None
    if stored != payload:
        raise Unreadable(f"E_TOMBSTONE_WRITE: the tombstone read back from {control} differs from the one "
                         "written; nothing deleted after this point")


def step_tombstone(run: Run) -> None:
    """S1. Skipped when this build's `retiring` tombstone is already stored."""
    stored = read_tombstone(run.entry, run.target.control)
    if stored is not None:
        return
    _put_tombstone(run, run.tombstone)
    run.out(f"S1 tombstone retiring written to {run.target.control} and read back")


def step_alias(run: Run) -> None:
    """S2. The data build alias goes in one alias call; a missing alias is done."""
    data_alias, _ = build_aliases(run.alias, run.target.build_id)
    aliases = delete_safe(run)
    if data_alias in aliases:
        run.entry.update_collection_aliases([
            models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=data_alias))])
    if data_alias in _aliases(run.entry):
        raise Unreadable(f"E_ALIAS_NOT_DELETED: {data_alias} is still present; nothing more changed, rerun")
    run.out(f"S2 data build alias {data_alias} is absent")


def _exists(peer: Peer, collection: str) -> bool:
    try:
        return bool(peer.client.collection_exists(collection))
    except Exception:  # noqa: BLE001 - fixed text
        raise Unreadable(f"E_PEER_UNREADABLE: peer {peer.index} ({peer.label}) could not be asked whether "
                         f"{collection} exists; rerun when every peer answers") from None


def _listed(peer: Peer, collection: str) -> list[Any]:
    try:
        return list(peer.client.list_snapshots(collection))
    except Exception:  # noqa: BLE001 - fixed text
        raise Unreadable(f"E_PEER_UNREADABLE: peer {peer.index} ({peer.label}) snapshot listing of "
                         f"{collection} could not be read; rerun when every peer answers") from None


def step_snapshots(run: Run) -> None:
    """S3. Delete the planned snapshots through the peer that stores each;
    a snapshot already gone is done. Then every peer must list none."""
    g = run.target
    if not _exists(run.peers[0], g.physical):
        return  # the collection is gone: its snapshots were verified absent before it went
    by_index = {p.index: p for p in run.peers}
    labels = _labels(run.registry)
    for s in run.snapshots:
        delete_safe(run)
        delete_snapshot_on_peer(by_index[s.peer], s.collection, s.name)
        run.out(f"S3 deleted peer {s.peer} ({labels.get(s.peer)}) {s.collection} {s.name}")
    snapshots_absent(run)
    run.out(f"S3 no snapshot of {g.physical} is listed on any of {len(run.peers)} peer(s)")


def snapshots_absent(run: Run) -> None:
    for peer in run.peers:
        left = _listed(peer, run.target.physical)
        if left:
            raise Refused(f"E_NOT_CONVERGED: peer {peer.index} ({peer.label}) still lists {len(left)} "
                          f"snapshot(s) of {run.target.physical}; the collection is kept, run the dry run again")


def step_collection(run: Run) -> None:
    """S4. The collection goes only after every peer lists no snapshot of it,
    and is then confirmed absent through every peer."""
    g = run.target
    if _exists(run.peers[0], g.physical):
        delete_safe(run)
        snapshots_absent(run)
        run.entry.delete_collection(g.physical)
    for peer in run.peers:
        # The delete is a consensus operation: another peer may apply it a moment later.
        deadline = time.monotonic() + _ABSENCE_WAIT_S
        while _exists(peer, g.physical) and time.monotonic() < deadline:
            time.sleep(0.5)
        if _exists(peer, g.physical):
            raise Unreadable(f"E_COLLECTION_NOT_DELETED: peer {peer.index} ({peer.label}) still has "
                             f"{g.physical}; rerun")
    run.out(f"S4 data collection {g.physical} is absent on {len(run.peers)} peer(s)")


def step_retired(run: Run) -> None:
    """S5. The only rewrite a tombstone ever gets."""
    stored = read_tombstone(run.entry, run.target.control)
    if stored is None or stored["build_id"] != run.target.build_id:
        raise Refused(f"E_TOMBSTONE: no tombstone of build {run.target.build_id} is stored")
    if stored["state"] == STATE_RETIRED:
        return
    _put_tombstone(run, {**stored, "state": STATE_RETIRED, "retired_at": _iso(datetime.now(UTC).timestamp()),
                         "completion": run.completion})
    run.out(f"S5 tombstone retired written to {run.target.control} and read back")


def data_disposal_record_path(progress: Path, alias: str) -> Path:
    """Append-only audit record beside the publish lock: one line when an
    executing run starts or resumes (who, the reader-stop attestation, the
    digest, any kept build that no longer verifies) and one when it ends.
    Audit only: a rerun converges from the tombstone, never from this file."""
    lock = publish_lock_path(progress, alias)
    stem = lock.name.removeprefix("publish-").removesuffix(".lock")
    return lock.with_name(f"data-disposal-{stem}.jsonl")


def kept_unverified(entry: QdrantSnapshotReader, settings: Settings, registry: Registry,
                    tombstone: dict[str, Any]) -> list[str]:
    """The kept builds the tombstone recorded at plan time that are no longer
    retained generations verifying against their seal and catalog. A resume
    finishes regardless (the tombstoned build is no rollback point either
    way); this is what it warns about and records."""
    failed: list[str] = []
    by_id = {g.build_id: g for g in registry.generations}
    for build_id in tombstone["keep"]["kept_build_ids"]:
        g = by_id.get(build_id)
        ok = g is not None and g.phase == "retained"
        if ok and g is not None:
            try:
                ok = verify_publication_seal(entry, g.control, page_size=settings.ingest_scan_page_size) and (
                    (g.build_schema or 0) < 3
                    or verify_build_catalog(entry, g.control, page_size=settings.ingest_scan_page_size))
            except Exception:  # noqa: BLE001 - reported with fixed text
                ok = False
        if not ok:
            failed.append(build_id)
    return failed


def _resume_digest(tombstone: dict[str, Any], unrecorded: list[dict[str, Any]]) -> str:
    """The digest a rerun of a `retiring` build takes: the recorded plan
    digest while every listed snapshot is in the tombstone's record;
    otherwise a digest that also covers the unrecorded snapshots, printed by
    the dry run, so deleting them is confirmed by whoever read that list."""
    if not unrecorded:
        return str(tombstone["plan_digest"])
    return _digest({"plan_schema": PLAN_SCHEMA, "resume_of": tombstone["plan_digest"],
                    "unrecorded_snapshots": unrecorded})


def _find(registry: Registry, build_id: str) -> Generation:
    found = [g for g in registry.generations if g.build_id == build_id]
    if len(found) != 1:
        raise Refused(f"E_UNKNOWN_BUILD: build {build_id} is not exactly one publication generation "
                      f"of {registry.alias}; nothing changed")
    return found[0]


def dispose_data(
    entry: QdrantSnapshotReader,
    settings: Settings,
    connect: Callable[[str], QdrantSnapshotReader],
    *,
    progress: Path,
    build_id: str,
    keep: int,
    below_keep: bool,
    execute: bool,
    plan_digest: str | None,
    readers_stopped: str | None,
    recovery_ref: str | None,
    operator: str | None,
    out: Callable[[str], None],
) -> None:
    if execute:
        require_reader_awareness()
        if plan_digest is None or readers_stopped is None:
            raise Refused("E_PLAN_DIGEST: --execute needs --plan-digest and --readers-stopped")
    alias = settings.qdrant_collection
    require_bound_inventory(progress, alias)
    with ExitStack() as stack:
        if execute:
            stack.enter_context(_publish_lock(progress, alias))
        sidecar_state, sidecar = read_sidecar(progress, alias)
        registry = read_registry(entry, settings, read_peer_view(settings, entry, connect),
                                 sidecar_state, sidecar)
        assess(registry, keep)
        g = _find(registry, build_id)
        unverified: list[str] = []
        resumed = False
        labels = _labels(registry)
        pending = [o.build_id for o in registry.generations if o.phase == STATE_RETIRING and o is not g]
        if pending:
            raise Refused(f"E_RETIRING_PENDING: build {pending[0]} is left in retiring; finish it first "
                          "(rerun its disposal); nothing changed")
        if g.phase == "tombstone-invalid":
            raise Refused(f"E_TOMBSTONE: the tombstone in {g.control} is not a valid record of build "
                          f"{g.build_id}; nothing changed")
        if g.phase == STATE_RETIRED:
            data_alias, _ = build_aliases(alias, g.build_id)
            if g.data_present or data_alias in g.aliases or not registry.snapshots_complete:
                raise Refused(f"E_INCONSISTENT: build {g.build_id} is recorded retired but its data collection "
                              "or data alias is present, or a peer did not answer; nothing changed")
            out(f"ALREADY RETIRED build {g.build_id} physical {g.physical} retired "
                f"{(g.tombstone or {}).get('retired_at')}; nothing to do")
            return
        if not registry.snapshots_complete:
            raise Refused(f"E_PEERS_INCOMPLETE: not every Qdrant peer answered "
                          f"({registry.peers.problem or 'LISTING_UNREADABLE'}); nothing changed")
        if g.phase == STATE_RETIRING:
            tombstone = dict(g.tombstone or {})
            blocking = [r for r in g.reasons if r in ("LIVE", "LIVE_UNRESOLVED", "FOREIGN_ALIAS",
                                                      "SIDECAR_UNREADABLE", "SIDECAR_STAGING", "SIDECAR_PREVIOUS")]
            if blocking:
                raise Refused(f"E_NOT_DISPOSABLE: {build_id} ({','.join(blocking)}); nothing more changed")
            recorded = {s["name"] for s in tombstone["snapshots"]}
            unrecorded = [r for r in _sorted_snapshots(g.snapshots, labels) if r["name"] not in recorded]
            digest = _resume_digest(tombstone, unrecorded)
            out(f"RESUME build {g.build_id} physical {g.physical}: tombstone retiring since "
                f"{tombstone['created_at']}; data collection {'present' if g.data_present else 'absent'}; "
                f"{len(g.snapshots)} snapshot(s) still listed")
            resumed = True
            unverified = kept_unverified(entry, settings, registry, tombstone)
            for failed in unverified:
                out(f"WARNING: W_KEPT_UNVERIFIED: kept build {failed}, recorded when this disposal was planned, "
                    "is no longer a retained build that verifies against its seal; the disposal still "
                    "finishes (a tombstoned build is no rollback point), check the remaining rollback points")
            for r in unrecorded:
                out(f"  UNRECORDED snapshot peer {r['peer']} {r['name']} {r['size']} bytes created "
                    f"{r['creation_time']} (made after the plan; deleted only under the digest below)")
        else:
            guard = keep_guard(registry, keep)
            reasons = _target_reasons(registry, g, guard)
            if reasons:
                raise Refused(f"E_NOT_DISPOSABLE: {build_id} ({','.join(reasons)}); nothing changed")
            _verify_kept(entry, settings, guard)
            kept_ids = sorted(k.build_id for k in guard.kept)
            plan = {
                "plan_schema": PLAN_SCHEMA, "operation": "dispose-data", "alias": alias, "live": registry.live,
                "build_id": g.build_id, "physical": g.physical, "control": g.control, "data_points": g.points,
                "snapshots": _sorted_snapshots(g.snapshots, labels),
                "keep": {"n": keep, "kept_build_ids": kept_ids, "below_keep_override": below_keep},
            }
            digest = _digest(plan)
            receipt = _receipt(entry, g.control)
            tombstone = {
                "record_type": TOMBSTONE_RECORD_TYPE, "tombstone_schema": TOMBSTONE_SCHEMA,
                "target_collection": g.control, "build_id": g.build_id, "logical_alias": alias,
                "data_collection": g.physical, "gen_fp": receipt["gen_fp"], "corpus_fp": receipt["corpus_fp"],
                "state": STATE_RETIRING, "content_seal": receipt.get("content_seal"),
                "source_catalog": receipt.get("source_catalog"), "data_points": g.points,
                "snapshots": plan["snapshots"], "keep": plan["keep"],
                "readers_stopped_at": readers_stopped, "recovery_ref": recovery_ref,
                "operator": operator or default_operator(), "plan_digest": digest,  # who planned and started it
                "tool_release": os.environ.get("IMAGE_SHA", "").strip() or None,
                "created_at": _iso(datetime.now(UTC).timestamp()), "retired_at": None, "completion": None,
            }
            out(f"PLAN dispose build {g.build_id} physical {g.physical} finished {_iso(g.finished_at)} "
                f"rank {g.rank} points {g.points}: delete data build alias, {len(g.snapshots)} snapshot(s) "
                f"({g.snapshot_bytes} bytes), the data collection; keep {g.control}, its control alias and "
                "a tombstone")
            for s in g.snapshots:
                out(f"  DELETE snapshot peer {s.peer} ({labels.get(s.peer)}) {s.name} {s.size_bytes} bytes "
                    f"created {s.creation_time}")
            out(f"PLAN live {registry.live}; keep {keep}{' (lowered by --allow-below-keep)' if below_keep else ''}; "
                f"kept and verified: {', '.join(kept_ids)}")
        out(f"PLAN DIGEST {digest}")
        if not execute:
            out("DRY RUN: nothing changed. This release cannot execute a data disposal "
                "(readers do not yet refuse a build that is being retired)."
                if build.READERS_REFUSE_TOMBSTONED_BUILDS is not True else
                "DRY RUN: nothing changed. With every reader stopped, rerun with --execute "
                "--plan-digest <digest above> --readers-stopped <UTC time>.")
            return
        if plan_digest != digest:
            raise Refused("E_PLAN_CHANGED: --plan-digest does not match the plan recomputed under the lock; "
                          "nothing changed by this run, read the dry run again")
        if not isinstance(entry, QdrantGenerationAdmin):
            raise Refused("E_NO_WRITER: this client cannot write the tombstone or delete")
        who = operator or default_operator()
        completion = {"resumed": resumed, "readers_stopped_at": readers_stopped, "operator": who,
                      "recovery_ref": recovery_ref, "kept_unverified": unverified}
        record = data_disposal_record_path(progress, alias)
        _record(record, {"event": "resumed" if resumed else "started", "alias": alias, "build_id": g.build_id,
                         "physical": g.physical, "plan_digest": digest, **completion})
        run = Run(entry, alias, progress, registry, g, list(g.snapshots), tombstone, out, completion)
        step_tombstone(run)
        step_alias(run)
        step_snapshots(run)
        step_collection(run)
        step_retired(run)
        _record(record, {"event": "retired", "build_id": g.build_id, "physical": g.physical})
        out(f"DONE build {g.build_id} is retired: {g.physical}, its data build alias and its snapshots are "
            f"gone; {g.control}, its control alias and the tombstone are kept")


def _receipt(entry: QdrantSnapshotReader, control: str) -> dict[str, Any]:
    from mainframe_rag.ingest.publish import read_publication_record

    try:
        payload = read_publication_record(entry, control)
    except Exception:  # noqa: BLE001 - fixed text
        payload = None
    if payload is None:
        raise Refused(f"E_QDRANT_UNREADABLE: the publication receipt in {control} could not be read; "
                      "nothing changed")
    return payload
