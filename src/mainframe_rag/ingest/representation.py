"""Collection representation manifest (issue #362, step 2: enforced).

A stored collection is a contract: dense model + immutable revision,
dimension, document-embedding recipe, sparse model/revision, extraction
rules, and identity schema. Same-dimension-but-different-representation
vectors must never silently mix — enforced at ingest preflight
(`check_ingest_compatible`) and serving readiness (lifespan +
`/healthz` via `serving_outcome`). Step 1 defined the contract and
recorded it; this step turns mismatches into explicit outcomes.

Compatibility policy (issue #362 req 2) — two classes, one table
(`compare_manifests` is the one rule; callers never open-code it):

- RE-EMBED-REQUIRED: a change means the stored vectors are stale. The
  gate rejects skips/serving until a deliberate migration re-embeds:
  extraction rules, identity schema, embed mode/model/revision/dim,
  contextual block (enabled, LLM id, prompt version, max chars),
  sparse model/weights revision. Unknown schema versions count here —
  an unreadable contract is a migration, never a pass.
- RECORD-ONLY: a change never requires re-embedding. The manifest records
  it for audit and evaluation attribution; a mismatch asks for
  re-evaluation, never a re-ingest: dense query prefix (query-side only —
  document chunks stay raw), endpoint URLs (routing, not weights — weight
  changes ride the operator revision attestation).

The query prefix is deliberately inside the manifest rather than the
generation fingerprint: prefix drift desyncs queries from documents, which
is an evaluation event, not a stored-vector event.

Storage: one fixed-ID point in `<collection>__completions` (a Qdrant
collection carries no metadata KV of its own). The completions collection
rides the #359 snapshot-clone, so publish staging inherits the live
manifest and the inner run overwrites it with the newly converged one.
Absent/unparseable manifest on a non-empty collection = legacy
unversioned state — an explicit outcome (attest-and-migrate via
`--reingest`), never a silent pass. An empty target needs no gate: the
run opens its contract `pending` and commits it after the run verifies.

Contract lifecycle (issue #391 F2): the manifest point carries an
envelope-level `state` — `pending` is written BEFORE a migration deletes
or re-embeds anything, `committed` only after the success-path residue
proof shows no marker under an older contract remains. A pending contract
is never skippable (`check_ingest_compatible`), never servable
(`serving_outcome`), and never a basis for a partial `--limit` migration
(`refuse_limited_migration`). Pre-state manifests read as committed.

Skip paths share the contract structurally, not per document: the
preflight proves run-level compatibility before any skip is evaluated, so
a stale completion can never cause a skip under a drifted
representation; `--reingest` (the deliberate migration step) bypasses
skips and re-embeds everything. Marker `manifest_digest` values are
audit at skip time (the generation identity gate is the versioned
fingerprint in `completion.py`) and the commit-time residue proof.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from dataclasses import dataclass

from pydantic import BaseModel, field_validator
from qdrant_client import models

from mainframe_rag.config import Settings
from mainframe_rag.ingest.context import CONTEXT_PROMPT_VERSION
from mainframe_rag.ports import AsyncQdrantPoints, QdrantPoints

MANIFEST_SCHEMA_VERSION = 1

# Envelope-level contract state (issue #391 F2), outside the manifest model
# so representation digests are unchanged by it. `pending` is written before
# a migration mutates anything; only the success-path commit flips it.
STATE_PENDING = "pending"
STATE_COMMITTED = "committed"

# Identity schema carried by the manifest (issue #361): the 361B migration
# switched destructive selectors, locks, completions, and the chunk key
# onto the source revision. Pre-migration manifests carry "doc_id".
IDENTITY_SCHEMA_SOURCE_REV = "source_rev"

# Fixed point id per completions collection: exactly one manifest point can
# exist, and reads need no filter (get-by-id, never a scan).
_MANIFEST_KEY_PREFIX = "representation-manifest"


class RepresentationManifest(BaseModel):
    """Versioned stored-representation contract. No volatile fields
    (timestamps, paths, URLs that vary by environment would break digest
    equality) — endpoint routing lives outside the contract; weight changes
    behind an endpoint ride the operator revision attestation."""

    schema_version: int = MANIFEST_SCHEMA_VERSION
    # Re-embed-required: stored vectors are stale when any of these change.
    extraction_rules: str
    identity_schema: str = IDENTITY_SCHEMA_SOURCE_REV
    embed_mode: str
    embed_model: str | None = None
    embed_model_revision: str = ""
    dense_dim: int | None = None
    contextual_enabled: bool = False
    context_llm_model: str | None = None
    context_prompt_version: str = CONTEXT_PROMPT_VERSION
    context_max_chars: int = 0
    sparse_model: str = ""
    sparse_weights_revision: str = ""
    # Record-only: audit + evaluation attribution, never a re-embed trigger.
    dense_query_prefix: str = ""

    @field_validator("context_llm_model", mode="before")
    @classmethod
    def _blank_context_model_is_absent(cls, value: object) -> object:
        """Blank and unset are the same contract value (issue #391, F1
        follow-up): the prod ingest Job renders `CONTEXT_LLM_MODEL` as an
        empty string while the agent leaves it unset, so without this rule
        every published generation would compare `reembed_required` on a
        field both sides mean as "no contextual LLM". Normalizes on build
        and on read, healing contracts already stored with ""."""
        if isinstance(value, str) and not value.strip():
            return None
        return value


def build_manifest(settings: Settings, rules_v: str) -> RepresentationManifest:
    """Pure function of (settings, rules): identical inputs digest
    identically on any machine, any mount, any PYTHONHASHSEED. `rules_v` is
    a parameter (not read here) so tests pin digests without depending on
    the tree's rule files; callers pass `extraction_rules_version()`. The
    operator revision is stripped: whitespace-only attestation is empty
    attestation (same rule as bearer-auth headers — never `Bearer None`,
    never `" "` as an identity)."""
    try:
        dim = settings.require_dense_dim()
    except RuntimeError:
        dim = None
    return RepresentationManifest(
        extraction_rules=rules_v,
        embed_mode=settings.embed_mode,
        embed_model=settings.embed_model,
        embed_model_revision=settings.embed_model_revision.strip(),
        dense_dim=dim,
        contextual_enabled=settings.contextual_embed_enabled,
        context_llm_model=settings.context_llm_model,
        context_prompt_version=CONTEXT_PROMPT_VERSION,
        context_max_chars=settings.context_max_chars,
        sparse_model=settings.bm25_model,
        sparse_weights_revision=settings.bm25_weights_revision,
        dense_query_prefix=settings.dense_query_prefix,
    )


def manifest_digest(settings: Settings, rules_v: str) -> str:
    """16-hex identity of the representation contract (same width idiom as
    `rules_v`). Canonical JSON (sorted keys, compact separators) over the
    fixed model field order — digest equality means contract equality."""
    return digest_of(build_manifest(settings, rules_v))


def digest_of(manifest: RepresentationManifest) -> str:
    """Digest of a manifest value (stored or wanted) — one rule with
    `manifest_digest`, so error attribution recomputes instead of
    re-reading the store."""
    canonical = json.dumps(
        manifest.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def manifest_point_id(completions_collection: str) -> str:
    """Deterministic manifest point id (chunk UUID5s untouched)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{_MANIFEST_KEY_PREFIX}|{completions_collection}"))


@dataclass(frozen=True)
class StoredManifest:
    """Stored contract plus its envelope state (issue #391 F2)."""

    manifest: RepresentationManifest
    state: str = STATE_COMMITTED


def write_manifest(
    client: QdrantPoints,
    completions_collection: str,
    settings: Settings,
    rules_v: str,
    *,
    state: str = STATE_COMMITTED,
) -> str:
    """Upsert (idempotent overwrite) the manifest point; returns its digest
    for the run log. No payload index needed — reads are get-by-id. The
    state is envelope metadata: it never enters the digest."""
    manifest = build_manifest(settings, rules_v)
    digest = manifest_digest(settings, rules_v)
    try:
        dim = settings.require_dense_dim()
    except RuntimeError:
        dim = None
    dummy_dim = dim or 1
    client.upsert(
        completions_collection,
        points=[
            models.PointStruct(
                id=manifest_point_id(completions_collection),
                vector={
                    "dense": [0.0] * dummy_dim,
                    "bm25": models.SparseVector(indices=[0], values=[1.0]),
                },
                payload={
                    "record_type": _MANIFEST_KEY_PREFIX,
                    "target_collection": completions_collection,
                    "manifest_digest": digest,
                    "manifest": manifest.model_dump(mode="json"),
                    "state": state,
                },
            )
        ],
        wait=True,
    )
    return digest


def _record_from_payload(payload: dict) -> StoredManifest | None:
    """One rule for parsing a stored manifest payload (sync + async readers,
    re-key): the manifest model plus its envelope state. Pre-state payloads
    read as committed — every manifest written before issue #391 F2 was
    committed by construction. A non-string state is corrupt (None, the
    legacy outcome); an unrecognized string is returned verbatim so callers
    treat it as not-committed."""
    if payload.get("record_type") != _MANIFEST_KEY_PREFIX:
        return None
    manifest = payload.get("manifest")
    if not isinstance(manifest, dict):
        return None
    state = payload.get("state", STATE_COMMITTED)
    if not isinstance(state, str):
        return None
    try:
        return StoredManifest(RepresentationManifest.model_validate(manifest), state)
    except Exception:  # noqa: BLE001 — corrupt stored contract reads as legacy
        return None


def read_manifest_record(
    client: QdrantPoints, completions_collection: str
) -> StoredManifest | None:
    """Stored contract + state, or None when absent/legacy/unparseable.
    Never raises on stored data: a corrupt manifest is a legacy outcome,
    not a crash."""
    if not client.collection_exists(completions_collection):
        return None
    try:
        points = client.retrieve(
            completions_collection,
            ids=[manifest_point_id(completions_collection)],
            with_payload=True,
        )
    except Exception:  # noqa: BLE001 — unreachable store reads as absent here
        return None
    if not points:
        return None
    return _record_from_payload(points[0].payload or {})


def read_manifest(
    client: QdrantPoints, completions_collection: str
) -> RepresentationManifest | None:
    """Model-only read, or None when absent/legacy/unparseable. Callers that
    must distinguish pending from committed use `read_manifest_record`."""
    record = read_manifest_record(client, completions_collection)
    return record.manifest if record is not None else None


def begin_manifest(
    client: QdrantPoints, completions_collection: str, settings: Settings, rules_v: str
) -> tuple[str, str]:
    """Open this run's contract handling; returns (digest, mode) with mode in
    `already_current | committed | pending` (issue #391 F2).

    A run that changes re-embed-required fields — or finds an absent or
    unfinished contract — declares the wanted contract `pending` BEFORE any
    document is deleted or re-embedded. Only the success path calls
    `commit_manifest`, after the caller proved no marker under an older
    contract remains, so an interrupted migration can never certify old
    vectors as the new representation. Record-only drift commits
    immediately: no stored vector is touched, so there is nothing to verify.
    Idempotent: a steady-state rerun (or an interrupted rerun of the same
    contract) stays zero-write semantically — a pending state is re-declared
    pending, never upgraded to committed by a begin."""
    wanted = build_manifest(settings, rules_v)
    stored = read_manifest_record(client, completions_collection)
    if stored is not None and stored.state == STATE_COMMITTED:
        if stored.manifest == wanted:
            return digest_of(wanted), "already_current"
        outcome, _ = compare_manifests(stored.manifest, wanted)
        if outcome == RECORD_ONLY_DRIFT:
            return write_manifest(client, completions_collection, settings, rules_v), "committed"
    return (
        write_manifest(client, completions_collection, settings, rules_v, state=STATE_PENDING),
        "pending",
    )


def commit_manifest(
    client: QdrantPoints, completions_collection: str, settings: Settings, rules_v: str
) -> str:
    """Flip this run's contract to committed (idempotent overwrite). The
    caller has already verified every document and proved no older-contract
    marker remains — this function checks nothing (issue #391 F2: the proof
    is a separate rule, not a hidden precondition here)."""
    return write_manifest(
        client, completions_collection, settings, rules_v, state=STATE_COMMITTED
    )


def refuse_limited_migration(
    client: QdrantPoints, settings: Settings, completions_collection: str, rules_v: str
) -> None:
    """`--limit` + a representation migration = refuse (issue #391 F2): a
    partial walk cannot prove the whole searchable collection was
    re-embedded, so it must not commit a collection-wide contract. Read-only;
    raises before any mutation. A fresh empty target is not a migration (no
    stored vectors to mix in), so a deliberate subset bootstrap stays
    possible; record-only drift is not a migration either."""
    stored = read_manifest_record(client, completions_collection)
    if stored is None:
        if not client.collection_exists(settings.qdrant_collection):
            return
        points, _ = client.scroll(settings.qdrant_collection, limit=1, with_payload=False)
        if not points:
            return
        raise RuntimeError(
            f"--limit refuses a legacy migration of {settings.qdrant_collection!r}: the "
            "target holds unattributed vectors and a partial walk cannot re-embed them "
            "all. Re-run with --reingest without --limit."
        )
    wanted = build_manifest(settings, rules_v)
    if (
        stored.state != STATE_COMMITTED
        or compare_manifests(stored.manifest, wanted)[0] == REEMBED_REQUIRED
    ):
        raise RuntimeError(
            f"--limit refuses a representation migration of {settings.qdrant_collection!r}: "
            "a partial walk cannot prove every stored vector was re-embedded under the "
            "wanted contract. Re-run without --limit."
        )


def require_in_place_reconverge(
    client: QdrantPoints, settings: Settings, completions_collection: str, rules_v: str
) -> None:
    """Guard the alias-mode forced in-place reconverge (issue #391 F2):
    `--reingest` may rebuild the live physical only when its stored contract
    IS the wanted contract (committed, or pending from an interrupted run of
    the same contract). A representation change — or a legacy/absent
    contract the caller cannot verify — must address a distinct staging
    generation; force never disables reader isolation. Read-only; raises
    before the alias target is touched."""
    stored = read_manifest_record(client, completions_collection)
    wanted = build_manifest(settings, rules_v)
    if stored is None:
        raise RuntimeError(
            f"refusing to reconverge {settings.qdrant_collection!r} in place: it carries "
            "no readable contract (legacy or absent), so the wanted representation "
            "cannot be proven equal — a migration must publish a distinct staging "
            "generation. Re-run without --reingest for the canonical remediation."
        )
    if compare_manifests(stored.manifest, wanted)[0] == REEMBED_REQUIRED:
        raise RuntimeError(
            f"refusing to reconverge {settings.qdrant_collection!r} in place: its stored "
            "contract differs from the wanted representation (revision/embedding drift), "
            "so rebuilding it would mutate the serving generation. Publish a distinct "
            "staging generation instead (the derived staging name changes with the "
            "representation fingerprint; re-run without --reingest to see the drift)."
        )


# Re-embed-required manifest fields (issue #362 req 2): a change means the
# stored vectors are stale — skips and serving stop until a deliberate
# migration re-embeds. schema_version rides along: an unknown contract is
# a migration, never a pass.
REEMBED_FIELDS = (
    "schema_version",
    "extraction_rules",
    "identity_schema",
    "embed_mode",
    "embed_model",
    "embed_model_revision",
    "dense_dim",
    "contextual_enabled",
    "context_llm_model",
    "context_prompt_version",
    "context_max_chars",
    "sparse_model",
    "sparse_weights_revision",
)

# Record-only manifest fields: audit + evaluation attribution, never a
# re-embed trigger. dense_query_prefix drift asks for re-evaluation, and
# endpoint URLs live outside the contract entirely (routing, not weights).
RECORD_ONLY_FIELDS = ("dense_query_prefix",)

COMPATIBLE = "compatible"
RECORD_ONLY_DRIFT = "record_only_drift"
REEMBED_REQUIRED = "reembed_required"


def compare_manifests(
    stored: RepresentationManifest, wanted: RepresentationManifest
) -> tuple[str, list[str]]:
    """One rule for the compatibility policy: (outcome, changed_fields).
    `stored=None` never reaches here — absent manifests are legacy/empty
    outcomes decided by the caller (they need target emptiness, which
    differs per sync/async side). Field order follows the model."""
    reembed = [f for f in REEMBED_FIELDS if getattr(stored, f) != getattr(wanted, f)]
    if reembed:
        return REEMBED_REQUIRED, reembed
    record = [f for f in RECORD_ONLY_FIELDS if getattr(stored, f) != getattr(wanted, f)]
    if record:
        return RECORD_ONLY_DRIFT, record
    return COMPATIBLE, []


def require_attested_revision(settings: Settings) -> None:
    """Operator attestation (issue #362 req 3): vllm mode needs a non-blank
    `EMBED_MODEL_REVISION` — a mutable gateway alias or a dimension is not
    an immutable model identity, and the application must never infer
    weights it cannot inspect. Hash mode is exempt (CI/dev only; the hash
    recipe never changes, and mode drift is itself re-embed-required).
    Config error, so it raises before any store contact."""
    if settings.embed_mode != "vllm":
        return
    if not settings.embed_model_revision.strip():
        raise RuntimeError(
            "ingest/serving refuses an unattested vllm embedding revision: set "
            "EMBED_MODEL_REVISION to the platform team's immutable model/config "
            f"revision for {settings.embed_model!r} (a gateway alias is mutable "
            "and a dimension is not an identity). Without it, a same-dimension "
            "model swap would silently mix stored and query vectors."
        )


def check_ingest_compatible(
    client: QdrantPoints,
    settings: Settings,
    completions_collection: str,
    rules_v: str,
) -> tuple[str, list[str]]:
    """Ingest preflight gate (issue #362 req 4/5): prove the target's stored
    representation accepts this run before any parse, delete, or upsert.
    Returns (wanted_digest, record_drift_fields); record-only drift
    proceeds (caller logs the re-evaluation note). Raises RuntimeError
    with the stable remediation otherwise:
    - unattested vllm revision → declare EMBED_MODEL_REVISION;
    - legacy unversioned target → `--reingest` to attest-and-migrate;
    - re-embed drift → `--reingest` to re-embed every doc.
    Callers bypass under `--reingest` (the one deliberate migration step,
    same override idiom as the #124 rules gate); a bypassed run skips
    nothing downstream, so it re-embeds everything instead of mixing.
    Never recreates a collection, never downgrades modes."""
    require_attested_revision(settings)
    wanted = build_manifest(settings, rules_v)
    digest = digest_of(wanted)
    stored = read_manifest_record(client, completions_collection)
    if stored is None:
        points, _ = client.scroll(
            settings.qdrant_collection, limit=1, with_payload=False
        )
        if not points:
            return digest, []  # empty target: the run commits its manifest
        raise RuntimeError(
            f"collection {settings.qdrant_collection!r} predates the representation "
            "manifest (stored points but no contract — legacy unversioned state). "
            "Re-ingest required: re-run with --reingest to attest-and-migrate "
            "(never skip against unattributed vectors)."
        )
    if stored.state != STATE_COMMITTED:
        raise RuntimeError(
            f"collection {settings.qdrant_collection!r} has an unfinished representation "
            f"migration (stored contract state {stored.state!r}): re-run with --reingest "
            "to resume the re-embed (never skip or serve against a contract that was "
            "not fully verified)."
        )
    outcome, fields = compare_manifests(stored.manifest, wanted)
    if outcome == REEMBED_REQUIRED:
        raise RuntimeError(
            f"representation drift on {', '.join(fields)}: collection "
            f"{settings.qdrant_collection!r} was embedded under a different "
            "contract (stored manifest digest "
            f"{digest_of(stored.manifest)!r}, this run wants {digest!r}). "
            "Re-ingest required: re-run with --reingest to re-embed every doc "
            "(never skip against incompatible vectors)."
        )
    return digest, fields if outcome == RECORD_ONLY_DRIFT else []


def rekey_manifest(
    client: QdrantPoints, src_completions: str, dst_completions: str
) -> bool:
    """Carry the manifest across a snapshot-clone (publish staging): the
    fixed point id embeds the collection name, so a byte copy is unreadable
    under the new name. Re-keys live's contract VERBATIM (same model, same
    vector, same envelope state, only the id and envelope target change) —
    never recomputes from current settings, or a drifted run would see its
    own wanted contract and sail through its preflight; carrying the state
    means a pending live cannot be laundered into a committed staging.
    Returns False when the source carries no manifest (legacy live: the
    inner preflight then reports legacy explicitly).

    The vector projection is explicit (issue #391 F5): `retrieve` defaults
    to `with_vectors=False`, so relying on the client default made this
    return False against every real server while the permissive fakes hid
    it. A source point without a vector is still refused — never fabricate
    the destination contract from current settings."""
    if not client.collection_exists(src_completions):
        return False
    points = client.retrieve(
        src_completions,
        ids=[manifest_point_id(src_completions)],
        with_payload=True,
        with_vectors=True,
    )
    if not points:
        return False
    stored = _record_from_payload(points[0].payload or {})
    if stored is None:
        return False
    vector = getattr(points[0], "vector", None)
    if vector is None:
        return False
    client.upsert(
        dst_completions,
        points=[
            models.PointStruct(
                id=manifest_point_id(dst_completions),
                vector=vector,
                payload={
                    "record_type": _MANIFEST_KEY_PREFIX,
                    "target_collection": dst_completions,
                    "manifest_digest": digest_of(stored.manifest),
                    "manifest": stored.manifest.model_dump(mode="json"),
                    "state": stored.state,
                },
            )
        ],
        wait=True,
    )
    return True


async def read_manifest_record_async(
    async_client: AsyncQdrantPoints | QdrantPoints, completions_collection: str
) -> StoredManifest | None:
    """Async mirror of `read_manifest_record` for the serving path (lifespan
    + `/healthz`): stored contract + state, or None when
    absent/legacy/unparseable. Never raises on stored content; transport
    errors propagate so the caller can report `unknown` instead of guessing.
    Sync test doubles resolve inline through the shared shim (same
    discipline as the retrieval legs)."""
    points = await _await_client(
        async_client.retrieve(
            completions_collection,
            ids=[manifest_point_id(completions_collection)],
            with_payload=True,
        )
    )
    if not points:
        return None
    return _record_from_payload(points[0].payload or {})


async def read_manifest_async(
    async_client: AsyncQdrantPoints | QdrantPoints, completions_collection: str
) -> RepresentationManifest | None:
    """Model-only async read. Serving callers that must distinguish pending
    from committed use `read_manifest_record_async`."""
    record = await read_manifest_record_async(async_client, completions_collection)
    return record.manifest if record is not None else None


async def serving_outcome(
    async_client: AsyncQdrantPoints | QdrantPoints,
    settings: Settings,
    completions_collection: str,
    rules_v: str,
) -> tuple[str, list[str]]:
    """One rule for serving readiness (lifespan + `/healthz` share it):
    (outcome, details). Outcomes: `compatible`, `record_only_drift`,
    `reembed_required`, `legacy` (non-empty target, no contract),
    `empty` (nothing stored yet), `pending` (an unfinished contract
    migration — issue #391 F2: never servable), `unknown` (store unreadable
    — a transient, never a pass and never a rejection). Attestation raises
    like the ingest path (config error, before contact)."""
    require_attested_revision(settings)
    try:
        stored = await read_manifest_record_async(async_client, completions_collection)
        if stored is None:
            points, _ = await _await_client(
                async_client.scroll(
                    settings.qdrant_collection, limit=1, with_payload=False
                )
            )
            return ("empty", []) if not points else ("legacy", [])
        if stored.state != STATE_COMMITTED:
            return "pending", []
        return compare_manifests(stored.manifest, build_manifest(settings, rules_v))
    except Exception:  # noqa: BLE001 — an unreadable store is unknown, not incompatible
        return "unknown", []


async def _await_client(res):
    """Sync/async client shim for the serving path: the pooled async client
    awaits while sync test doubles resolve inline — one helper serves
    lifespan + `/healthz` so the twin call sites cannot diverge."""
    if inspect.isawaitable(res):
        return await res
    return res
