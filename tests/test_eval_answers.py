"""Unit tests for the answer-tier eval helpers (scripts/eval_answers.py):
verdict logic (judge), deterministic stratified sampling (select_sample),
and aggregation (summarize).

Hermetic: no Qdrant, no vLLM, no TestClient — the pure helpers are imported
directly. The live tier runs via `make eval-answers` (like
scripts/test_local_e2e_vllm.py, never part of plain pytest)."""

from __future__ import annotations

from scripts.eval_answers import (
    ZERO_HITS_ANSWER,
    is_explicit_refusal,
    judge,
    select_sample,
    summarize,
)


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


# ---------------------------------------------------------------- judge: answer
def test_answer_grounded_passes() -> None:
    verdict, fails, warns = judge(_entry(), "LFAREA is set in IEASYSxx.", ["SA23-1380-70 ref, p. 1"])
    assert verdict == "pass"
    assert fails == [] and warns == []


def test_answer_zero_citations_fails() -> None:
    verdict, fails, _ = judge(_entry(), "LFAREA is set in IEASYSxx.", [])
    assert verdict == "fail"
    assert any("zero validated citations" in f for f in fails)


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
    assert not is_explicit_refusal("LFAREA reserves 64-bit frames above the bar.")


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
