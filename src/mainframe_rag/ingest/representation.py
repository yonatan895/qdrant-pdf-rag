"""Collection representation manifest (issue #362, step 1: record-only).

A stored collection is a contract: dense model + immutable revision,
dimension, document-embedding recipe, sparse model/revision, extraction
rules, and identity schema. Same-dimension-but-different-representation
vectors must never silently mix — the 362B step enforces that at ingest
preflight and serving readiness. This step defines the contract, records
it, and ties completions/inventory to it; enforcement stays off.

Compatibility policy (issue #362 req 2) — two classes, one table:

- RE-EMBED-REQUIRED: a change means the stored vectors are stale. The
  362B gate rejects skips/serving until a deliberate migration re-embeds:
  extraction rules, identity schema, embed mode/model/revision/dim,
  contextual block (enabled, LLM id, prompt version, max chars),
  sparse model/weights revision.
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
Absent/unparseable manifest = legacy unversioned collection — an explicit
362B outcome (attest-and-migrate), never a silent pass.
"""

from __future__ import annotations

import hashlib
import json
import uuid

from pydantic import BaseModel
from qdrant_client import models

from mainframe_rag.config import Settings
from mainframe_rag.ingest.context import CONTEXT_PROMPT_VERSION
from mainframe_rag.ports import QdrantPoints

MANIFEST_SCHEMA_VERSION = 1

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


def build_manifest(settings: Settings, rules_v: str) -> RepresentationManifest:
    """Pure function of (settings, rules): identical inputs digest
    identically on any machine, any mount, any PYTHONHASHSEED. `rules_v` is
    a parameter (not read here) so tests pin digests without depending on
    the tree's rule files; callers pass `extraction_rules_version()`."""
    try:
        dim = settings.require_dense_dim()
    except RuntimeError:
        dim = None
    return RepresentationManifest(
        extraction_rules=rules_v,
        embed_mode=settings.embed_mode,
        embed_model=settings.embed_model,
        embed_model_revision=settings.embed_model_revision,
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
    canonical = json.dumps(
        build_manifest(settings, rules_v).model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def manifest_point_id(completions_collection: str) -> str:
    """Deterministic manifest point id (chunk UUID5s untouched)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{_MANIFEST_KEY_PREFIX}|{completions_collection}"))


def write_manifest(
    client: QdrantPoints, completions_collection: str, settings: Settings, rules_v: str
) -> str:
    """Upsert (idempotent overwrite) the manifest point; returns its digest
    for the run log. No payload index needed — reads are get-by-id."""
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
                },
            )
        ],
        wait=True,
    )
    return digest


def read_manifest(
    client: QdrantPoints, completions_collection: str
) -> RepresentationManifest | None:
    """Stored manifest, or None when absent/legacy/unparseable (fail
    closed in 362B — this step only records). Never raises on stored data:
    a corrupt manifest is a legacy outcome, not a crash."""
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
    payload = points[0].payload or {}
    if payload.get("record_type") != _MANIFEST_KEY_PREFIX:
        return None
    manifest = payload.get("manifest")
    if not isinstance(manifest, dict):
        return None
    try:
        return RepresentationManifest.model_validate(manifest)
    except Exception:  # noqa: BLE001 — corrupt stored contract reads as legacy
        return None


def ensure_manifest(
    client: QdrantPoints, completions_collection: str, settings: Settings, rules_v: str
) -> tuple[str, bool]:
    """Commit the run's contract (idempotent): read the stored manifest and
    overwrite only when it differs, so steady-state reruns stay zero-write.
    Returns (digest, committed). The compare-then-write is one rule with
    the write — callers never open-code it."""
    wanted = build_manifest(settings, rules_v)
    if read_manifest(client, completions_collection) == wanted:
        return manifest_digest(settings, rules_v), False
    return write_manifest(client, completions_collection, settings, rules_v), True
