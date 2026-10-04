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
    _LEADING_MARKER,
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
def budget_client(monkeypatch, synthetic_pdf, servable_representation_gate):
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



class _DenseTokenizer:
    """Remote-style counter that sees ~1 token per char: far denser than
    the word-count estimator, as for numeric/table-heavy excerpts."""

    remote_confirmed = True

    def count_messages(self, messages) -> int:
        return sum(len(m.content) for m in messages)


def test_estimator_drift_trims_past_four_rounds_instead_of_refusing():
    """Issue #307 (TBL-02, 2026-10-03): eight dense excerpts planned by the
    estimator overshoot the real window by far more than four last-excerpt
    trims can remove. The prompt must keep shrinking while evidence remains
    droppable, so PromptBudgetExceeded means 'nothing left to trim', never
    'ran out of rounds'; the surviving manifest still fits and is non-empty."""
    settings = _tokenizer_settings(
        llm_max_model_len=4096,
        llm_reserved_output_tokens=800,
        llm_token_safety_margin=64,
    )
    body = "\n".join(f"R{i:02d}  {i * 4096:08X}  {i * 77:06d}" for i in range(40))
    hits = [_hit(body, chunk_type="table", index=f"c{n}", page=str(n)) for n in range(1, 9)]
    prepared = build_messages(
        "which table", hits, tokenizer=_DenseTokenizer(), settings=settings,
        complexity="simple",
    )
    assert prepared.budget_verified is True
    assert 1 <= len(prepared.evidence.entries) < 8
    limit = 4096 - 800 - settings.llm_thinking_reserve_tokens_simple - 64
    assert _DenseTokenizer().count_messages(prepared.messages) <= limit


def test_trim_round_limit_scales_with_trimmable_content():
    from mainframe_rag.agent.answer import _MAX_TRIM_ROUNDS, _trim_round_limit

    assert _trim_round_limit(0) == _MAX_TRIM_ROUNDS
    assert _trim_round_limit(8) > _trim_round_limit(2) > _MAX_TRIM_ROUNDS
    assert _trim_round_limit(3, 2, base=_MAX_TRIM_ROUNDS * 2) == _MAX_TRIM_ROUNDS * 2 + 6 + 2


# ---------------------------------------------------------------------------
# Requested-passage range selection with truthful source offsets (#632).
# Original synthetic text only.
# ---------------------------------------------------------------------------


def _entry(code: str, body_chars: int, fill: str = "alpha ") -> str:
    explanation = (fill * (body_chars // len(fill) + 1))[:body_chars].rstrip()
    return f"{code} Synthetic message text.\nExplanation: {explanation}"


def _spans_for(parts: list[str]) -> tuple[tuple[int, int, str], ...]:
    out, pos = [], 0
    for part in parts:
        out.append((pos, pos + len(part), "atomic"))
        pos += len(part) + 2
    return tuple(out)


def _message_hit(parts: list[str], index: str = "m1", units=None) -> SearchHit:
    text = "\n\n".join(parts)
    return _hit(
        text,
        chunk_type="message",
        units=_spans_for(parts) if units is None else units,
        index=index,
    )


def _supplied(prepared, index: int = 1) -> str:
    """Exact excerpt text the model receives (without the [i] cite header)."""
    return _excerpt_body(prepared, index).split("\n", 1)[1]


def _assert_offsets_reconstruct(prepared, hit, index: int = 1) -> None:
    entry = next(e for e in prepared.evidence.entries if e.prompt_index == index)
    source = hit.text.strip()
    expected = (
        (_LEADING_MARKER if entry.start_char > 0 else "")
        + source[entry.start_char : entry.included_chars]
        + (_TRUNCATED_SUFFIX if entry.included_chars < len(source) else "")
    )
    assert _supplied(prepared, index) == expected
    assert entry.truncated == (entry.start_char > 0 or entry.included_chars < len(source))


TWO_ENTRIES = [_entry("ABC100I", 2550), _entry("ABC200E", 800, "bravo ")]


@pytest.mark.parametrize("tokenized", [False, True])
def test_requested_later_entry_is_supplied_not_unrelated_prefix(tokenized):
    hit = _message_hit(TWO_ENTRIES)
    assert 3400 < len(hit.text) and hit.text.index("ABC200E") > 2500
    kwargs = (
        {"tokenizer": FallbackTokenizer(), "settings": _tokenizer_settings()}
        if tokenized
        else {}
    )
    prepared = build_messages("What does ABC200E mean?", [hit], **kwargs)
    shipped = _supplied(prepared)
    assert shipped.startswith(_LEADING_MARKER + "ABC200E Synthetic message text.")
    assert "ABC100I" not in shipped
    assert TWO_ENTRIES[1] in shipped
    entry = prepared.evidence.entries[0]
    assert entry.start_char == hit.text.index("ABC200E")
    assert entry.units_total == 2 and entry.units_retained == 1
    _assert_offsets_reconstruct(prepared, hit)


def test_unrequested_and_unmatched_queries_keep_prefix_behavior():
    hit = _message_hit(TWO_ENTRIES)
    for query in ("q", "What does ABC300I mean?", "What does ABC100I mean?"):
        prepared = build_messages(query, [hit])
        entry = prepared.evidence.entries[0]
        assert entry.start_char == 0, query
        assert _supplied(prepared).startswith("ABC100I"), query
        assert "ABC200E" not in _supplied(prepared), query
        _assert_offsets_reconstruct(prepared, hit)


def test_incidental_mention_is_not_an_entry_heading():
    parts = [
        _entry("ABC100I", 1400) + " See also ABC200E for details.",
        _entry("ABC150W", 1300),
        _entry("ABC200E", 500, "bravo "),
    ]
    hit = _message_hit(parts)
    prepared = build_messages("ABC200E", [hit], max_chunk_chars=2200)
    assert parts[2] in _supplied(prepared)
    entry = prepared.evidence.entries[0]
    assert entry.start_char > 0
    _assert_offsets_reconstruct(prepared, hit)
    # A near-miss identifier (longer token) is not a heading either.
    near = _message_hit([_entry("ABC2001E", 1800), _entry("ABC200E", 400, "bravo ")])
    prepared = build_messages("ABC200E", [near], max_chunk_chars=1500)
    assert near.text.index("ABC200E Synthetic") == prepared.evidence.entries[0].start_char


def test_multiple_requested_identifiers_share_one_contiguous_range():
    parts = [_entry("ABC050I", 1500), _entry("ABC100I", 500), _entry("ABC200E", 500, "bravo ")]
    hit = _message_hit(parts)
    prepared = build_messages("ABC100I and ABC200E", [hit], max_chunk_chars=1400)
    shipped = _supplied(prepared)
    assert parts[1] in shipped and parts[2] in shipped
    assert "ABC050I" not in shipped
    _assert_offsets_reconstruct(prepared, hit)


def test_unfittable_entry_is_omitted_explicitly():
    """An entry larger than the cap is never partially shipped."""
    hit = _message_hit([_entry("ABC100I", 3400)])
    prepared = build_messages("ABC100I", [hit], max_chunk_chars=1000)
    assert prepared.evidence.entries == ()
    assert prepared.evidence.omitted_indices == (1,)
    assert prepared.evidence.cite_for_index(1) is None


@pytest.mark.parametrize("order", ["retrieval", "stable_cache"])
def test_chat_path_supplies_requested_later_entry(order):
    hit = _message_hit(TWO_ENTRIES)
    prepared = build_chat_messages(
        [ChatMessage(role="user", content="What does ABC200E mean?")], [hit], order=order
    )
    body = prepared.messages[-1].content
    assert TWO_ENTRIES[1] in body and "ABC100I" not in body
    assert prepared.evidence.entries[0].start_char == hit.text.index("ABC200E")


class _OvershootTokenizer:
    """Reports overshoot for the first N counts, forcing real trim rounds."""

    remote_confirmed = False

    def __init__(self, over_rounds: int):
        self.left = over_rounds

    def count_messages(self, messages) -> int:
        if self.left > 0 and "[1] " in messages[-1].content:
            self.left -= 1
            return 6528 + 150
        return 5


def test_offsets_reconstruct_exactly_after_every_trim_round():
    settings = _tokenizer_settings(llm_max_model_len=8192)
    parts = [_entry("ABC100I", 600), _entry("ABC200E", 1200, "bravo "), _entry("ABC300I", 400)]
    hit = _message_hit(parts)
    outcomes = []
    for rounds in range(1, 4):
        prepared = build_messages(
            "ABC200E",
            [hit],
            tokenizer=_OvershootTokenizer(rounds),
            settings=settings,
            max_chunk_chars=1500,
        )
        entries = prepared.evidence.entries
        if not entries:
            assert prepared.evidence.omitted_indices == (1,)
            outcomes.append("omitted")
            continue
        _assert_offsets_reconstruct(prepared, hit)
        # The requested entry is whole whenever the excerpt survives.
        assert parts[1] in _supplied(prepared)
        outcomes.append(entries[0].included_chars)
    assert outcomes  # every round count produced a consistent result


def test_trim_dropping_requested_entry_omits_the_excerpt():
    from mainframe_rag.agent.answer import (
        _hit_spans,
        _packed_excerpt,
        _requested_identifiers,
        _select_range,
    )

    hit = _message_hit([_entry("ABC100I", 1500), _entry("ABC200E", 900, "bravo ")])
    source = hit.text.strip()
    start, end, required = _select_range(
        source, _hit_spans(hit, source), 1200, _requested_identifiers("ABC200E")
    )
    assert start == source.index("ABC200E") and required == end
    packed = [_packed_excerpt(1, hit, source, start, end, required)]
    # Overshoot large enough that only a cut inside the entry would fit.
    _verify_trim_last(packed, used=1000, verify_limit=1000 - 100)
    assert packed == []


@pytest.mark.parametrize("tail", [" Applies to all devices.", ""])
def test_prose_cut_ends_on_sentence_boundary(tail):
    lead = "Run the utility during the nightly maintenance window on every system. " * 3
    text = lead + "Use FAST=YES only when recovery mode is disabled." + tail
    hit = _hit(text, chunk_type="narrative", units=())
    cap = len(lead) + len("Use FAST=YES")
    prepared = build_messages("q", [hit], max_chunk_chars=cap, max_chunk_chars_narrative=cap)
    shipped = _supplied(prepared)
    assert "FAST=YES" not in shipped
    assert shipped.removesuffix(_TRUNCATED_SUFFIX).endswith("window on every system.")
    _assert_offsets_reconstruct(prepared, hit)


def test_prose_cut_never_separates_assertion_from_its_qualifier():
    lead = "Run the utility during the nightly maintenance window on every system. " * 3
    text = lead + "Use FAST=YES. However, do not use it when recovery mode is enabled. More."
    hit = _hit(text, chunk_type="narrative", units=())
    cap = len(lead) + len("Use FAST=YES. However")
    prepared = build_messages("q", [hit], max_chunk_chars=cap, max_chunk_chars_narrative=cap)
    assert "FAST=YES" not in _supplied(prepared)
    # Whole text fits: both sentences ship together.
    prepared = build_messages("q", [hit], max_chunk_chars=len(text) + 5)
    assert "However, do not use it" in _supplied(prepared)


def test_malformed_unit_spans_fall_back_without_inventing_anchors():
    overlapping = ((0, 3000, "atomic"), (10, 20, "atomic"))
    hit = _message_hit(TWO_ENTRIES, units=overlapping)
    prepared = build_messages("ABC200E", [hit])
    entry = prepared.evidence.entries[0]
    assert entry.start_char == 0 and entry.included_chars <= 3000
    _assert_offsets_reconstruct(prepared, hit)


def _chunker_hits(text: str, heading: str) -> list[SearchHit]:
    from pathlib import Path

    from mainframe_rag.ingest.chunk import make_chunks
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc

    parsed = ParsedDoc(
        path=Path("synthetic.pdf"),
        sha256="deadbeef",
        doc_id="SA99-0000-00",
        title=heading,
        toc=((1, heading, 1),),
        page_count=1,
    )
    return [
        SearchHit(
            chunk_id=f"k{c.ordinal}",
            score=1.0,
            cite="SA99-0000-00 Synthetic Codes, H, p. 1-1",
            heading=heading,
            text=c.text,
            doc_id="SA99-0000-00",
            title="Synthetic Codes",
            page_label="1-1",
            chunk_type=c.chunk_type,
            message_ids=(),
            units=tuple((u.start, u.end, u.kind) for u in c.units or ()),
        )
        for c in make_chunks(parsed, [text])
    ]


def test_current_chunker_fixture_supplies_requested_code_entry():
    entries = [
        f"0C{n}\nExplanation:\n" + ("Synthetic condition text for this code. " * 12)
        for n in range(1, 10)
    ]
    heading = "System completion codes"
    hits = _chunker_hits(heading + "\n\n" + "\n\n".join(entries), heading)
    target = next(h for h in hits if "\n0C9\n" in f"\n{h.text}")
    assert target.text.index("0C9") > 600  # a later entry in its chunk
    prepared = build_messages("abend code 0C9", [target], max_chunk_chars=900)
    assert "0C9\nExplanation:" in _supplied(prepared)
    _assert_offsets_reconstruct(prepared, target)
