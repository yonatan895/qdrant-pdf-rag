"""Exact-evidence service and its HTTP consumer (issue #405).

Hermetic: an async Qdrant double that models published builds (immutable
per-build aliases, paired control records, stored points) and records every
call, so the read-only boundary is asserted, not assumed. Expected bytes are
literal and computed here with hashlib/base64 only; nothing is compared with
the implementation's own encoder output alone.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from mainframe_rag.agent.evidence import (
    PUBLIC_FAILURES,
    SOURCES_PUBLIC_FAILURES,
    AccessGrant,
    AccessUnavailable,
    EvidenceFailure,
    EvidenceService,
    SharedCorpusAccess,
    TrustedCaller,
    Unauthenticated,
    build_evidence,
    encode_reference,
    parse_reference,
)
from mainframe_rag.agent.metrics import endpoint_for_path
from mainframe_rag.agent.serving import ServingGeneration
from mainframe_rag.config import Settings
from mainframe_rag.ingest.build import BuildBinding, build_aliases, decode_build_binding
from mainframe_rag.ingest.chunk import make_chunk_id
from mainframe_rag.ingest.publish import publication_metadata_point_id
from tests.fakes import settings_kw
from tests.helpers_agent_local import set_agent_env

LOGICAL = "wt405_manuals"
BUILD_A = "00000000-0000-4000-8000-00000000000a"
BUILD_B = "00000000-0000-4000-8000-00000000000b"
SHA = "a" * 64
REV = f"synthetic|guide|1|{SHA}"
CHUNK = make_chunk_id(REV, "Guide > Steps", 2, 0)
TEXT = "//A EXEC PGM=ONE\n\n//B µ PGM=TWO"  # 31 characters, 32 UTF-8 bytes
CALLER = TrustedCaller()


def _settings(**overrides) -> Settings:
    return Settings(**settings_kw(**{"qdrant_collection": LOGICAL, **overrides}))


def _payload(**overrides) -> dict:
    payload = {
        "vendor": "synthetic", "product": "synthos", "version": "1",
        "doc_id": "SYN-0001-00", "source_rev": REV, "title": "Synthetic Guide",
        "heading_path": "Guide > Steps", "page_label": "iii-iv",
        "page_start": 2, "page_end": 3, "chunk_type": "narrative",
        "message_ids": [], "members": [], "system_codes": [], "sha256": SHA,
        "rules_v": "r", "text": TEXT, "units": [[0, 16, "atomic"], [18, 31, "atomic"]],
    }
    payload.update(overrides)
    return payload


# Independent witness: the canonical envelope bytes written out by hand.
def _expected_envelope(build_id: str = BUILD_A, text: str = TEXT, spans: str | None = None) -> bytes:
    spans = spans or '[{"end":16,"start":0},{"end":32,"start":18}]'
    text_json = json.dumps(text, ensure_ascii=False)
    return (
        f'{{"atomic_spans":{spans},"build_id":"{build_id}","chunk_id":"{CHUNK}",'
        '"chunk_type":"narrative","doc_id":"SYN-0001-00",'
        '"generation_fingerprint":"0123456789abcdef","heading_path":"Guide > Steps",'
        '"physical_page_end":4,"physical_page_start":3,"printed_label":"iii-iv",'
        '"product":"synthos","profile":"stored-payload","schema":1,'
        f'"source_revision":"{REV}","source_sha256":"{SHA}","text":{text_json},'
        '"title":"Synthetic Guide","version":"1"}'
    ).encode()


def _expected_ref(build_id: str, envelope: bytes) -> str:
    raw = bytes.fromhex(build_id.replace("-", "")) + bytes.fromhex(CHUNK.replace("-", ""))
    raw += hashlib.sha256(envelope).digest()
    return "ep1." + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _control_payload(build_id: str, physical: str, catalog: list | None = None) -> dict:
    control = physical + "__completions"
    digest = "0" * 64
    payload = {
        "record_type": "publication-metadata", "target_collection": control,
        "build_schema": 2, "build_id": build_id, "logical_alias": LOGICAL,
        "data_collection": physical, "gen_fp": "0123456789abcdef", "corpus_fp": "c" * 12,
        "content_seal": {"schema": 1, "data": {"count": 1, "sha256": digest},
                         "control": {"count": 1, "sha256": digest}},
    }
    if catalog is not None:  # a #827 receipt: schema 3 binds the catalog summary
        from mainframe_rag.ingest.catalog import catalog_summary

        payload.update(build_schema=3, source_catalog=catalog_summary(catalog))
    return payload


# Catalog sources (#828). Two revisions share a doc id; two products; one
# source with no product/version and no honest page labels. Entries are the
# catalog's stored shape (0-based pages); the expected wire shape is written
# out separately below.
SHA2, SHA3 = "b" * 64, "c" * 64
REV2 = f"synthetic|guide|2|{SHA2}"
REV3 = f"unknown|||{SHA3}"


def _entry(rev, sha, doc_id, title, product, version, pages, labels, chunks, indexed):
    return {"source_revision": rev, "source_sha256": sha, "doc_id": doc_id, "title": title,
            "product": product, "version": version, "chunk_count": chunks,
            "pages": {"first": pages[0], "last": pages[1]},
            "page_labels": {"first": labels[0], "last": labels[1]}, "indexed_pages": indexed}


SOURCES = [
    _entry(REV, SHA, "SYN-0001-00", "Synthetic Guide", "synthos", "1", (2, 3), ("iii", "iv"), 1, 2),
    _entry(REV2, SHA2, "SYN-0001-00", "Synthetic Guide", "synthos", "2", (0, 9), ("1", "10"), 4, 10),
    _entry(REV3, SHA3, "OTHER-02", "Other Manual", None, None, (4, 4), (None, None), 1, 1),
]
# Independent wire witness: one-based physical pages, labels verbatim.
WIRE = {
    REV: {"source_revision": REV, "source_sha256": SHA, "doc_id": "SYN-0001-00",
          "title": "Synthetic Guide", "product": "synthos", "version": "1", "chunk_count": 1,
          "physical_pages": {"first": 3, "last": 4}, "page_labels": {"first": "iii", "last": "iv"},
          "indexed_pages": 2},
    REV2: {"source_revision": REV2, "source_sha256": SHA2, "doc_id": "SYN-0001-00",
           "title": "Synthetic Guide", "product": "synthos", "version": "2", "chunk_count": 4,
           "physical_pages": {"first": 1, "last": 10}, "page_labels": {"first": "1", "last": "10"},
           "indexed_pages": 10},
    REV3: {"source_revision": REV3, "source_sha256": SHA3, "doc_id": "OTHER-02",
           "title": "Other Manual", "product": None, "version": None, "chunk_count": 1,
           "physical_pages": {"first": 5, "last": 5}, "page_labels": {"first": None, "last": None},
           "indexed_pages": 1},
}


def _catalog_id(physical: str, rev: str) -> str:
    """The #827 point id, derived here from its documented recipe."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"source-catalog|{physical}__completions|{rev}"))


def _listing_order(physical: str, revs) -> list[str]:
    """Qdrant scrolls UUID ids in integer order (checked on the real server)."""
    return sorted(revs, key=lambda r: uuid.UUID(_catalog_id(physical, r)).int)


def _matches(payload: dict, flt) -> bool:
    for cond in (flt.must if flt is not None else None) or []:
        value: object = payload
        for part in cond.key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if type(cond).__name__ != "FieldCondition" or value != cond.match.value:
            return False
    return True


class EvidenceQdrant:
    """Async read-only double. No write method exists, so any write attempt is
    an AttributeError, and every call is recorded."""

    def __init__(self):
        self.aliases: dict[str, str] = {}
        self.collections: dict[str, dict[str, dict]] = {}
        self.calls: list[str] = []
        self.scroll_projections: list[object] = []
        self.before_retrieve = None  # async hook(name, ids)
        self.before_scroll = None  # async hook(name)

    def publish(self, build_id, physical, payloads, *, serving=False, retained_aliases=True,
                catalog=None):
        self.collections[physical] = dict(payloads)
        control = physical + "__completions"
        self.collections[control] = {
            publication_metadata_point_id(control): _control_payload(build_id, physical, catalog),
            # A completion marker: listing must filter it out.
            str(uuid.UUID(int=1)): {"doc_id": "SYN-0001-00", "source_rev": REV, "sha256": SHA,
                                    "expected_chunks": 1, "target_collection": physical},
        }
        for entry in catalog or []:
            self.collections[control][_catalog_id(physical, entry["source_revision"])] = {
                "record_type": "source-catalog-entry", "catalog_schema": 1, "entry": dict(entry),
            }
        if retained_aliases:
            data_alias, control_alias = build_aliases(LOGICAL, build_id)
            self.aliases[data_alias] = physical
            self.aliases[control_alias] = control
        if serving:
            self.aliases[LOGICAL] = physical

    async def get_aliases(self):
        self.calls.append("get_aliases")
        await asyncio.sleep(0)
        return SimpleNamespace(aliases=[
            SimpleNamespace(alias_name=a, collection_name=c) for a, c in sorted(self.aliases.items())
        ])

    async def collection_exists(self, name):
        self.calls.append("collection_exists")
        await asyncio.sleep(0)
        return name in self.collections

    async def retrieve(self, name, ids, *, with_payload=True, with_vectors=False):
        self.calls.append("retrieve")
        assert with_vectors is False
        if self.before_retrieve is not None:
            await self.before_retrieve(name, ids)
        await asyncio.sleep(0)
        if name not in self.collections:
            raise ConnectionError("collection missing: storage-secret-detail")
        store = self.collections[name]
        return [SimpleNamespace(id=i, payload=store[i], vector=None) for i in ids if i in store]

    async def scroll(self, name, *, scroll_filter=None, limit=10, with_payload, offset=None):
        """Faithful to the pinned server (verified read-only on a real build):
        integer UUID order, inclusive offset that may name an absent or
        non-matching id, next_page_offset = next matching id, top-level
        payload projection."""
        self.calls.append("scroll")
        self.scroll_projections.append(with_payload)
        if self.before_scroll is not None:
            await self.before_scroll(name)
        await asyncio.sleep(0)
        if name not in self.collections:
            raise ConnectionError("collection missing: storage-secret-detail")
        store = self.collections[name]
        ids = sorted(store, key=lambda i: uuid.UUID(i).int)
        if offset is not None:
            ids = [i for i in ids if uuid.UUID(i).int >= uuid.UUID(str(offset)).int]
        matching = [i for i in ids if _matches(store[i], scroll_filter)]
        page, rest = matching[:limit], matching[limit:]
        keep = (lambda k: True) if with_payload is True else (lambda k: k in with_payload)
        return ([SimpleNamespace(id=i, payload={k: v for k, v in store[i].items() if keep(k)},
                                 vector=None) for i in page],
                rest[0] if rest else None)


def _world(payload=None, build=BUILD_A, physical="wt405_gen_a", **publish_kw):
    qd = EvidenceQdrant()
    qd.publish(build, physical, {CHUNK: payload or _payload()}, **publish_kw)
    return qd


def _service(qd, access=None, **settings_overrides):
    return EvidenceService(qd, _settings(**settings_overrides), access or SharedCorpusAccess())


def _generation(qd, physical, outcome="compatible") -> ServingGeneration:
    """What the serving gate hands a search for `physical`: the build binding
    it decoded from the build's control record (agent.serving)."""
    control = physical + "__completions"
    record = qd.collections.get(control, {}).get(publication_metadata_point_id(control))
    binding = decode_build_binding(record, control) if record is not None else None
    return ServingGeneration(physical, outcome, (), binding)


async def _refusal(coro) -> EvidenceFailure:
    with pytest.raises(EvidenceFailure) as caught:
        await coro
    return caught.value


class _Access:
    """Scriptable authority."""

    def __init__(self):
        self.allow = True
        self.version = "v1"
        self.unavailable = False
        self.unauthenticated = False
        self.seen = []

    async def authorize(self, caller, scope):
        self.seen.append(scope)
        await asyncio.sleep(0)
        if self.unauthenticated:
            raise Unauthenticated
        if self.unavailable:
            raise AccessUnavailable
        return AccessGrant(self.version) if self.allow else None

    async def current_version(self):
        if self.unavailable:
            raise AccessUnavailable
        return self.version


# --------------------------------------------------------------- reference


def test_reference_matches_independent_literal_envelope_bytes():

    binding = BuildBinding(BUILD_A, LOGICAL, "wt405_gen_a", "0123456789abcdef", "c" * 12)
    got = build_evidence(binding, CHUNK, _payload())
    envelope = _expected_envelope()
    assert got.digest == hashlib.sha256(envelope).hexdigest()
    assert got.reference == _expected_ref(BUILD_A, envelope)
    assert len(got.reference) == 90
    # Byte offsets, not character offsets: the second item starts after the
    # two-byte micro sign's neighbours shift it (31 chars -> 32 bytes).
    assert got.atomic_spans == ((0, 16), (18, 32))
    assert got.text_bytes == 32 == len(TEXT.encode())


@pytest.mark.parametrize("field,changed", [
    ("text", "//A EXEC PGM=ONE\n\n//B µ PGM=TW0"),
    ("page_start", 3), ("page_label", "iii"), ("doc_id", "SYN-0002-00"),
    ("sha256", "b" * 64), ("source_rev", REV + "x"),
    ("units", [[0, 31, "atomic"]]),
])
def test_every_stored_field_changes_the_reference(field, changed):

    binding = BuildBinding(BUILD_A, LOGICAL, "p", "0123456789abcdef", "c" * 12)
    base = build_evidence(binding, CHUNK, _payload()).reference
    assert build_evidence(binding, CHUNK, _payload(**{field: changed})).reference != base


def test_reference_roundtrip_and_malformed_rejections():
    ref = _expected_ref(BUILD_A, _expected_envelope())
    parsed = parse_reference(ref)
    assert (parsed.build_id, parsed.chunk_id) == (BUILD_A, CHUNK)
    assert parsed.digest == hashlib.sha256(_expected_envelope()).digest()
    assert encode_reference(parsed.build_id, parsed.chunk_id, parsed.digest) == ref

    body = ref[4:]
    last = body[-1]
    bad_tail = body[:-1] + ("B" if last != "B" else "C")  # non-zero unused bits
    for malformed in (
        "", "ep1.", ref[:-1], ref + "A", ref + "=", "e1." + body, "EP1." + body,
        "ep1." + body.replace("-", "+", 1) if "-" in body else "ep1." + body[:-2] + "+/",
        "ep1." + bad_tail, ref.replace("ep1.", "ep1.é"), " " + ref,
        "ep1." + "A" * 86,  # zero build UUID
        "ep1./etc/passwd", "https://x/" + body, None, 7,
    ):
        with pytest.raises(EvidenceFailure) as caught:
            parse_reference(malformed)
        assert caught.value.kind == "invalid_reference", malformed


# ------------------------------------------------------------- exact reads


@pytest.mark.anyio
async def test_exact_read_returns_stored_bytes_provenance_and_stays_read_only():
    qd = _world(serving=True)
    ref = _expected_ref(BUILD_A, _expected_envelope())
    got = await _service(qd).read_evidence(CALLER, ref)
    assert got.text.encode("utf-8") == TEXT.encode("utf-8")
    assert (got.source_revision, got.source_sha256) == (REV, SHA)
    assert (got.doc_id, got.title, got.product, got.version) == (
        "SYN-0001-00", "Synthetic Guide", "synthos", "1")
    assert (got.physical_page_start, got.physical_page_end, got.printed_label) == (3, 4, "iii-iv")
    assert got.atomic_spans == ((0, 16), (18, 32))
    assert got.chunk_id == CHUNK and got.build_id == BUILD_A and got.reference == ref
    assert set(qd.calls) <= {"get_aliases", "collection_exists", "retrieve"}
    assert not hasattr(qd, "upsert") and not hasattr(qd, "update_collection_aliases")


@pytest.mark.anyio
async def test_unrecorded_units_are_null_not_empty_and_missing_page_end_is_explicit():
    payload = _payload()
    del payload["units"], payload["page_end"]
    qd = _world(payload)
    service = _service(qd)
    minted = await service.mint_references(_generation(qd, "wt405_gen_a"), [CHUNK])
    got = await service.read_evidence(CALLER, minted[CHUNK])
    assert got.atomic_spans is None, "absent units must not read as 'no atomic items'"
    assert got.physical_page_end is None and got.physical_page_start == 3


@pytest.mark.anyio
async def test_old_reference_survives_alias_swap_and_repair_and_never_reads_successor():
    """Same recipe (same gen_fp), changed corpus text, same chunk id."""
    qd = _world(serving=True)
    old_ref = (await _service(qd).mint_references(_generation(qd, "wt405_gen_a"), [CHUNK]))[CHUNK]
    new_payload = _payload(text="//A EXEC PGM=NEW", units=[[0, 16, "atomic"]])
    qd.publish(BUILD_B, "wt405_gen_b", {CHUNK: new_payload}, serving=True)  # alias swap
    service = _service(qd)

    old = await service.read_evidence(CALLER, old_ref)
    assert old.text == TEXT and old.build_id == BUILD_A

    new_ref = (await service.mint_references(_generation(qd, "wt405_gen_b"), [CHUNK]))[CHUNK]
    assert new_ref != old_ref
    assert (await service.read_evidence(CALLER, new_ref)).text == "//A EXEC PGM=NEW"

    # A forged token pairing build B with the old digest must not return B's text.
    forged = encode_reference(BUILD_B, CHUNK, parse_reference(old_ref).digest)
    refusal = await _refusal(service.read_evidence(CALLER, forged))
    assert (refusal.kind, refusal.reason) == ("corrupt", "digest_mismatch")


@pytest.mark.anyio
async def test_removed_build_is_unavailable_never_the_successor():
    qd = _world(serving=True)
    ref = (await _service(qd).mint_references(_generation(qd, "wt405_gen_a"), [CHUNK]))[CHUNK]
    qd.publish(BUILD_B, "wt405_gen_b", {CHUNK: _payload(text="successor", units=[])}, serving=True)
    data_alias, control_alias = build_aliases(LOGICAL, BUILD_A)
    del qd.aliases[data_alias], qd.aliases[control_alias]  # retired build
    refusal = await _refusal(_service(qd).read_evidence(CALLER, ref))
    assert (refusal.kind, refusal.reason) == ("unavailable", "unknown_build")
    # The next ordinary operation still works against the successor.
    successor = (await _service(qd).mint_references(_generation(qd, "wt405_gen_b"), [CHUNK]))[CHUNK]
    assert (await _service(qd).read_evidence(CALLER, successor)).text == "successor"


@pytest.mark.anyio
@pytest.mark.parametrize("break_it,reason", [
    (lambda qd: qd.aliases.pop(build_aliases(LOGICAL, BUILD_A)[1]), "build_aliases"),
    (lambda qd: qd.collections["wt405_gen_a__completions"].clear(), "control_mismatch"),
    (lambda qd: qd.collections["wt405_gen_a__completions"].update(
        {publication_metadata_point_id("wt405_gen_a__completions"):
         _control_payload(BUILD_B, "wt405_gen_a")}), "control_mismatch"),
    (lambda qd: qd.collections["wt405_gen_a"].clear(), "chunk_missing"),
])
async def test_missing_or_redirected_controls_refuse_instead_of_guessing(break_it, reason):
    qd = _world(serving=True)
    ref = (await _service(qd).mint_references(_generation(qd, "wt405_gen_a"), [CHUNK]))[CHUNK]
    break_it(qd)
    refusal = await _refusal(_service(qd).read_evidence(CALLER, ref))
    assert (refusal.kind, refusal.reason) == ("corrupt", reason)


@pytest.mark.anyio
async def test_a_data_alias_redirected_to_another_build_is_refused():
    qd = _world(serving=True)
    qd.publish(BUILD_B, "wt405_gen_b", {CHUNK: _payload(text="other")}, serving=True)
    ref_a = (await _service(qd).mint_references(_generation(qd, "wt405_gen_a"), [CHUNK]))[CHUNK]
    data_alias, _ = build_aliases(LOGICAL, BUILD_A)
    qd.aliases[data_alias] = "wt405_gen_b"  # redirect; control alias still names gen_a
    refusal = await _refusal(_service(qd).read_evidence(CALLER, ref_a))
    assert refusal.kind == "corrupt"


@pytest.mark.anyio
async def test_stored_data_changed_under_the_pinned_build_is_refused():
    qd = _world(serving=True)
    service = _service(qd)
    ref = (await service.mint_references(_generation(qd, "wt405_gen_a"), [CHUNK]))[CHUNK]
    qd.collections["wt405_gen_a"][CHUNK] = _payload(text=TEXT + " edited")
    refusal = await _refusal(service.read_evidence(CALLER, ref))
    assert (refusal.kind, refusal.reason) == ("corrupt", "digest_mismatch")


@pytest.mark.anyio
@pytest.mark.parametrize("override,reason", [
    ({"text": ""}, "text"), ({"chunk_type": "code"}, "chunk_type"),
    ({"sha256": "xyz"}, "source_sha256"), ({"source_rev": ""}, "source_revision"), ({"page_start": True}, "page_start"),
    ({"page_end": 1}, "page_end"), ({"units": "bad"}, "units_shape"),
    ({"units": [[0, 40, "atomic"]]}, "units_range"), ({"title": None}, "payload_field_type"),
    ({"text": "bad\ud800"}, "text_encoding"),
])
async def test_payload_that_cannot_form_a_complete_envelope_is_never_returned(override, reason):
    qd = _world(serving=True)
    service = _service(qd)
    ref = (await service.mint_references(_generation(qd, "wt405_gen_a"), [CHUNK]))[CHUNK]
    qd.collections["wt405_gen_a"][CHUNK] = _payload(**override)
    refusal = await _refusal(service.read_evidence(CALLER, ref))
    assert refusal.kind == "corrupt" and refusal.reason == reason
    # ...and mint skips it rather than issuing a partial reference.
    assert await service.mint_references(_generation(qd, "wt405_gen_a"), [CHUNK]) == {}


# ------------------------------------------------------------------ budget


@pytest.mark.anyio
async def test_budget_is_whole_chunk_or_explicit_413_never_a_prefix():
    qd = _world(serving=True)
    service = _service(qd)
    ref = (await service.mint_references(_generation(qd, "wt405_gen_a"), [CHUNK]))[CHUNK]
    assert (await service.read_evidence(CALLER, ref, max_bytes=32)).text == TEXT
    for budget in (31, 16, 1):
        refusal = await _refusal(service.read_evidence(CALLER, ref, max_bytes=budget))
        assert refusal.kind == "budget_exceeded"
    # The server cap binds even when the caller asks for more.
    qd.collections["wt405_gen_a"][CHUNK] = big = _payload(text="x" * 2048, units=[])
    del big["units"]
    capped = _service(qd, evidence_max_bytes=1024)
    ref2 = (await capped.mint_references(_generation(qd, "wt405_gen_a"), [CHUNK]))[CHUNK]
    refusal = await _refusal(capped.read_evidence(CALLER, ref2, max_bytes=1_000_000))
    assert refusal.kind == "budget_exceeded"


# ------------------------------------------------------------------- access


@pytest.mark.anyio
async def test_revocation_applies_to_the_next_read_on_the_same_service():
    qd = _world(serving=True)
    access = _Access()
    service = _service(qd, access)
    ref = (await service.mint_references(_generation(qd, "wt405_gen_a"), [CHUNK]))[CHUNK]
    assert (await service.read_evidence(CALLER, ref)).text == TEXT
    access.allow = False  # warm path, no cache clearing
    refusal = await _refusal(service.read_evidence(CALLER, ref))
    assert (refusal.kind, refusal.reason) == ("unavailable", "denied")
    assert access.seen[-1].source_revision == REV and access.seen[-1].product == "synthos"


@pytest.mark.anyio
async def test_revocation_between_admission_and_response_is_redecided():
    qd = _world(serving=True)
    access = _Access()
    service = _service(qd, access)
    ref = (await service.mint_references(_generation(qd, "wt405_gen_a"), [CHUNK]))[CHUNK]
    original = access.authorize

    async def revoke_after_grant(caller, scope):
        grant = await original(caller, scope)
        access.version, access.allow = "v2", False  # policy moves after the grant
        return grant

    access.authorize = revoke_after_grant
    refusal = await _refusal(service.read_evidence(CALLER, ref))
    assert (refusal.kind, refusal.reason) == ("unavailable", "denied")


@pytest.mark.anyio
async def test_policy_outage_and_missing_identity_fail_closed():
    qd = _world(serving=True)
    access = _Access()
    service = _service(qd, access)
    ref = (await service.mint_references(_generation(qd, "wt405_gen_a"), [CHUNK]))[CHUNK]
    access.unavailable = True
    assert (await _refusal(service.read_evidence(CALLER, ref))).kind == "access_unavailable"
    access.unavailable, access.unauthenticated = False, True
    assert (await _refusal(service.read_evidence(CALLER, ref))).kind == "unauthenticated"


@pytest.mark.anyio
async def test_denied_caller_learns_nothing_from_corruption_or_size():
    qd = _world(serving=True)
    access = _Access()
    service = _service(qd, access)
    ref = (await service.mint_references(_generation(qd, "wt405_gen_a"), [CHUNK]))[CHUNK]
    qd.collections["wt405_gen_a"][CHUNK] = _payload(text="tampered")
    access.allow = False
    refusal = await _refusal(service.read_evidence(CALLER, ref, max_bytes=1))
    assert (refusal.kind, refusal.reason) == ("unavailable", "denied")


@pytest.mark.anyio
async def test_product_and_version_assertions_only_narrow():
    qd = _world(serving=True)
    service = _service(qd)
    ref = (await service.mint_references(_generation(qd, "wt405_gen_a"), [CHUNK]))[CHUNK]
    assert (await service.read_evidence(CALLER, ref, product="synthos", version="1")).text == TEXT
    for kwargs in ({"product": "z/OS"}, {"version": "2"}):
        refusal = await _refusal(service.read_evidence(CALLER, ref, **kwargs))
        assert (refusal.kind, refusal.reason) == ("unavailable", "scope_mismatch")


# -------------------------------------------------- deadline / cancellation


@pytest.mark.anyio
async def test_cancellation_propagates_through_the_real_await_boundary():
    qd = _world(serving=True)
    ref = (await _service(qd).mint_references(_generation(qd, "wt405_gen_a"), [CHUNK]))[CHUNK]
    entered, released = asyncio.Event(), asyncio.Event()

    async def stall(name, ids):
        entered.set()
        try:
            await asyncio.sleep(30)
        finally:
            released.set()

    qd.before_retrieve = stall
    task = asyncio.create_task(_service(qd).read_evidence(CALLER, ref))
    await asyncio.wait_for(entered.wait(), 2)
    assert not task.done(), "the read must be suspended, not already completed"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert released.is_set(), "cancellation must reach the awaited storage call"


# ------------------------------------------------------------------ minting


@pytest.mark.anyio
async def test_legacy_or_refused_or_foreign_generations_mint_nothing():
    legacy = EvidenceQdrant()
    legacy.collections["wt405_old"] = {CHUNK: _payload()}  # no control record at all
    assert await _service(legacy).mint_references(_generation(legacy, "wt405_old"), [CHUNK]) == {}

    # Unpublished builds never reach minting: the serving gate refuses them
    # (test_serving_gate.test_serving_requires_full_published_build_pair).
    qd = _world(serving=True)
    refused = _generation(qd, "wt405_gen_a", outcome="unknown")
    assert await _service(qd).mint_references(refused, [CHUNK]) == {}
    mismatched = replace(_generation(qd, "wt405_gen_a"), physical="wt405_gen_b")
    assert await _service(qd).mint_references(mismatched, [CHUNK]) == {}

    other = EvidenceService(qd, _settings(qdrant_collection="another_corpus"),
                            SharedCorpusAccess())
    assert await other.mint_references(_generation(qd, "wt405_gen_a"), [CHUNK]) == {}
    assert await _service(qd).mint_references(_generation(qd, "wt405_gen_a"), []) == {}
    assert qd.calls == [], "refusals are decided from the validated generation alone"


@pytest.mark.anyio
async def test_minted_reference_equals_the_independent_witness_and_reads_back():
    qd = _world(serving=True)
    minted = await _service(qd).mint_references(_generation(qd, "wt405_gen_a"), [CHUNK, CHUNK, "absent"])
    assert minted == {CHUNK: _expected_ref(BUILD_A, _expected_envelope())}
    assert qd.calls == ["retrieve"], "the gate validated the build; minting adds one read"
    assert (await _service(qd).read_evidence(CALLER, minted[CHUNK])).text == TEXT


def test_public_failure_table_is_fixed_and_leaks_nothing():
    assert {k: v[:2] for k, v in PUBLIC_FAILURES.items()} == {
        "invalid_reference": (400, "invalid_evidence_reference"),
        "unauthenticated": (401, "authentication_required"),
        "unavailable": (404, "evidence_unavailable"),
        "corrupt": (503, "evidence_unavailable"),
        "access_unavailable": (503, "access_unavailable"),
        "budget_exceeded": (413, "evidence_budget_exceeded"),
        "timeout": (504, "evidence_timeout"),
        "upstream": (502, "upstream_error"),
    }
    assert PUBLIC_FAILURES["unavailable"][2] == PUBLIC_FAILURES["corrupt"][2]


# ------------------------------------------------------------- listing (#828)

PHYS = "wt405_gen_a"


def _listing_world(catalog=SOURCES, **publish_kw):
    return _world(catalog=catalog, **publish_kw)


async def _walk(svc, *, limit, served=BUILD_A, **kw):
    """Every page of one listing until the only end signal, null cursor."""
    pages, cursor = [], None
    while True:
        page = await svc.list_sources(CALLER, limit=limit, cursor=cursor,
                                      served_build=None if cursor else served, **kw)
        pages.append(page)
        cursor = page.next_cursor
        if cursor is None:
            return pages


@pytest.mark.anyio
async def test_listing_returns_catalog_entries_in_id_order_and_stays_read_only():
    qd = _listing_world()
    page = await _service(qd).list_sources(CALLER, limit=50, served_build=BUILD_A)
    order = _listing_order(PHYS, [REV, REV2, REV3])
    assert [e.wire() for e in page.sources] == [WIRE[r] for r in order]
    assert page.build_id == BUILD_A and page.next_cursor is None
    # Catalog metadata only: one projected scroll, never the data collection.
    assert set(qd.calls) <= {"get_aliases", "collection_exists", "retrieve", "scroll"}
    assert qd.scroll_projections == [["record_type", "catalog_schema", "entry"]]


@pytest.mark.anyio
async def test_limit_one_walk_returns_every_source_once_and_ends_on_null_cursor():
    pages = await _walk(_service(_listing_world()), limit=1)
    seen = [e.source_revision for p in pages for e in p.sources]
    assert seen == _listing_order(PHYS, [REV, REV2, REV3]) and len(pages) == 3
    assert all(p.build_id == BUILD_A for p in pages)
    assert [p.next_cursor is None for p in pages] == [False, False, True]


@pytest.mark.anyio
async def test_filters_only_narrow_by_exact_stored_labels():
    svc = _service(_listing_world())

    async def revs(**kw):
        return [e.source_revision for p in await _walk(svc, limit=50, **kw) for e in p.sources]

    assert sorted(await revs(product="synthos")) == sorted([REV, REV2])
    assert await revs(product="synthos", version="2") == [REV2]
    assert await revs(product="SYNTHOS") == []  # exact equality, no case folding
    page = await svc.list_sources(CALLER, limit=50, served_build=BUILD_A, product="none")
    assert page.sources == () and page.next_cursor is None


class _ByRevision(_Access):
    """Grants per source revision."""

    def __init__(self, denied=()):
        super().__init__()
        self.denied = set(denied)

    async def authorize(self, caller, scope):
        grant = await super().authorize(caller, scope)
        return None if scope.source_revision in self.denied else grant


@pytest.mark.anyio
async def test_denied_sources_are_omitted_without_any_count_or_marker():
    qd = _listing_world()
    page = await _service(qd, _ByRevision({REV2})).list_sources(CALLER, limit=50, served_build=BUILD_A)
    assert [e.source_revision for e in page.sources] == _listing_order(PHYS, [REV, REV3])
    everything_denied = await _service(qd, _ByRevision({REV, REV2, REV3})).list_sources(
        CALLER, limit=50, served_build=BUILD_A)
    no_match = await _service(qd).list_sources(CALLER, limit=50, served_build=BUILD_A, product="none")
    assert everything_denied == no_match  # identical shape: empty page, null cursor
    # A short page keeps its cursor: denials never end a listing early.
    walk = await _walk(_service(qd, _ByRevision({REV, REV2, REV3})), limit=1)
    assert len(walk) == 3 and all(p.sources == () for p in walk)


@pytest.mark.anyio
async def test_a_corrupt_visible_source_fails_the_page_and_a_corrupt_denied_one_is_unobservable():
    qd = _listing_world()
    control = PHYS + "__completions"
    qd.collections[control][_catalog_id(PHYS, REV2)]["entry"]["chunk_count"] = 0
    failure = await _refusal(_service(qd).list_sources(CALLER, limit=50, served_build=BUILD_A))
    assert (failure.kind, failure.reason) == ("corrupt", "catalog_entry")
    page = await _service(qd, _ByRevision({REV2})).list_sources(CALLER, limit=50, served_build=BUILD_A)
    assert REV2 not in [e.source_revision for e in page.sources]
    # An entry stored under another revision's point id is corrupt too.
    qd = _listing_world()
    store = qd.collections[control]
    store[_catalog_id(PHYS, REV)]["entry"], store[_catalog_id(PHYS, REV2)]["entry"] = (
        store[_catalog_id(PHYS, REV2)]["entry"], store[_catalog_id(PHYS, REV)]["entry"])
    failure = await _refusal(_service(qd).list_sources(CALLER, limit=50, served_build=BUILD_A))
    assert failure.kind == "corrupt"


@pytest.mark.anyio
async def test_a_build_without_a_catalog_lists_nothing_and_refuses():
    qd = _world()  # a pre-#827 receipt (schema 2), the same data
    failure = await _refusal(_service(qd).list_sources(CALLER, limit=50, served_build=BUILD_A))
    assert (failure.kind, failure.reason) == ("corrupt", "no_catalog")
    assert "scroll" not in qd.calls


@pytest.mark.anyio
async def test_a_listing_stays_on_its_build_across_an_alias_move_and_never_reads_the_successor():
    qd = _listing_world(serving=True)
    svc = _service(qd)
    first = await svc.list_sources(CALLER, limit=1, served_build=BUILD_A)
    qd.publish(BUILD_B, "wt405_gen_b", {}, serving=True, catalog=[SOURCES[2]])
    cursor, seen = first.next_cursor, [e.source_revision for e in first.sources]
    while cursor is not None:
        page = await svc.list_sources(CALLER, limit=1, cursor=cursor)
        assert page.build_id == BUILD_A
        seen += [e.source_revision for e in page.sources]
        cursor = page.next_cursor
    assert seen == _listing_order(PHYS, [REV, REV2, REV3])
    # A fresh listing of the new served build lists B only.
    fresh = await svc.list_sources(CALLER, limit=50, served_build=BUILD_B)
    assert [e.source_revision for e in fresh.sources] == [REV3] and fresh.build_id == BUILD_B
    # Once A is gone, its cursor is unavailable, never B's entries.
    for name in build_aliases(LOGICAL, BUILD_A):
        del qd.aliases[name]
    failure = await _refusal(svc.list_sources(CALLER, limit=1, cursor=first.next_cursor))
    assert (failure.kind, failure.reason) == ("unavailable", "unknown_build")


@pytest.mark.anyio
@pytest.mark.parametrize("cursor", [
    "", "ls1.", "ep1." + "A" * 43, "ls1." + "A" * 42, "ls1." + "A" * 43 + "=",
    "ls1." + "A" * 43,  # zero build
    "ls1." + "_" * 43,  # non-canonical trailing bits
    "ls1." + "é" * 43,
])
async def test_malformed_cursors_refuse_before_any_storage_contact(cursor):
    qd = _listing_world()
    failure = await _refusal(_service(qd).list_sources(CALLER, limit=5, cursor=cursor))
    assert failure.kind == "invalid_cursor" and qd.calls == []


def test_cursor_roundtrip_is_canonical():
    from mainframe_rag.agent.evidence import encode_sources_cursor, parse_sources_cursor

    point = _catalog_id(PHYS, REV)
    token = encode_sources_cursor(BUILD_A, point)
    raw = bytes.fromhex(BUILD_A.replace("-", "")) + bytes.fromhex(point.replace("-", ""))
    assert token == "ls1." + base64.urlsafe_b64encode(raw).decode().rstrip("=") and len(token) == 47
    assert parse_sources_cursor(token) == (BUILD_A, point)


@pytest.mark.anyio
async def test_page_byte_budget_ends_a_page_early_and_the_cursor_resumes_at_that_entry():
    big = [dict(e, title=e["title"] + "x" * 150_000) for e in SOURCES]
    svc = _service(_listing_world(catalog=big))
    pages = await _walk(svc, limit=50)
    assert [len(p.sources) for p in pages] == [1, 1, 1]  # first entry always included
    assert [e.source_revision for p in pages for e in p.sources] == _listing_order(PHYS, [REV, REV2, REV3])


@pytest.mark.anyio
async def test_a_policy_change_before_the_response_is_redecided():
    class Revoking(_ByRevision):
        async def current_version(self):
            if self.version == "v1":
                self.version, self.denied = "v2", {REV2}
            return self.version

    page = await _service(_listing_world(), Revoking()).list_sources(CALLER, limit=50, served_build=BUILD_A)
    assert REV2 not in [e.source_revision for e in page.sources] and len(page.sources) == 2


@pytest.mark.anyio
async def test_listing_fails_closed_on_policy_outage_identity_storage_fault_and_timeout():
    qd = _listing_world()
    access = _Access()
    access.unavailable = True
    assert (await _refusal(_service(qd, access).list_sources(
        CALLER, limit=5, served_build=BUILD_A))).kind == "access_unavailable"
    access.unavailable, access.unauthenticated = False, True
    assert (await _refusal(_service(qd, access).list_sources(
        CALLER, limit=5, served_build=BUILD_A))).kind == "unauthenticated"

    async def boom(name):
        raise ConnectionError("storage-secret-detail")

    qd.before_scroll = boom
    failure = await _refusal(_service(qd).list_sources(CALLER, limit=5, served_build=BUILD_A))
    assert failure.kind == "upstream" and "storage-secret-detail" not in failure.reason

    async def stall(name):
        await asyncio.sleep(5)

    qd.before_scroll = stall
    failure = await _refusal(_service(qd, evidence_timeout_s=0.05).list_sources(
        CALLER, limit=5, served_build=BUILD_A))
    assert failure.kind == "timeout"
    qd.before_scroll = None  # the next ordinary listing recovers
    assert len((await _service(qd).list_sources(CALLER, limit=5, served_build=BUILD_A)).sources) == 3


def test_sources_failure_table_is_fixed_and_leaks_nothing():

    assert SOURCES_PUBLIC_FAILURES == {
        "invalid_cursor": (400, "invalid_sources_cursor", "the sources cursor is not valid"),
        "unauthenticated": (401, "authentication_required", "authentication is required"),
        "unavailable": (404, "sources_unavailable", "the requested sources are not available"),
        "corrupt": (503, "sources_unavailable", "the requested sources are not available"),
        "access_unavailable": (503, "access_unavailable", "access policy is not available"),
        "timeout": (504, "sources_timeout", "the sources listing timed out"),
        "upstream": (502, "upstream_error", "sources listing failed"),
    }


# --------------------------------------------------------------------- HTTP


@pytest.fixture
def http(monkeypatch, synthetic_pdf, servable_representation_gate):
    from fastapi.testclient import TestClient

    from mainframe_rag.agent import app as app_mod
    from mainframe_rag.retrieve.query import SearchHit

    set_agent_env(monkeypatch, QDRANT_COLLECTION=LOGICAL)

    hit = SearchHit(
        chunk_id=CHUNK, score=0.5, cite="c", heading="Guide > Steps", text=TEXT,
        doc_id="SYN-0001-00", title="Synthetic Guide", page_label="iii-iv",
        chunk_type="narrative", message_ids=(), product="synthos", version="1",
    )

    class Llm:
        calls = 0

        def chat(self, *a, **k):
            Llm.calls += 1
            raise AssertionError("evidence routes must never call the reasoning model")

    def search(qdrant, embedder, collection, query, **kwargs):
        return [hit], "semantic", {"embed_ms": 1, "qdrant_ms": 1}

    with TestClient(app_mod.app) as client:
        qd = _world(serving=True, catalog=SOURCES)
        monkeypatch.setattr(app_mod, "qdrant", qd)
        monkeypatch.setattr(app_mod, "retrieve_search", search)
        monkeypatch.setattr(app_mod, "llm", Llm())
        monkeypatch.setattr(app_mod, "serving_gate", __import__("tests.fakes", fromlist=["x"])
                            .ServingGateFake(physical="wt405_gen_a",
                                             binding=_generation(qd, "wt405_gen_a").binding))
        yield SimpleNamespace(client=client, qd=qd, app=app_mod, llm=Llm, monkeypatch=monkeypatch)


def _search(http):
    r = http.client.post("/v1/search", json={"query": "step"})
    assert r.status_code == 200, r.text
    return r.json()


def test_search_hits_carry_a_reference_that_reads_back_exactly(http):
    body = _search(http)
    ref = body["hits"][0]["reference"]
    assert ref == _expected_ref(BUILD_A, _expected_envelope())
    r = http.client.get(f"/v1/evidence/{ref}")
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["text"] == TEXT and got["text"].encode() == TEXT.encode()
    assert got["completeness"] == "complete" and got["reference"] == ref
    assert got["digest"] == hashlib.sha256(_expected_envelope()).hexdigest()
    assert got["build_id"] == BUILD_A and got["chunk_id"] == CHUNK
    assert got["source_revision"] == REV and got["source_sha256"] == SHA
    assert got["atomic_spans"] == [{"start": 0, "end": 16}, {"start": 18, "end": 32}]
    assert got["location"] == {"physical_page_start": 3, "physical_page_end": 4,
                               "printed_label": "iii-iv"}
    assert (got["doc_id"], got["title"], got["heading"], got["chunk_type"]) == (
        "SYN-0001-00", "Synthetic Guide", "Guide > Steps", "narrative")
    assert got["text_bytes"] == 32 and http.llm.calls == 0
    assert set(http.qd.calls) <= {"get_aliases", "collection_exists", "retrieve"}


def test_search_without_a_build_binding_returns_null_reference_and_still_succeeds(http):
    http.app.serving_gate.binding = None  # what the gate validates for a legacy generation
    assert _search(http)["hits"][0]["reference"] is None


def test_search_survives_a_reference_fault_with_null_reference(http):
    async def boom(name, ids):
        raise ConnectionError("storage-secret-detail")

    http.qd.before_retrieve = boom
    body = _search(http)
    assert body["hits"][0]["reference"] is None
    assert "storage-secret-detail" not in json.dumps(body)


def _get(http, ref, **params):
    return http.client.get(f"/v1/evidence/{ref}", params=params)


def _error(r, status, code):
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == {"code", "message"} and body["code"] == code
    assert body["message"] == next(m for s, c, m in PUBLIC_FAILURES.values()
                                   if (s, c) == (status, code))
    return body


def test_http_failure_envelopes_are_fixed_and_leak_nothing(http):
    ref = _search(http)["hits"][0]["reference"]
    _error(_get(http, "not-a-reference"), 400, "invalid_evidence_reference")
    _error(_get(http, "ep1." + "A" * 86), 400, "invalid_evidence_reference")
    unknown = encode_reference(BUILD_B, CHUNK, parse_reference(ref).digest)
    _error(_get(http, unknown), 404, "evidence_unavailable")
    _error(_get(http, ref, max_bytes=8), 413, "evidence_budget_exceeded")
    _error(_get(http, ref, product="other"), 404, "evidence_unavailable")
    assert _get(http, ref, max_bytes=0).status_code == 422
    assert _get(http, ref, max_bytes=2_000_000).status_code == 422
    http.qd.collections["wt405_gen_a"][CHUNK] = _payload(text="changed")
    body = _error(_get(http, ref), 503, "evidence_unavailable")
    assert "digest" not in body["message"] and "changed" not in json.dumps(body)


def test_http_access_outcomes_map_per_contract(http):
    ref = _search(http)["hits"][0]["reference"]
    access = _Access()
    http.monkeypatch.setattr(http.app, "evidence_access", access)
    assert _get(http, ref).status_code == 200
    access.allow = False
    _error(_get(http, ref), 404, "evidence_unavailable")
    access.allow, access.unavailable = True, True
    _error(_get(http, ref), 503, "access_unavailable")
    access.unavailable, access.unauthenticated = False, True
    _error(_get(http, ref), 401, "authentication_required")
    access.unauthenticated = False
    assert _get(http, ref).status_code == 200  # the next ordinary read recovers


@pytest.mark.parametrize("kind, hook, timeout_code", [
    ("evidence", "before_retrieve", "evidence_timeout"),
    ("sources", "before_scroll", "sources_timeout"),
])
def test_http_storage_fault_and_deadline_use_fixed_envelopes(http, kind, hook, timeout_code):
    """A storage fault is a fixed 502 with no upstream text; a stalled read is
    the endpoint's timeout code; the next ordinary request recovers."""
    if kind == "evidence":
        ref = _search(http)["hits"][0]["reference"]  # resolved before the fault
        request = lambda: _get(http, ref)
        error = _error
    else:
        request = lambda: _sources(http)
        error = _sources_error

    async def boom(*_a):
        raise ConnectionError("storage-secret-detail")

    setattr(http.qd, hook, boom)
    body = error(request(), 502, "upstream_error")
    assert "storage-secret-detail" not in json.dumps(body)

    async def stall(*_a):
        await asyncio.sleep(5)

    setattr(http.qd, hook, stall)
    http.monkeypatch.setattr(http.app.settings, "evidence_timeout_s", 0.05)
    error(request(), 504, timeout_code)
    setattr(http.qd, hook, None)
    http.monkeypatch.setattr(http.app.settings, "evidence_timeout_s", 10.0)
    assert request().status_code == 200


def test_evidence_endpoint_is_counted_with_a_bounded_label():

    assert endpoint_for_path("/v1/evidence/ep1.AAAA") == "evidence"
    assert endpoint_for_path("/v1/evidence") is None
    assert endpoint_for_path("/v1/search") == "search"


# ---------------------------------------------------------- HTTP listing (#828)


def _sources(http, **params):
    return http.client.get("/v1/sources", params=params)


def _sources_error(r, status, code):

    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == {"code", "message"} and body["code"] == code
    assert body["message"] in {m for _, c, m in SOURCES_PUBLIC_FAILURES.values() if c == code} | {
        "the retrieval generation is not available"}
    return body


def test_http_sources_page_shape_walk_and_no_model_call(http):
    r = _sources(http)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"request_id", "build_id", "sources", "next_cursor"}
    assert body["build_id"] == BUILD_A and body["next_cursor"] is None
    assert body["sources"] == [WIRE[rev] for rev in _listing_order(PHYS, [REV, REV2, REV3])]
    seen, cursor = [], None
    while True:
        page = _sources(http, limit=1, **({"cursor": cursor} if cursor else {})).json()
        seen += [s["source_revision"] for s in page["sources"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == _listing_order(PHYS, [REV, REV2, REV3])
    assert http.llm.calls == 0 and TEXT not in json.dumps(body)


def test_http_sources_bounds_and_cursor_envelopes(http):
    assert _sources(http, limit=0).status_code == 422
    assert _sources(http, limit=201).status_code == 422
    assert _sources(http, cursor="x" * 65).status_code == 422
    calls = len(http.qd.calls)
    _sources_error(_sources(http, cursor="ls1.not-a-cursor"), 400, "invalid_sources_cursor")
    assert len(http.qd.calls) == calls  # refused before any storage contact
    from mainframe_rag.agent.evidence import encode_sources_cursor

    unknown = encode_sources_cursor(BUILD_B, _catalog_id(PHYS, REV))
    _sources_error(_sources(http, cursor=unknown), 404, "sources_unavailable")


def test_http_sources_on_a_build_without_a_catalog_is_503(http):
    qd = _world(serving=True)  # pre-#827 receipt
    http.monkeypatch.setattr(http.app, "qdrant", qd)
    _sources_error(_sources(http), 503, "sources_unavailable")


def test_http_sources_refuses_a_non_servable_generation(http):
    http.app.serving_gate.outcome = "reembed_required"
    _sources_error(_sources(http), 503, "representation_unavailable")


def test_http_sources_access_outcomes_map_per_contract(http):
    access = _ByRevision({REV2})
    http.monkeypatch.setattr(http.app, "evidence_access", access)
    assert [s["source_revision"] for s in _sources(http).json()["sources"]] == _listing_order(
        PHYS, [REV, REV3])
    access.denied = {REV, REV2, REV3}
    denied, no_match = _sources(http).json(), _sources(http, product="none").json()
    denied.pop("request_id"), no_match.pop("request_id")
    assert denied == no_match == {"build_id": BUILD_A, "sources": [], "next_cursor": None}
    access.denied, access.unavailable = set(), True
    _sources_error(_sources(http), 503, "access_unavailable")
    access.unavailable, access.unauthenticated = False, True
    _sources_error(_sources(http), 401, "authentication_required")
    access.unauthenticated = False
    assert len(_sources(http).json()["sources"]) == 3  # the next ordinary listing recovers


def test_sources_endpoint_is_counted_with_a_bounded_label():

    assert endpoint_for_path("/v1/sources") == "sources"
    assert endpoint_for_path("/v1/sources/") == "sources"
    assert endpoint_for_path("/v1/sources/x") is None
