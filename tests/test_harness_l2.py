"""Unit tests for harness L2 (answer tier) — pure functions, no stack.

The contract under test: structural fails gate, rates are trend data, the
citation validator stays the single source of truth (L2 maps citations to
hits by exact cite-string match, never re-parses model text), and the
judge must fail closed on unparseable output.
"""

import logging
import re

import pytest

from mainframe_rag.eval.answer_tier import (
    _AlertCapture,
    apply_l2_measurements,
    gate_l2,
    summarize_l2,
    syntax_check,
)
from mainframe_rag.eval.judging import (
    JUDGE_REASONING_EFFORT,
    JudgeError,
    cited_doc_ids,
    judge_chat,
    judge_messages,
    parse_judge_label,
    parse_relevance_label,
    precision_recall,
)


def _hit(cite: str, doc_id: str, text: str = "LFAREA=(1M)") -> dict:
    return {"cite": cite, "doc_id": doc_id, "text": text}


HITS = [
    _hit("SA23-1380-70 z/OS MVS Init Tuning Ref, p. 100", "SA23-1380-70"),
    _hit("SC34-6428-08 CICS Sys Def, p. 55", "SC34-6428-08"),
]


# ---------------------------------------------------------------- citation mapping

def test_cited_doc_ids_maps_and_records_unmatched():
    docs, unmatched = cited_doc_ids(
        ["[1] SA23-1380-70 z/OS MVS Init Tuning Ref, p. 100",
         "[3] GC34-6442-07 CICS Messages, p. 116"],
        HITS,
    )
    assert docs == {"SA23-1380-70"}
    assert unmatched == ["[3] GC34-6442-07 CICS Messages, p. 116"]


# ---------------------------------------------------------------- precision / recall

def test_precision_recall_semantics():
    p, r = precision_recall({"A", "X", "Y"}, {"A", "B"})
    assert p == pytest.approx(1 / 3) and r == 0.5
    # Zero validated citations on an answer row is a recall miss, not a
    # denominator gap — the row must pull the average down.
    assert precision_recall(set(), {"A"}) == (None, 0.0)
    # Abstain rows carry no gold and stay out of the denominators.
    assert precision_recall({"A"}, set()) == (None, None)


# ---------------------------------------------------------------- judge

def test_run_l2_stays_on_server_default_temperature(monkeypatch):
    """Issue #596: run_l2 must send temperature=None (server default) — its
    thresholds were recorded under that sampling, so silently inheriting the
    answer eval's temp-0 default would compare temp-0 means against temp-0.2
    references. Moving L2/L4 to 0 needs its own threshold re-record PR."""
    import fastapi.testclient

    from mainframe_rag.eval import answer_tier

    sent: list[object] = []

    def fake_run_query(client, entry, *args, **kwargs):
        sent.append(kwargs.get("temperature", "MISSING"))
        return {
            "id": entry["id"],
            "query": entry["query"],
            "query_class": entry["query_class"],
            "expected_behavior": entry["expected_behavior"],
            "verdict": "pass",
            "failures": [],
            "warns": [],
            "answer": "LFAREA is set in IEASYSxx.",
            "citations": [],
            "citations_inferred": False,
            "request_id": "r1",
            "path": "llm",
        }

    class _SearchClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, path, json=None):
            assert path == "/v1/search"

            class _Resp:
                status_code = 200

                @staticmethod
                def json():
                    return {"hits": []}

            return _Resp()

    monkeypatch.setattr(answer_tier, "run_query", fake_run_query)
    monkeypatch.setattr(fastapi.testclient, "TestClient", _SearchClient)
    entry = {
        "id": "CMP-01",
        "query": "What does IEA500I mean?",
        "query_class": "message_id",
        "expected_behavior": "answer",
        "expected_doc_ids": [],
    }
    results, _ = answer_tier.run_l2([entry], 1, judge_enabled=False)
    assert [r["verdict"] for r in results] == ["pass"]
    assert sent == [None]


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


# ---------------------------------------------------------------- relevance judge

# ---------------------------------------------------------------- syntax gold

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


def _llm_row(rid: str = "X", **kw) -> dict:
    """One L2 row: the fields apply_l2_measurements writes plus the summary's."""
    row = {
        "id": rid, "query": "q", "query_class": "syntax", "expected_behavior": "answer",
        "verdict": "pass", "failures": [], "path": "llm", "citations": [],
        "request_id": "abc", "answer": "body", "script": None,
        "cited_doc_ids": ["D1"], "truncated": False, "syntax_ok": True,
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


def test_apply_missing_request_id_fails_closed():
    # A silent truncated=false on a missing join key would undercount the
    # truncation story — the row must fail instead.
    row = _llm_row(request_id=None)
    apply_l2_measurements(row, _entry(gold=["SA23-1380-70"]), HITS, alerts={})
    assert row["truncated"] is None
    assert row["verdict"] == "fail"
    assert any("truncation unverifiable" in f for f in row["failures"])


def test_apply_syntax_miss_fails_row():
    row = _llm_row(answer="prose without the construct")
    apply_l2_measurements(row, _entry(gold=["SA23-1380-70"], pattern=r"\)REQ"), HITS, alerts={})
    assert row["syntax_ok"] is False
    assert row["verdict"] == "fail"
    assert any("syntax pattern missed" in f for f in row["failures"])


# ---------------------------------------------------------------- summarize + gate

def test_summarize_rates_and_structural_counts():
    rows = [
        _llm_row("A", citations=["[1] c"], citation_precision=1.0, citation_recall=1.0,
                 judge_label="entailed"),
        _llm_row("B", verdict="fail", failures=["x"], citations=["[1] c"],
                 judge_label="contradiction"),
        _llm_row("C", path="zero_hits", citations=[]),
        _llm_row("D", verdict="error", failures=["HTTP 502"]),
    ]
    m = summarize_l2(rows)
    assert m["structural_fails"] == 1
    assert m["errors"] == 1
    assert m["answer_llm_n"] == 2  # C is zero-hits, D is an error — both excluded
    assert m["grounded_rate"] == 1.0
    assert m["citation_precision"] == 1.0 and m["citation_recall"] == 1.0
    assert m["faithfulness"]["entailed"] == 0.5 and m["faithfulness"]["contradiction"] == 0.5
    assert m["syntax_compliance"] == 1.0


def test_gate_passes_on_rates_and_holds_on_fails():
    good = summarize_l2([_llm_row("A", judge_label="entailed")])
    assert gate_l2(good) == ("pass", [])
    bad = summarize_l2([_llm_row("B", verdict="fail", failures=["syntax pattern missed: x"])])
    verdict, reasons = gate_l2(bad)
    assert verdict == "hold" and reasons
    judge_bad = summarize_l2([_llm_row("C", judge_error="JudgeError: junk")])
    assert gate_l2(judge_bad)[0] == "hold"


def test_gate_holds_on_judge_error_alone():
    m = summarize_l2([_llm_row("A", judge_error="JudgeError: unparseable")])
    # the judge failing closed must hold the gate even with no other failure
    assert m["faithfulness"]["judge_errors"] == 1
    verdict, reasons = gate_l2(m)
    assert verdict == "hold"
    assert any("judge" in r for r in reasons)


def test_apply_fails_vacuous_body_with_validated_citations():
    # Issue #192: a weak model emitting only a copied Citations: section
    # photographs healthy P/R over zero substance — the row must fail.
    row = _llm_row(answer="   ", citations=["[2] SC34-6428-08 CICS Sys Def, p. 55"])
    apply_l2_measurements(row, _entry(gold=["SC34-6428-08"]), HITS, alerts={})
    assert row["verdict"] == "fail"
    assert any("no substantive prose" in f for f in row["failures"])


# --------------------------------------------- issue #298 by-complexity slice
# --------------------------------------------- issue #299 WHY aggregations
@pytest.mark.parametrize("relevance_enabled,search_failure", [(True, False), (False, True)])
def test_run_l2_http_sequence_attribution_and_next_run(monkeypatch, relevance_enabled, search_failure):
    import json
    from types import SimpleNamespace

    from fastapi import FastAPI

    from mainframe_rag import config
    from mainframe_rag.agent import answer as answer_mod
    from mainframe_rag.agent import app as app_mod
    from mainframe_rag.eval.answer_tier import run_l2

    app = FastAPI()
    events = []
    clients = []
    state = {"fail": search_failure, "signals": True, "answers": 0}
    logger = logging.getLogger("agent")
    monkeypatch.setattr(logger, "level", logging.INFO)
    prior_handlers = list(logger.handlers)

    @app.post("/v1/search")
    def search(body: dict):
        assert body["limit"] == 8
        events.append(("search", body["query"]))
        if state["fail"]:
            raise RuntimeError("synthetic search failed")
        return {"hits": [{"cite": "Synthetic reference p. 1", "doc_id": "doc", "text": "Use the documented command."}]}

    @app.post("/v1/answer")
    def answer(body: dict):
        events.append(("answer", body["query"]))
        state["answers"] += 1
        rid = f"request-{state['answers']}"
        if state["signals"]:
            logger.info(json.dumps({"action": "answer", "request_id": rid,
                                    "completion_tokens": state["answers"], "finish_reason": "stop"}))
            if state["answers"] == 1:
                logger.warning(json.dumps({"action": "answer_alert", "request_id": rid,
                                           "alert": "finish_reason_non_stop", "finish_reason": "length"}))
        return {"request_id": rid, "answer": "Use the documented command.",
                "citations": [] if body["query"] == "missing" else ["[1] Synthetic reference p. 1"]}

    class Judge:
        def __init__(self, settings):
            self.closed = False
            clients.append(self)

        def chat(self, messages, *, temperature, reasoning_effort):
            assert temperature == 0.0
            assert reasoning_effort == "low"
            faithfulness = messages[1].content.startswith("EXCERPTS:")
            events.append(("judge", "faithfulness" if faithfulness else "relevance"))
            return SimpleNamespace(content='{"label":"entailed"}' if faithfulness else '{"label":"relevant"}')

        def close(self):
            self.closed = True

    monkeypatch.setattr(app_mod, "app", app)
    monkeypatch.setattr(answer_mod, "HttpxLLMClient", Judge)
    monkeypatch.setattr(config, "load_settings", lambda: object())
    entries = [_entry("A", gold=["doc"]), _entry("B", gold=["doc"]),
               {**_entry("C", gold=["doc"]), "query": "missing"}]
    judge_events = [("judge", "faithfulness")]
    if relevance_enabled:
        judge_events.append(("judge", "relevance"))
    expected = [("search", "q"), ("answer", "q"), *judge_events,
                ("answer", "q"), *judge_events, ("search", "missing"), ("answer", "missing")]

    if search_failure:
        with pytest.raises(RuntimeError, match="synthetic search failed"):
            run_l2(entries, None, relevance_enabled=relevance_enabled)
        assert events == [("search", "q")]
    else:
        rows, metrics = run_l2(entries, None, relevance_enabled=relevance_enabled)
        assert events == expected
        assert [row["verdict"] for row in rows] == ["pass", "pass", "fail"]
        assert [row["completion_tokens"] for row in rows] == [1, 2, 3]
        assert [row["truncated"] for row in rows] == [True, False, False]
        assert metrics["structural_fails"] == 1
    assert logger.handlers == prior_handlers
    assert len(clients) == 1 and clients[0].closed

    # Same request ids and queries in the next ordinary run must not inherit
    # either the previous cache or its answer/alert records.
    events.clear()
    state.update(fail=False, signals=False, answers=0)
    rows, metrics = run_l2(entries, None, relevance_enabled=relevance_enabled)
    assert events == expected
    assert [row["verdict"] for row in rows] == ["pass", "pass", "fail"]
    assert [row["completion_tokens"] for row in rows] == [None, None, None]
    assert [row["truncated"] for row in rows] == [False, False, False]
    assert metrics["structural_fails"] == 1
    assert logger.handlers == prior_handlers
    assert len(clients) == 2 and all(client.closed for client in clients)


def test_judge_labels_parse_and_fail_closed_on_junk():
    assert parse_judge_label('The judgment:\n```json\n{"label": "contradiction"}\n```\nDone.') == "contradiction"
    assert parse_relevance_label('```json\n{"label": "irrelevant"}\n```') == "irrelevant"
    for parse in (parse_judge_label, parse_relevance_label):
        for junk in ('{"label": "mostly_true"}', "I think the answer is supported."):
            with pytest.raises(JudgeError):
                parse(junk)


def test_syntax_check_miss_is_false_and_bad_pattern_fails_closed():
    assert syntax_check(r"(?m)^\s*//", "Here is the JCL:", "//DD1 DD DSN=X") is True
    assert syntax_check(r"\)REQ", "A plain prose answer.", None) is False
    with pytest.raises(JudgeError):
        syntax_check(r"([unclosed", "answer", None)


def test_apply_truncation_join_and_content_floor_exemptions():
    cite = ["[2] SC34-6428-08 CICS Sys Def, p. 55"]
    gold = _entry(gold=["SC34-6428-08"])
    row = _llm_row(request_id="abc", answer="Set the value in the member. [1]", citations=cite)
    apply_l2_measurements(row, gold, HITS, alerts={"abc": "length"})
    assert row["truncated"] is True and row["verdict"] == "pass"
    # Index markers alone are not substance; exempt: no cites, script, abstain.
    row = _llm_row(answer="[1]", citations=cite)
    apply_l2_measurements(row, gold, HITS, alerts={})
    assert row["verdict"] == "fail"
    for kw, entry in (
        ({"answer": "   ", "citations": []}, gold),
        ({"answer": "   ", "script": "//SYSIN DD *", "citations": cite}, gold),
        ({"answer": "   ", "expected_behavior": "abstain", "citations": cite},
         _entry(behavior="abstain", gold=["SC34-6428-08"])),
    ):
        row = _llm_row(**kw)
        apply_l2_measurements(row, entry, HITS, alerts={})
        assert not any("no substantive prose" in f for f in row["failures"]), kw


def test_gate_holds_on_relevance_judge_error_alone():
    m = summarize_l2([_llm_row("A", judge_label="entailed", relevance_error="JudgeError: junk")])
    assert m["relevance"]["judge_errors"] == 1
    verdict, reasons = gate_l2(m)
    assert verdict == "hold"
    assert any("relevance judge" in r for r in reasons)


def test_judge_evidence_is_bounded_before_the_prompt():
    from mainframe_rag.eval.judging import JUDGE_MAX_EVIDENCE_CHARS, evidence_for_citations

    hits = [_hit("SA23-1380-70 z/OS MVS Init Tuning Ref, p. 100", "SA23-1380-70", "x" * (JUDGE_MAX_EVIDENCE_CHARS * 2))]
    evidence, unmapped = evidence_for_citations(["[1] SA23-1380-70 z/OS MVS Init Tuning Ref, p. 100"], hits)
    assert unmapped == []
    assert len(evidence) < JUDGE_MAX_EVIDENCE_CHARS + 100
    assert evidence.endswith("[truncated for the judge]")


# -------------------------------------------- neighbour expansion join (#1314)


def test_pool_join_keeps_the_first_match_for_shared_cites():
    """#1314 review B1: the sibling-pool join is unchanged. Two hits sharing
    one cite line (same-page chunks) map a bare or [n]-prefixed citation to
    the first of them only, and the judge sees that hit's text once per
    citation. Literals observed on origin/main a94cf865."""
    from mainframe_rag.eval.judging import citation_to_hit, evidence_for_citations

    shared_cite = "SA23-1380-70 z/OS MVS Init Tuning Ref, IEASYSxx > LFAREA, p. 100"
    hits = [
        _hit(shared_cite, "SA23-1380-70", "Parent chunk step 1."),
        _hit("SC34-6428-08 CICS Sys Def, p. 55", "SC34-6428-08", "Unrelated chunk."),
        _hit(shared_cite, "SC34-6428-08", "Same-page chunk step 2."),
    ]
    assert citation_to_hit(f"[3] {shared_cite}", hits) == hits[0]
    assert cited_doc_ids([shared_cite, f"[3] {shared_cite}"], hits) == ({"SA23-1380-70"}, [])
    assert evidence_for_citations([shared_cite, shared_cite], hits) == (
        "Parent chunk step 1.\n\n---\n\nParent chunk step 1.", [])


def test_packed_join_maps_shared_cites_to_every_packed_entry():
    """#1314: on an expanded response the packed join maps a bare citation to
    every packed entry carrying its line (same-page neighbours repeat their
    parent's cite) and a [n] prefix to that prompt slot alone."""
    from mainframe_rag.eval.judging import packed_citation_hits, packed_cited_doc_ids

    shared_cite = "SA23-1380-70 z/OS MVS Init Tuning Ref, IEASYSxx > LFAREA, p. 100"
    hits = [
        _hit(shared_cite, "SA23-1380-70", "Parent chunk step 1."),
        _hit("SC34-6428-08 CICS Sys Def, p. 55", "SC34-6428-08", "Unrelated chunk."),
        {**_hit(shared_cite, "SA23-1380-71", "Same-page neighbour step 2."), "neighbour_of": "c1"},
    ]
    assert packed_citation_hits(f"[3] {shared_cite}", hits) == [hits[2]]
    assert packed_citation_hits(shared_cite, hits) == [hits[0], hits[2]]
    assert packed_citation_hits("SX00-0000-00 Absent, p. 1", hits) == []

    supplied = [
        {"index": 1, "citation": shared_cite, "doc_id": "SA23-1380-70", "text": "Parent slice."},
        {"index": 17, "citation": shared_cite, "doc_id": "SA23-1380-71", "text": "Neighbour slice."},
    ]
    assert packed_citation_hits(f"[17] {shared_cite}", supplied) == [supplied[1]]
    # A prefix whose slot does not carry the cite falls back to the bare line.
    assert packed_citation_hits(f"[9] {shared_cite}", supplied) == supplied
    assert packed_cited_doc_ids([shared_cite, "SX00-0000-00 Absent, p. 1"], supplied) == (
        {"SA23-1380-70", "SA23-1380-71"}, ["SX00-0000-00 Absent, p. 1"])


def _excerpt(index: int, cite: str, doc_id: str, text: str) -> dict:
    return {"index": index, "citation": cite, "duplicate_citation": False, "doc_id": doc_id,
            "title": "Synthetic Reference", "heading": "Guide > Procedure", "page_label": "5",
            "pdf_page_start": 5, "pdf_page_end": 5, "start_char": 0, "end_char": len(text),
            "source_chars": len(text), "truncated_start": False, "truncated_end": False,
            "text_clipped": False, "text": text}


def _run_l2_buffered(monkeypatch, pool: list[dict], supplied: list[dict]):
    """run_l2 over a stub app: /v1/search returns 'pool', the buffered
    /v1/answer cites every distinct line of 'supplied'. Returns the row, the
    metrics and the faithfulness judge's prompts."""
    import json
    from types import SimpleNamespace

    from fastapi import FastAPI

    from mainframe_rag import config
    from mainframe_rag.agent import answer as answer_mod
    from mainframe_rag.agent import app as app_mod
    from mainframe_rag.eval.answer_tier import run_l2

    app = FastAPI()
    prompts: list[str] = []

    @app.post("/v1/search")
    def search(body: dict):
        return {"hits": pool}

    @app.post("/v1/answer")
    def answer(body: dict):
        logging.getLogger("agent").info(
            json.dumps({"action": "answer", "request_id": "req-1", "finish_reason": "stop"}))
        return {"request_id": "req-1", "answer": "Open the panel, then verify the status.",
                "citations": list(dict.fromkeys(x["citation"] for x in supplied)),
                "citations_inferred": False, "inferred_indices": [],
                "verification_state": "accepted", "script": None,
                "supplied_evidence": supplied, "evidence_omitted": 0}

    class Judge:
        def __init__(self, settings):
            pass

        def chat(self, messages, *, temperature, reasoning_effort):
            prompts.append(messages[1].content)
            return SimpleNamespace(content='{"label": "entailed"}')

        def close(self):
            pass

    monkeypatch.setattr(app_mod, "app", app)
    monkeypatch.setattr(answer_mod, "HttpxLLMClient", Judge)
    monkeypatch.setattr(config, "load_settings", lambda: object())
    rows, metrics = run_l2([_entry("EXP-1", gold=["SA23-1380-70"])], None, judge_enabled=True)
    [row] = rows
    return row, metrics, prompts


def test_l2_judge_sees_the_first_pool_hit_for_a_shared_cite(monkeypatch):
    """#1314 review B1 (expand off): two packed chunks on one page share a
    cite line and the answer cites it bare. The judge's evidence is the first
    pool hit only, as on origin/main a94cf865, not both packed texts."""
    cite = "SA23-1380-70 Synthetic Reference, Guide > Procedure, p. 5"
    pool = [_hit(cite, "SA23-1380-70", "STEP1 open the panel."),
            _hit(cite, "SA23-1380-70", "STEP2 verify the status.")]
    supplied = [_excerpt(1, cite, "SA23-1380-70", "STEP1 open the panel."),
                _excerpt(2, cite, "SA23-1380-70", "STEP2 verify the status.")]
    row, metrics, prompts = _run_l2_buffered(monkeypatch, pool, supplied)
    assert row["verdict"] == "pass", row["failures"]
    assert prompts == [
        "EXCERPTS:\nSTEP1 open the panel.\n\nANSWER:\nOpen the panel, then verify the status."]
    assert (row["citation_precision"], row["citation_recall"]) == (1.0, 1.0)
    assert metrics["faithfulness"]["judged"] == 1


def test_l2_pool_drift_still_fails_closed(monkeypatch):
    """#1314 review B2 (expand off): the /v1/search pool differs from the
    answer's retrieval, so the validated citation does not map to the pool.
    The row fails closed with no judge call, as on origin/main a94cf865,
    even though the citation maps to the response's own supplied evidence."""
    cite = "SA23-1380-70 Synthetic Reference, Guide > Procedure, p. 5"
    pool = [_hit("SC34-6428-08 CICS Sys Def, p. 55", "SC34-6428-08", "Unrelated chunk.")]
    supplied = [_excerpt(1, cite, "SA23-1380-70", "STEP1 open the panel.")]
    row, metrics, prompts = _run_l2_buffered(monkeypatch, pool, supplied)
    assert row["verdict"] == "fail"
    assert row["unmatched_citations"] == [cite]
    assert row["failures"] == [f"1 validated citation(s) not in the fetched hit set: {[cite]}"]
    assert row["citation_precision"] is None and row["citation_recall"] == 0.0
    assert row["hit_doc_ids"] == ["SC34-6428-08"] and row["gold_retrieved"] is False
    assert prompts == []
    assert metrics["structural_fails"] == 1 and metrics["unmapped_citations"] == 1


def test_l2_keeps_the_pool_join_when_more_was_packed_than_pooled(monkeypatch):
    """#1314: the buffered /v1/answer body carries no 'hits', so it cannot
    show that expansion ran; more packed excerpts than pool hits (an
    'answer_hit_limit' above L2's pool depth, or neighbours) keep the pool
    join and a citation outside the pool fails closed."""
    cite, deep = ("SA23-1380-70 Synthetic Reference, Guide > Procedure, p. 5",
                  "SA23-1380-70 Synthetic Reference, Guide > Procedure, p. 6")
    pool = [_hit(cite, "SA23-1380-70", "STEP1 open the panel.")]
    supplied = [_excerpt(1, cite, "SA23-1380-70", "STEP1 open the panel."),
                _excerpt(2, deep, "SA23-1380-70", "STEP3 save the changes.")]
    row, _, prompts = _run_l2_buffered(monkeypatch, pool, supplied)
    assert row["verdict"] == "fail"
    assert row["unmatched_citations"] == [deep]
    assert row["hit_doc_ids"] == ["SA23-1380-70"]
    assert prompts == []
