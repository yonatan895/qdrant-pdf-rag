"""Exact-evidence service (issue #405 E1): references and read-only exact reads.

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

Read-only by construction: the service touches only `get_aliases`,
`collection_exists` and `retrieve`, so the serving (read-only) credential
suffices. It performs no model, embed or rerank call.
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

from mainframe_rag.config import Settings
from mainframe_rag.ingest.build import (
    BuildBinding,
    build_aliases,
    build_phase,
    decode_build_binding,
)
from mainframe_rag.ingest.classify import ChunkType
from mainframe_rag.ingest.publish import publication_metadata_point_id
from mainframe_rag.ingest.representation import _await_client
from mainframe_rag.logs import error_type

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

    def __init__(self, client: Any, settings: Settings, access: EvidenceAccess) -> None:
        self._client = client
        self._settings = settings
        self._access = access

    # -- storage reads (each is a real await boundary on the async client)

    async def _aliases(self) -> dict[str, str]:
        listing = await _await_client(self._client.get_aliases())
        return {a.alias_name: a.collection_name for a in listing.aliases}

    async def _binding(self, physical: str) -> BuildBinding | None:
        control = _control_of(physical)
        if not await _await_client(self._client.collection_exists(control)):
            return None
        records = await _await_client(self._client.retrieve(
            control, ids=[publication_metadata_point_id(control)], with_payload=True,
        ))
        if not records:
            return None
        payload = records[0].payload or {}
        if payload.get("record_type") != "publication-metadata":
            raise EvidenceFailure("corrupt", "control_record")
        try:
            return decode_build_binding(payload, control)
        except (ValueError, TypeError) as exc:
            raise EvidenceFailure("corrupt", "control_binding") from exc

    # -- minting (search side)

    async def mint_references(self, physical: str, chunk_ids: Sequence[str]) -> dict[str, str]:
        """References for the hits a search just served from `physical`.

        Empty when the serving generation has no build binding (legacy/in-place
        generations never mint) or is not the published/retained build of the
        configured logical corpus. A chunk whose stored payload cannot form a
        complete envelope is skipped, never given a partial reference."""
        ids = list(dict.fromkeys(chunk_ids))
        if not ids:
            return {}
        binding = await self._binding(physical)
        if (
            binding is None
            or binding.physical != physical
            or binding.alias != self._settings.qdrant_collection
        ):
            return {}
        try:
            phase = build_phase(binding, await self._aliases())
        except ValueError:
            return {}
        if phase not in ("published", "retained"):
            return {}
        records = await _await_client(self._client.retrieve(physical, ids, with_payload=True))
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
        records = await _await_client(self._client.retrieve(
            physical, [parsed.chunk_id], with_payload=True,
        ))
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

    async def _pinned_build(self, build_id: str) -> tuple[BuildBinding, str]:
        """Resolve the build through its immutable aliases and verify the pair.

        Reads `settings.qdrant_collection` as the logical corpus only to NAME
        the per-build aliases; the ordinary serving alias is never consulted,
        so it moving (or being repaired/retired) cannot redirect this read."""
        logical = self._settings.qdrant_collection
        data_alias, control_alias = build_aliases(logical, build_id)
        aliases = await self._aliases()
        physical, control = aliases.get(data_alias), aliases.get(control_alias)
        if physical is None and control is None:
            raise EvidenceFailure("unavailable", "unknown_build")
        if physical is None or control != _control_of(physical):
            raise EvidenceFailure("corrupt", "build_aliases")
        binding = await self._binding(physical)
        if (
            binding is None
            or binding.build_id != build_id
            or binding.physical != physical
            or binding.alias != logical
        ):
            raise EvidenceFailure("corrupt", "control_mismatch")
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
