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
CLI source triple, corpus content): identical reruns converge the same
staging (crash-safe resume), changed inputs address a new one, and a
derived name equal to the live physical means "already published".

Corpus deletions are NOT swept (status quo: same as in-place runs —
stale-generation points survive until an operator cleans them; see
docs/ingest.md). Publishing a `--limit` subset or an empty corpus is
refused fail-closed by the caller.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from mainframe_rag.config import Settings
from mainframe_rag.ingest.completion import (
    completion_collection_for,
    completion_collection_name,
    delete_completion,
    delete_legacy_markers,
    is_doc_complete,
    plan_retire_deletes,
    representation_fingerprint,
)
from mainframe_rag.ingest.inventory import InventoryRecord
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


PUBLISH_STATE_VERSION = 1

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
    progress: Path, alias: str, staging: str, gen_fp: str, corpus_fp: str
) -> None:
    """Record the in-flight build atomically (tmp + rename: a crash never
    leaves a torn sidecar). Overwrites any superseded state with a log at
    the call site."""
    path = publish_state_path(progress, alias)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(
            {
                "version": PUBLISH_STATE_VERSION,
                "alias": alias,
                "staging": staging,
                "gen_fp": gen_fp,
                "corpus_fp": corpus_fp,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    tmp.replace(path)


def read_publish_state(progress: Path, alias: str) -> dict | None:
    """Return the recorded build, None when absent. A present-but-corrupt
    sidecar fails closed: unknown build state must never be guessed."""
    path = publish_state_path(progress, alias)
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise RuntimeError(
            f"unreadable publish state at {path}: {exc} — remove it explicitly "
            "to abandon the recorded build, then rerun."
        ) from exc
    if (
        not isinstance(state, dict)
        or state.get("version") != PUBLISH_STATE_VERSION
        or state.get("alias") != alias
    ):
        raise RuntimeError(
            f"unrecognized publish state at {path}: refusing to guess the "
            "recorded build — remove it explicitly to abandon it, then rerun."
        )
    return state


def clear_publish_state(progress: Path, alias: str) -> bool:
    """Forget the recorded build after a terminal outcome. Returns whether
    a sidecar existed (callers log the cleanup)."""
    path = publish_state_path(progress, alias)
    if not path.exists():
        return False
    path.unlink()
    return True


def resolve_publish_staging(
    client: QdrantPoints,
    settings: Settings,
    *,
    gen_fp: str,
    corpus_fp: str,
    live: str | None,
    force_reingest: bool,
    state: dict | None,
) -> tuple[str, bool]:
    """Select the staging collection for this build (issue #405 R2).

    Returns (staging, resumed). Same inputs always address the same name,
    so an interrupted run resumes its unfinished build instead of
    allocating another suffix. A committed retained generation is never
    returned as writable workspace: rollback-by-republish allocates a
    suffixed candidate. A foreign unfinished staging (no matching record)
    fails closed — only the operator may clear it.
    """
    from mainframe_rag.ingest.representation import STATE_COMMITTED, read_manifest_record

    base = staging_name_for(settings.qdrant_collection, gen_fp, corpus_fp)
    matched = (
        state
        if state is not None
        and state.get("gen_fp") == gen_fp
        and state.get("corpus_fp") == corpus_fp
        else None
    )
    if matched is not None and matched.get("staging") != base:
        raise RuntimeError(
            f"publish state records staging {matched.get('staging')!r} for these "
            f"inputs but the derived name is {base!r}: refusing a build the "
            "inputs cannot explain — remove the state file explicitly, then rerun."
        )
    if live == base and not force_reingest:
        return base, False
    if live == base and force_reingest:
        return base, False
    if matched is not None and client.collection_exists(base):
        return base, True
    if matched is not None:
        return base, False
    if client.collection_exists(base):
        staging_settings = settings.model_copy(update={"qdrant_collection": base})
        record = read_manifest_record(client, completion_collection_name(staging_settings))
        if record is not None and record.state == STATE_COMMITTED:
            for attempt in range(1, _MAX_STAGING_SUFFIX_ATTEMPTS + 1):
                candidate = f"{base}_{attempt}"
                if candidate != live and not client.collection_exists(candidate):
                    return candidate, False
            raise RuntimeError(
                f"derived staging {base!r} is a retained published generation and "
                f"no free suffixed candidate exists after {_MAX_STAGING_SUFFIX_ATTEMPTS} "
                "attempts — operator cleanup required."
            )
        raise RuntimeError(
            f"staging {base!r} exists from an unrecorded unfinished build: refusing "
            "to reuse or overwrite it — remove it explicitly, or resume the run "
            "whose inputs recorded it, then rerun."
        )
    return base, False


def ensure_staging(
    client: QdrantPoints, settings: Settings, staging_settings: Settings, live: str | None
) -> str:
    """Prepare the staging generation; returns reused | repaired | cloned | fresh.

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
    if read_manifest_record(client, staging_completions) != live_record:
        raise RuntimeError(
            f"staging manifest at {staging_completions!r} does not match the live "
            "contract — refusing to publish from an incomplete staging generation."
        )


def verify_all_complete(
    client: QdrantPoints,
    staging_settings: Settings,
    walked: list[tuple[str, str]],
    inventory: dict[str, InventoryRecord],
    rules_v: str,
    src_labels: str,
    retired: frozenset[str] = frozenset(),
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
            client, staging_settings, rec.doc_id,
            sha256=sha, rules_v=rules_v, source_labels=src_labels,
            source_rev=rec.source_rev,
        ):
            problems.append(path_str)
    problems.extend(
        audit_unmarked_residue(client, staging_settings, walked, inventory, rules_v, retired)
    )
    return problems


def audit_unmarked_residue(
    client: QdrantPoints,
    staging_settings: Settings,
    walked: list[tuple[str, str]],
    inventory: dict[str, InventoryRecord],
    rules_v: str,
    retired: frozenset[str] = frozenset(),
) -> list[str]:
    """Read-only coverage audit (issue #405 R1): every searchable point in
    the candidate generation must be attributable to the walked corpus. A
    point counts as covered when its (doc_id, source_rev) has a verified
    walked inventory record, or — for pre-revision legacy points — when its
    doc_id was walked. Anything else (including points for explicitly
    retired documents, which must already be gone) is a problem, never a
    deletion: absence from a partial walk is not a removal instruction."""
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
    walked_doc_ids = {
        rec.doc_id
        for path_str, _ in walked
        if (rec := inventory.get(path_str)) and rec.doc_id
    }
    residue = 0
    sample: list[str] = []
    for p in scroll_all_points(
        client,
        staging,
        scroll_filter=None,
        with_payload=["doc_id", "source_rev"],
        page_size=staging_settings.ingest_scan_page_size,
    ):
        payload = p.payload or {}
        doc_id = payload.get("doc_id")
        source_rev = payload.get("source_rev")
        if doc_id is not None and doc_id in retired:
            return [
                (
                    f"{staging}: retired document {doc_id!r} still present — "
                    "remove its file or drop the --retire-doc flag."
                )
            ]
        covered = (doc_id, source_rev) in valid_pairs or (
            source_rev is None and doc_id in walked_doc_ids
        )
        if not covered:
            residue += 1
            if len(sample) < 3:
                sample.append(str(p.id))
    if residue:
        return [
            (
                f"{staging}: unmarked residue detected ({residue} point(s)"
                f"{', e.g. ' + ', '.join(sample) if sample else ''}) — refusing "
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
    explicit whole-document retirement whose staging holds no named
    revision afterwards (checked live, mirroring the refresh sole-history
    rule). Returns {doc_id: deleted revision count} for the run log."""
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
