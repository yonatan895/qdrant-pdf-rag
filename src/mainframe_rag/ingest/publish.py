"""Alias publication for ingest generations (issue #359 req 4/5).

Readers must see a complete old or a complete new generation, never an
uncommitted mix. With `INGEST_ALIAS_PUBLISH=true`, ingest converges a
versioned staging collection — snapshot-cloned from live so the superseded
generation stays complete behind the alias for rollback — and the
`<collection>` alias swaps to it in one atomic call only after EVERY walked
document verifies. The superseded physical is kept (plus a safety snapshot):
rollback and GC are deliberate operator actions, never automatic.

Completion markers are cloned too, but a marker certifies its own
`target_collection`: a new staging generation re-embeds the walked corpus
(never a cross-generation skip), and the alias-steady-state path re-verifies
read-only instead. The clone's value is that live is untouched until the
verified swap, not free incremental re-embedding.

Staging names derive deterministically from (representation fingerprint,
CLI source triple, corpus content): identical reruns resume the same
recorded build — including a suffixed allocation from the
rollback-by-republish or forced-repair path — changed inputs address a new
one, and a derived name equal to the live physical means "already
published". A forced rebuild of that live physical (issue #391 current
packet) allocates a suffixed repair build instead of mutating it: readers
keep the complete old generation until the verified atomic swap.

Corpus deletions are NOT swept (status quo: same as in-place runs —
stale-generation points survive until an operator cleans them; see
docs/ingest.md). Publishing a `--limit` subset or an empty corpus is
refused fail-closed by the caller.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from mainframe_rag.config import Settings
from mainframe_rag.ingest.build import (
    BUILD_SCHEMA,
    BuildBinding,
    canonical_build_id,
    decode_build_binding,
)
from mainframe_rag.ingest.completion import (
    _doc_id_filter,
    _verify_batch,
    completion_collection_for,
    completion_collection_name,
    delete_completion,
    delete_legacy_markers,
    is_doc_complete,
    legacy_markers,
    plan_retire_deletes,
    representation_fingerprint,
)
from mainframe_rag.ingest.inventory import InventoryRecord
from mainframe_rag.ingest.placement import (
    PeerClusterView,
    PlacementPolicy,
    consensus_name,
    evaluate_cluster,
    evaluate_collection_placement,
    member_peer_ids,
    observe_collection,
)
from mainframe_rag.ingest.seal import capture_content_seal
from mainframe_rag.ports import QdrantPoints


@dataclass(frozen=True)
class PublishTarget:
    """Resolved publication endpoints for one run."""

    alias: str
    staging: str
    live: str | None
    legacy: bool


def generation_fingerprint(settings: Settings, rules_v: str, cli_triple: str) -> str:
    """16-hex identity of everything that changes stored vectors/payloads
    for a fixed corpus: extraction rules + embed coordinates + CLI source
    triple. Extensible by extension (equality is exact-match; unknown
    segments simply address a fresh staging)."""
    digest = hashlib.sha256()
    digest.update(representation_fingerprint(settings, rules_v).encode("utf-8"))
    digest.update(b"\0")
    digest.update(cli_triple.encode("utf-8"))
    return digest.hexdigest()[:16]


def corpus_fingerprint(entries: list[tuple[str, str]]) -> str:
    """12-hex identity of the walked corpus: sorted (path, file-sha) pairs."""
    digest = hashlib.sha256()
    for path_str, sha in sorted(entries):
        digest.update(path_str.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:12]


def staging_name_for(collection: str, gen_fp: str, corpus_fp: str) -> str:
    """Deterministic staging address: safe charset, bounded length."""
    return f"{collection}__gen{gen_fp}{corpus_fp}"


PUBLISH_STATE_VERSION = 2

# Cap on suffixed staging candidates when the derived name collides with a
# retained published generation (rollback-by-republish): collisions are rare
# and deliberate, but allocation must still terminate fail-closed.
_MAX_STAGING_SUFFIX_ATTEMPTS = 64


def publish_state_path(progress: Path, alias: str) -> Path:
    """Build-state sidecar beside the progress file (issue #405 R2): the
    unfinished staging an interrupted run must resume. Same directory as
    the target lock so one progress directory owns one publication."""
    from mainframe_rag.ingest.completion import publish_lock_path

    lock_path = publish_lock_path(progress, alias)
    return lock_path.with_name(lock_path.name.removesuffix(".lock") + ".json")


def write_publish_state(
    progress: Path,
    alias: str,
    staging: str,
    gen_fp: str,
    corpus_fp: str,
    retire_plan: dict[str, dict[str, set[str] | bool]] | None = None,
    retire_docs: tuple[str, ...] | None = None,
    *,
    build_id: str | None = None,
    previous: str | None = None,
) -> None:
    """Record the in-flight build atomically (tmp + rename: a crash never
    leaves a torn sidecar). Overwrites any superseded state with a log at
    the call site."""
    path = publish_state_path(progress, alias)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    payload: dict = {
        "version": PUBLISH_STATE_VERSION,
        "build_id": canonical_build_id(build_id) if build_id is not None else str(uuid.uuid4()),
        "previous": previous,
        "alias": alias,
        "staging": staging,
        "gen_fp": gen_fp,
        "corpus_fp": corpus_fp,
    }
    if retire_docs is not None:
        payload["retire_docs"] = list(retire_docs)
    if retire_plan is not None:
        serialized_plan: dict[str, dict[str, Any]] = {}
        for doc_id, p in sorted(retire_plan.items()):
            entry: dict[str, Any] = dict(p)
            revs_val = entry.get("revs")
            if isinstance(revs_val, (set, frozenset)):
                entry["revs"] = sorted(revs_val)
            serialized_plan[doc_id] = entry
        payload["retire_plan"] = serialized_plan
    tmp.write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(path)



def _publish_state_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """JSON objects must not hide conflicting fields through last-key-wins."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate publish state field")
        result[key] = value
    return result


def _validate_recorded_retirements(state: dict) -> None:
    """Validate persisted authorization before converting revision lists to sets.

    Older v1 records may omit either optional retirement field. When both
    exist, the plan must express exactly the recorded request, never widen it.
    """
    requests: dict[str, set[str | None]] | None = None
    if "retire_docs" in state:
        flags = state["retire_docs"]
        if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
            raise ValueError("invalid recorded retirement requests")
        requests = {}
        for flag in flags:
            doc_id, sep, rev = flag.partition("@")
            doc_id = doc_id.strip()
            if not doc_id or (sep and not rev.strip()):
                raise ValueError("invalid recorded retirement request")
            requests.setdefault(doc_id, set()).add(rev.strip() if sep else None)
    if "retire_plan" not in state:
        return
    plan = state["retire_plan"]
    if not isinstance(plan, dict):
        raise TypeError("invalid recorded retirement plan")
    normalized = {}
    for doc_id, entry in plan.items():
        if not isinstance(doc_id, str) or not doc_id.strip() or not isinstance(entry, dict):
            raise ValueError("invalid recorded retirement entry")
        if set(entry) != {"revs", "legacy", "whole"}:
            raise ValueError("invalid recorded retirement fields")
        revs = entry["revs"]
        if (
            not isinstance(revs, list)
            or any(not isinstance(rev, str) or not rev.strip() for rev in revs)
            or type(entry["legacy"]) is not bool
            or type(entry["whole"]) is not bool
        ):
            raise ValueError("invalid recorded retirement types")
        if len(set(revs)) != len(revs) or (entry["legacy"] and not entry["whole"]):
            raise ValueError("invalid recorded retirement scope")
        if not revs and not entry["legacy"]:
            raise ValueError("empty recorded retirement selection")
        normalized[doc_id] = {**entry, "revs": set(revs)}
    if requests is not None:
        if set(requests) != set(normalized):
            raise ValueError("recorded retirement request and plan disagree")
        for doc_id, selected in requests.items():
            entry = normalized[doc_id]
            if entry["whole"] != (None in selected):
                raise ValueError("recorded whole-document retirement disagrees")
            if None not in selected and entry["revs"] != selected:
                raise ValueError("recorded revision retirement disagrees")
    state["retire_plan"] = normalized


def read_publish_state(progress: Path, alias: str) -> dict | None:
    """Return the recorded build, None when absent. A present-but-corrupt
    sidecar fails closed: unknown build state must never be guessed."""
    path = publish_state_path(progress, alias)
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_publish_state_object)
    except (ValueError, OSError) as exc:
        raise RuntimeError(
            f"unreadable publish state at {path}: {exc} — remove it explicitly "
            "to abandon the recorded build, then rerun."
        ) from exc
    if (
        not isinstance(state, dict)
        or type(state.get("version")) is not int
        or state.get("version") not in (1, PUBLISH_STATE_VERSION)
        or state.get("alias") != alias
        or not isinstance(state.get("staging"), str)
        or not state["staging"]
        or not isinstance(state.get("gen_fp"), str)
        or not state["gen_fp"]
        or not isinstance(state.get("corpus_fp"), str)
        or not state["corpus_fp"]
    ):
        raise RuntimeError(
            f"unrecognized publish state at {path}: refusing to guess the "
            "recorded build — remove it explicitly to abandon it, then rerun."
        )
    if state["version"] == PUBLISH_STATE_VERSION:
        try:
            canonical_build_id(state.get("build_id"))
            if "previous" not in state or (state["previous"] is not None and
                                           (not isinstance(state["previous"], str) or not state["previous"])):
                raise ValueError("invalid previous target")
        except (ValueError, TypeError) as exc:
            raise RuntimeError("invalid build identity in publish state; refusing to infer it") from exc
    try:
        _validate_recorded_retirements(state)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"invalid retirement authorization in publish state at {path}: "
            "refusing to guess the recorded build — restore its valid record "
            "or explicitly abandon it before retrying."
        ) from exc
    return state


def commit_retired_inventory(
    progress: Path,
    inventory: dict[str, InventoryRecord],
    retire_plan: dict[str, dict[str, set[str] | bool]],
) -> int:
    """Persist committed retirement disposition to inventory progress log (issue #391 S422-F1).

    For each document in the approved removal plan, appends a retirement record
    (status='retired') so subsequent runs recognize the deliberate removal rather
    than demanding missing chunks.
    """
    from mainframe_rag.ingest.inventory import append_record

    count = 0
    for doc_id, plan in sorted(retire_plan.items()):
        whole = bool(plan.get("whole", False))
        legacy = bool(plan.get("legacy", False))
        raw_revs = plan.get("revs")
        revs: set[str] = set(raw_revs) if isinstance(raw_revs, (set, frozenset, list)) else set()
        for rec in inventory.values():
            if rec.doc_id != doc_id or rec.status not in ("upserted", "skipped"):
                continue
            should_retire = False
            if (
                whole
                or (legacy and rec.source_rev is None)
                or (rec.source_rev is not None and rec.source_rev in revs)
            ):
                should_retire = True
            if should_retire:
                append_record(
                    progress,
                    rec.model_copy(
                        update={
                            "status": "retired",
                            "finished_at": time.time(),
                        }
                    ),
                )
                count += 1
    return count


def clear_publish_state(progress: Path, alias: str) -> bool:
    """Forget the recorded build after a terminal outcome. Returns whether
    a sidecar existed (callers log the cleanup)."""
    path = publish_state_path(progress, alias)
    if not path.exists():
        return False
    path.unlink()
    return True


_PUBLICATION_METADATA_PREFIX = "publication-metadata"


def publication_metadata_point_id(completions_collection: str) -> str:
    """Deterministic point ID for publication metadata in completions."""
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"{_PUBLICATION_METADATA_PREFIX}|{completions_collection}")
    )


def write_publication_metadata(
    client: QdrantPoints,
    completions_collection: str,
    settings: Settings,
    *,
    gen_fp: str,
    corpus_fp: str,
    build_id: str | None = None,
    logical_alias: str | None = None,
) -> None:
    """Record publication fingerprints on cutover (issue #391 Q418-R1).
    Allows subsequent ordinary runs to recognize successful repair generations
    as steady state."""
    from qdrant_client import models

    try:
        dim = settings.require_dense_dim()
    except RuntimeError:
        dim = None
    dummy_dim = dim or 1
    build_fields = {}
    if build_id is not None:
        if not logical_alias:
            raise ValueError("new builds require their logical alias")
        build_fields = {"build_schema": BUILD_SCHEMA, "build_id": canonical_build_id(build_id),
                        "logical_alias": logical_alias, "data_collection": settings.qdrant_collection}
        build_fields["content_seal"] = capture_content_seal(
            client, build_id=build_id, alias=logical_alias, physical=settings.qdrant_collection,
            gen_fp=gen_fp, corpus_fp=corpus_fp,
            receipt_id=publication_metadata_point_id(completions_collection),
        )
    client.upsert(
        completions_collection,
        points=[
            models.PointStruct(
                id=publication_metadata_point_id(completions_collection),
                vector={
                    "dense": [0.0] * dummy_dim,
                    "bm25": models.SparseVector(indices=[0], values=[1.0]),
                },
                payload={
                    "record_type": _PUBLICATION_METADATA_PREFIX,
                    "target_collection": completions_collection,
                    "gen_fp": gen_fp,
                    "corpus_fp": corpus_fp,
                    **build_fields,
                },
            )
        ],
        wait=True,
    )


def read_publication_record(client: QdrantPoints, completions_collection: str) -> dict | None:
    if not client.collection_exists(completions_collection):
        return None
    points = client.retrieve(completions_collection,
                             ids=[publication_metadata_point_id(completions_collection)], with_payload=True)
    if not points:
        return None
    payload = points[0].payload or {}
    if payload.get("record_type") != _PUBLICATION_METADATA_PREFIX:
        raise RuntimeError("invalid publication control record")
    try:
        decode_build_binding(payload, completions_collection)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("invalid or unsupported build control record") from exc
    return payload


def verify_publication_seal(client: QdrantPoints, completions_collection: str) -> bool:
    """Compare retained content independently of surviving completions/inventory.

    Older completed receipts remain readable, without acquiring this capability.
    A future rollback consumer must require True, never backfill a missing seal.
    """
    payload = read_publication_record(client, completions_collection)
    if payload is None or "content_seal" not in payload:
        return False
    binding = decode_build_binding(payload, completions_collection)
    if binding is None:
        raise RuntimeError("content seal lacks build identity")
    observed = capture_content_seal(
        client, build_id=binding.build_id, alias=binding.alias, physical=binding.physical,
        gen_fp=binding.gen_fp, corpus_fp=binding.corpus_fp,
        receipt_id=publication_metadata_point_id(completions_collection),
    )
    if observed != payload["content_seal"]:
        raise RuntimeError("stored build content does not match its seal")
    return True


def read_build_binding(client: QdrantPoints, completions_collection: str) -> BuildBinding | None:
    payload = read_publication_record(client, completions_collection)
    return decode_build_binding(payload, completions_collection) if payload is not None else None


def read_publication_metadata(client: QdrantPoints, completions_collection: str) -> tuple[str, str] | None:
    """Read fingerprints; a present unknown mandatory build schema fails closed."""
    payload = read_publication_record(client, completions_collection)
    if payload is None:
        return None
    gen_fp, corpus_fp = payload.get("gen_fp"), payload.get("corpus_fp")
    if isinstance(gen_fp, str) and isinstance(corpus_fp, str):
        return gen_fp, corpus_fp
    return None


def delete_publication_metadata(
    client: QdrantPoints,
    completions_collection: str,
    *,
    ancestor_completions: str | None = None,
) -> None:
    """Remove publication metadata from staging completions during preparation (issue #391 S423-N1).

    Removes both the destination point ID (if already present from an earlier
    attempt) and any inherited publication receipt(s) cloned from ancestor_completions
    or found in the collection, while leaving document completions and the
    representation manifest untouched.
    """
    from qdrant_client import models

    if not client.collection_exists(completions_collection):
        return
    ids_to_delete: set[str] = {publication_metadata_point_id(completions_collection)}
    if ancestor_completions:
        ids_to_delete.add(publication_metadata_point_id(ancestor_completions))

    try:
        offset = None
        while True:
            records, offset = client.scroll(
                completions_collection,
                limit=100,
                with_payload=True,
                offset=offset,
            )
            for rec in records:
                payload = getattr(rec, "payload", None) or {}
                if payload.get("record_type") == _PUBLICATION_METADATA_PREFIX:
                    ids_to_delete.add(str(rec.id))
            if offset is None:
                break
    except Exception:  # noqa: BLE001, S110 — unreadable store skips scroll receipt cleanup
        pass

    client.delete(
        completions_collection,
        points_selector=models.PointIdsList(points=sorted(ids_to_delete)),
        wait=True,
    )


def _fresh_staging_candidate(
    client: QdrantPoints, base: str, live: str | None, skip: frozenset[str] = frozenset()
) -> str:
    """Allocate a suffixed staging name outside the live and skipped
    generations. Collisions are rare and deliberate (rollback-by-republish),
    but allocation still terminates fail-closed."""
    for attempt in range(1, _MAX_STAGING_SUFFIX_ATTEMPTS + 1):
        candidate = f"{base}_{attempt}"
        if (candidate != live and candidate not in skip
                and not client.collection_exists(candidate)
                and not client.collection_exists(completion_collection_for(candidate))):
            return candidate
    raise RuntimeError(
        f"derived staging {base!r} is a retained published generation and "
        f"no free suffixed candidate exists after {_MAX_STAGING_SUFFIX_ATTEMPTS} "
        "attempts — operator cleanup required."
    )


def resolve_publish_staging(
    client: QdrantPoints,
    settings: Settings,
    *,
    gen_fp: str,
    corpus_fp: str,
    live: str | None,
    force_reingest: bool,
    has_retirements: bool = False,
    state: dict | None,
) -> tuple[str, bool]:
    """Select the staging collection for this build (issue #405 R2).

    Returns (staging, resumed). Same inputs always address the same build,
    so an interrupted run resumes its unfinished staging — including a
    suffixed allocation from the rollback-by-republish or repair path —
    instead of allocating another suffix. A committed retained generation is
    never taken as workspace without a record binding it to these inputs; a
    foreign record (no fingerprint match) fails closed — only the operator
    may clear it.

    Deliberately no manifest-state check on the resume path: staging is
    cloned from live, so an interrupted clone carries a COMMITTED manifest
    indistinguishable from a finished build — manifest state cannot tell
    them apart, and checking it would strand every post-clone crash retry
    on a fresh suffix. Resume safety comes from fingerprint binding (the
    record pins the exact intended inputs), converge re-verification of
    every document before any swap, and never building into the serving
    generation (a record naming live finalizes through the read-only
    steady-state path).

    A forced rebuild of the serving generation (issue #391 current packet)
    must not mutate it in place: `--reingest` on a derived name equal to
    live allocates a distinct, sidecar-recorded repair generation; the
    caller clones live into it and swaps only after full verification, so
    readers keep a complete old generation until the atomic cutover. A
    plain rerun with the same inputs remains the read-only steady-state
    re-verify.
    """
    base = staging_name_for(settings.qdrant_collection, gen_fp, corpus_fp)
    matched = (
        state
        if state is not None
        and state.get("gen_fp") == gen_fp
        and state.get("corpus_fp") == corpus_fp
        else None
    )
    if state is not None and matched is None:
        raise RuntimeError(
            f"publish state records staging {state.get('staging')!r} for different "
            f"inputs (gen {state.get('gen_fp')!r}, corpus {state.get('corpus_fp')!r}): "
            "refusing a build the current inputs cannot explain — remove the "
            "state file explicitly to abandon the recorded build, then rerun."
        )
    if matched is not None:
        recorded = matched.get("staging")
        if not isinstance(recorded, str) or not recorded:
            raise RuntimeError(
                "publish state records these inputs but names no staging: "
                "refusing to guess the recorded build — remove the state file "
                "explicitly to abandon it, then rerun."
            )
        if recorded == live:
            # The recorded build already serves: a crash between the swap
            # and the sidecar cleanup. Returned as-is so the caller takes
            # the read-only steady-state path — never a build into live.
            return recorded, True
        if client.collection_exists(recorded):
            return recorded, True
        # The recorded build was cleaned up outside the lock: rebuild at
        # the recorded name so the sidecar stays accurate (rebuilding at
        # the derived name would strand it and read as an unrecorded
        # build on the next retry).
        return recorded, False
    if live is not None:
        live_completions = completion_collection_for(live)
        pub_meta = read_publication_metadata(client, live_completions)
        is_live_steady = (pub_meta is not None and pub_meta == (gen_fp, corpus_fp)) or (
            live == base
        )
        if is_live_steady:
            if not force_reingest and not has_retirements:
                return live, False
            return _fresh_staging_candidate(client, base, live), False
    if client.collection_exists(base) or client.collection_exists(completion_collection_for(base)):
        staging_settings = settings.model_copy(update={"qdrant_collection": base})
        from mainframe_rag.ingest.representation import STATE_COMMITTED, read_manifest_record

        record = read_manifest_record(client, completion_collection_name(staging_settings))
        if record is not None and record.state == STATE_COMMITTED:
            return _fresh_staging_candidate(client, base, live), False
        raise RuntimeError(
            f"staging {base!r} exists from an unrecorded unfinished build: refusing "
            "to reuse or overwrite it — remove it explicitly, or resume the run "
            "whose inputs recorded it, then rerun."
        )
    return base, False


def ensure_staging(
    client: QdrantPoints, settings: Settings, staging_settings: Settings, live: str | None
) -> str:
    """Prepare staging; returns sealed | reused | repaired | cloned | fresh.

    Reuse is safe by construction: the converge pipeline re-verifies every
    document (PR-1 logic), so partial or older staging states heal instead
    of publishing. Cloning carries live's points AND completion markers;
    markers certify their own target, so walked documents re-embed into the
    new generation rather than skip across physicals.

    Reuse also verifies the metadata half of preparation (issue #391 F5):
    a crash between the data clone and the completions clone/re-key leaves a
    staging data collection whose manifest is unreadable under the staging
    id. Existence alone is not completed preparation — when live carries a
    contract, the completions collection is re-copied (count-verified) from
    live and the manifest re-keyed verbatim. Genuinely legacy live (no
    completions collection or no manifest point) transfers nothing: absence
    is the explicit inner-preflight outcome, not a repair. Live data and
    metadata are never modified.
    """
    from mainframe_rag.ingest.qdrant_io import clone_collection
    from mainframe_rag.ingest.representation import read_manifest

    staging = staging_settings.qdrant_collection
    # A verified build is immutable even if preparation is called directly.
    # Its caller revalidates coverage/placement instead of repairing metadata.
    if read_build_binding(client, completion_collection_for(staging)) is not None:
        return "sealed"
    if client.collection_exists(staging):
        staging_completions = completion_collection_name(staging_settings)
        if read_manifest(client, staging_completions) is not None:
            return "reused"
        if live is not None and read_manifest(client, completion_collection_for(live)) is not None:
            _transfer_staging_metadata(client, settings, staging_settings, live)
            return "repaired"
        return "reused"
    if live is None:
        return "fresh"  # inner pipeline creates both collections
    clone_collection(client, settings, live, staging)
    _transfer_staging_metadata(client, settings, staging_settings, live)
    return "cloned"


def _transfer_staging_metadata(
    client: QdrantPoints, settings: Settings, staging_settings: Settings, live: str
) -> None:
    """Clone live's completions into staging (when not already copied) and
    re-key the manifest verbatim; verify, never assume.

    When live carries a readable manifest, the staging manifest must be
    readable and identical afterward — model AND envelope state (a pending
    live must not be laundered into a committed staging via the copy).
    Otherwise preparation is incomplete and the inner preflight would
    misread inherited state as legacy (or, worse, a publish would verify
    against the wrong contract). An existing but unreadable staging
    completions collection is a partial copy or a pre-re-key clone: it is
    discarded and re-copied from live, so the transferred metadata is
    count-verified instead of trusted. Raises with explicit remediation
    when the transfer still cannot complete; live data and metadata are
    never modified.
    """
    from mainframe_rag.ingest.qdrant_io import clone_collection
    from mainframe_rag.ingest.representation import read_manifest_record, rekey_manifest

    live_completions = completion_collection_for(live)
    staging_completions = completion_collection_name(staging_settings)
    live_record = read_manifest_record(client, live_completions)
    if live_record is None:
        return  # legacy live: absence is explicit downstream, never repaired here
    if client.collection_exists(staging_completions):
        # Staging-only cleanup: live is the source and is never touched, and
        # the inner run re-creates any marker it needs.
        client.delete_collection(staging_completions)
    clone_collection(client, settings, live_completions, staging_completions)
    if not rekey_manifest(client, live_completions, staging_completions):
        raise RuntimeError(
            f"staging metadata transfer failed: {live_completions!r} carries a "
            f"manifest but {staging_completions!r} has none after the clone/re-key "
            "— refusing to publish from an incomplete staging generation."
        )
    delete_publication_metadata(
        client,
        staging_completions,
        ancestor_completions=live_completions,
    )
    if read_manifest_record(client, staging_completions) != live_record:
        raise RuntimeError(
            f"staging manifest at {staging_completions!r} does not match the live "
            "contract — refusing to publish from an incomplete staging generation."
        )


def verify_searchable_coverage(
    client: QdrantPoints,
    settings: Settings,
    walked: list[tuple[str, str]],
    inventory: dict[str, InventoryRecord],
    rules_v: str,
    src_labels: str,
    *,
    retired: frozenset[str] = frozenset(),
    retire_plan: dict[str, dict[str, set[str] | bool]] | None = None,
    pending_removals: frozenset[str] = frozenset(),
    allow_approved_legacy: bool = False,
) -> list[str]:
    """One read-only coverage rule: every walked document verifies and every
    searchable point is attributable to a verified walked generation
    (issue #391 current packet). Empty means attributable.

    Two callers, one policy:
    - the alias-swap gate (`verify_all_complete`) allows approved pre-361B
      legacy membership, because a compatible committed generation may carry
      lazily migrated history;
    - the migration commit (`_commit_migration_representation`) is strict
      (`allow_approved_legacy=False`): a new contract may only be declared
      over vectors this run re-embedded. `pending_removals` are points under
      a lock-validated `--retire-doc` plan that applies before the swap
      audit, so the commit does not block on data whose removal is already
      approved and enforced downstream. Never deletes."""
    problems: list[str] = []
    for path_str, sha in walked:
        rec = inventory.get(path_str)
        if (
            rec is None
            or rec.sha256 != sha
            or rec.rules_version != rules_v
            or rec.status not in ("upserted", "skipped")
            or not rec.doc_id
            or not rec.source_rev
        ):
            problems.append(path_str)
            continue
        if not is_doc_complete(
            client,
            settings,
            rec.doc_id,
            sha256=sha,
            rules_v=rules_v,
            source_labels=src_labels,
            source_rev=rec.source_rev,
        ):
            problems.append(path_str)
    legacy_ids: set[str] = set()
    if allow_approved_legacy:
        legacy_ids, legacy_problems = verify_approved_legacy_points(
            client,
            settings,
            inventory,
            rules_v,
            walked_paths={p for p, _ in walked},
            retired=retired,
            retire_plan=retire_plan,
        )
        problems.extend(legacy_problems)
    problems.extend(
        audit_unmarked_residue(
            client,
            settings,
            walked,
            inventory,
            rules_v,
            retired,
            retire_plan,
            pending_removals=pending_removals,
            allow_approved_legacy=allow_approved_legacy,
            verified_legacy_ids=legacy_ids,
        )
    )
    return problems


def verify_all_complete(
    client: QdrantPoints,
    staging_settings: Settings,
    walked: list[tuple[str, str]],
    inventory: dict[str, InventoryRecord],
    rules_v: str,
    src_labels: str,
    retired: frozenset[str] = frozenset(),
    retire_plan: dict[str, dict[str, set[str] | bool]] | None = None,
) -> list[str]:
    """Paths that must block publication: missing/stale inventory, an
    unverified staging generation, a contract that is not committed
    (issue #391 F2: a pending migration must never be swapped into
    service), or searchable points no walked document accounts for
    (issue #405 R1). Empty means publishable. Read-only: problems refuse
    the cutover, never delete."""
    from mainframe_rag.ingest.representation import (
        COMPATIBLE,
        RECORD_ONLY_DRIFT,
        STATE_COMMITTED,
        build_manifest,
        compare_manifests,
        read_manifest_record,
    )

    record = read_manifest_record(client, completion_collection_name(staging_settings))
    problems: list[str] = []
    if record is None:
        if walked:
            problems.append(
                f"{staging_settings.qdrant_collection}: missing or unreadable metadata manifest"
            )
    elif record.state != STATE_COMMITTED:
        problems.append(f"{staging_settings.qdrant_collection}: contract {record.state!r}")
    else:
        wanted = build_manifest(staging_settings, rules_v)
        outcome, fields = compare_manifests(record.manifest, wanted)
        if outcome not in (COMPATIBLE, RECORD_ONLY_DRIFT):
            problems.append(
                f"{staging_settings.qdrant_collection}: representation drift on {', '.join(fields)}"
            )
    problems.extend(
        verify_searchable_coverage(
            client,
            staging_settings,
            walked,
            inventory,
            rules_v,
            src_labels,
            retired=retired,
            retire_plan=retire_plan,
            allow_approved_legacy=True,
        )
    )
    return problems


def verify_staging_distribution(
    client: QdrantPoints,
    staging_settings: Settings,
) -> list[str]:
    """Strict configured-distribution gate for publication cutover (issue #360).

    The candidate corpus collection and its paired control collection must
    both carry the explicitly selected policy verbatim. Unlike the lenient
    ingest compatibility check (which treats unreadable live values as
    unknown, not mismatches), an unreadable, missing, or mismatched value
    here refuses cutover: publication must never certify an unknown
    topology. Read-only: problems refuse the swap, never recreate or
    downgrade. Empty when no explicit policy is selected (dev path).
    """
    policy = staging_settings.collection_distribution_kwargs()
    if not policy:
        return []
    attrs = (
        ("shard_number", "shard_number"),
        ("replication_factor", "replication_factor"),
        ("write_consistency_factor", "write_consistency_factor"),
    )
    staging = staging_settings.qdrant_collection
    problems: list[str] = []
    for collection in (staging, completion_collection_name(staging_settings)):
        try:
            exists = client.collection_exists(collection)
        except Exception as exc:  # noqa: BLE001 - read failure is a refusal
            problems.append(
                f"{collection}: existence unreadable "
                f"({type(exc).__name__}: {exc}) — cannot certify an unknown "
                "topology (issue #360)"
            )
            continue
        if not exists:
            problems.append(
                f"{collection}: required collection absent — cannot certify an "
                "unknown topology (issue #360)"
            )
            continue
        try:
            params = client.get_collection(collection).config.params
        except Exception as exc:  # noqa: BLE001 - read failure is a refusal
            problems.append(
                f"{collection}: configured policy unreadable "
                f"({type(exc).__name__}: {exc}) — cannot certify an unknown "
                "topology (issue #360)"
            )
            continue
        for kwarg, attr in attrs:
            if kwarg not in policy:
                continue
            want = policy[kwarg]
            live = getattr(params, attr, None)
            if live is None:
                problems.append(
                    f"{collection}: configured {attr} is unknown/unreadable; "
                    "cannot certify an unknown policy (issue #360)"
                )
            elif live != want:
                problems.append(
                    f"{collection}: configured {attr}={live} != selected {want} "
                    "(snapshot-gated replica/rebuild migration, issue #360) — "
                    "never lower the production policy to hide it"
                )
    return problems


def _cluster_info_of(client: QdrantPoints, collection: str):
    """Eagerly-bound collection_cluster_info fetch for observe_collection
    (avoids loop-variable closures when observing several endpoints)."""
    return client.collection_cluster_info(collection)


def _placement_peer_view(endpoint: str, client: QdrantPoints) -> PeerClusterView:
    """One direct peer endpoint's own control-plane view for the in-process
    gate (mirrors the verify_placement CLI reader: an unreadable view is an
    explicit error, never a missing member). A standalone server reports no
    peer id or membership (`{"status":"disabled"}`); that shape is carried
    verbatim so the single-node profile can judge it on its own terms."""
    try:
        status = client.cluster_status()
    except Exception as exc:  # noqa: BLE001 - reported as unreadable membership
        return PeerClusterView(
            endpoint, False, None, (), "", f"{type(exc).__name__}: {exc}"
        )
    return PeerClusterView(
        endpoint=endpoint,
        reachable=True,
        peer_id=getattr(status, "peer_id", None),
        member_peer_ids=member_peer_ids(status),
        consensus=consensus_name(status),
    )


def verify_staging_placement(
    clients_by_endpoint: Mapping[str, QdrantPoints],
    staging_settings: Settings,
) -> list[str]:
    """In-process ACTIVE-copy gate for publication cutover (issue #360).

    Reuses the placement evaluation owner over per-endpoint observations:
    every required shard of the staging corpus AND its paired control
    collection needs RF ACTIVE copies on distinct peers, and the endpoints
    must form one healthy cluster. Degraded, recovering, unverifiable and
    unservable all refuse cutover while the old generation keeps serving;
    only a healthy verdict passes. Read-only: problems refuse the swap,
    never recreate or downgrade. Empty when no explicit policy is selected
    (dev path).

    `clients_by_endpoint` maps each direct peer endpoint to a client bound
    to it. The entry URL must never appear here as a peer identity: a
    load-balanced Service alternating backends would masquerade as distinct
    copies, and duplicate peer identities refuse. An empty mapping refuses
    for any explicit policy — zero observations never certify, not even
    1/1/1 (the single-node caller supplies its one endpoint explicitly).
    The explicit 1/1/1 profile judges its single ACTIVE copy from collection
    info alone: a standalone server reports no cluster peer id, so no
    membership binding is possible there — the same trust as every other
    single-endpoint read in the pipeline.
    """
    policy = staging_settings.collection_distribution_kwargs()
    if not policy:
        return []
    staging = staging_settings.qdrant_collection
    missing_keys = [
        key
        for key in ("shard_number", "replication_factor", "write_consistency_factor")
        if key not in policy
    ]
    if missing_keys:
        return [
            (f"{staging}: placement cannot be judged on a partial policy "
            f"(missing: {', '.join(missing_keys)}) — select the complete tuple "
            "(issue #360)")
        ]
    if not clients_by_endpoint:
        return [
            (f"{staging}: replication_factor={policy['replication_factor']} selected "
            "but no direct peer endpoints were supplied (QDRANT_PEER_URLS) — "
            "placement cannot be certified through the entry endpoint "
            "(issue #360)")
        ]
    expected = PlacementPolicy(
        shard_number=policy["shard_number"],
        replication_factor=policy["replication_factor"],
        write_consistency_factor=policy["write_consistency_factor"],
    )
    endpoints = list(clients_by_endpoint)
    if expected.as_tuple == (1, 1, 1) and len(endpoints) == 1:
        return _verify_single_node_placement(
            endpoints[0], clients_by_endpoint[endpoints[0]], staging_settings
        )
    if len(endpoints) < expected.replication_factor:
        return [
            (f"{staging}: {len(endpoints)} peer endpoint(s) cannot hold "
            f"replication_factor={expected.replication_factor} distinct copies — "
            "configure every expected peer (issue #360)")
        ]
    views = tuple(
        _placement_peer_view(endpoint, clients_by_endpoint[endpoint])
        for endpoint in endpoints
    )
    cluster = evaluate_cluster(expected_peers=len(endpoints), views=views)
    if cluster.state != "healthy":
        detail = "; ".join(cluster.problems) or "no healthy cluster"
        return [f"{staging}: cluster {cluster.state}: {detail} (issue #360)"]
    collections = (staging, completion_collection_name(staging_settings))
    observations = [
        observe_collection(
            collection,
            endpoint,
            fetch=partial(_cluster_info_of, clients_by_endpoint[endpoint]),
        )
        for endpoint in endpoints
        for collection in collections
    ]
    problems: list[str] = []
    for collection in collections:
        verdict = evaluate_collection_placement(
            collection, observations, expected, accepted_peers=cluster.member_ids
        )
        if verdict.state != "healthy":
            detail = "; ".join(verdict.problems) or "no ACTIVE quorum"
            problems.append(
                f"{collection}: placement {verdict.state}: {detail} (issue #360)"
            )
    return problems


def _verify_single_node_placement(
    endpoint: str, client: QdrantPoints, staging_settings: Settings
) -> list[str]:
    """Judge the explicit 1/1/1 profile through its one endpoint: the single
    copy of every required shard must be ACTIVE with no transfers. The peer
    id comes from the collection observation itself — standalone servers
    report no cluster-level identity."""
    staging = staging_settings.qdrant_collection
    expected = PlacementPolicy(1, 1, 1)
    collections = (staging, completion_collection_name(staging_settings))
    observations = [
        observe_collection(
            collection, endpoint, fetch=lambda name: client.collection_cluster_info(name)
        )
        for collection in collections
    ]
    unreachable = [view for view in observations if not view.reachable]
    if unreachable:
        return [
            (f"{staging}: single peer endpoint {endpoint} unreachable "
            f"({unreachable[0].error or 'no response'}) — placement unverifiable "
            "(issue #360)")
        ]
    peer_ids: list[int] = []
    for view in observations:
        if view.peer_id is None:
            return [
                (
                    f"{staging}: peer identity unreadable from {endpoint} — "
                    "placement unverifiable (issue #360)"
                )
            ]
        peer_ids.append(view.peer_id)
    if len(set(peer_ids)) != 1:
        return [
            (f"{staging}: peer identity inconsistent across the required "
            "collections — placement unverifiable (issue #360)")
        ]
    accepted = (peer_ids[0],)
    problems: list[str] = []
    for collection in collections:
        verdict = evaluate_collection_placement(
            collection, observations, expected, accepted_peers=accepted
        )
        if verdict.state != "healthy":
            detail = "; ".join(verdict.problems) or "no ACTIVE copy"
            problems.append(
                f"{collection}: placement {verdict.state}: {detail} (issue #360)"
            )
    return problems


def _is_point_retired(
    doc_id: str | None,
    source_rev: str | None,
    retired: frozenset[str],
    retire_plan: dict[str, dict[str, set[str] | bool]] | None,
) -> bool:
    """Check whether a point is covered by an approved retirement plan (issue #391 R-REV)."""
    if not doc_id or doc_id not in retired:
        return False
    if not retire_plan or doc_id not in retire_plan:
        return True
    entry = retire_plan[doc_id]
    if entry.get("whole"):
        return True
    revs = entry.get("revs")
    if source_rev is not None and isinstance(revs, (set, frozenset)) and source_rev in revs:
        return True
    return bool(source_rev is None and entry.get("legacy"))


def _retired_still_present(
    client: QdrantPoints,
    staging_settings: Settings,
    staging: str,
    doc_id: str,
    retire_plan: dict[str, dict[str, set[str] | bool]] | None,
    valid_pairs: set[tuple[str, str]] | None = None,
) -> str:
    """Name the exact gap when an approved removal did not fully land
    (issue #405 R1, #391 R-REV): every refusal names the operator path through,
    never a dead end. Inspects what remains live under the retired doc_id:

    - named revision(s) remain outside the walked corpus: staging changed
      after planning (or the plan covered only part of the document) — rerun
      to re-plan, or retire the whole document;
    - only sourceless legacy remains after a whole-document retirement that
      covered it: the apply-time sole-history check saw staging change —
      rerun to re-plan;
    - only sourceless legacy remains outside the approved removal: a
      partial retirement never takes legacy points — whole-document
      --retire-doc covers approved sourceless history, anything else needs
      manual resolution.
    """
    from mainframe_rag.ingest.qdrant_io import stored_doc_revisions

    remaining = stored_doc_revisions(client, staging_settings, doc_id)
    entry = (retire_plan or {}).get(doc_id, {})
    is_whole = bool(entry.get("whole", False))
    valid = valid_pairs or set()
    named = sorted(str(r) for r in remaining if r is not None and (doc_id, r) not in valid)
    if named:
        return (
            f"{staging}: retired document {doc_id!r} still present as revision(s) "
            f"{named} — the approved removal no longer matches staging: rerun to "
            "re-plan against the current inventory, or retire the whole document."
        )
    if entry.get("legacy"):
        return (
            f"{staging}: retired document {doc_id!r} still present after its approved "
            "removal covered sourceless history — staging changed after the delete: "
            "rerun to re-plan against the current inventory."
        )
    if is_whole:
        return (
            f"{staging}: retired document {doc_id!r} persists as sourceless legacy "
            "point(s) with no approved sourceless history — whole-document retirement "
            "already ran and covered nothing sourceless: resolve manually, never by "
            "re-running the same flag."
        )
    return (
        f"{staging}: retired document {doc_id!r} persists as sourceless legacy "
        f"point(s) outside the approved (partial) removal — a named-only retirement "
        f"never takes legacy points: use whole-document --retire-doc {doc_id!r} to "
        "cover approved sourceless history, or resolve manually."
    )


def approved_legacy_membership(
    inventory: dict[str, InventoryRecord],
) -> set[tuple[str, str]]:
    """(doc_id, sha256) of approved sourceless pre-361B history: inventory
    lines with no revision stamp, a content sha, and an approved status.
    Kept for backward compatibility; publication verification uses
    `verify_approved_legacy_points` for digest-level verification."""
    return {
        (rec.doc_id, rec.sha256)
        for rec in inventory.values()
        if rec.doc_id
        and rec.sha256
        and rec.source_rev is None
        and rec.status in ("upserted", "skipped")
    }


def verify_approved_legacy_points(
    client: QdrantPoints,
    staging_settings: Settings,
    inventory: dict[str, InventoryRecord],
    rules_v: str,
    *,
    walked_paths: set[str] | frozenset[str] = frozenset(),
    retired: frozenset[str] = frozenset(),
    retire_plan: dict[str, dict[str, set[str] | bool]] | None = None,
) -> tuple[set[str], list[str]]:
    """Verify stored pre-361B legacy points against approved digests (issue #391 Q417-L1).

    Every sourceless point permitted by the publication compatibility bridge must
    belong to an approved, verifiable legacy document: expected chunk count, chunk
    IDs, extraction rules, and stored excerpt text must match approved digests.
    Unverifiable history fails closed with an explicit --reingest remediation;
    unexpected points carrying an approved source-file hash are refused.

    Returns (verified_point_ids, problems).
    """
    from mainframe_rag.ingest.qdrant_io import scroll_all_points

    staging = staging_settings.qdrant_collection
    if not client.collection_exists(staging):
        return set(), []

    walked_doc_ids = {rec.doc_id for p in walked_paths if (rec := inventory.get(p)) and rec.doc_id}
    approved_legacy: dict[tuple[str, str], InventoryRecord] = {}
    for rec in inventory.values():
        if (
            rec.doc_id
            and rec.sha256
            and rec.source_rev is None
            and rec.status in ("upserted", "skipped")
        ):
            if rec.path in walked_paths:
                continue
            if retire_plan and rec.doc_id in retire_plan:
                entry = retire_plan[rec.doc_id]
                if entry.get("whole") or entry.get("legacy"):
                    continue
                if rec.doc_id not in walked_doc_ids:
                    continue
            elif rec.doc_id in retired:
                continue
            approved_legacy[(rec.doc_id, rec.sha256)] = rec

    if not approved_legacy:
        return set(), []

    verified_ids: set[str] = set()
    problems: list[str] = []

    for (doc_id, sha256), rec in sorted(approved_legacy.items()):
        expected_chunks = rec.chunks
        chunk_ids_digest = rec.chunk_ids_digest
        content_digest = rec.content_digest
        expected_rules = rec.rules_version

        if not chunk_ids_digest or not content_digest or expected_chunks < 1:
            markers = [
                m for m in legacy_markers(client, staging_settings, doc_id) if m.sha256 == sha256
            ]
            if markers:
                m = markers[0]
                expected_chunks = m.expected_chunks
                chunk_ids_digest = m.chunk_ids_digest
                content_digest = m.content_digest
                expected_rules = m.rules_v

        if not chunk_ids_digest or not content_digest or expected_chunks < 1:
            problems.append(
                f"{staging}: legacy document {doc_id!r} has no verifiable chunk/content "
                "digest — re-ingest with --reingest to upgrade to the current representation."
            )
            continue

        if expected_rules != rules_v:
            problems.append(
                f"{staging}: legacy document {doc_id!r} extraction rules {expected_rules!r} "
                f"mismatch current rules {rules_v!r} — re-ingest with --reingest to upgrade."
            )
            continue

        doc_points = scroll_all_points(
            client,
            staging,
            scroll_filter=_doc_id_filter(doc_id),
            with_payload=["doc_id", "source_rev", "sha256", "rules_v", "text"],
            page_size=staging_settings.ingest_scan_page_size,
        )
        legacy_pts = [
            p
            for p in doc_points
            if (p.payload or {}).get("source_rev") is None
            and (p.payload or {}).get("sha256") == sha256
        ]

        if len(legacy_pts) != expected_chunks:
            problems.append(
                f"{staging}: legacy document {doc_id!r} chunk count mismatch: expected "
                f"{expected_chunks}, found {len(legacy_pts)} — refusing cutover without "
                "complete verified evidence."
            )
            continue

        if not _verify_batch(legacy_pts, sha256, rules_v, "", chunk_ids_digest, content_digest):
            problems.append(
                f"{staging}: legacy document {doc_id!r} stored points fail content/digest "
                "verification — refusing cutover without verified evidence."
            )
            continue

        verified_ids.update(str(p.id) for p in legacy_pts)

    return verified_ids, problems


def audit_unmarked_residue(
    client: QdrantPoints,
    staging_settings: Settings,
    walked: list[tuple[str, str]],
    inventory: dict[str, InventoryRecord],
    rules_v: str,
    retired: frozenset[str] = frozenset(),
    retire_plan: dict[str, dict[str, set[str] | bool]] | None = None,
    *,
    pending_removals: frozenset[str] = frozenset(),
    allow_approved_legacy: bool = False,
    verified_legacy_ids: set[str] | None = None,
) -> list[str]:
    """Read-only coverage audit (issue #405 R1, #391 Q417-L1): every searchable
    point in the candidate generation must be attributable to the walked corpus.
    A point counts as covered when its (doc_id, source_rev) has a verified
    walked inventory record, or — when `allow_approved_legacy` — when it is a
    sourceless pre-revision point verified against approved legacy digests
    (content and chunk-identity verification, never merely a shared file hash;
    issue #391 Q417-L1). `pending_removals` are doc_ids under a lock-validated
    removal plan applied before the swap audit; their points are excused here
    and enforced gone by the retired check once the plan is applied. Anything
    else (including points for explicitly retired documents, which must already
    be gone) is a problem, never a deletion: absence from a partial walk is not
    a removal instruction."""
    from mainframe_rag.ingest.qdrant_io import scroll_all_points

    staging = staging_settings.qdrant_collection
    if not client.collection_exists(staging):
        return []
    valid_pairs = {
        (rec.doc_id, rec.source_rev)
        for path_str, sha in walked
        if (rec := inventory.get(path_str))
        and rec.sha256 == sha
        and rec.doc_id
        and rec.source_rev
        and rec.rules_version == rules_v
        and rec.status in ("upserted", "skipped")
    }
    if verified_legacy_ids is None and allow_approved_legacy:
        verified_legacy_ids, legacy_probs = verify_approved_legacy_points(
            client,
            staging_settings,
            inventory,
            rules_v,
            walked_paths={p for p, _ in walked},
            retired=retired,
            retire_plan=retire_plan,
        )
        if legacy_probs:
            return legacy_probs
    residue = 0
    sample: list[str] = []
    for p in scroll_all_points(
        client,
        staging,
        scroll_filter=None,
        with_payload=["doc_id", "source_rev", "sha256"],
        page_size=staging_settings.ingest_scan_page_size,
    ):
        payload = p.payload or {}
        doc_id = payload.get("doc_id")
        source_rev = payload.get("source_rev")
        if (doc_id, source_rev) in valid_pairs:
            continue
        if (
            source_rev is None
            and allow_approved_legacy
            and verified_legacy_ids is not None
            and str(p.id) in verified_legacy_ids
        ):
            continue
        if doc_id is not None and doc_id in retired:
            return [
                _retired_still_present(
                    client,
                    staging_settings,
                    staging,
                    str(doc_id),
                    retire_plan,
                    valid_pairs=valid_pairs,
                )
            ]
        if _is_point_retired(doc_id, source_rev, pending_removals, retire_plan):
            # The approved removal applies before verification: the swap gate
            # still refuses any remnant the plan does not actually delete.
            continue
        residue += 1
        if len(sample) < 3:
            sample.append(str(p.id))
    if residue:
        return [
            (
                f"{staging}: unmarked residue detected ({residue} point(s)"
                f"{', e.g. ' + ', '.join(sample) if sample else ''}) — a searchable "
                "point must match a verified walked generation (or, for sourceless "
                "legacy history, verified legacy document digests); refusing "
                "cutover without an explicit approved removal."
            )
        ]
    return []


def plan_approved_removals(
    flags: tuple[str, ...],
    inventory: dict[str, InventoryRecord],
) -> tuple[dict[str, dict[str, set[str] | bool]], frozenset[str]]:
    """Parse --retire-doc flags (repeatable `DOCID` or `DOCID@SOURCEREV`)
    and validate them against the last approved inventory (issue #405 R1).
    Returns (delete plan, retired doc_ids). Malformed flags, unknown
    documents, and unapproved revisions fail closed before any mutation —
    a missing file is never itself a removal instruction."""
    requested: dict[str, set[str | None]] = {}
    for flag in flags:
        doc_id, sep, rev = flag.partition("@")
        doc_id = doc_id.strip()
        if not doc_id or (sep and not rev.strip()):
            raise RuntimeError(
                f"malformed --retire-doc {flag!r}: expected DOCID or DOCID@SOURCEREV."
            )
        requested.setdefault(doc_id, set()).add(rev.strip() if sep else None)
    approved: dict[str, set[str | None]] = {}
    for rec in inventory.values():
        if rec.doc_id and rec.status in ("upserted", "skipped"):
            approved.setdefault(rec.doc_id, set()).add(rec.source_rev)
    plan = plan_retire_deletes(approved, requested)
    return plan, frozenset(plan)


def apply_approved_removals(
    client: QdrantPoints,
    staging_settings: Settings,
    plan: dict[str, dict[str, set[str] | bool]],
) -> dict[str, int]:
    """Delete explicitly approved retirements from staging (issue #405 R1).
    Named revisions go through server-side revision-filtered deletes plus
    their marker invalidation; legacy sourceless points go only with an
    explicit whole-document retirement covering approved sourceless history
    (named and mixed-history documents alike), and only when no named
    revision remains in staging afterwards (checked live, mirroring the
    refresh sole-history rule). Returns {doc_id: deleted revision count}
    for the run log."""
    from mainframe_rag.ingest.qdrant_io import (
        delete_by_doc,
        delete_by_revision,
        stored_doc_revisions,
    )

    removed: dict[str, int] = {}
    for doc_id in sorted(plan):
        revs = plan[doc_id]["revs"]
        assert isinstance(revs, set)
        for rev in sorted(str(r) for r in revs):
            delete_completion(client, staging_settings, doc_id, source_rev=rev)
            delete_by_revision(client, staging_settings, rev)
        count = len(revs)
        if plan[doc_id]["legacy"]:
            remaining = stored_doc_revisions(client, staging_settings, doc_id)
            if remaining <= {None}:
                delete_by_doc(client, staging_settings, doc_id)
                delete_legacy_markers(client, staging_settings, doc_id)
                count += 1
        removed[doc_id] = count
    return removed
