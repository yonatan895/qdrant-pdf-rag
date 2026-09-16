"""Prompt packing preserves atomic code/table units (issue #368).

Boundary tests that fail against character-only truncation and pass after
the fix. All fixtures are original synthetic text (no vendor material).

The core counterexample: a JCL continuation statement cut mid-statement by
a char cap ships today as `//S\\n... [truncated]` — a partial statement
presented as a complete excerpt, with only the generic suffix as witness.
Every test below asserts *whole units or explicit omission*, never suffix
presence.
"""

from __future__ import annotations

import pytest

from mainframe_rag.agent.answer import (
    _TRUNCATED_SUFFIX,
    PromptBudgetExceeded,
    _verify_trim_last,
    build_chat_messages,
    build_messages,
    parse_answer,
)
from mainframe_rag.agent.tokenizer import FallbackTokenizer
from mainframe_rag.ingest.chunk import UnitSpan, units_for_text
from mainframe_rag.ports import ChatMessage
from mainframe_rag.retrieve.query import SearchHit
from tests.fakes import settings_kw

# Three JCL statements; the second carries a `//  ` continuation card.
JCL_TEXT = (
    "//JOB1   JOB (ACCT),'TEST',CLASS=A\n"
    "//STEP1  EXEC PGM=IEFBR14\n"
    "//       PARM1=ALPHA,PARM2=BETA\n"
    "//STEP2  EXEC PGM=SORT"
)
JCL_STMT1 = "//JOB1   JOB (ACCT),'TEST',CLASS=A"
JCL_STMT2 = "//STEP1  EXEC PGM=IEFBR14\n//       PARM1=ALPHA,PARM2=BETA"
JCL_STMT3 = "//STEP2  EXEC PGM=SORT"

# Table with a header/caption line plus three data rows (2-space columns).
TABLE_TEXT = (
    "Command    Function\n"
    "D A,L      Display active units\n"
    "D U,,,ALL  Display all units\n"
    "V ONLINE   Vary device online"
)
TABLE_HEADER = "Command    Function"

REXX_TEXT = "/* REXX */\nsay 'hello';\nx = 1 + 2;\nsay x"


def _hit(text: str, chunk_type: str = "syntax", units=None, index: str = "c1", page: str = "1") -> SearchHit:
    """Shape-valid synthetic cite (`SA22-0000-00 ... p. 1-<page>`) so the
    citation-eligibility tests exercise the allowlist, not the shape gate."""
    kwargs = {}
    if units != "absent":
        kwargs["units"] = units
    cite = f"SA22-0000-00 Synthetic Reference, H, p. 1-{page}"
    return SearchHit(
        chunk_id=index,
        score=1.0,
        cite=cite,
        heading="H",
        text=text,
        doc_id="SA22-0000-00",
        title="Synthetic Reference",
        page_label=f"1-{page}",
        chunk_type=chunk_type,
        message_ids=(),
        **kwargs,
    )


def _excerpt_body(prepared, index: int = 1) -> str:
    user_text = prepared.messages[1].content
    marker = f"[{index}]"
    start = user_text.index(marker)
    tail = user_text.index("\n\nPlease answer based strictly")
    return user_text[start:tail]


# ---------------------------------------------------------------------------
# Per-chunk caps: whole statements or explicit omission (legacy-point path:
# units absent -> shared fallback detector, still boundary-aware).
# ---------------------------------------------------------------------------


def test_per_chunk_cap_never_splits_jcl_statement():
    """A cap landing inside the second JCL statement must snap back to the
    first: no partial card may ship as an excerpt."""
    cap = len(JCL_STMT1) + 10  # mid-statement-2 by construction
    assert cap < len(JCL_STMT1) + 1 + len(JCL_STMT2)
    prepared = build_messages("q", [_hit(JCL_TEXT)], max_chunk_chars=cap)
    body = _excerpt_body(prepared)
    assert JCL_STMT1 in body
    assert "//STEP1" not in body  # no fragment of statement 2
    assert body.rstrip().endswith(_TRUNCATED_SUFFIX.strip())
    entry = prepared.evidence.entries[0]
    assert entry.truncated is True
    assert entry.units_total == 3
    assert entry.units_retained == 1
    assert entry.included_chars == len(JCL_STMT1)


def test_per_chunk_cap_keeps_table_header_and_whole_rows():
    """A cap inside the second data row keeps header + first row only; the
    cut row vanishes entirely instead of shipping half a row."""
    cap = len(TABLE_HEADER) + 1 + len("D A,L      Display active units") + 6
    prepared = build_messages(
        "q", [_hit(TABLE_TEXT, chunk_type="table")], max_chunk_chars=cap
    )
    body = _excerpt_body(prepared)
    assert TABLE_HEADER in body
    assert "D A,L      Display active units" in body
    assert "D U,,," not in body
    entry = prepared.evidence.entries[0]
    assert (entry.units_total, entry.units_retained) == (4, 2)


def test_chunk_too_small_for_first_unit_is_omitted():
    """A cap smaller than the first statement omits the excerpt with
    explicit omission metadata — never an empty or partial body."""
    prepared = build_messages("q", [_hit(JCL_TEXT)], max_chunk_chars=10)
    assert prepared.evidence.entries == ()
    assert prepared.evidence.omitted_indices == (1,)


def test_narrative_keeps_character_truncation():
    """No blanket ban on safe narrative truncation: prose without atomic
    units still takes the legacy char cut (non-goal of #368)."""
    prose = "Background: " + "word " * 500
    prepared = build_messages(
        "q", [_hit(prose, chunk_type="narrative")], max_chunk_chars=100
    )
    body = _excerpt_body(prepared)
    assert body.rstrip().endswith(_TRUNCATED_SUFFIX.strip())
    entry = prepared.evidence.entries[0]
    assert (entry.units_total, entry.units_retained) == (0, 0)


def test_persisted_spans_drive_packing():
    """Persisted unit spans (the payload path) are honored exactly like the
    fallback: a whole-chunk span set that fits ships uncut."""
    spans = units_for_text(JCL_TEXT)
    assert len(spans) == 3
    assert all(isinstance(s, UnitSpan) for s in spans)
    hit = _hit(JCL_TEXT, units=tuple((s.start, s.end, s.kind) for s in spans))
    prepared = build_messages("q", [hit], max_chunk_chars=10000)
    entry = prepared.evidence.entries[0]
    assert entry.truncated is False
    assert (entry.units_total, entry.units_retained) == (3, 3)


def test_old_point_without_units_uses_shared_fallback():
    """Legacy points (no persisted spans) still get boundary protection via
    the shared detector — never a silent return to char slicing."""
    prepared = build_messages("q", [_hit(JCL_TEXT)], max_chunk_chars=len(JCL_STMT1) + 10)
    body = _excerpt_body(prepared)
    assert "//STEP1" not in body
    assert prepared.evidence.entries[0].units_total == 3


# ---------------------------------------------------------------------------
# Total-budget remainder and tokenizer verification trims.
# ---------------------------------------------------------------------------


def test_budget_remainder_packs_whole_units_then_stops():
    """The total-context remainder cut keeps whole statements only and
    stops; the remainder never ships a partial statement."""
    lines = [f"//S{i:02d} EXEC PGM=P{i:02d},PARM=ABCDEFGHIJ" for i in range(12)]
    text = "\n".join(lines)
    lead = _hit("Intro sentence.", chunk_type="narrative", index="c0")
    hit = _hit(text, index="c1")
    lead_len = len("[1] SA22-0000-00 Synthetic Reference, H, p. 1-1") + 1 + len("Intro sentence.")
    full = len("[2] SA22-0000-00 Synthetic Reference, H, p. 1-1") + 1 + len(text)
    room = lead_len + 300  # remainder > 200 guard, but short of the full hit
    assert 200 < room - lead_len < full
    prepared = build_messages("q", [lead, hit], max_context_chars=room)
    assert prepared.evidence.omitted_indices == ()
    entry = prepared.evidence.entries[1]
    assert entry.truncated is True
    assert entry.units_total == 12
    assert 1 <= entry.units_retained < 12
    shipped = _excerpt_body(prepared, 2)[: -len(_TRUNCATED_SUFFIX)].splitlines()
    assert shipped[0].startswith("[2] ")
    assert shipped[1:] and all(line in lines for line in shipped[1:])


def test_verify_trim_snaps_to_statement_boundary():
    """A verification overshoot landing mid-statement snaps back to the
    prior statement end; the suffix marks whole-unit omission only."""
    from mainframe_rag.agent.answer import PackedExcerpt

    lines = [f"//S{i:02d} EXEC PGM=P{i:02d},PARM=ABCDEFGHIJ" for i in range(12)]
    text = "\n".join(lines)
    hit = _hit(text)
    excerpt = PackedExcerpt(index=1, hit=hit, body=text, truncated=False)
    packed = [excerpt]
    # Overshoot sized to cut ~100 chars into the 12-statement body.
    _verify_trim_last(packed, used=1000, verify_limit=1000 - 11)
    assert len(packed) == 1
    assert packed[0].truncated is True
    shipped = packed[0].body[: -len(_TRUNCATED_SUFFIX)].splitlines()
    assert 1 <= len(shipped) < len(lines)
    # Every shipped line is a complete statement — no partial card.
    assert all(line in lines for line in shipped)
    assert packed[0].body.rstrip().endswith(_TRUNCATED_SUFFIX.strip())


def test_verify_trim_pops_when_no_unit_fits():
    """An overshoot larger than the whole excerpt removes it instead of
    shipping a sliver."""
    from mainframe_rag.agent.answer import PackedExcerpt

    hit = _hit(JCL_TEXT)
    packed = [PackedExcerpt(index=1, hit=hit, body=JCL_TEXT, truncated=False)]
    _verify_trim_last(packed, used=100000, verify_limit=10)
    assert packed == []


# ---------------------------------------------------------------------------
# Final budget compliance: raise, verify, or explicitly estimate.
# ---------------------------------------------------------------------------


class _HugeTokenizer:
    remote_confirmed = False

    def count_messages(self, messages) -> int:
        return 10**9


class _TwoPhaseTokenizer:
    """First count explodes (forces trims), later counts fit: evidence can
    empty while fixed content still fits — no raise, explicit estimate."""

    remote_confirmed = False

    def __init__(self):
        self.calls = 0

    def count_messages(self, messages) -> int:
        self.calls += 1
        return 10**9 if self.calls == 1 else 5


class _RemoteOkTokenizer:
    remote_confirmed = True

    def count_messages(self, messages) -> int:
        return 5


def _tokenizer_settings(**overrides):
    from mainframe_rag.config import Settings

    return Settings(**settings_kw(**overrides))


def test_irreducible_fixed_overflow_raises_before_generation():
    """Fixed content alone over the window: no model call may happen; the
    failure is an explicit budget error carrying counts, never content."""
    settings = _tokenizer_settings()
    with pytest.raises(PromptBudgetExceeded) as exc_info:
        build_messages(
            "q", [_hit(JCL_TEXT)], tokenizer=_HugeTokenizer(), settings=settings
        )
    assert exc_info.value.used > exc_info.value.limit
    assert JCL_STMT1 not in str(exc_info.value)


def test_zero_excerpts_but_fitting_fixed_returns_empty_manifest():
    """Everything trimmable gone but fixed content fits: empty evidence,
    no raise, explicitly estimator-based."""
    settings = _tokenizer_settings()
    prepared = build_messages(
        "q", [_hit(JCL_TEXT)], tokenizer=_TwoPhaseTokenizer(), settings=settings
    )
    assert prepared.evidence.entries == ()
    assert prepared.evidence.omitted_indices == (1,)
    assert prepared.budget_verified is False


def test_no_tokenizer_reports_unverified_budget():
    """The char-packing path cannot confirm compliance: it must say so."""
    prepared = build_messages("q", [_hit(JCL_TEXT)], max_chunk_chars=10000)
    assert prepared.budget_verified is False
    assert len(prepared.evidence.entries) == 1


def test_fallback_tokenizer_reports_estimated():
    """Estimator-only verification is not confirmation, even when it fits."""
    settings = _tokenizer_settings()
    prepared = build_messages(
        "q", [_hit(JCL_TEXT)], tokenizer=FallbackTokenizer(), settings=settings
    )
    assert prepared.budget_verified is False


def test_remote_tokenizer_reports_verified():
    """A real tokenizer measurement inside the window is confirmed."""
    settings = _tokenizer_settings()
    prepared = build_messages(
        "q", [_hit(JCL_TEXT)], tokenizer=_RemoteOkTokenizer(), settings=settings
    )
    assert prepared.budget_verified is True


# ---------------------------------------------------------------------------
# Manifest wiring: omitted units are not citation-eligible.
# ---------------------------------------------------------------------------


def test_wholly_omitted_chunk_is_not_citation_eligible():
    """Citations follow the retained manifest, not retrieval rank: packed
    chunks' cites are accepted while the omitted chunk's cite and bracket
    label are both rejected."""
    lead = _hit("Intro sentence.", chunk_type="narrative", index="c0", page="1")
    hit1 = _hit(JCL_TEXT, index="c1", page="2")
    hit2 = _hit(REXX_TEXT, index="c2", page="3")
    lead_len = len("[1] SA22-0000-00 Synthetic Reference, H, p. 1-1") + 1 + len("Intro sentence.")
    header1 = "[2] SA22-0000-00 Synthetic Reference, H, p. 1-2"
    room = lead_len + len(header1) + 1 + len(JCL_TEXT) + 1 + 10
    prepared = build_messages("q", [lead, hit1, hit2], max_context_chars=room)
    assert prepared.evidence.omitted_indices == (3,)
    assert prepared.evidence.cite_for_index(3) is None
    parsed = parse_answer(
        f"Answer text [2] and [3].\n\nCitations:\n{hit1.cite}\n{hit2.cite}",
        prepared.evidence,
    )
    assert parsed.citations == [hit1.cite]
    # The omitted chunk's block line is rejected as unmapped (bracket
    # inference only runs when no explicit citation survived).
    assert parsed.cites_rejected_unmapped == 1


def test_chat_path_matches_single_turn_on_units():
    """Chat packing (both order modes) honors the same unit rule as the
    single-turn path for one shared manifest shape."""
    messages = [ChatMessage(role="user", content="How do I run the sort?")]
    for order in ("retrieval", "stable_cache"):
        prepared = build_chat_messages(
            messages,
            [_hit(JCL_TEXT)],
            max_chunk_chars=len(JCL_STMT1) + 10,
            order=order,
        )
        assert "//STEP1" not in prepared.messages[-1].content, order
        assert prepared.evidence.entries[0].units_retained == 1, order


# ---------------------------------------------------------------------------
# Explicit error contract: 422 on JSON/chat, error event on streams, fixed
# console banner — and never a model call.
# ---------------------------------------------------------------------------


class _NeverLLM:
    """Fails the test if the model is called: budget overflow must raise
    before generation."""

    def chat(self, *args, **kwargs):
        raise AssertionError("model must not be called on budget overflow")

    async def chat_stream(self, *args, **kwargs):
        raise AssertionError("model must not be called on budget overflow")
        yield {}


class _RouteOvershootTokenizer:
    remote_confirmed = False

    def count_messages(self, messages) -> int:
        return 10**9


def _route_search(qdrant, embedder, collection, query, product=None, version=None,
                  limit=8, *args, **kwargs):
    hit = _hit(JCL_TEXT, index="c1", page="2")
    return [hit], "identifier", {"embed_ms": 1}


@pytest.fixture
def budget_client(monkeypatch, synthetic_pdf):
    from fastapi.testclient import TestClient

    from mainframe_rag.agent import app as app_mod

    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setenv("LLM_MODEL_REASONING", "test-reasoning-model")
    monkeypatch.setenv("UI_ENABLED", "true")
    monkeypatch.setattr(app_mod, "retrieve_search", _route_search)
    with TestClient(app_mod.app) as c:
        monkeypatch.setattr(app_mod, "llm", _NeverLLM())
        monkeypatch.setattr(app_mod, "tokenizer", _RouteOvershootTokenizer())
        yield c


def test_answer_route_reports_budget_exceeded_422(budget_client):
    """Fixed 422 envelope with a stable code — never exception text, never
    a 500, never a model call."""
    resp = budget_client.post("/v1/answer", json={"query": "IEA500I"})
    assert resp.status_code == 422
    assert resp.json() == {
        "code": "prompt_budget_exceeded",
        "message": "prompt exceeds the model token budget",
    }


def test_chat_route_reports_budget_exceeded_422(budget_client):
    resp = budget_client.post(
        "/v1/chat", json={"messages": [{"role": "user", "content": "IEA500I"}]}
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "prompt_budget_exceeded"


def test_answer_stream_reports_budget_error_event(budget_client):
    """Headers are already sent when the generator runs: the stream carries
    an error event and ends WITHOUT a final."""
    resp = budget_client.post("/v1/answer?stream=true", json={"query": "IEA500I"})
    assert resp.status_code == 200
    names = [
        line[7:].strip()
        for line in resp.text.splitlines()
        if line.startswith("event: ")
    ]
    assert "error" in names
    assert "final" not in names


def test_chat_stream_reports_budget_error_frames(budget_client):
    """Chat streaming on budget overflow yields the error frame and done,
    with no content delta frames."""
    import json

    resp = budget_client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "IEA500I"}], "stream": True},
    )
    assert resp.status_code == 200
    lines = [line.strip() for line in resp.text.splitlines() if line.startswith("data: ")]
    assert len(lines) == 2
    err_payload = json.loads(lines[0][6:])
    assert "error" in err_payload
    assert err_payload["error"]["code"] == "upstream_error"
    assert lines[1] == "data: [DONE]"


def test_console_reports_budget_banner(budget_client):
    """The operator console names the fault as a request to shrink, with a
    422 page status — not the generic server-fault banner."""
    resp = budget_client.post("/ui/chat", data={"message": "IEA500I?", "messages": ""})
    assert resp.status_code == 422
    assert "token budget" in resp.text
    assert "could not complete this request" not in resp.text


# ---------------------------------------------------------------------------
# Ingest and storage round-trip: span emission, payload persistence, parsing.
# ---------------------------------------------------------------------------


def test_make_chunks_emits_unit_spans_for_structured_and_prose():
    from mainframe_rag.ingest.chunk import UNIT_ATOMIC, make_chunks
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc

    parsed = ParsedDoc(
        path="manual.pdf",
        doc_id="SC14-7315-70",
        sha256="abc123",
        vendor="IBM",
        product="z/OS",
        version="3.2",
        title="Sample Manual",
        page_count=1,
    )
    # JCL content
    chunks_jcl = make_chunks(parsed, [JCL_TEXT], ["1-1"])
    assert len(chunks_jcl) == 1
    assert chunks_jcl[0].units is not None
    assert len(chunks_jcl[0].units) == 3
    assert all(u.kind == UNIT_ATOMIC for u in chunks_jcl[0].units)

    # Narrative content
    prose = "Alpha narrative paragraph. Beta sentence here."
    chunks_prose = make_chunks(parsed, [prose], ["1-1"])
    assert len(chunks_prose) == 1
    assert chunks_prose[0].units == ()


def test_stored_spans_caps_oversize_lists():
    from mainframe_rag.ingest.chunk import UNIT_ATOMIC, UnitSpan, _stored_spans

    spans = tuple(UnitSpan(i * 10, i * 10 + 5, UNIT_ATOMIC) for i in range(600))
    assert _stored_spans(spans) is None


def test_upsert_chunks_persists_units_only_when_present():
    from mainframe_rag.config import Settings
    from mainframe_rag.ingest.chunk import Chunk, UnitSpan
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc
    from mainframe_rag.ingest.qdrant_io import upsert_chunks

    class _Recorder:
        def __init__(self):
            self.points = []

        def upsert(self, collection_name, *, points, wait=True):
            self.points.extend(points)
            return True

    parsed = ParsedDoc(
        path="manual.pdf",
        doc_id="SC14-7315-70",
        sha256="abc123",
        vendor="IBM",
        product="z/OS",
        version="3.2",
        title="Sample Manual",
        page_count=1,
    )
    chunk_structured = Chunk(
        chunk_id="00000000-0000-0000-0000-000000000001",
        doc_id="SC14-7315-70",
        heading_path="H",
        page_start=1,
        page_label="1-1",
        chunk_type="syntax",
        text=JCL_TEXT,
        message_ids=[],
        members=[],
        ordinal=0,
        units=(UnitSpan(0, len(JCL_STMT1), "atomic"),),
    )
    chunk_prose = Chunk(
        chunk_id="00000000-0000-0000-0000-000000000002",
        doc_id="SC14-7315-70",
        heading_path="H",
        page_start=1,
        page_label="1-1",
        chunk_type="narrative",
        text="Some prose",
        message_ids=[],
        members=[],
        ordinal=1,
        units=(),
    )
    vectors = [
        ([0.1] * 4, ([1], [1.0])),
        ([0.1] * 4, ([1], [1.0])),
    ]
    client = _Recorder()
    settings = Settings(**settings_kw())
    upsert_chunks(client, settings, parsed, [chunk_structured, chunk_prose], vectors)

    assert len(client.points) == 2
    # Structured chunk has units persisted
    assert client.points[0].payload["units"] == [[0, len(JCL_STMT1), "atomic"]]
    # Prose chunk has units omitted
    assert "units" not in client.points[1].payload


def test_parse_unit_spans_fail_closed():
    from mainframe_rag.retrieve.query import _parse_unit_spans

    assert _parse_unit_spans(None) is None
    assert _parse_unit_spans("not a list") is None
    assert _parse_unit_spans([[0, 10, "atomic"]]) == ((0, 10, "atomic"),)
    assert _parse_unit_spans([[0, 10, "prose"]]) == ((0, 10, "prose"),)
    # Malformed: wrong length
    assert _parse_unit_spans([[0, 10]]) is None
    # Malformed: invalid kind
    assert _parse_unit_spans([[0, 10, "invalid"]]) is None
    # Malformed: negative index
    assert _parse_unit_spans([[-1, 10, "atomic"]]) is None
    # Malformed: inverted range
    assert _parse_unit_spans([[10, 5, "atomic"]]) is None
    # Malformed: bool in place of int
    assert _parse_unit_spans([[True, 10, "atomic"]]) is None


def test_hit_spans_fails_closed_on_corrupt_payload_spans():
    from mainframe_rag.agent.answer import _hit_spans

    hit = _hit(JCL_TEXT, units=((50, 10, "atomic"),))  # start > end -> invalid
    spans = _hit_spans(hit, JCL_TEXT)
    # Falls back to shared redetection: returns 3 valid spans for JCL_TEXT
    assert len(spans) == 3
    assert all(s.kind == "atomic" for s in spans)

    hit_overlap = _hit(JCL_TEXT, units=((10, 20, "atomic"), (15, 30, "atomic")))  # start < cursor
    spans_overlap = _hit_spans(hit_overlap, JCL_TEXT)
    assert len(spans_overlap) == 3

