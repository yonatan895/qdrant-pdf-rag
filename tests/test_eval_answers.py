"""Unit tests for the answer-tier eval helpers (scripts/eval_answers.py):
verdict logic (judge), deterministic stratified sampling (select_sample),
and aggregation (summarize).

Hermetic: no Qdrant, no vLLM, no TestClient — the pure helpers are imported
directly. The live tier runs via `make eval-answers` (like
scripts/test_local_e2e_vllm.py, never part of plain pytest)."""

from __future__ import annotations

import json
import logging

from scripts.eval_answers import (
    ZERO_HITS_ANSWER,
    _AnswerCapture,
    is_explicit_refusal,
    is_zero_hits_answer,
    judge,
    run_query,
    select_sample,
    summarize,
)

from mainframe_rag.agent.answer import is_refusal


def _entry(**overrides) -> dict:
    base = {
        "id": "MSG-01",
        "query": "What does IEA500I report?",
        "query_class": "message_id",
        "expected_behavior": "answer",
        "expected_doc_ids": [],
    }
    base.update(overrides)
    return base


# ----------------------------------------------------------------- refusal helper
def test_refusal_helper_shared_with_agent() -> None:
    """Issue #135: the eval's refusal verdicts and the agent's zero-citation
    rule must be the same predicate — one helper, one marker list."""
    assert is_explicit_refusal is is_refusal


def test_refusal_marker_battery() -> None:
    """Adversarial battery phrasings must fire the refusal predicate:
    the battery's 'no information regarding' refusal shipped 4 citations
    of real-but-unsupporting chunks because no marker caught it."""
    assert is_refusal("No information regarding private keys is available in the excerpts.")
    assert is_refusal("There is no information about that parameter in the manuals provided.")
    assert is_refusal("The excerpts do not answer this question.")
    assert is_refusal("no manual excerpts carry dsn90221i")  # case-folded
    assert is_refusal("The EXCERPTS DO NOT CONTAIN the requested syntax.")
    assert is_refusal("**Not documented in the excerpts.**")  # wrapped markup
    assert not is_refusal("LFAREA reserves 64-bit frames above the bar.")
    assert not is_refusal("")


# ---------------------------------------------------------------- judge: answer
def test_answer_grounded_passes() -> None:
    verdict, fails, warns = judge(_entry(), "LFAREA is set in IEASYSxx.", ["SA23-1380-70 ref, p. 1"])
    assert verdict == "pass"
    assert fails == [] and warns == []


def test_answer_zero_citations_fails() -> None:
    verdict, fails, _ = judge(_entry(), "LFAREA is set in IEASYSxx.", [])
    assert verdict == "fail"
    assert any("zero validated citations" in f for f in fails)


def test_answer_inferred_citations_fail_grounding() -> None:
    # Issue #269: cites mapped from bare bracket markers with no explicit
    # citation line are surfaced as citations_inferred — never grounded.
    verdict, fails, _ = judge(
        _entry(), "LFAREA is set in IEASYSxx [1].",
        ["SA23-1380-70 ref, p. 1"], citations_inferred=True,
    )
    assert verdict == "fail"
    assert any("only inferred citations" in f for f in fails)


def test_answer_explicit_citations_still_pass() -> None:
    verdict, fails, _ = judge(
        _entry(), "LFAREA is set in IEASYSxx.",
        ["SA23-1380-70 ref, p. 1"], citations_inferred=False,
    )
    assert verdict == "pass"
    assert fails == []


def test_answer_refusal_fails() -> None:
    verdict, fails, _ = judge(_entry(), "The excerpts do not cover LFAREA.", ["SA23-1380-70 ref, p. 1"])
    assert verdict == "fail"
    assert any("explicit refusal" in f for f in fails)


def test_answer_zero_citations_with_refusal_fails_once() -> None:
    # refusal already explains the zero citations: no duplicate zero-cite failure
    _, fails, _ = judge(_entry(), ZERO_HITS_ANSWER, [])
    assert any("explicit refusal" in f for f in fails)
    assert not any("zero validated citations" in f for f in fails)


def test_answer_empty_body_fails() -> None:
    verdict, fails, _ = judge(_entry(), "   ", ["SA23-1380-70 ref, p. 1"])
    assert verdict == "fail"
    assert any("empty answer body" in f for f in fails)


# --------------------------------------------------------------- judge: abstain
def test_abstain_zero_citations_clean_passes() -> None:
    verdict, fails, warns = judge(_entry(expected_behavior="abstain"), ZERO_HITS_ANSWER, [])
    assert verdict == "pass"
    assert fails == [] and warns == []


def test_abstain_silent_warns_but_passes() -> None:
    verdict, fails, warns = judge(_entry(expected_behavior="abstain"), "I cannot help with that.", [])
    assert verdict == "pass"
    assert fails == []
    assert any("silent abstention" in w for w in warns)


def test_abstain_grounded_without_refusal_fails() -> None:
    verdict, fails, _ = judge(
        _entry(expected_behavior="abstain"),
        "IEA500I reports a synthetic failure.",
        ["SA38-0673-70 ref, p. 2"],
    )
    assert verdict == "fail"
    assert any("trap answered" in f for f in fails)


def test_abstain_hedged_citation_warns_but_passes() -> None:
    verdict, fails, warns = judge(
        _entry(expected_behavior="abstain"),
        "The excerpts do not answer this; [1] only covers IEA501I.",
        ["SA38-0673-70 ref, p. 2"],
    )
    assert verdict == "pass"
    assert fails == []
    assert any("hedged abstention" in w for w in warns)


# ------------------------------------------------------- judge: gold substrings
def test_gold_must_contain_enforced() -> None:
    verdict, fails, _ = judge(
        _entry(gold_must_contain=["JES2", "JES3"]),
        "JES2 does X.",
        ["SA32-0990-02 ref, p. 3"],
    )
    assert verdict == "fail"
    assert any("JES3" in f for f in fails)


def test_gold_must_contain_casefolded() -> None:
    verdict, _, _ = judge(_entry(gold_must_contain=["jes2"]), "JES2 does X.", ["c1"])
    assert verdict == "pass"


def test_gold_must_not_contain_enforced() -> None:
    verdict, fails, _ = judge(_entry(gold_must_not_contain=["SETROPTS NO"]), "Run SETROPTS NO...", ["c1"])
    assert verdict == "fail"
    assert any("forbidden substring" in f for f in fails)


def test_abstain_gold_phrase_pin() -> None:
    # Seed semantics: NEG-01 demands the literal refusal phrase; the agent's
    # fixed zero-hits wording does not satisfy it (deliberate strictness).
    verdict, fails, _ = judge(
        _entry(id="NEG-01", expected_behavior="abstain", gold_must_contain=["excerpts do not answer"]),
        ZERO_HITS_ANSWER,
        [],
    )
    assert verdict == "fail"
    assert any("excerpts do not answer" in f for f in fails)


def test_zero_hits_path_skips_gold_checks() -> None:
    # run_query suppresses gold checks on the canned zero-hits message (no
    # model text to judge); the structural abstain verdict still passes.
    entry = _entry(
        id="MSG-01",
        expected_behavior="abstain",
        gold_must_contain=["IEA500I"],
        must_cite_identifier="IEA500I",
    )
    verdict, fails, _ = judge(entry, ZERO_HITS_ANSWER, [], judge_gold=False)
    assert verdict == "pass"
    assert fails == []


def test_zero_hits_answer_entry_still_fails_structurally() -> None:
    # The gold suppression must not mask the retrieval gap: an answer-tier
    # query refused by the canned zero-hits message is still a FAIL.
    verdict, fails, _ = judge(
        _entry(id="DOC-01", gold_must_contain=["SA23-1380"]), ZERO_HITS_ANSWER, [], judge_gold=False
    )
    assert verdict == "fail"
    assert any("explicit refusal" in f for f in fails)


# ------------------------------------------------- judge: must_cite_identifier
def test_must_cite_identifier_in_citations() -> None:
    verdict, _, _ = judge(_entry(must_cite_identifier="IEA794I"), "See the message text.", ["SA38-0673-70, IEA794I, p. 5"])
    assert verdict == "pass"


def test_must_cite_identifier_in_body() -> None:
    verdict, fails, _ = judge(_entry(must_cite_identifier="IEA794I"), "IEA794I reports GRS state.", [])
    # zero citations still fail structurally; the identifier check adds nothing
    assert verdict == "fail"
    assert any("zero validated citations" in f for f in fails)
    assert not any("identifier" in f for f in fails)


def test_must_cite_identifier_absent_fails() -> None:
    verdict, fails, _ = judge(_entry(must_cite_identifier="SMFPRMxx"), "Something else entirely.", ["c1"])
    assert verdict == "fail"
    assert any("SMFPRMxx" in f for f in fails)


# ----------------------------------------------------------------- is_explicit_refusal
def test_refusal_markers() -> None:
    assert is_explicit_refusal(ZERO_HITS_ANSWER)
    assert is_explicit_refusal("The excerpts do not answer this question.")
    assert is_explicit_refusal("No manual excerpts carry DSN90221I.")
    assert not is_explicit_refusal("LFAREA reserves 64-bit frames above the bar.")


def test_is_zero_hits_answer_recognizes_both_canned_forms() -> None:
    assert is_zero_hits_answer(ZERO_HITS_ANSWER)
    assert is_zero_hits_answer("No manual excerpts carry DSN90221I.")
    assert is_zero_hits_answer("No manual excerpts carry DSN9000I, DSN9001I, +5 more.")
    assert not is_zero_hits_answer("No manual excerpts carry")
    assert not is_zero_hits_answer("The model wrote its own refusal here.")


# ------------------------------------------------------------------ select_sample
def test_select_sample_covers_every_class_deterministically() -> None:
    entries = [
        _entry(id=f"MSG-{i:02d}", query_class="message_id", query=f"q msg {i}") for i in range(10)
    ] + [
        _entry(id=f"NEG-{i:02d}", query_class="negative", query=f"q neg {i}") for i in range(6)
    ] + [
        _entry(id=f"SYN-{i:02d}", query_class="syntax", query=f"q syn {i}") for i in range(4)
    ]
    sample = select_sample(entries, 8)
    classes = {e["query_class"] for e in sample}
    assert classes == {"message_id", "negative", "syntax"}
    again = select_sample(entries, 8)
    assert [e["id"] for e in sample] == [e["id"] for e in again]  # no RNG


def test_select_sample_small_class_fully_drained_first() -> None:
    entries = [
        _entry(id=f"MSG-{i:02d}", query_class="message_id", query=f"q {i}") for i in range(9)
    ] + [_entry(id="VER-01", query_class="version", query="q v")]
    sample = select_sample(entries, 5)
    assert sum(1 for e in sample if e["query_class"] == "version") == 1
    assert len(sample) == 5


def test_select_sample_cap_above_total_returns_all() -> None:
    entries = [_entry(id="MSG-01"), _entry(id="NEG-01", query_class="negative", query="q")]
    assert len(select_sample(entries, 50)) == 2


def test_select_sample_zero_cap() -> None:
    assert select_sample([_entry()], 0) == []


# --------------------------------------------------------------------- summarize
def test_summarize_rates_and_counts() -> None:
    results = [
        {"verdict": "pass", "expected_behavior": "answer", "query_class": "message_id", "citations": ["a"]},
        {"verdict": "fail", "expected_behavior": "answer", "query_class": "message_id", "citations": [], "warns": []},
        {"verdict": "pass", "expected_behavior": "abstain", "query_class": "negative", "citations": [], "warns": ["w"]},
        {"verdict": "error", "expected_behavior": "answer", "query_class": "syntax", "failures": ["HTTP 502 (upstream_error)"]},
    ]
    m = summarize(results)
    assert m["queries"] == 4
    assert m["judged"] == 3
    assert m["errors"] == 1
    assert m["answer_n"] == 2 and m["abstain_n"] == 1
    assert m["answer_pass_rate"] == 0.5
    assert m["abstain_pass_rate"] == 1.0
    assert m["failures"] == 1 and m["warns"] == 1
    assert m["citations_per_answer"] == 0.5
    assert m["by_class"]["message_id"] == {"n": 2, "pass": 1}


def test_summarize_empty() -> None:
    m = summarize([])
    assert m["queries"] == 0 and m["answer_pass_rate"] is None and m["citations_per_answer"] is None


def test_summarize_counts_inferred_rows() -> None:
    results = [
        {"verdict": "fail", "expected_behavior": "answer", "query_class": "syntax",
         "citations": ["c1"], "citations_inferred": True},
        {"verdict": "pass", "expected_behavior": "answer", "query_class": "syntax",
         "citations": ["c1"], "citations_inferred": False},
    ]
    assert summarize(results)["inferred_citations"] == 1


# ------------------------------------------------- issue #298 attribution fields
def _answer_log_record(**payload) -> logging.LogRecord:
    base = {"request_id": "r1", "action": "answer"}
    base.update(payload)
    return logging.LogRecord("agent", logging.INFO, __file__, 0, json.dumps(base), None, None)


def test_answer_capture_keeps_answer_lines_only() -> None:
    cap = _AnswerCapture()
    cap.emit(_answer_log_record(query_complexity="complex", finish_reason="length",
                                prompt_tokens=2500, completion_tokens=1500,
                                reasoning_tokens=1100, total_tokens=4000,
                                inline_bracket_present=False,
                                citations_header_present=True,
                                cites_rejected_shape_bad=1,
                                cites_rejected_unmapped=2))
    cap.emit(_answer_log_record(action="answer_alert", alert="finish_reason_non_stop"))
    cap.emit(logging.LogRecord("agent", logging.INFO, __file__, 0, "not json", None, None))
    cap.emit(_answer_log_record(request_id="", query_complexity="simple"))
    assert cap.signals == {"r1": {"query_complexity": "complex", "finish_reason": "length",
                                  "prompt_tokens": 2500, "completion_tokens": 1500,
                                  "reasoning_tokens": 1100, "total_tokens": 4000,
                                  "inline_bracket_present": False,
                                  "citations_header_present": True,
                                  "cites_rejected_shape_bad": 1,
                                  "cites_rejected_unmapped": 2}}


class _StubClient:
    def __init__(self, payload: dict | Exception):
        self.payload = payload

    def post(self, *args, **kwargs):
        if isinstance(self.payload, Exception):
            raise self.payload
        return _StubResponse(self.payload)


class _StubResponse:
    def __init__(self, payload: dict):
        self.payload = payload
        self.status_code = 200

    def json(self) -> dict:
        return self.payload


def _answer_payload(**overrides) -> dict:
    base = {
        "request_id": "r1",
        "answer": "LFAREA reserves frames above the bar.\nCitations:\nSA23-1380-70 ref, p. 1",
        "citations": ["SA23-1380-70 ref, p. 1"],
        "citations_inferred": False,
        "inferred_indices": [],
        "script": None,
    }
    base.update(overrides)
    return base


def test_run_query_joins_answer_signals() -> None:
    signals = {"r1": {"query_complexity": "simple", "finish_reason": "stop",
                       "prompt_tokens": 900, "completion_tokens": 120,
                       "reasoning_tokens": 60, "total_tokens": 1020,
                       "inline_bracket_present": True,
                       "citations_header_present": True,
                       "cites_rejected_shape_bad": 1,
                       "cites_rejected_unmapped": 2}}
    row = run_query(_StubClient(_answer_payload()), _entry(), signals)
    assert row["verdict"] == "pass"
    assert row["query_complexity"] == "simple"
    assert row["finish_reason"] == "stop"
    assert (row["prompt_tokens"], row["completion_tokens"],
            row["reasoning_tokens"], row["total_tokens"]) == (900, 120, 60, 1020)
    assert row["citations_header_present"] is True
    assert row["inline_bracket_present"] is True
    assert (row["cites_rejected_shape_bad"], row["cites_rejected_unmapped"]) == (1, 2)
    assert row["inferred_indices"] == []


def test_run_query_without_signals_leaves_nones() -> None:
    row = run_query(_StubClient(_answer_payload(answer="Plain prose, no header.")), _entry())
    assert row["query_complexity"] is None
    assert row["finish_reason"] is None
    assert row["prompt_tokens"] is None
    # Parse-time signal (issue #299): the returned body never carries the
    # header, so without the log join the field is unknown, not False.
    assert row["citations_header_present"] is None
    assert row["inline_bracket_present"] is None
    assert row["cites_rejected_shape_bad"] is None
    assert row["cites_rejected_unmapped"] is None


def test_run_query_error_row_has_no_signal_fields() -> None:
    row = run_query(_StubClient(ConnectionError("down")), _entry())
    assert row["verdict"] == "error"
    assert row.get("query_complexity") is None
    assert row.get("citations_header_present") is None


def test_summarize_by_complexity_counts_pass() -> None:
    results = [
        {"verdict": "pass", "expected_behavior": "answer", "query_class": "syntax",
         "query_complexity": "simple"},
        {"verdict": "fail", "expected_behavior": "answer", "query_class": "syntax",
         "query_complexity": "simple", "failures": ["x"]},
        {"verdict": "pass", "expected_behavior": "answer", "query_class": "message_id",
         "query_complexity": "complex"},
        {"verdict": "fail", "expected_behavior": "answer", "query_class": "message_id",
         "failures": ["x"]},
    ]
    by_complexity = summarize(results)["by_complexity"]
    assert by_complexity["simple"] == {"n": 2, "pass": 1}
    assert by_complexity["complex"] == {"n": 1, "pass": 1}
    # error rows never reach the judged set; a judged row without the join
    # lands in unknown rather than a third complexity.
    assert by_complexity["unknown"] == {"n": 1, "pass": 0}


# ------------------------------------------------- issue #299 WHY taxonomy
def test_failure_bucket_redacts_quoted_substrings() -> None:
    from scripts.eval_answers import failure_bucket
    assert failure_bucket("trap answered: 3 validated citation(s)") == "trap answered"
    assert failure_bucket("trap answered: 5 validated citation(s)") == "trap answered"
    assert failure_bucket("missing required substring: 'LFAREA 1M'") == "missing required substring"
    assert failure_bucket("zero validated citations") == "zero validated citations"
    assert (
        failure_bucket("only inferred citations (no explicit Citations: block)")
        == "only inferred citations …"
    )
    assert (
        failure_bucket("3 validated citation(s) not in the fetched hit set: ['c1', 'c2']")
        == "N validated citation(s) not in the fetched hit set"
    )


def test_run_query_carries_gold_and_pool_join() -> None:
    hits = [{"doc_id": "SA23-1380-09", "cite": "c1"}, {"doc_id": "OTHER-01", "cite": "c2"}]
    row = run_query(_StubClient(_answer_payload()), _entry(expected_doc_ids=["SA23-1380-09"]), None, hits)
    assert row["expected_doc_ids"] == ["SA23-1380-09"]
    assert row["hit_doc_ids"] == ["SA23-1380-09", "OTHER-01"]
    assert row["gold_retrieved"] is True
    assert row["abstention_zeroed"] is False


def test_run_query_gold_absent_from_pool_is_reader_blame_shape() -> None:
    hits = [{"doc_id": "OTHER-01", "cite": "c2"}]
    row = run_query(_StubClient(_answer_payload()), _entry(expected_doc_ids=["SA23-1380-09"]), None, hits)
    assert row["gold_retrieved"] is False


def test_run_query_without_pool_leaves_join_nones() -> None:
    row = run_query(_StubClient(_answer_payload()), _entry(expected_doc_ids=["SA23-1380-09"]))
    assert row["hit_doc_ids"] is None
    assert row["gold_retrieved"] is None
    assert row["expected_doc_ids"] == ["SA23-1380-09"]


def test_run_query_flags_cited_then_zeroed_abstention() -> None:
    body = "The excerpts do not contain this information."
    row = run_query(_StubClient(_answer_payload(answer=body, citations=[])), _entry())
    assert row["abstention_zeroed"] is True


def test_summarize_by_failure_histogram() -> None:
    results = [
        {"verdict": "fail", "expected_behavior": "answer", "query_class": "syntax",
         "failures": ["zero validated citations"]},
        {"verdict": "fail", "expected_behavior": "answer", "query_class": "syntax",
         "failures": ["trap answered: 3 validated citation(s)"]},
        {"verdict": "fail", "expected_behavior": "answer", "query_class": "syntax",
         "failures": ["trap answered: 1 validated citation(s)"]},
        {"verdict": "pass", "expected_behavior": "answer", "query_class": "syntax"},
    ]
    assert summarize(results)["by_failure"] == {
        "trap answered": 2, "zero validated citations": 1,
    }


# --------------------------------------------- issue #299 citation WHY modes
def test_why_mode_precedence_and_branches() -> None:
    """Every mode fires on its claimed shape; most-specific wins."""
    from scripts.eval_answers import why_mode

    assert why_mode({"verdict": "error"}) == "error"
    assert why_mode({"verdict": "pass", "path": "zero_hits"}) == "zero_hits"
    assert why_mode({"verdict": "pass", "abstention_zeroed": True}) == "abstention_zeroed"
    assert why_mode({"verdict": "pass", "citations": ["c"]}) == "cited_explicit"
    assert why_mode({"verdict": "pass", "citations": ["c"], "citations_inferred": True}) == "cited_inferred"
    # Missing header signal on a truncated row never asserts the cut shape.
    assert why_mode({"verdict": "fail", "truncated": True}) == "unknown"
    assert why_mode({"verdict": "fail", "truncated": True,
                     "citations_header_present": False,
                     "inline_bracket_present": False,
                     "cites_rejected_shape_bad": 0,
                     "cites_rejected_unmapped": 0}) == "truncated_before_cites"
    # A truncated row whose header survived is not cut before cites.
    assert why_mode({"verdict": "fail", "truncated": True,
                     "citations_header_present": True,
                     "cites_rejected_unmapped": 1}) == "fabricated_unmapped"
    assert why_mode({"verdict": "fail", "cites_rejected_unmapped": 1,
                     "cites_rejected_shape_bad": 2}) == "fabricated_unmapped"
    assert why_mode({"verdict": "fail", "cites_rejected_shape_bad": 1}) == "malformed_shape_bad"
    assert why_mode({"verdict": "fail", "inline_bracket_present": True}) == "bracket_unmatched"
    # Missing attempt signals: 'absent' is a claim, so the row reads unknown.
    assert why_mode({"verdict": "fail"}) == "unknown"
    # Every attempt signal present and negative: genuinely absent.
    assert why_mode({"verdict": "fail", "inline_bracket_present": False,
                     "cites_rejected_shape_bad": 0,
                     "cites_rejected_unmapped": 0}) == "absent"


def test_inferred_index_off_gold_maps_indices_to_pool_order() -> None:
    from scripts.eval_answers import inferred_index_off_gold

    gold_pool = {"verdict": "pass", "inferred_indices": [2],
                 "expected_doc_ids": ["SA23-1380-09"],
                 "hit_doc_ids": ["OTHER-01", "SA23-1380-09"]}
    assert inferred_index_off_gold(gold_pool) is False
    off = {"verdict": "pass", "inferred_indices": [1],
           "expected_doc_ids": ["SA23-1380-09"],
           "hit_doc_ids": ["OTHER-01", "SA23-1380-09"]}
    assert inferred_index_off_gold(off) is True
    out_of_range = {"verdict": "pass", "inferred_indices": [9],
                    "expected_doc_ids": ["SA23-1380-09"],
                    "hit_doc_ids": ["SA23-1380-09"]}
    assert inferred_index_off_gold(out_of_range) is True
    # Missing or uncoercible inputs never fabricate a verdict.
    assert inferred_index_off_gold({"verdict": "pass"}) is False
    assert inferred_index_off_gold(
        {"verdict": "pass", "inferred_indices": [1], "hit_doc_ids": ["SA23-1380-09"]}
    ) is False
    assert inferred_index_off_gold(
        {"verdict": "pass", "inferred_indices": ["x"],
         "expected_doc_ids": ["SA23-1380-09"], "hit_doc_ids": ["SA23-1380-09"]}
    ) is False


def test_run_query_malformed_inferred_indices_is_an_error_row() -> None:
    """A malformed provenance field fails the row closed instead of raising
    through the runner (issue #299 review)."""
    row = run_query(_StubClient(_answer_payload(inferred_indices=["not-a-number"])), _entry())
    assert row["verdict"] == "error"
    assert row["failures"] == ["malformed inferred_indices in response"]


def test_summarize_by_why_and_off_gold() -> None:
    results = [
        {"verdict": "pass", "expected_behavior": "answer", "query_class": "syntax",
         "citations": ["c1"]},
        {"verdict": "pass", "expected_behavior": "answer", "query_class": "syntax",
         "citations": ["c1"], "citations_inferred": True, "inferred_indices": [1],
         "expected_doc_ids": ["DOC-1"], "hit_doc_ids": ["OTHER", "DOC-1"]},
        {"verdict": "fail", "expected_behavior": "answer", "query_class": "syntax",
         "citations": [], "cites_rejected_unmapped": 2},
        {"verdict": "fail", "expected_behavior": "answer", "query_class": "syntax",
         "citations": [], "cites_rejected_shape_bad": 1},
    ]
    metrics = summarize(results)
    assert metrics["by_why"] == {
        "cited_explicit": 1, "cited_inferred": 1,
        "fabricated_unmapped": 1, "malformed_shape_bad": 1,
    }
    assert metrics["inferred_index_off_gold"] == 1
