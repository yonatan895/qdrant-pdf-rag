"""Serving-generation gate (issues #391 F3/F4).

The agent must answer only from a generation whose representation contract
it validated. Two rules enforce that:

- the configured `<collection>` name is resolved to the PHYSICAL generation
  behind the alias (or the flat collection itself), and validation reads
  that generation's own `<physical>__completions` contract — never the
  alias-derived name, which could hold stale metadata for another physical
  (F4);
- validation is cached for a short TTL and each request binds to the
  validated physical name it received, so an alias swap between validation
  and retrieval can redirect a later request only after revalidation, never
  an in-flight one (F3/F4).

Readiness (`/healthz`) and request refusal share this boundary; the gate
itself is pure read-only and raises nothing client-facing — callers map a
non-servable `ServingGeneration` to their stable response.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from mainframe_rag.config import Settings
from mainframe_rag.ingest.build import (
    BuildBinding,
    decode_build_binding,
    require_published_binding,
)
from mainframe_rag.ingest.publish import publication_metadata_point_id
from mainframe_rag.ingest.representation import (
    resolve_serving_generation as resolve_representation_generation,
)
from mainframe_rag.ports import AsyncQdrantReader

# Outcomes a request may be served against (`resolve_serving_generation`
# vocabulary). `empty` is deliberately absent: readiness keeps it as the
# bootstrap state, but there is nothing to retrieve yet.
SERVABLE_OUTCOMES = ("compatible", "record_only_drift")


@dataclass(frozen=True)
class ServingGeneration:
    """One validated serving target: the resolved physical collection (None
    when nothing is published), its representation outcome, and the build
    binding the gate validated as published or retained. `binding` is None
    for completed legacy generations, which never mint evidence references."""

    physical: str | None
    outcome: str
    details: tuple[str, ...] = ()
    binding: BuildBinding | None = None

    @property
    def servable(self) -> bool:
        return self.physical is not None and self.outcome in SERVABLE_OUTCOMES


async def resolve_published_generation(
    client: AsyncQdrantReader, settings: Settings, rules_v: str,
) -> ServingGeneration:
    """Validate representation and immutable publication before caching a target."""
    physical, outcome, details = await resolve_representation_generation(client, settings, rules_v)
    if physical is None or outcome not in SERVABLE_OUTCOMES:
        return ServingGeneration(physical, outcome, tuple(details))
    control = physical + "__completions"
    try:
        aliases = await client.get_aliases()
        records = await client.retrieve(
            control, ids=[publication_metadata_point_id(control)], with_payload=True,
        )
        payload = (records[0].payload or {}) if records else None
        if payload is not None and payload.get("record_type") != "publication-metadata":
            raise ValueError("invalid publication control record")
        binding = decode_build_binding(payload, control) if payload is not None else None
        # Publication may advance after representation validation. The
        # already resolved old physical remains valid as a retained build.
        require_published_binding(binding, settings.qdrant_collection, physical,
                                  {a.alias_name: a.collection_name for a in aliases.aliases})
    except Exception:  # noqa: BLE001 — fixed refusal, never upstream/storage text
        return ServingGeneration(physical, "unknown", ("build_control",))
    return ServingGeneration(physical, outcome, tuple(details), binding)


class ServingGate:
    """TTL-cached alias resolution + contract validation.

    Within the TTL window every request receives the cached generation, so
    one validation serves many requests and each request stays bound to the
    physical it validated. After the TTL a fresh resolution replaces the
    cache — that is how a rollback, degradation, or recovery becomes
    visible without a restart. Refusals are cached too (negative caching):
    a degraded generation must not force a Qdrant round-trip per request.
    Concurrent misses share one validation (single-flight lock).
    """

    def __init__(self, ttl_s: float) -> None:
        self._ttl_s = ttl_s
        self._cached: ServingGeneration | None = None
        self._checked_at = 0.0
        self._lock = asyncio.Lock()

    async def generation(
        self,
        client: AsyncQdrantReader,
        settings: Settings,
        rules_v: str,
        *,
        fresh: bool = False,
    ) -> ServingGeneration:
        """Validated generation for this request/probe. `fresh=True` bypasses
        the cache and replaces it (readiness probes; startup gate)."""
        if not fresh and self._fresh():
            assert self._cached is not None
            return self._cached
        async with self._lock:
            if not fresh and self._fresh():
                assert self._cached is not None
                return self._cached
            generation = await resolve_published_generation(client, settings, rules_v)
            self._cached = generation
            self._checked_at = time.monotonic()
            return generation

    def invalidate(self) -> None:
        self._cached = None
        self._checked_at = 0.0

    def _fresh(self) -> bool:
        return (
            self._cached is not None
            and (time.monotonic() - self._checked_at) < self._ttl_s
        )
