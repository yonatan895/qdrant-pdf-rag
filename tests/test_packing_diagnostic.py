"""Packing diagnostic (issue #811 phase 0): synthetic, hermetic cases.

Every fixture is original synthetic text. The diagnostic drives the real
answer-path prompt builder; these tests pin what it reports for hits whose
packing outcome is known by construction: kept, cut by the cross-hit budget
break (with and without room left), too large for the per-chunk cap, and
never retrieved.
"""

from __future__ import annotations

import json

from scripts.packing_diagnostic import (
    BUDGET_BREAK,
    CAT_NOT_RETRIEVED,
    CAT_OK,
    CAT_PACKING_LOSS,
    NO_UNIT_FITS_CAP,
    NOT_RETRIEVED,
    RETRIEVED_DROPPED,
    RETRIEVED_PACKED,
    render_markdown,
    select_entries,
    summarize,
    trace_row,
)

from mainframe_rag.agent.answer import build_messages
from mainframe_rag.agent.tokenizer import FallbackTokenizer
from mainframe_rag.config import Settings
from mainframe_rag.eval.datasets import GoldenEntry
from mainframe_rag.retrieve.query import SearchHit
from tests.fakes import settings_kw

_MARK = "zzmark"


def _settings(**overrides) -> Settings:
    base = {
        "llm_max_model_len": 2048,
        "llm_reserved_output_tokens": 128,
        "llm_thinking_reserve_tokens_simple": 0,
        "llm_thinking_reserve_tokens_complex": 0,
        "llm_token_safety_margin": 0,
        "llm_max_chunk_tokens_narrative": 350,
        "prompt_max_chunk_chars": 4000,
    }
    return Settings(**settings_kw(**{**base, **overrides}))


def _entry_text(code: str, words: int) -> str:
    return f"{code} Synthetic message text.\nExplanation: " + " ".join([_MARK] * words)


def _hit(doc: str, parts: list[str], chunk_id: str) -> SearchHit:
    text = "\n\n".join(parts)
    units, pos = [], 0
    for part in parts:
        units.append((pos, pos + len(part), "atomic"))
        pos += len(part) + 2
    return SearchHit(
        chunk_id=chunk_id,
        score=1.0,
        cite=f"{doc} Synthetic Reference, H, p. 1-1",
        heading="H",
        text=text,
        doc_id=doc,
        title="Synthetic Reference",
        page_label="1-1",
        chunk_type="message",
        message_ids=(),
        units=tuple(units),
    )


def _golden(row_id: str, query: str, docs: list[str], **kw) -> GoldenEntry:
    return GoldenEntry(id=row_id, query=query, expected_doc_ids=docs, **kw)


def _budget(hits: list[SearchHit], query: str, settings: Settings) -> int:
    entry = _golden("MSG-00", query, [hits[0].doc_id])
    return trace_row("MSG-00", entry, hits, settings).budget_tokens


def test_budget_break_drops_later_hits_even_when_a_small_one_would_fit():
    """Rank 2 does not fit what rank 1 left; rank 3 is small and would fit,
    but the planner stops at rank 2: reported as a budget cut that still
    fit the leftover budget (the skip-and-continue signal)."""
    settings = _settings(prompt_max_chunk_chars=10000)
    query = "Describe the synthetic messages."
    small = [_entry_text("ABC100I", 20)]
    probe = [_hit("SA00-0001-01", small, "c1")]
    budget = _budget(probe, query, settings)
    # Rank 1 takes about half the budget; rank 2 is one atomic unit that
    # cannot fit the rest; rank 3 is tiny.
    first = _hit("SA00-0001-01", [_entry_text("ABC100I", int(budget * 0.45))], "c1")
    big = _hit("SA00-0002-01", [_entry_text("ABC200E", int(budget * 0.9))], "c2")
    tiny = _hit("SA00-0003-01", [_entry_text("ABC300W", 15)], "c3")

    row = trace_row("MSG-01", _golden("MSG-01", query, ["SA00-0003-01"]), [first, big, tiny], settings)

    assert [h.status for h in row.hits] == ["packed", "dropped", "dropped"]
    assert [h.drop_reason for h in row.hits] == [None, BUDGET_BREAK, BUDGET_BREAK]
    assert row.hits[1].fits_remaining_budget is False
    assert row.hits[2].fits_remaining_budget is True
    assert row.category == CAT_PACKING_LOSS
    assert [(t.key, t.outcome) for t in row.targets if t.kind == "doc"] == [
        ("SA00-0003-01", RETRIEVED_DROPPED)
    ]
    # The carrier fits what is left (skip-and-continue) and an empty budget.
    assert row.targets[0].hit_ranks == [3]
    assert row.targets[0].recovered_by == ["skip_and_continue", "prioritize_doc_only"]


def test_unit_larger_than_chunk_cap_is_not_a_budget_cut():
    settings = _settings(prompt_max_chunk_chars=500)
    query = "What does ABC100I mean?"
    huge = _hit("SA00-0001-01", [_entry_text("ABC100I", 400)], "c1")
    ok = _hit("SA00-0002-01", [_entry_text("ABC200E", 10)], "c2")

    row = trace_row("SYS-01", _golden("SYS-01", query, ["SA00-0001-01"]), [huge, ok], settings)

    assert row.hits[0].status == "dropped"
    assert row.hits[0].drop_reason == NO_UNIT_FITS_CAP
    assert row.hits[0].fits_remaining_budget is None
    assert row.hits[1].status == "packed"
    entry_target = next(t for t in row.targets if t.kind == "entry-heading")
    assert (entry_target.key, entry_target.outcome) == ("ABC100I", RETRIEVED_DROPPED)
    assert (row.requested_retrieved, row.requested_in_prompt) == (1, 0)
    assert row.category == CAT_PACKING_LOSS


def test_requested_entry_later_in_a_hit_is_found_in_the_packed_range():
    """The requested entry is the SECOND unit of a hit larger than the cap:
    the production planner packs that unit (not the prefix), and the
    diagnostic reports the entry as supplied."""
    settings = _settings(prompt_max_chunk_chars=900)
    query = "What does ABC200E mean?"
    hit = _hit(
        "SA00-0001-01",
        [_entry_text("ABC100I", 100), _entry_text("ABC200E", 20)],
        "c1",
    )

    row = trace_row("SYS-02", _golden("SYS-02", query, ["SA00-0001-01"]), [hit], settings)

    assert row.hits[0].status == "packed_truncated"
    assert row.hits[0].start and row.hits[0].start > 0
    assert row.hits[0].entry_ids == ["ABC200E"]
    assert row.hits[0].entry_ids_in_prompt == ["ABC200E"]
    assert (row.requested_retrieved, row.requested_in_prompt) == (1, 1)
    assert row.category == CAT_OK


def test_gold_document_never_retrieved_is_not_a_packing_loss():
    settings = _settings()
    other = _hit("SA00-0009-01", [_entry_text("ABC100I", 10)], "c1")
    row = trace_row(
        "CMP-01",
        _golden("CMP-01", "Compare JES2 versus JES3 spool.", ["SA00-0001-01", "SA00-0002-01"]),
        [other],
        settings,
    )
    assert {t.outcome for t in row.targets} == {NOT_RETRIEVED}
    assert row.category == CAT_NOT_RETRIEVED
    assert row.hits[0].gold_doc is False


def test_comparison_row_reports_each_expected_doc():
    settings = _settings()
    a = _hit("SA00-0001-01", [_entry_text("ABC100I", 10)], "c1")
    b = _hit("SA00-0002-01", [_entry_text("ABC200E", 10)], "c2")
    row = trace_row(
        "CMP-03", _golden("CMP-03", "Compare A versus B.", ["SA00-0001-01", "SA00-0002-01"]),
        [a, b], settings,
    )
    assert {(t.key, t.outcome) for t in row.targets} == {
        ("SA00-0001-01", RETRIEVED_PACKED),
        ("SA00-0002-01", RETRIEVED_PACKED),
    }
    assert row.category == CAT_OK


def test_heading_row_matches_on_heading_not_doc_alone():
    settings = _settings()
    hit = _hit("SA00-0001-01", [_entry_text("ABC100I", 10)], "c1")
    wrong = _golden("TBL-01", "Which table lists fields?", ["SA00-0001-01"], expected_heading="zzz")
    right = _golden("TBL-01", "Which table lists fields?", ["SA00-0001-01"], expected_heading="h")
    assert trace_row("TBL-01", wrong, [hit], settings).category == CAT_NOT_RETRIEVED
    assert trace_row("TBL-01", right, [hit], settings).category == CAT_OK


def test_packed_ranges_equal_the_answer_path_manifest():
    """Drift guard: the diagnostic reports the manifest the answer path's own
    prompt builder produces for the same hits and settings."""
    settings = _settings(prompt_max_chunk_chars=900)
    query = "What does ABC200E mean?"
    hits = [
        _hit("SA00-0001-01", [_entry_text("ABC100I", 100), _entry_text("ABC200E", 20)], "c1"),
        _hit("SA00-0002-01", [_entry_text("ABC300W", 30)], "c2"),
    ]
    row = trace_row("SYS-03", _golden("SYS-03", query, ["SA00-0001-01"]), hits, settings)
    direct = build_messages(
        query,
        hits,
        max_context_chars=settings.prompt_max_context_chars,
        max_chunk_chars=settings.prompt_max_chunk_chars,
        splunk_context_max_chars=settings.splunk_context_max_chars,
        complexity="simple",
        tokenizer=FallbackTokenizer(),
        settings=settings,
        order=settings.prompt_order,
        reasoning_effort="low",
    )
    expected = {e.prompt_index: (e.start_char, e.included_chars) for e in direct.evidence.entries}
    got = {h.rank: (h.start, h.end) for h in row.hits if h.status != "dropped"}
    assert got == expected


def test_report_carries_no_chunk_text_and_summarizes_by_class():
    settings = _settings(prompt_max_chunk_chars=500)
    huge = _hit("SA00-0001-01", [_entry_text("ABC100I", 400)], "c1")
    ok = _hit("SA00-0002-01", [_entry_text("ABC200E", 10)], "c2")
    rows = [
        trace_row("SYS-01", _golden("SYS-01", "What does ABC100I mean?", ["SA00-0001-01"]), [huge, ok], settings),
        trace_row("SYS-02", _golden("SYS-02", "What does ABC200E mean?", ["SA00-0002-01"]), [huge, ok], settings),
    ]
    summary = summarize(rows)
    assert summary["SYS"]["rows"] == 2
    assert summary["SYS"]["categories"] == {CAT_PACKING_LOSS: 1, CAT_OK: 1}
    assert summary["SYS"]["drop_reasons"] == {NO_UNIT_FITS_CAP: 2}
    assert summary["SYS"]["targets"]["entry-heading"] == {RETRIEVED_DROPPED: 1, RETRIEVED_PACKED: 1}
    assert (summary["SYS"]["requested_ids"], summary["SYS"]["requested_in_prompt"]) == (2, 1)
    from dataclasses import asdict

    blob = json.dumps([asdict(r) for r in rows]) + render_markdown(rows, summary)
    assert _MARK not in blob and "Synthetic message text" not in blob


def test_select_entries_by_class_or_explicit_ids():
    entries = [_golden(i, "q", ["SA00-0001-01"]) for i in ("CMP-01", "TBL-01", "SYS-02", "MSG-03")]
    assert [e.id for e in select_entries(entries, ["cmp", "TBL"], [])] == ["CMP-01", "TBL-01"]
    assert [e.id for e in select_entries(entries, ["CMP"], ["msg-03"])] == ["MSG-03"]


def test_profiles_parse_and_apply_through_settings_validation():
    import pytest
    from scripts.packing_diagnostic import apply_overrides, parse_profile

    name, overrides = parse_profile("lane:llm_max_model_len=4096,prompt_max_chunk_chars=3000")
    assert name == "lane" and overrides == {"llm_max_model_len": 4096, "prompt_max_chunk_chars": 3000}
    assert parse_profile("target") == ("target", {})
    applied = apply_overrides(_settings(), overrides)
    assert (applied.llm_max_model_len, applied.prompt_max_chunk_chars) == (4096, 3000)
    for bad in ("x:not_a_key=1", "x:llm_max_model_len", ":llm_max_model_len=4096"):
        with pytest.raises(ValueError):
            parse_profile(bad)
    with pytest.raises(ValueError):  # Settings bounds still apply (ge=512)
        apply_overrides(_settings(), {"llm_max_model_len": 10})


def test_requested_id_named_only_inside_other_text_is_a_mention_target():
    """Rules older than code-entry extraction: the identifier is named in the
    text but heads no unit. It is reported as a mention (retrieved), and an
    identifier named in no gold-doc hit is reported as absent."""
    settings = _settings(prompt_max_chunk_chars=600)
    filler = "alpha " * 60
    text = f"{filler}\n\nRunning prose says ZZQ100I is documented here."
    hit = SearchHit(
        chunk_id="c1", score=1.0, cite="SA00-0001-01 Synthetic Reference, H, p. 1-1",
        heading="H", text=text, doc_id="SA00-0001-01", title="Synthetic Reference",
        page_label="1-1", chunk_type="narrative", message_ids=(), units=((0, len(filler.rstrip()), "prose"), (len(filler) + 1, len(text), "prose")),
    )
    row = trace_row("MSG-77", _golden("MSG-77", "What does ZZQ100I mean?", ["SA00-0001-01"]), [hit], settings)
    target = next(t for t in row.targets if t.kind.startswith("entry-"))
    assert target.kind == "entry-mention"
    assert row.hits[0].mention_ids == ["ZZQ100I"]
    absent = trace_row("MSG-78", _golden("MSG-78", "What does ZZQ999I mean?", ["SA00-0001-01"]), [hit], settings)
    assert next(t for t in absent.targets if t.kind.startswith("entry-")).kind == "entry-none"
