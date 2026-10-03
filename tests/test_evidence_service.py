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
from types import SimpleNamespace

import pytest

from mainframe_rag.agent.evidence import (
    PUBLIC_FAILURES,
    AccessGrant,
    AccessUnavailable,
    EvidenceFailure,
    EvidenceService,
    SharedCorpusAccess,
    TrustedCaller,
    Unauthenticated,
    encode_reference,
    parse_reference,
)
from mainframe_rag.config import Settings
from mainframe_rag.ingest.build import build_aliases
from mainframe_rag.ingest.chunk import make_chunk_id
from mainframe_rag.ingest.publish import publication_metadata_point_id
from tests.fakes import settings_kw

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


def _control_payload(build_id: str, physical: str) -> dict:
    control = physical + "__completions"
    digest = "0" * 64
    return {
        "record_type": "publication-metadata", "target_collection": control,
        "build_schema": 2, "build_id": build_id, "logical_alias": LOGICAL,
        "data_collection": physical, "gen_fp": "0123456789abcdef", "corpus_fp": "c" * 12,
        "content_seal": {"schema": 1, "data": {"count": 1, "sha256": digest},
                         "control": {"count": 1, "sha256": digest}},
    }


class EvidenceQdrant:
    """Async read-only double. No write method exists, so any write attempt is
    an AttributeError, and every call is recorded."""

    def __init__(self):
        self.aliases: dict[str, str] = {}
        self.collections: dict[str, dict[str, dict]] = {}
        self.calls: list[str] = []
        self.before_retrieve = None  # async hook(name, ids)

    def publish(self, build_id, physical, payloads, *, serving=False, retained_aliases=True):
        self.collections[physical] = dict(payloads)
        control = physical + "__completions"
        self.collections[control] = {
            publication_metadata_point_id(control): _control_payload(build_id, physical)
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


def _world(payload=None, build=BUILD_A, physical="wt405_gen_a", **publish_kw):
    qd = EvidenceQdrant()
    qd.publish(build, physical, {CHUNK: payload or _payload()}, **publish_kw)
    return qd


def _service(qd, access=None, **settings_overrides):
    return EvidenceService(qd, _settings(**settings_overrides), access or SharedCorpusAccess())


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
    from mainframe_rag.agent.evidence import build_evidence
    from mainframe_rag.ingest.build import BuildBinding

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
    ("page_start", 3), ("page_end", 4), ("page_label", "iii"), ("title", "Other"),
    ("doc_id", "SYN-0002-00"), ("product", "other"), ("version", "2"),
    ("heading_path", "Guide > Other"), ("sha256", "b" * 64), ("source_rev", REV + "x"),
    ("units", [[0, 31, "atomic"]]),
])
def test_every_stored_field_changes_the_reference(field, changed):
    from mainframe_rag.agent.evidence import build_evidence
    from mainframe_rag.ingest.build import BuildBinding

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
    minted = await service.mint_references("wt405_gen_a", [CHUNK])
    got = await service.read_evidence(CALLER, minted[CHUNK])
    assert got.atomic_spans is None, "absent units must not read as 'no atomic items'"
    assert got.physical_page_end is None and got.physical_page_start == 3


@pytest.mark.anyio
async def test_old_reference_survives_alias_swap_and_repair_and_never_reads_successor():
    """Same recipe (same gen_fp), changed corpus text, same chunk id."""
    qd = _world(serving=True)
    old_ref = (await _service(qd).mint_references("wt405_gen_a", [CHUNK]))[CHUNK]
    new_payload = _payload(text="//A EXEC PGM=NEW", units=[[0, 16, "atomic"]])
    qd.publish(BUILD_B, "wt405_gen_b", {CHUNK: new_payload}, serving=True)  # alias swap
    service = _service(qd)

    old = await service.read_evidence(CALLER, old_ref)
    assert old.text == TEXT and old.build_id == BUILD_A

    new_ref = (await service.mint_references("wt405_gen_b", [CHUNK]))[CHUNK]
    assert new_ref != old_ref
    assert (await service.read_evidence(CALLER, new_ref)).text == "//A EXEC PGM=NEW"

    # A forged token pairing build B with the old digest must not return B's text.
    forged = encode_reference(BUILD_B, CHUNK, parse_reference(old_ref).digest)
    refusal = await _refusal(service.read_evidence(CALLER, forged))
    assert (refusal.kind, refusal.reason) == ("corrupt", "digest_mismatch")


@pytest.mark.anyio
async def test_removed_build_is_unavailable_never_the_successor():
    qd = _world(serving=True)
    ref = (await _service(qd).mint_references("wt405_gen_a", [CHUNK]))[CHUNK]
    qd.publish(BUILD_B, "wt405_gen_b", {CHUNK: _payload(text="successor", units=[])}, serving=True)
    data_alias, control_alias = build_aliases(LOGICAL, BUILD_A)
    del qd.aliases[data_alias], qd.aliases[control_alias]  # retired build
    refusal = await _refusal(_service(qd).read_evidence(CALLER, ref))
    assert (refusal.kind, refusal.reason) == ("unavailable", "unknown_build")
    # The next ordinary operation still works against the successor.
    successor = (await _service(qd).mint_references("wt405_gen_b", [CHUNK]))[CHUNK]
    assert (await _service(qd).read_evidence(CALLER, successor)).text == "successor"


@pytest.mark.anyio
@pytest.mark.parametrize("break_it,reason", [
    (lambda qd: qd.aliases.pop(build_aliases(LOGICAL, BUILD_A)[1]), "build_aliases"),
    (lambda qd: qd.aliases.__setitem__(build_aliases(LOGICAL, BUILD_A)[1], "other__completions"),
     "build_aliases"),
    (lambda qd: qd.collections["wt405_gen_a__completions"].clear(), "control_mismatch"),
    (lambda qd: qd.collections["wt405_gen_a__completions"].update(
        {publication_metadata_point_id("wt405_gen_a__completions"):
         _control_payload(BUILD_B, "wt405_gen_a")}), "control_mismatch"),
    (lambda qd: qd.collections["wt405_gen_a"].clear(), "chunk_missing"),
])
async def test_missing_or_redirected_controls_refuse_instead_of_guessing(break_it, reason):
    qd = _world(serving=True)
    ref = (await _service(qd).mint_references("wt405_gen_a", [CHUNK]))[CHUNK]
    break_it(qd)
    refusal = await _refusal(_service(qd).read_evidence(CALLER, ref))
    assert (refusal.kind, refusal.reason) == ("corrupt", reason)


@pytest.mark.anyio
async def test_a_data_alias_redirected_to_another_build_is_refused():
    qd = _world(serving=True)
    qd.publish(BUILD_B, "wt405_gen_b", {CHUNK: _payload(text="other")}, serving=True)
    ref_a = (await _service(qd).mint_references("wt405_gen_a", [CHUNK]))[CHUNK]
    data_alias, _ = build_aliases(LOGICAL, BUILD_A)
    qd.aliases[data_alias] = "wt405_gen_b"  # redirect; control alias still names gen_a
    refusal = await _refusal(_service(qd).read_evidence(CALLER, ref_a))
    assert refusal.kind == "corrupt"


@pytest.mark.anyio
async def test_stored_data_changed_under_the_pinned_build_is_refused():
    qd = _world(serving=True)
    service = _service(qd)
    ref = (await service.mint_references("wt405_gen_a", [CHUNK]))[CHUNK]
    qd.collections["wt405_gen_a"][CHUNK] = _payload(text=TEXT + " edited")
    refusal = await _refusal(service.read_evidence(CALLER, ref))
    assert (refusal.kind, refusal.reason) == ("corrupt", "digest_mismatch")


@pytest.mark.anyio
@pytest.mark.parametrize("override,reason", [
    ({"text": ""}, "text"), ({"text": 5}, "text"), ({"chunk_type": "code"}, "chunk_type"),
    ({"chunk_type": "prose"}, "chunk_type"), ({"sha256": "xyz"}, "source_sha256"),
    ({"source_rev": ""}, "source_revision"), ({"page_start": -1}, "page_start"),
    ({"page_start": True}, "page_start"), ({"page_end": 1}, "page_end"),
    ({"units": "bad"}, "units_shape"), ({"units": [[0, 40, "atomic"]]}, "units_range"),
    ({"units": [[5, 9, "atomic"], [0, 3, "prose"]]}, "units_range"),
    ({"units": [[0, 3, "other"]]}, "units_range"), ({"title": None}, "payload_field_type"),
    ({"text": "bad\ud800"}, "text_encoding"),
])
async def test_payload_that_cannot_form_a_complete_envelope_is_never_returned(override, reason):
    qd = _world(serving=True)
    service = _service(qd)
    ref = (await service.mint_references("wt405_gen_a", [CHUNK]))[CHUNK]
    qd.collections["wt405_gen_a"][CHUNK] = _payload(**override)
    refusal = await _refusal(service.read_evidence(CALLER, ref))
    assert refusal.kind == "corrupt" and refusal.reason == reason
    # ...and mint skips it rather than issuing a partial reference.
    assert await service.mint_references("wt405_gen_a", [CHUNK]) == {}


# ------------------------------------------------------------------ budget


@pytest.mark.anyio
async def test_budget_is_whole_chunk_or_explicit_413_never_a_prefix():
    qd = _world(serving=True)
    service = _service(qd)
    ref = (await service.mint_references("wt405_gen_a", [CHUNK]))[CHUNK]
    assert (await service.read_evidence(CALLER, ref, max_bytes=32)).text == TEXT
    for budget in (31, 16, 1):
        refusal = await _refusal(service.read_evidence(CALLER, ref, max_bytes=budget))
        assert refusal.kind == "budget_exceeded"
    # The server cap binds even when the caller asks for more.
    qd.collections["wt405_gen_a"][CHUNK] = big = _payload(text="x" * 2048, units=[])
    del big["units"]
    capped = _service(qd, evidence_max_bytes=1024)
    ref2 = (await capped.mint_references("wt405_gen_a", [CHUNK]))[CHUNK]
    refusal = await _refusal(capped.read_evidence(CALLER, ref2, max_bytes=1_000_000))
    assert refusal.kind == "budget_exceeded"


# ------------------------------------------------------------------- access


@pytest.mark.anyio
async def test_revocation_applies_to_the_next_read_on_the_same_service():
    qd = _world(serving=True)
    access = _Access()
    service = _service(qd, access)
    ref = (await service.mint_references("wt405_gen_a", [CHUNK]))[CHUNK]
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
    ref = (await service.mint_references("wt405_gen_a", [CHUNK]))[CHUNK]
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
    ref = (await service.mint_references("wt405_gen_a", [CHUNK]))[CHUNK]
    access.unavailable = True
    assert (await _refusal(service.read_evidence(CALLER, ref))).kind == "access_unavailable"
    access.unavailable, access.unauthenticated = False, True
    assert (await _refusal(service.read_evidence(CALLER, ref))).kind == "unauthenticated"


@pytest.mark.anyio
async def test_denied_caller_learns_nothing_from_corruption_or_size():
    qd = _world(serving=True)
    access = _Access()
    service = _service(qd, access)
    ref = (await service.mint_references("wt405_gen_a", [CHUNK]))[CHUNK]
    qd.collections["wt405_gen_a"][CHUNK] = _payload(text="tampered")
    access.allow = False
    refusal = await _refusal(service.read_evidence(CALLER, ref, max_bytes=1))
    assert (refusal.kind, refusal.reason) == ("unavailable", "denied")


@pytest.mark.anyio
async def test_product_and_version_assertions_only_narrow():
    qd = _world(serving=True)
    service = _service(qd)
    ref = (await service.mint_references("wt405_gen_a", [CHUNK]))[CHUNK]
    assert (await service.read_evidence(CALLER, ref, product="synthos", version="1")).text == TEXT
    for kwargs in ({"product": "z/OS"}, {"version": "2"}):
        refusal = await _refusal(service.read_evidence(CALLER, ref, **kwargs))
        assert (refusal.kind, refusal.reason) == ("unavailable", "scope_mismatch")


# -------------------------------------------------- deadline / cancellation


@pytest.mark.anyio
async def test_deadline_is_a_typed_timeout():
    qd = _world(serving=True)
    ref = (await _service(qd).mint_references("wt405_gen_a", [CHUNK]))[CHUNK]

    async def stall(name, ids):
        await asyncio.sleep(5)

    qd.before_retrieve = stall
    refusal = await _refusal(_service(qd, evidence_timeout_s=0.05).read_evidence(CALLER, ref))
    assert refusal.kind == "timeout"


@pytest.mark.anyio
async def test_cancellation_propagates_through_the_real_await_boundary():
    qd = _world(serving=True)
    ref = (await _service(qd).mint_references("wt405_gen_a", [CHUNK]))[CHUNK]
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


@pytest.mark.anyio
async def test_storage_failure_is_a_fixed_upstream_refusal_without_storage_text():
    qd = _world(serving=True)
    ref = (await _service(qd).mint_references("wt405_gen_a", [CHUNK]))[CHUNK]
    del qd.collections["wt405_gen_a"]
    refusal = await _refusal(_service(qd).read_evidence(CALLER, ref))
    assert refusal.kind == "upstream" and refusal.reason == "ConnectionError"
    assert "storage-secret-detail" not in str(PUBLIC_FAILURES[refusal.kind])


# ------------------------------------------------------------------ minting


@pytest.mark.anyio
async def test_legacy_or_unpublished_or_foreign_generations_mint_nothing():
    legacy = EvidenceQdrant()
    legacy.collections["wt405_old"] = {CHUNK: _payload()}  # no control record at all
    assert await _service(legacy).mint_references("wt405_old", [CHUNK]) == {}

    unpublished = _world(retained_aliases=False)  # sealed, never published
    assert await _service(unpublished).mint_references("wt405_gen_a", [CHUNK]) == {}

    foreign = _world(serving=True)
    other = EvidenceService(foreign, _settings(qdrant_collection="another_corpus"),
                            SharedCorpusAccess())
    assert await other.mint_references("wt405_gen_a", [CHUNK]) == {}
    assert await _service(foreign).mint_references("wt405_gen_a", []) == {}


@pytest.mark.anyio
async def test_minted_reference_equals_the_independent_witness_and_reads_back():
    qd = _world(serving=True)
    minted = await _service(qd).mint_references("wt405_gen_a", [CHUNK, CHUNK, "absent"])
    assert minted == {CHUNK: _expected_ref(BUILD_A, _expected_envelope())}
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


# --------------------------------------------------------------------- HTTP


@pytest.fixture
def http(monkeypatch, synthetic_pdf, servable_representation_gate):
    from fastapi.testclient import TestClient

    from mainframe_rag.agent import app as app_mod
    from mainframe_rag.retrieve.query import SearchHit

    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("QDRANT_COLLECTION", LOGICAL)
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setenv("LLM_MODEL_REASONING", "test-reasoning-model")

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
        qd = _world(serving=True)
        monkeypatch.setattr(app_mod, "qdrant", qd)
        monkeypatch.setattr(app_mod, "retrieve_search", search)
        monkeypatch.setattr(app_mod, "llm", Llm())
        monkeypatch.setattr(app_mod, "serving_gate", __import__("tests.fakes", fromlist=["x"])
                            .ServingGateFake(physical="wt405_gen_a"))
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
    http.qd.collections["wt405_gen_a__completions"].clear()  # legacy-shaped generation
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


def test_http_storage_fault_and_deadline_use_fixed_envelopes(http):
    ref = _search(http)["hits"][0]["reference"]

    async def boom(name, ids):
        raise ConnectionError("storage-secret-detail")

    http.qd.before_retrieve = boom
    body = _error(_get(http, ref), 502, "upstream_error")
    assert "storage-secret-detail" not in json.dumps(body)

    async def stall(name, ids):
        await asyncio.sleep(5)

    http.qd.before_retrieve = stall
    http.monkeypatch.setattr(http.app.settings, "evidence_timeout_s", 0.05)
    _error(_get(http, ref), 504, "evidence_timeout")
    http.qd.before_retrieve = None
    http.monkeypatch.setattr(http.app.settings, "evidence_timeout_s", 10.0)
    assert _get(http, ref).status_code == 200


def test_evidence_endpoint_is_counted_with_a_bounded_label():
    from mainframe_rag.agent.metrics import endpoint_for_path

    assert endpoint_for_path("/v1/evidence/ep1.AAAA") == "evidence"
    assert endpoint_for_path("/v1/evidence") is None
    assert endpoint_for_path("/v1/search") == "search"
