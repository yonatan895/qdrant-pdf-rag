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

Corpus deletions and unmarked residue are swept from staging before cutover
(issue #405 Invariant D3): all searchable points in a published generation are
attributable to verified generation coverage. Publishing a `--limit` subset or
an empty corpus is refused fail-closed by the caller.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from qdrant_client import models

from mainframe_rag.config import Settings
from mainframe_rag.ingest.completion import (
    completion_collection_for,
    completion_collection_name,
    is_doc_complete,
    representation_fingerprint,
)
from mainframe_rag.ingest.inventory import InventoryRecord
from mainframe_rag.ingest.qdrant_io import (
    delete_by_doc,
    delete_by_revision,
    scroll_all_points,
)
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


def staging_name_for(
    collection: str,
    gen_fp: str,
    corpus_fp: str,
    counter: int | None = None,
) -> str:
    """Deterministic staging address: safe charset, bounded length."""
    base = f"{collection}__gen{gen_fp}{corpus_fp}"
    if counter is not None:
        return f"{base}_{counter}"
    return base


def resolve_staging_name(
    client: QdrantPoints,
    collection: str,
    gen_fp: str,
    corpus_fp: str,
    live: str | None,
    force_reingest: bool = False,
) -> str:
    """Resolve a distinct physical staging collection name.

    Under Invariant D4 (Immutable Publication Lifetime Model), a physical
    generation is strictly immutable once published. Publication (including
    --reingest / force_reingest) must never mutate the active serving collection
    in place. When the base generation matches the currently live collection,
    a distinct physical name is allocated so active readers remain completely
    isolated.
    """
    base = staging_name_for(collection, gen_fp, corpus_fp)
    if not force_reingest:
        if live is not None and (live == base or live.startswith(f"{base}_")):
            return live
        return base
    if (live is None or (live != base and not live.startswith(f"{base}_"))) and not client.collection_exists(base):
        return base
    counter = 1
    while True:
        candidate = staging_name_for(collection, gen_fp, corpus_fp, counter=counter)
        if candidate != live and not client.collection_exists(candidate):
            return candidate
        counter += 1


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
) -> list[str]:
    """Paths that must block publication: missing/stale inventory, an
    unverified staging generation, or a contract that is not committed
    (issue #391 F2: a pending migration must never be swapped into
    service). Empty means publishable."""
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
    # Issue #405 Invariant D3: unmarked residue exclusion
    problems.extend(
        audit_unmarked_residue(client, staging_settings, walked, inventory, rules_v)
    )
    return problems


def sweep_unmarked_residue(
    client: QdrantPoints,
    staging_settings: Settings,
    walked: list[tuple[str, str]],
    inventory: dict[str, InventoryRecord],
) -> int:
    """Sweep points and completion markers from staging that do not belong
    to the walked corpus (issue #405 Invariant D3)."""
    staging = staging_settings.qdrant_collection
    if not client.collection_exists(staging):
        return 0
    valid_pairs = {
        (rec.doc_id, rec.source_rev)
        for path_str, _ in walked
        if (rec := inventory.get(path_str))
        and rec.doc_id
        and rec.source_rev
        and rec.status in ("upserted", "skipped")
    }
    valid_docs = {doc_id for doc_id, _ in valid_pairs}

    points = scroll_all_points(
        client,
        staging,
        scroll_filter=None,
        with_payload=["doc_id", "source_rev"],
        page_size=staging_settings.ingest_scan_page_size,
    )
    all_swept_ids: list[Any] = []
    stale_revs: set[str] = set()
    stale_docs: set[str] = set()

    for p in points:
        payload = p.payload or {}
        doc_id = payload.get("doc_id")
        source_rev = payload.get("source_rev")
        if doc_id and source_rev:
            if (doc_id, source_rev) not in valid_pairs:
                stale_revs.add(source_rev)
                all_swept_ids.append(p.id)
        elif doc_id and not source_rev:
            if doc_id not in valid_docs:
                stale_docs.add(doc_id)
                all_swept_ids.append(p.id)
        else:
            all_swept_ids.append(p.id)

    for rev in stale_revs:
        delete_by_revision(client, staging_settings, rev)
    for doc in stale_docs:
        delete_by_doc(client, staging_settings, doc)
    if all_swept_ids:
        client.delete(
            staging,
            points_selector=models.PointIdsList(points=all_swept_ids),
            wait=True,
        )

    _sweep_completion_markers(client, staging_settings, valid_pairs, valid_docs)
    return len(all_swept_ids)


def _sweep_completion_markers(
    client: QdrantPoints,
    staging_settings: Settings,
    valid_pairs: set[tuple[str, str]],
    valid_docs: set[str],
) -> None:
    comp_col = completion_collection_name(staging_settings)
    if not client.collection_exists(comp_col):
        return
    comp_points = scroll_all_points(
        client,
        comp_col,
        scroll_filter=None,
        with_payload=["doc_id", "source_rev"],
        page_size=staging_settings.ingest_scan_page_size,
    )
    stale_comp_ids: list[Any] = []
    for p in comp_points:
        payload = p.payload or {}
        doc_id = payload.get("doc_id")
        if not doc_id:
            continue  # preserve manifest point (no doc_id)
        source_rev = payload.get("source_rev")
        if source_rev:
            if (doc_id, source_rev) not in valid_pairs:
                stale_comp_ids.append(p.id)
        elif doc_id not in valid_docs:
            stale_comp_ids.append(p.id)
    if stale_comp_ids:
        client.delete(
            comp_col,
            points_selector=models.PointIdsList(points=stale_comp_ids),
            wait=True,
        )


def audit_unmarked_residue(
    client: QdrantPoints,
    staging_settings: Settings,
    walked: list[tuple[str, str]],
    inventory: dict[str, InventoryRecord],
    rules_v: str,
) -> list[str]:
    """Audit staging collection to certify absence of unmarked residue (Invariant D3)."""
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

    points = scroll_all_points(
        client,
        staging,
        scroll_filter=None,
        with_payload=["doc_id", "source_rev", "rules_v"],
        page_size=staging_settings.ingest_scan_page_size,
    )
    residue_ids: list[str] = []
    for p in points:
        payload = p.payload or {}
        doc_id = payload.get("doc_id")
        source_rev = payload.get("source_rev")
        point_rules = payload.get("rules_v")
        if (
            doc_id is None
            or source_rev is None
            or (doc_id, source_rev) not in valid_pairs
            or point_rules != rules_v
        ):
            residue_ids.append(str(p.id))

    if residue_ids:
        sample = ", ".join(residue_ids[:3])
        return [
            f"{staging}: unmarked residue detected ({len(residue_ids)} point(s), e.g. {sample})"
        ]
    return []
