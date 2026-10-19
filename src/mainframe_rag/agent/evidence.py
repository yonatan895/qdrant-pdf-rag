"""Exact-evidence service (issue #405 E1): references, read-only exact reads,
and source listing from the publish-time catalog (issue #828).

One shared, async, LLM-free use case that HTTP (`GET /v1/evidence/{reference}`,
references on `POST /v1/search` hits) and the downstream knowledge MCP adapter
consume. Contract owner: docs/evidence-contract.md (stored-payload profile).

What a reference binds. A search hit's `ep1.` reference carries the full build
UUID of the published build that served it, the point's existing UUID5 chunk id
and the SHA-256 of a canonical envelope built from the point's STORED payload
(text, source revision/hash, doc/page identity, heading, chunk type, recorded
atomic unit ranges). A read pins the build through its immutable per-build
aliases (never the ordinary serving alias, so an alias swap cannot redirect it),
verifies the paired control record, fetches the one point, re-derives the
envelope and compares digests. Changed, missing or unverifiable stored data is
an explicit refusal; nothing is re-extracted, re-embedded, searched or taken
from another build.

What it does not do (see the contract's status table): the full `e1.` profile
needs a byte-to-page location map the ingest path does not retain, so this
profile reports the stored chunk page span only; and there is no retirement
tombstone writer yet, so an absent build is "unavailable", never "retired".

Source listing (#828) reads only the pinned build's catalog entries
(`ingest/catalog.py`, #827) from its control collection: one filtered,
projected `scroll` per page, never chunk text or the data collection.

Read-only by construction: the service touches only `get_aliases`,
`collection_exists`, `retrieve` and `scroll`, so the serving (read-only)
credential suffices. It performs no model, embed or rerank call.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, get_args

from mainframe_rag.agent.serving import ServingGeneration
from mainframe_rag.config import Settings
from mainframe_rag.ingest.build import (
    BuildBinding,
    build_aliases,
    decode_build_binding,
)
from mainframe_rag.ingest.catalog import (
    CATALOG_RECORD_TYPE,
    SourceCatalogError,
    decode_catalog_point,
)
from mainframe_rag.ingest.classify import ChunkType
from mainframe_rag.ingest.publish import publication_metadata_point_id
from mainframe_rag.logs import error_type
from mainframe_rag.ports import AsyncQdrantReader

log = logging.getLogger("agent.evidence")

REFERENCE_PREFIX = "ep1."
ENVELOPE_SCHEMA = 1
ENVELOPE_PROFILE = "stored-payload"
_REFERENCE_BYTES = 16 + 16 + 32
_REFERENCE_LEN = len(REFERENCE_PREFIX) + (_REFERENCE_BYTES * 4 + 2) // 3
_CHUNK_TYPES = frozenset(get_args(ChunkType))
_HEX64 = frozenset("0123456789abcdef")

FailureKind = Literal[
    "invalid_reference",
    "invalid_cursor",
    "unauthenticated",
    "unavailable",
    "corrupt",
    "access_unavailable",
    "budget_exceeded",
    "timeout",
    "upstream",
]

# Public mapping, one owner: (HTTP status, stable code, fixed message). The
# internal reason stays on the exception for logs only. `unavailable` (denied
# or unknown) and `corrupt` (missing/changed retained bytes or controls) share
# code and message by design: the status is the only public distinction.
PUBLIC_FAILURES: dict[FailureKind, tuple[int, str, str]] = {
    "invalid_reference": (400, "invalid_evidence_reference", "the evidence reference is not valid"),
    "unauthenticated": (401, "authentication_required", "authentication is required"),
    "unavailable": (404, "evidence_unavailable", "the requested evidence is not available"),
    "corrupt": (503, "evidence_unavailable", "the requested evidence is not available"),
    "access_unavailable": (503, "access_unavailable", "access policy is not available"),
    "budget_exceeded": (
        413, "evidence_budget_exceeded", "the evidence exceeds the requested size budget",
    ),
    "timeout": (504, "evidence_timeout", "the evidence read timed out"),
    "upstream": (502, "upstream_error", "evidence read failed"),
}

# The source listing's own table (#828): same shape, listing wording. A cursor
# for an unknown or no-longer-retained build is 404; missing catalog,
# redirected controls or a malformed visible entry is 503 (same code).
SOURCES_PUBLIC_FAILURES: dict[FailureKind, tuple[int, str, str]] = {
    "invalid_cursor": (400, "invalid_sources_cursor", "the sources cursor is not valid"),
    "unauthenticated": (401, "authentication_required", "authentication is required"),
    "unavailable": (404, "sources_unavailable", "the requested sources are not available"),
    "corrupt": (503, "sources_unavailable", "the requested sources are not available"),
    "access_unavailable": (503, "access_unavailable", "access policy is not available"),
    "timeout": (504, "sources_timeout", "the sources listing timed out"),
    "upstream": (502, "upstream_error", "sources listing failed"),
}

SOURCES_CURSOR_PREFIX = "ls1."
_CURSOR_BYTES = 16 + 16
_CURSOR_LEN = len(SOURCES_CURSOR_PREFIX) + (_CURSOR_BYTES * 4 + 2) // 3
SOURCES_LIMIT_MAX = 200
# Bounds (#828 D4): catalog records examined per request, and serialized
# entries per page (the first entry is always included).
SOURCES_SCAN_CAP = 200
SOURCES_PAGE_BYTES = 256 * 1024
_CATALOG_FIELDS = ["record_type", "catalog_schema", "entry"]


class EvidenceFailure(Exception):
    """A typed refusal. `reason` is a fixed internal label for logs/tests."""

    def __init__(self, kind: FailureKind, reason: str) -> None:
        super().__init__(reason)
        self.kind: FailureKind = kind
        self.reason = reason


# ---------------------------------------------------------------- access port


@dataclass(frozen=True)
class TrustedCaller:
    """Caller identity established by the approved ingress, never by request
    fields, filters or session ids. `principal=None` is the shared-corpus
    caller of a deployment whose ingress authenticates upstream (#373)."""

    principal: str | None = None


@dataclass(frozen=True)
class EvidenceScope:
    """What an entitlement decision may depend on, taken from stored data."""

    source_revision: str
    doc_id: str
    product: str | None
    version: str | None


@dataclass(frozen=True)
class AccessGrant:
    policy_version: str


class AccessUnavailable(Exception):
    """The authority cannot decide: fail closed, never fall back to a cache."""


class Unauthenticated(Exception):
    """No usable caller identity where one is required."""


class EvidenceAccess(Protocol):
    async def authorize(self, caller: TrustedCaller, scope: EvidenceScope) -> AccessGrant | None:
        """Grant, or None for denied. May raise AccessUnavailable/Unauthenticated."""

    async def current_version(self) -> str:
        """The authority's present policy version (raise AccessUnavailable)."""


class SharedCorpusAccess:
    """Today's deployment mode: any caller that reached the API may read the
    shared corpus. It is explicit here so the service never has an implicit
    allow-all; per-source entitlement is #373 and replaces this object, not the
    service. Evidence is never cached, so there is no cached permission."""

    async def authorize(self, caller: TrustedCaller, scope: EvidenceScope) -> AccessGrant | None:
        return AccessGrant(policy_version="shared-corpus")

    async def current_version(self) -> str:
        return "shared-corpus"


# ------------------------------------------------------------ envelope / ref


@dataclass(frozen=True)
class Evidence:
    reference: str
    digest: str
    build_id: str
    chunk_id: str
    generation_fingerprint: str
    source_revision: str
    source_sha256: str
    doc_id: str
    title: str
    product: str | None
    version: str | None
    heading_path: str
    chunk_type: str
    text: str
    text_bytes: int
    atomic_spans: tuple[tuple[int, int], ...] | None
    physical_page_start: int
    physical_page_end: int | None
    printed_label: str | None


@dataclass(frozen=True)
class ParsedReference:
    build_id: str
    chunk_id: str
    digest: bytes


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("not a string")
    parsed = uuid.UUID(value)
    if str(parsed) != value:
        raise ValueError("not canonical")
    return value


def encode_reference(build_id: str, chunk_id: str, digest: bytes) -> str:
    raw = uuid.UUID(_canonical_uuid(build_id)).bytes + uuid.UUID(_canonical_uuid(chunk_id)).bytes
    if len(digest) != 32:
        raise ValueError("digest must be 32 bytes")
    token = base64.urlsafe_b64encode(raw + digest).rstrip(b"=").decode("ascii")
    return REFERENCE_PREFIX + token


def parse_reference(token: object) -> ParsedReference:
    """Strict, canonical-only decode. Raises EvidenceFailure(invalid_reference)."""

    def bad() -> EvidenceFailure:
        return EvidenceFailure("invalid_reference", "malformed")

    if not isinstance(token, str) or len(token) != _REFERENCE_LEN or not token.isascii():
        raise bad()
    if not token.startswith(REFERENCE_PREFIX):
        raise bad()
    body = token[len(REFERENCE_PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (binascii.Error, ValueError) as exc:
        raise bad() from exc
    if len(raw) != _REFERENCE_BYTES or base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != body:
        raise bad()
    build = uuid.UUID(bytes=raw[:16])
    chunk = uuid.UUID(bytes=raw[16:32])
    if build.int == 0:
        raise bad()
    return ParsedReference(str(build), str(chunk), raw[32:])


def encode_sources_cursor(build_id: str, next_point: str) -> str:
    """`ls1.` + URL-safe base64 of the build UUID and the next catalog point
    id to examine. A position, never a grant: every page re-authorizes."""
    raw = uuid.UUID(_canonical_uuid(build_id)).bytes + uuid.UUID(_canonical_uuid(next_point)).bytes
    return SOURCES_CURSOR_PREFIX + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def parse_sources_cursor(token: object) -> tuple[str, str]:
    """Strict, canonical-only decode to (build_id, next point id). Raises
    EvidenceFailure(invalid_cursor) before any storage contact."""

    def bad() -> EvidenceFailure:
        return EvidenceFailure("invalid_cursor", "malformed")

    if not isinstance(token, str) or len(token) != _CURSOR_LEN or not token.isascii():
        raise bad()
    if not token.startswith(SOURCES_CURSOR_PREFIX):
        raise bad()
    body = token[len(SOURCES_CURSOR_PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (binascii.Error, ValueError) as exc:
        raise bad() from exc
    if len(raw) != _CURSOR_BYTES or base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != body:
        raise bad()
    build = uuid.UUID(bytes=raw[:16])
    if build.int == 0:
        raise bad()
    return str(build), str(uuid.UUID(bytes=raw[16:]))


@dataclass(frozen=True)
class SourceEntry:
    """One published source revision of the pinned build (catalog entry).
    Pages are one-based physical, like exact reads; labels are printed
    labels, None where no honest label exists (#271)."""

    source_revision: str
    source_sha256: str
    doc_id: str
    title: str
    product: str | None
    version: str | None
    chunk_count: int
    physical_page_first: int
    physical_page_last: int
    page_label_first: str | None
    page_label_last: str | None
    indexed_pages: int

    def wire(self) -> dict[str, Any]:
        return {
            "source_revision": self.source_revision, "source_sha256": self.source_sha256,
            "doc_id": self.doc_id, "title": self.title, "product": self.product,
            "version": self.version, "chunk_count": self.chunk_count,
            "physical_pages": {"first": self.physical_page_first, "last": self.physical_page_last},
            "page_labels": {"first": self.page_label_first, "last": self.page_label_last},
            "indexed_pages": self.indexed_pages,
        }


@dataclass(frozen=True)
class SourcePage:
    build_id: str
    sources: tuple[SourceEntry, ...]
    next_cursor: str | None


def _source_entry(entry: Mapping[str, Any]) -> SourceEntry:
    return SourceEntry(
        source_revision=entry["source_revision"], source_sha256=entry["source_sha256"],
        doc_id=entry["doc_id"], title=entry["title"], product=entry["product"],
        version=entry["version"], chunk_count=entry["chunk_count"],
        physical_page_first=entry["pages"]["first"] + 1,
        physical_page_last=entry["pages"]["last"] + 1,
        page_label_first=entry["page_labels"]["first"],
        page_label_last=entry["page_labels"]["last"],
        indexed_pages=entry["indexed_pages"],
    )


def _catalog_scope(payload: Mapping[str, Any]) -> EvidenceScope:
    """Just the labels an entitlement decision needs, so authorization runs
    before the entry's other fields are judged (a corrupt but denied entry
    stays unobservable)."""
    entry = payload.get("entry")
    if not isinstance(entry, dict):
        raise EvidenceFailure("corrupt", "catalog_entry")
    rev = entry.get("source_revision")
    if not isinstance(rev, str) or not rev:
        raise EvidenceFailure("corrupt", "source_revision")
    return EvidenceScope(rev, _str(entry.get("doc_id")),
                         _opt_str(entry.get("product")), _opt_str(entry.get("version")))


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise EvidenceFailure("corrupt", "payload_field_type")
    return value


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise EvidenceFailure("corrupt", "payload_field_type")
    return value


def _atomic_ranges(raw: object, text: str) -> tuple[tuple[int, int], ...] | None:
    """Stored `units` ([start, end, kind] over characters) as UTF-8 byte ranges
    of the atomic items. Absent key means not recorded (None), never "no
    atomic items": the chunker omits it for known prose AND for capped spans."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise EvidenceFailure("corrupt", "units_shape")
    cumulative = [0]
    for ch in text:
        cumulative.append(cumulative[-1] + len(ch.encode("utf-8")))
    ranges: list[tuple[int, int]] = []
    previous_end = 0
    for entry in raw:
        if not isinstance(entry, list) or len(entry) != 3:
            raise EvidenceFailure("corrupt", "units_shape")
        start, end, kind = entry
        if (
            _int(start) is None
            or _int(end) is None
            or kind not in ("atomic", "prose")
            or not (previous_end <= start < end <= len(text))
        ):
            raise EvidenceFailure("corrupt", "units_range")
        previous_end = end
        if kind == "atomic":
            ranges.append((cumulative[start], cumulative[end]))
    return tuple(ranges)


def scope_of(payload: Mapping[str, Any]) -> EvidenceScope:
    rev = payload.get("source_rev")
    if not isinstance(rev, str) or not rev:
        raise EvidenceFailure("corrupt", "source_revision")
    return EvidenceScope(rev, _str(payload.get("doc_id", "")),
                         _opt_str(payload.get("product")), _opt_str(payload.get("version")))


def build_evidence(binding: BuildBinding, chunk_id: str, payload: Mapping[str, Any]) -> Evidence:
    """The one derivation used for minting AND reading, so both agree by
    construction. Fails closed on any stored field that is not what the
    ingest writer produces."""
    scope = scope_of(payload)
    sha = payload.get("sha256")
    if not isinstance(sha, str) or len(sha) != 64 or not set(sha) <= _HEX64:
        raise EvidenceFailure("corrupt", "source_sha256")
    text = payload.get("text")
    if not isinstance(text, str) or not text:
        raise EvidenceFailure("corrupt", "text")
    chunk_type = payload.get("chunk_type")
    if not isinstance(chunk_type, str) or chunk_type not in _CHUNK_TYPES:
        raise EvidenceFailure("corrupt", "chunk_type")
    title = _str(payload.get("title", ""))
    heading_path = _str(payload.get("heading_path", ""))
    page_start = _int(payload.get("page_start"))
    if page_start is None or page_start < 0:
        raise EvidenceFailure("corrupt", "page_start")
    page_end: int | None = None
    if payload.get("page_end") is not None:
        page_end = _int(payload.get("page_end"))
        if page_end is None or page_end < page_start:
            raise EvidenceFailure("corrupt", "page_end")
    label = _str(payload.get("page_label", ""))
    try:
        text_bytes = len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise EvidenceFailure("corrupt", "text_encoding") from exc
    spans = _atomic_ranges(payload.get("units"), text)
    envelope = {
        "schema": ENVELOPE_SCHEMA,
        "profile": ENVELOPE_PROFILE,
        "build_id": binding.build_id,
        "chunk_id": chunk_id,
        "generation_fingerprint": binding.gen_fp,
        "source_revision": scope.source_revision,
        "source_sha256": sha,
        "doc_id": scope.doc_id,
        "title": title,
        "product": scope.product,
        "version": scope.version,
        "heading_path": heading_path,
        "chunk_type": chunk_type,
        "text": text,
        "atomic_spans": None if spans is None else [{"start": s, "end": e} for s, e in spans],
        # One-based physical pages (stored indexes are zero-based). The label
        # is the stored display label (possibly a range string), null if empty.
        "physical_page_start": page_start + 1,
        "physical_page_end": None if page_end is None else page_end + 1,
        "printed_label": label or None,
    }
    try:
        encoded = json.dumps(
            envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (ValueError, UnicodeEncodeError) as exc:
        raise EvidenceFailure("corrupt", "envelope_encoding") from exc
    digest = hashlib.sha256(encoded).digest()
    return Evidence(
        reference=encode_reference(binding.build_id, chunk_id, digest),
        digest=digest.hex(),
        build_id=binding.build_id,
        chunk_id=chunk_id,
        generation_fingerprint=binding.gen_fp,
        source_revision=scope.source_revision,
        source_sha256=sha,
        doc_id=scope.doc_id,
        title=title,
        product=scope.product,
        version=scope.version,
        heading_path=heading_path,
        chunk_type=chunk_type,
        text=text,
        text_bytes=text_bytes,
        atomic_spans=spans,
        physical_page_start=page_start + 1,
        physical_page_end=None if page_end is None else page_end + 1,
        printed_label=label or None,
    )


# --------------------------------------------------------------------- service


def _control_of(physical: str) -> str:
    return physical + "__completions"


class EvidenceService:
    """`client` is the serving (read-only) Qdrant client, sync or async."""

    def __init__(
        self, client: AsyncQdrantReader, settings: Settings, access: EvidenceAccess
    ) -> None:
        self._client = client
        self._settings = settings
        self._access = access

    # -- storage reads (each is a real await boundary on the async client)

    async def _aliases(self) -> dict[str, str]:
        listing = await self._client.get_aliases()
        return {a.alias_name: a.collection_name for a in listing.aliases}

    async def _binding(self, physical: str) -> tuple[BuildBinding, bool] | None:
        """The build binding and whether its receipt binds a source catalog
        (build schema 3, #827); `decode_build_binding` validates both."""
        control = _control_of(physical)
        if not await self._client.collection_exists(control):
            return None
        records = await self._client.retrieve(
            control, ids=[publication_metadata_point_id(control)], with_payload=True,
        )
        if not records:
            return None
        payload = records[0].payload or {}
        if payload.get("record_type") != "publication-metadata":
            raise EvidenceFailure("corrupt", "control_record")
        try:
            return decode_build_binding(payload, control), "source_catalog" in payload
        except (ValueError, TypeError) as exc:
            raise EvidenceFailure("corrupt", "control_binding") from exc

    # -- minting (search side)

    async def mint_references(
        self, generation: ServingGeneration, chunk_ids: Sequence[str]
    ) -> dict[str, str]:
        """References for the hits a search just served from `generation`.

        Minting uses the build binding the serving gate validated as published
        or retained (agent.serving) instead of re-reading the control plane per
        request. An exact read re-resolves the build through its immutable
        aliases, so a build retired after validation is refused there, never
        read from a successor. Empty when the generation is not servable, has
        no build binding, or binds
        another logical corpus. A chunk whose stored payload cannot form a
        complete envelope is skipped, never given a partial reference."""
        ids = list(dict.fromkeys(chunk_ids))
        binding = generation.binding
        if (
            not ids
            or not generation.servable
            or binding is None
            or binding.physical != generation.physical
            or binding.alias != self._settings.qdrant_collection
        ):
            return {}
        records = await self._client.retrieve(binding.physical, ids, with_payload=True)
        minted: dict[str, str] = {}
        for record in records:
            chunk_id = str(record.id)
            try:
                minted[chunk_id] = build_evidence(binding, chunk_id, record.payload or {}).reference
            except (EvidenceFailure, ValueError):
                log.warning("evidence reference not minted: reason=payload_incomplete")
        return minted

    # -- exact read

    async def read_evidence(
        self,
        caller: TrustedCaller,
        reference: str,
        *,
        max_bytes: int | None = None,
        product: str | None = None,
        version: str | None = None,
    ) -> Evidence:
        """Exact stored evidence for one reference, or a typed EvidenceFailure.

        `product`/`version` are optional caller assertions that may only
        narrow: a mismatch is the same refusal as a denied read."""
        parsed = parse_reference(reference)  # no storage contact before this passes
        try:
            async with asyncio.timeout(self._settings.evidence_timeout_s):
                return await self._read(caller, parsed, max_bytes, product, version)
        except TimeoutError as exc:
            raise EvidenceFailure("timeout", "deadline") from exc
        except EvidenceFailure:
            raise
        except Exception as exc:
            raise EvidenceFailure("upstream", error_type(exc)) from exc

    async def _read(
        self,
        caller: TrustedCaller,
        parsed: ParsedReference,
        max_bytes: int | None,
        product: str | None,
        version: str | None,
    ) -> Evidence:
        cap = self._settings.evidence_max_bytes
        budget = cap if max_bytes is None else min(max_bytes, cap)
        binding, physical = await self._pinned_build(parsed.build_id)
        records = await self._client.retrieve(
            physical, [parsed.chunk_id], with_payload=True,
        )
        if len(records) != 1 or str(records[0].id) != parsed.chunk_id:
            raise EvidenceFailure("corrupt", "chunk_missing")
        payload = records[0].payload or {}
        scope = scope_of(payload)
        grant = await self._authorize(caller, scope)
        if (product is not None and scope.product != product) or (
            version is not None and scope.version != version
        ):
            raise EvidenceFailure("unavailable", "scope_mismatch")
        evidence = build_evidence(binding, parsed.chunk_id, payload)
        if not hmac.compare_digest(bytes.fromhex(evidence.digest), parsed.digest):
            raise EvidenceFailure("corrupt", "digest_mismatch")
        if evidence.text_bytes > budget:
            raise EvidenceFailure("budget_exceeded", "over_budget")
        # Before the response is produced, re-ask the authority: a policy
        # change since admission is re-decided, never assumed unchanged.
        if await self._current_version() != grant.policy_version:
            await self._authorize(caller, scope)
        return evidence

    # -- source listing (#828)

    async def list_sources(
        self,
        caller: TrustedCaller,
        *,
        limit: int,
        cursor: str | None = None,
        served_build: str | None = None,
        product: str | None = None,
        version: str | None = None,
    ) -> SourcePage:
        """One page of the pinned build's sources, or a typed EvidenceFailure.

        The first page lists `served_build` (the serving gate's validated
        build); later pages list the cursor's build only, resolved through
        its immutable aliases, so an alias move cannot redirect a listing in
        flight. `product`/`version` narrow by exact label equality only.
        Denied sources are omitted with no count; a malformed visible entry
        fails the whole page."""
        if cursor is not None:
            build_id, offset = parse_sources_cursor(cursor)  # no storage contact before this
        elif served_build is not None:
            build_id, offset = served_build, None
        else:
            raise EvidenceFailure("unavailable", "no_served_build")
        if not 1 <= limit <= SOURCES_LIMIT_MAX:
            raise ValueError("limit out of range")
        try:
            async with asyncio.timeout(self._settings.evidence_timeout_s):
                return await self._list(caller, build_id, offset, limit, product, version)
        except TimeoutError as exc:
            raise EvidenceFailure("timeout", "deadline") from exc
        except EvidenceFailure:
            raise
        except Exception as exc:
            raise EvidenceFailure("upstream", error_type(exc)) from exc

    async def _list(
        self,
        caller: TrustedCaller,
        build_id: str,
        offset: str | None,
        limit: int,
        product: str | None,
        version: str | None,
    ) -> SourcePage:
        from qdrant_client import models

        _, physical = await self._pinned_build(build_id, require_catalog=True)
        control = _control_of(physical)
        must: list[models.Condition] = [
            models.FieldCondition(key="record_type", match=models.MatchValue(value=CATALOG_RECORD_TYPE))
        ]
        for key, value in (("entry.product", product), ("entry.version", version)):
            if value is not None:
                must.append(models.FieldCondition(key=key, match=models.MatchValue(value=value)))
        records, after = await self._client.scroll(
            control, scroll_filter=models.Filter(must=must), limit=min(limit, SOURCES_SCAN_CAP),
            offset=offset, with_payload=_CATALOG_FIELDS,
        )
        kept: list[tuple[SourceEntry, EvidenceScope, AccessGrant]] = []
        used = 0
        next_point = None if after is None else str(after)
        for record in records:
            payload = record.payload or {}
            scope = _catalog_scope(payload)
            grant = await self._authorize_listing(caller, scope)
            if grant is None:
                continue  # denied: omitted, never counted
            try:
                entry = _source_entry(decode_catalog_point(control, record.id, payload))
            except SourceCatalogError as exc:
                raise EvidenceFailure("corrupt", "catalog_entry") from exc
            size = len(json.dumps(entry.wire(), ensure_ascii=False).encode("utf-8"))
            if kept and used + size > SOURCES_PAGE_BYTES:
                next_point = str(record.id)  # inclusive resume at this entry
                break
            used += size
            kept.append((entry, scope, grant))
        # A policy change since admission is re-decided before responding.
        current = await self._current_version()
        if any(grant.policy_version != current for _, _, grant in kept):
            kept = [k for k in kept if await self._authorize_listing(caller, k[1]) is not None]
        return SourcePage(
            build_id=build_id,
            sources=tuple(entry for entry, _, _ in kept),
            next_cursor=None if next_point is None else encode_sources_cursor(build_id, next_point),
        )

    async def _authorize_listing(
        self, caller: TrustedCaller, scope: EvidenceScope
    ) -> AccessGrant | None:
        try:
            return await self._authorize(caller, scope)
        except EvidenceFailure as exc:
            if exc.kind == "unavailable":
                return None
            raise

    async def _pinned_build(
        self, build_id: str, *, require_catalog: bool = False
    ) -> tuple[BuildBinding, str]:
        """Resolve the build through its immutable aliases and verify the pair.

        Reads `settings.qdrant_collection` as the logical corpus only to NAME
        the per-build aliases; the ordinary serving alias is never consulted,
        so it moving (or being repaired/retired) cannot redirect this read.
        `require_catalog` refuses a build whose receipt binds no catalog."""
        logical = self._settings.qdrant_collection
        data_alias, control_alias = build_aliases(logical, build_id)
        aliases = await self._aliases()
        physical, control = aliases.get(data_alias), aliases.get(control_alias)
        if physical is None and control is None:
            raise EvidenceFailure("unavailable", "unknown_build")
        if physical is None or control != _control_of(physical):
            raise EvidenceFailure("corrupt", "build_aliases")
        bound = await self._binding(physical)
        binding = bound[0] if bound is not None else None
        if (
            binding is None
            or binding.build_id != build_id
            or binding.physical != physical
            or binding.alias != logical
        ):
            raise EvidenceFailure("corrupt", "control_mismatch")
        if require_catalog and not (bound is not None and bound[1]):
            raise EvidenceFailure("corrupt", "no_catalog")
        return binding, physical

    async def _authorize(self, caller: TrustedCaller, scope: EvidenceScope) -> AccessGrant:
        try:
            grant = await self._access.authorize(caller, scope)
        except Unauthenticated as exc:
            raise EvidenceFailure("unauthenticated", "no_identity") from exc
        except AccessUnavailable as exc:
            raise EvidenceFailure("access_unavailable", "policy_unavailable") from exc
        if grant is None:
            raise EvidenceFailure("unavailable", "denied")
        return grant

    async def _current_version(self) -> str:
        try:
            return await self._access.current_version()
        except AccessUnavailable as exc:
            raise EvidenceFailure("access_unavailable", "policy_unavailable") from exc
