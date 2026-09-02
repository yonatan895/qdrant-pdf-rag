"""Unit tests for harness L2 (answer tier) — pure functions, no stack.

The contract under test: structural fails gate, rates are trend data, the
citation validator stays the single source of truth (L2 maps citations to
hits by exact cite-string match, never re-parses model text), and the
judge must fail closed on unparseable output.
"""

import logging

import pytest
from scripts.harness_l2 import (
    JUDGE_MAX_EVIDENCE_CHARS,
    JudgeError,
    _AlertCapture,
    citation_to_hit,
    cited_doc_ids,
    evidence_for_citations,
    gate_l2,
    judge_messages,
    parse_judge_label,
    precision_recall,
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
    # evidence text and the answer body.
    msgs = judge_messages("Use LFAREA=(1M) per the reference.", "LFAREA=(1M) syntax...")
    assert all("[" not in m.content or "EXCERPTS" in m.content for m in msgs)
    assert msgs[0].role == "system" and msgs[1].role == "user"
    assert "contradiction" in msgs[0].content


def test_evidence_bounded_for_judge_prompt():
    hits = [_hit(f"c{i}", f"D{i}", "x" * 4000) for i in range(4)]
    evidence, unmapped = evidence_for_citations([f"[{i}] c{i}" for i in range(4)], hits)
    assert len(evidence) <= JUDGE_MAX_EVIDENCE_CHARS + 40
    assert unmapped == []


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
