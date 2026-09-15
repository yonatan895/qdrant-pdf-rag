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
from dataclasses import dataclass

from mainframe_rag.config import Settings
from mainframe_rag.ingest.completion import (
    completion_collection_for,
    completion_collection_name,
    is_doc_complete,
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
    readable and identical afterward — otherwise preparation is incomplete
    and the inner preflight would misread inherited state as legacy (or,
    worse, a publish would verify against the wrong contract). An existing
    but unreadable staging completions collection is a partial copy or a
    pre-re-key clone: it is discarded and re-copied from live, so the
    transferred metadata is count-verified instead of trusted. Raises with
    explicit remediation when the transfer still cannot complete; live data
    and metadata are never modified.
    """
    from mainframe_rag.ingest.qdrant_io import clone_collection
    from mainframe_rag.ingest.representation import read_manifest, rekey_manifest

    live_completions = completion_collection_for(live)
    staging_completions = completion_collection_name(staging_settings)
    live_manifest = read_manifest(client, live_completions)
    if live_manifest is None:
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
    if read_manifest(client, staging_completions) != live_manifest:
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
    """Paths that must block publication: missing/stale inventory or an
    unverified staging generation. Empty means publishable."""
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
            client, staging_settings, rec.doc_id,
            sha256=sha, rules_v=rules_v, source_labels=src_labels,
            source_rev=rec.source_rev,
        ):
            problems.append(path_str)
    return problems
