"""Unit tests for harness L2 (answer tier) — pure functions, no stack.

The contract under test: structural fails gate, rates are trend data, the
citation validator stays the single source of truth (L2 maps citations to
hits by exact cite-string match, never re-parses model text), and the
judge must fail closed on unparseable output.
"""

import logging
import re

import pytest
from scripts.harness_l2 import (
    JUDGE_MAX_EVIDENCE_CHARS,
    JUDGE_REASONING_EFFORT,
    JudgeError,
    _AlertCapture,
    _by_complexity_truncation,
    apply_l2_measurements,
    citation_to_hit,
    cited_doc_ids,
    evidence_for_citations,
    gate_l2,
    judge_chat,
    judge_messages,
    parse_judge_label,
    parse_relevance_label,
    precision_recall,
    relevance_messages,
    summarize_l2,
    syntax_check,
)


def _hit(cite: str, doc_id: str, text: str = "LFAREA=(1M)") -> dict:
    return {"cite": cite, "doc_id": doc_id, "text": text}


HITS = [
    _hit("SA23-1380-70 z/OS MVS Init Tuning Ref, p. 100", "SA23-1380-70"),
    _hit("SC34-6428-08 CICS Sys Def, p. 55", "SC34-6428-08"),
]


# ---------------------------------------------------------------- citation mapping

def test_citation_to_hit_exact_cite_match():
    assert citation_to_hit("[2] SC34-6428-08 CICS Sys Def, p. 55", HITS) == HITS[1]


def test_citation_to_hit_none_when_absent():
    assert citation_to_hit("[9] SA23-1385-70 JCL, p. 3", HITS) is None


def test_cited_doc_ids_maps_and_records_unmatched():
    docs, unmatched = cited_doc_ids(
        ["[1] SA23-1380-70 z/OS MVS Init Tuning Ref, p. 100",
         "[3] GC34-6442-07 CICS Messages, p. 116"],
        HITS,
    )
    assert docs == {"SA23-1380-70"}
    assert unmatched == ["[3] GC34-6442-07 CICS Messages, p. 116"]


# ---------------------------------------------------------------- precision / recall

def test_precision_recall_full_overlap():
    assert precision_recall({"A", "B"}, {"A", "B"}) == (1.0, 1.0)


def test_precision_recall_partial():
    p, r = precision_recall({"A", "X", "Y"}, {"A", "B"})
    assert p == pytest.approx(1 / 3)
    assert r == 0.5


def test_precision_recall_no_citations_zero_recall_not_none():
    # Zero validated citations on an answer row is a recall miss, not a
    # denominator gap — the row must pull the average down.
    assert precision_recall(set(), {"A"}) == (None, 0.0)


def test_precision_recall_no_gold_is_none():
    # Abstain rows carry no gold and stay out of the denominators.
    assert precision_recall({"A"}, set()) == (None, None)


# ---------------------------------------------------------------- judge

def test_parse_judge_label_plain_json():
    assert parse_judge_label('{"label": "entailed"}') == "entailed"


def test_parse_judge_label_fenced_with_prose():
    reply = 'The judgment:\n```json\n{"label": "contradiction"}\n```\nDone.'
    assert parse_judge_label(reply) == "contradiction"


def test_parse_judge_label_unknown_label_fails_closed():
    with pytest.raises(JudgeError):
        parse_judge_label('{"label": "mostly_true"}')


def test_parse_judge_label_garbage_fails_closed():
    with pytest.raises(JudgeError):
        parse_judge_label("I think the answer is supported.")


def test_judge_messages_never_contain_citation_markers():
    # The agent's validator owns citation semantics; the judge sees only the
    # evidence text and the answer body. No bracket-index citation marker may
    # reach the judge in either message, and both inputs must be present.
    evidence = "LFAREA=(1M) syntax..."
    answer = "Set LFAREA=(1M) in IEASYSxx."
    msgs = judge_messages(answer, evidence)
    for m in msgs:
        assert re.search(r"\[\d+\]", m.content) is None, m.content
        assert "SA23-1380-70" not in m.content
    assert evidence in msgs[1].content and answer in msgs[1].content
    assert msgs[0].role == "system" and msgs[1].role == "user"
    assert "contradiction" in msgs[0].content


def test_judge_chat_pins_temperature_and_reasoning_effort():
    # Issue #305: the judge must not run on the server-default effort — at
    # default the model put its CoT in the content channel and the JSON label
    # never appeared (judge infra fail). Both judge legs share this one call
    # shape.
    calls: list[tuple[list, dict]] = []

    class _Client:
        def chat(self, messages, **kwargs):
            calls.append((messages, kwargs))
            return object()

    msgs = judge_messages("A.", "E.")
    judge_chat(_Client(), msgs)
    assert calls == [(msgs, {"temperature": 0.0, "reasoning_effort": JUDGE_REASONING_EFFORT})]
    assert JUDGE_REASONING_EFFORT == "low"


def test_evidence_bounded_for_judge_prompt():
    hits = [_hit(f"c{i}", f"D{i}", "x" * 4000) for i in range(4)]
    evidence, unmapped = evidence_for_citations([f"[{i}] c{i}" for i in range(4)], hits)
    assert len(evidence) <= JUDGE_MAX_EVIDENCE_CHARS + 40
    assert unmapped == []


# ---------------------------------------------------------------- relevance judge

def test_parse_relevance_label_plain_and_fenced():
    assert parse_relevance_label('{"label": "relevant"}') == "relevant"
    assert parse_relevance_label('```json\n{"label": "irrelevant"}\n```') == "irrelevant"


def test_parse_relevance_label_fails_closed_on_unknown():
    with pytest.raises(JudgeError):
        parse_relevance_label('{"label": "mostly_right"}')
    with pytest.raises(JudgeError):
        parse_relevance_label("Looks relevant to me.")


def test_relevance_messages_see_question_and_answer_only():
    msgs = relevance_messages("What is LFAREA?", "Set LFAREA=(1M).")
    assert "What is LFAREA?" in msgs[1].content
    assert "Set LFAREA=(1M)." in msgs[1].content
    assert all(re.search(r"\[\d+\]", m.content) is None for m in msgs)
    assert msgs[0].role == "system" and msgs[1].role == "user"
    assert "irrelevant" in msgs[0].content


# ---------------------------------------------------------------- syntax gold

def test_syntax_check_matches_body():
    assert syntax_check(r"(?i)\bIEASYSxx\b", "Add LFAREA to IEASYSxx.", None) is True


def test_syntax_check_matches_script():
    assert syntax_check(r"(?m)^\s*//", "Here is the JCL:", "//DD1 DD DSN=X") is True


def test_syntax_check_miss_is_false_not_error():
    assert syntax_check(r"\)REQ", "A plain prose answer.", None) is False


def test_syntax_check_invalid_pattern_fails_closed():
    with pytest.raises(JudgeError):
        syntax_check(r"([unclosed", "answer", None)


# ---------------------------------------------------------------- truncation capture

def _emit(handler: _AlertCapture, payload: str) -> None:
    record = logging.LogRecord("agent", logging.WARNING, "p", 1, payload, None, None)
    handler.emit(record)


def test_alert_capture_joins_request_ids():
    h = _AlertCapture()
    _emit(h, '{"request_id": "abc", "action": "answer_alert", "alert": "finish_reason_non_stop", "finish_reason": "length"}')
    _emit(h, '{"request_id": "def", "action": "answer", "citations": 2}')  # not an alert
    _emit(h, "not json at all")  # never crashes the handler
    assert h.alerts == {"abc": "length"}


# ---------------------------------------------------------------- apply_l2_measurements

def _entry(eid: str = "X", behavior: str = "answer", gold: list | None = None, pattern: str | None = None) -> dict:
    e: dict = {"id": eid, "query": "q", "query_class": "syntax", "expected_behavior": behavior}
    if gold is not None:
        e["expected_doc_ids"] = gold
    if pattern:
        e["syntax_pattern"] = pattern
    return e


def _llm_row(**kw) -> dict:
    row = {
        "id": "X", "query": "q", "query_class": "syntax", "expected_behavior": "answer",
        "verdict": "pass", "failures": [], "path": "llm", "citations": [],
        "request_id": "abc", "answer": "body", "script": None,
    }
    row.update(kw)
    return row


def test_apply_fail_closed_on_unmatched_citation():
    # A validated citation that does not map back to the fetched pool means
    # the P/R join is broken — the row must fail, not degrade quietly.
    row = _llm_row(citations=["[1] SA23-1380-70 z/OS MVS Init Tuning Ref, p. 100", "[2] GC34-6442-07 CICS, p. 9"])
    apply_l2_measurements(row, _entry(gold=["SA23-1380-70"]), HITS, alerts={})
    assert row["verdict"] == "fail"
    assert any("not in the fetched hit set" in f for f in row["failures"])
    assert row["citation_precision"] == 1.0  # mapped subset still recorded
    assert row["unmatched_citations"] == ["[2] GC34-6442-07 CICS, p. 9"]


def test_apply_all_citations_mapped_keeps_pass():
    row = _llm_row(citations=["[2] SC34-6428-08 CICS Sys Def, p. 55"])
    apply_l2_measurements(row, _entry(gold=["SC34-6428-08"]), HITS, alerts={})
    assert row["verdict"] == "pass"
    assert row["citation_precision"] == 1.0 and row["citation_recall"] == 1.0
    assert row["unmatched_citations"] == []


def test_apply_zero_hits_path_skips_unmatched_fail():
    # The canned zero-hits message has no citations and no model text; the
    # answer-tier verdict already fired — no join to break.
    row = _llm_row(path="zero_hits", citations=[], request_id="abc")
    apply_l2_measurements(row, _entry(gold=["SA23-1380-70"]), HITS, alerts={})
    assert row["verdict"] == "pass"


def test_apply_missing_request_id_fails_closed():
    # A silent truncated=false on a missing join key would undercount the
    # truncation story — the row must fail instead.
    row = _llm_row(request_id=None)
    apply_l2_measurements(row, _entry(gold=["SA23-1380-70"]), HITS, alerts={})
    assert row["truncated"] is None
    assert row["verdict"] == "fail"
    assert any("truncation unverifiable" in f for f in row["failures"])


def test_apply_truncation_join():
    row = _llm_row(request_id="abc")
    apply_l2_measurements(row, _entry(gold=["SA23-1380-70"]), HITS, alerts={"abc": "length"})
    assert row["truncated"] is True
    assert row["verdict"] == "pass"


def test_apply_syntax_miss_fails_row():
    row = _llm_row(answer="prose without the construct")
    apply_l2_measurements(row, _entry(gold=["SA23-1380-70"], pattern=r"\)REQ"), HITS, alerts={})
    assert row["syntax_ok"] is False
    assert row["verdict"] == "fail"
    assert any("syntax pattern missed" in f for f in row["failures"])


def test_apply_abstain_row_gets_no_precision():
    row = _llm_row(expected_behavior="abstain", query_class="negative", citations=[])
    apply_l2_measurements(row, _entry(behavior="abstain", gold=[]), HITS, alerts={})
    assert "citation_precision" not in row
    assert row["verdict"] == "pass"


# ---------------------------------------------------------------- summarize + gate

def _row(rid: str, **kw) -> dict:
    base = {
        "id": rid, "query": "q", "query_class": "syntax", "expected_behavior": "answer",
        "verdict": "pass", "failures": [], "path": "llm", "citations": ["[1] c"],
        "cited_doc_ids": ["D1"], "truncated": False, "syntax_ok": True,
    }
    base.update(kw)
    return base


def test_summarize_rates_and_structural_counts():
    rows = [
        _row("A", citation_precision=1.0, citation_recall=1.0, judge_label="entailed"),
        _row("B", verdict="fail", failures=["x"], judge_label="contradiction"),
        _row("C", path="zero_hits", citations=[]),
        _row("D", verdict="error", failures=["HTTP 502"]),
    ]
    m = summarize_l2(rows)
    assert m["structural_fails"] == 1
    assert m["errors"] == 1
    assert m["answer_llm_n"] == 2  # C is zero-hits, D is an error — both excluded
    assert m["grounded_rate"] == 1.0
    assert m["citation_precision"] == 1.0 and m["citation_recall"] == 1.0
    assert m["faithfulness"]["entailed"] == 0.5 and m["faithfulness"]["contradiction"] == 0.5
    assert m["syntax_compliance"] == 1.0


def test_summarize_empty_is_none_not_crash():
    m = summarize_l2([_row("A", path="zero_hits", citations=[])])
    assert m["grounded_rate"] is None
    assert m["citation_precision"] is None
    assert m["faithfulness"]["entailed"] is None
    assert m["syntax_compliance"] is None


def test_gate_passes_on_rates_and_holds_on_fails():
    good = summarize_l2([_row("A", judge_label="entailed")])
    assert gate_l2(good) == ("pass", [])
    bad = summarize_l2([_row("B", verdict="fail", failures=["syntax pattern missed: x"])])
    verdict, reasons = gate_l2(bad)
    assert verdict == "hold" and reasons
    judge_bad = summarize_l2([_row("C", judge_error="JudgeError: junk")])
    assert gate_l2(judge_bad)[0] == "hold"


def test_gate_holds_on_judge_error_alone():
    m = summarize_l2([_row("A", judge_error="JudgeError: unparseable")])
    # the judge failing closed must hold the gate even with no other failure
    assert m["faithfulness"]["judge_errors"] == 1
    verdict, reasons = gate_l2(m)
    assert verdict == "hold"
    assert any("judge" in r for r in reasons)


def test_summarize_relevance_block():
    m = summarize_l2([
        _row("A", relevance_label="relevant"),
        _row("B", relevance_label="partial"),
    ])
    assert m["relevance"]["judged"] == 2
    assert m["relevance"]["relevant"] == 0.5
    assert m["relevance"]["partial"] == 0.5
    assert m["relevance"]["irrelevant"] == 0.0


def test_summarize_grounded_rate_excludes_inferred_citations():
    # Issue #269: bracket-mapped cites are surfaced but never "grounded".
    m = summarize_l2([
        _row("A"),
        _row("B", citations=["[1] c"], citations_inferred=True),
    ])
    assert m["grounded_rate"] == 0.5
    assert m["inferred_citations"] == 1


def test_summarize_answer_completeness_over_gold_rows():
    # Issue #305: the gate is gold in the fetched pool; complete means the
    # validated citations cover every expected doc (recall 1.0). Rows without
    # the join stay out of the denominator.
    m = summarize_l2([
        _row("A", gold_retrieved=True, citation_recall=1.0),
        _row("B", gold_retrieved=True, citation_recall=0.5),
        _row("C", gold_retrieved=False, citation_recall=0.0),
        _row("D"),  # no pool join at all
    ])
    assert m["answer_completeness_n"] == 2
    assert m["answer_completeness"] == 0.5


def test_summarize_answer_completeness_none_without_pool_join():
    m = summarize_l2([_row("A")])
    assert m["answer_completeness_n"] == 0
    assert m["answer_completeness"] is None


def test_gate_holds_on_relevance_judge_error():
    m = summarize_l2([_row("A", relevance_error="JudgeError: unparseable")])
    verdict, reasons = gate_l2(m)
    assert verdict == "hold"
    assert any("relevance judge error" in r for r in reasons)


def test_apply_fails_vacuous_body_with_validated_citations():
    # Issue #192: a weak model emitting only a copied Citations: section
    # photographs healthy P/R over zero substance — the row must fail.
    row = _llm_row(answer="   ", citations=["[2] SC34-6428-08 CICS Sys Def, p. 55"])
    apply_l2_measurements(row, _entry(gold=["SC34-6428-08"]), HITS, alerts={})
    assert row["verdict"] == "fail"
    assert any("no substantive prose" in f for f in row["failures"])


def test_apply_index_markers_only_body_fails():
    row = _llm_row(answer="[1]", citations=["[2] SC34-6428-08 CICS Sys Def, p. 55"])
    apply_l2_measurements(row, _entry(gold=["SC34-6428-08"]), HITS, alerts={})
    assert row["verdict"] == "fail"
    assert any("no substantive prose" in f for f in row["failures"])


def test_apply_substantive_body_with_citations_passes():
    row = _llm_row(
        answer="Set the value in the member. [1]",
        citations=["[2] SC34-6428-08 CICS Sys Def, p. 55"],
    )
    apply_l2_measurements(row, _entry(gold=["SC34-6428-08"]), HITS, alerts={})
    assert row["verdict"] == "pass"
    assert not any("no substantive prose" in f for f in row["failures"])


def test_apply_vacuous_body_without_citations_not_floored():
    # No cites: honest abstentions stay judged by the refusal verdict,
    # never by the content floor.
    row = _llm_row(answer="   ", citations=[])
    apply_l2_measurements(row, _entry(gold=["SC34-6428-08"]), HITS, alerts={})
    assert not any("no substantive prose" in f for f in row["failures"])


def test_apply_script_answer_exempt_from_floor():
    # Code carries the substance for syntax entries.
    row = _llm_row(
        answer="   ", script="//SYSIN DD *",
        citations=["[2] SC34-6428-08 CICS Sys Def, p. 55"],
    )
    apply_l2_measurements(row, _entry(gold=["SC34-6428-08"]), HITS, alerts={})
    assert not any("no substantive prose" in f for f in row["failures"])


def test_apply_abstain_entry_exempt_from_floor():
    row = _llm_row(
        expected_behavior="abstain",
        answer="   ", citations=["[2] SC34-6428-08 CICS Sys Def, p. 55"],
    )
    apply_l2_measurements(
        row, _entry(behavior="abstain", gold=["SC34-6428-08"]), HITS, alerts={}
    )
    assert not any("no substantive prose" in f for f in row["failures"])


# --------------------------------------------- issue #298 by-complexity slice
def test_by_complexity_truncation_groups_unknown_last():
    def rate(n, d):
        return round(n / d, 4) if d else None

    rows = [
        _row("A", query_complexity="simple", truncated=False),
        _row("B", query_complexity="simple", truncated=True),
        _row("C", query_complexity="complex", truncated=True),
        _row("D", truncated=False),  # no join -> unknown
    ]
    assert _by_complexity_truncation(rows, rate) == {
        "complex": {"n": 1, "truncated": 1, "truncation_rate": 1.0},
        "simple": {"n": 2, "truncated": 1, "truncation_rate": 0.5},
        "unknown": {"n": 1, "truncated": 0, "truncation_rate": 0.0},
    }


def test_summarize_carries_by_complexity_slice():
    rows = [
        _row("A", query_complexity="complex", truncated=True),
        _row("B", query_complexity="complex", truncated=False),
    ]
    m = summarize_l2(rows)
    assert m["by_complexity"]["complex"] == {"n": 2, "truncated": 1, "truncation_rate": 0.5}


# --------------------------------------------- issue #299 WHY aggregations
def test_summarize_by_class_pr_means():
    from scripts.harness_l2 import _by_class_pr
    rows = [
        _row("A", query_class="syntax", citation_precision=1.0, citation_recall=0.5),
        _row("B", query_class="syntax", citation_precision=0.5, citation_recall=0.5),
        _row("C", query_class="negative"),
    ]
    by_class = _by_class_pr(rows)
    assert by_class["syntax"] == {"n": 2, "citation_precision": 0.75, "citation_recall": 0.5}
    assert by_class["negative"] == {"n": 1, "citation_precision": None, "citation_recall": None}


def test_summarize_by_failure_histogram():
    from scripts.harness_l2 import _by_failure_histogram
    rows = [
        _row("A", verdict="fail", failures=["zero validated citations"]),
        _row("B", verdict="fail", failures=["zero validated citations"]),
        _row("C", verdict="pass"),
    ]
    assert _by_failure_histogram(rows) == {"zero validated citations": 2}


def test_summarize_carries_why_slices():
    rows = [_row("A", query_class="syntax", citation_precision=1.0, citation_recall=1.0,
                 verdict="fail", failures=["zero validated citations"])]
    m = summarize_l2(rows)
    assert m["by_class"]["syntax"]["n"] == 1
    assert m["by_failure"] == {"zero validated citations": 1}


def test_summarize_by_why_modes_and_off_gold():
    from scripts.harness_l2 import _by_why
    rows = [
        _row("A"),
        _row("B", citations=[], truncated=True, citations_header_present=False),
        _row("C", citations=[], cites_rejected_shape_bad=1),
        _row("D", citations=["[1] c"], citations_inferred=True, inferred_indices=[1],
             expected_doc_ids=["D1"], hit_doc_ids=["D1"]),
        # Truncated with no capture signal: unknown, never 'before cites'.
        _row("E", citations=[], truncated=True),
    ]
    assert _by_why(rows) == {
        "cited_explicit": 1, "cited_inferred": 1,
        "truncated_before_cites": 1, "malformed_shape_bad": 1, "unknown": 1,
    }
    m = summarize_l2(rows)
    assert m["by_why"]["cited_explicit"] == 1
    assert m["inferred_index_off_gold"] == 0
