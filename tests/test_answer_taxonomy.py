"""Answer failure taxonomy (issue #830): synthetic, hermetic cases.

Answer rows come from the real ``run_query`` and ``judge`` over stub HTTP
responses, and diagnostic rows from the real ``trace_row`` over synthetic
hits, so the join reads what its producers actually write. Every fixture is
original synthetic text.
"""

from __future__ import annotations

import json
from dataclasses import asdict

import httpx2
import pytest
from scripts import answer_taxonomy as tax
from scripts.packing_diagnostic import trace_row

from mainframe_rag.config import Settings
from mainframe_rag.eval.answers import ZERO_HITS_ANSWER, run_query
from mainframe_rag.eval.datasets import GoldenEntry
from mainframe_rag.retrieve.query import SearchHit
from tests.fakes import settings_kw

GOLD = "SA00-0001-01"
OTHER = "SA00-0002-01"
CITE = f"{GOLD} Synthetic Reference, H, p. 1-1"
QUERY_TEXT = "What does ZZQ100I mean in the synthetic reference?"
ANSWER_TEXT = "ZZQ100I reports that the synthetic widget started."
DECLINE = "The excerpts do not answer this question."


class _Response:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body

    def json(self) -> dict:
        return self._body


class _Client:
    def __init__(self, reply: dict | Exception, status: int = 200):
        self.reply = reply
        self.status = status

    def post(self, *args, **kwargs):
        if isinstance(self.reply, Exception):
            raise self.reply
        return _Response(self.status, self.reply)


def _entry(row_id: str = "MSG-01", behavior: str = "answer", **kw) -> dict:
    base = {
        "id": row_id,
        "query": QUERY_TEXT,
        "query_class": "message_id",
        "expected_behavior": behavior,
        "expected_doc_ids": [GOLD] if behavior == "answer" else [],
        "expected_verification_state": "accepted" if behavior == "answer" else "insufficient_evidence",
    }
    base.update(kw)
    return base


def _payload(answer: str = ANSWER_TEXT, citations=(CITE,), state: str = "accepted", **kw) -> dict:
    return {
        "request_id": "r1",
        "answer": answer,
        "citations": list(citations),
        "citations_inferred": False,
        "inferred_indices": [],
        "verification_state": state,
        **kw,
    }


def _answer_row(entry: dict, reply: dict | Exception, status: int = 200, pool=None) -> dict:
    return run_query(_Client(reply, status), entry, hits=pool)


def _diag(category: str, targets=(), gold_hit: bool = True) -> dict:
    return {
        "id": "MSG-01",
        "category": category,
        "targets": [
            {"kind": k, "key": key, "outcome": o, "hit_ranks": [], "recovered_by": []}
            for k, key, o in targets
        ],
        "hits": [{"rank": 1, "gold_doc": gold_hit}],
    }


_OK = _diag("ok", [("doc", GOLD, "retrieved_packed")])


@pytest.mark.parametrize(
    "entry,reply,status,diag,primary,also",
    [
        pytest.param(_entry(), _payload(), 200, _OK, tax.PASS, [], id="pass"),
        pytest.param(
            _entry(), _payload(DECLINE, (), "insufficient_evidence"), 200, _OK,
            tax.REFUSAL_GOLD_PRESENT, [], id="refusal-gold-present",
        ),
        pytest.param(
            _entry(), _payload(DECLINE, (), "insufficient_evidence"), 200,
            _diag("partial_retrieval", [("doc", GOLD, "retrieved_packed"), ("doc", OTHER, "not_retrieved")]),
            tax.RETRIEVAL_MISS, [tax.REFUSAL_GOLD_PRESENT], id="refusal-with-a-diagnostic-target-not-retrieved",
        ),
        pytest.param(
            _entry(), _payload(DECLINE, (), "insufficient_evidence"), 200,
            _diag("packing_loss", [("doc", GOLD, "retrieved_dropped")]),
            tax.PACKING_LOSS, [tax.REFUSAL_GOLD_PRESENT], id="refusal-gold-dropped-by-packing",
        ),
        pytest.param(
            _entry(), _payload(ZERO_HITS_ANSWER, (), "insufficient_evidence"), 200, _OK,
            tax.RETRIEVAL_MISS, [tax.REFUSAL_GOLD_PRESENT], id="served-zero-hits",
        ),
        pytest.param(
            _entry(), _payload(citations=(), state="unverified_draft"), 200, _OK,
            tax.CITATION_FORMAT, [], id="zero-citations",
        ),
        pytest.param(
            _entry(), _payload(citations_inferred=True, inferred_indices=[1], state="unverified_draft"),
            200, _OK, tax.CITATION_FORMAT, [], id="only-inferred",
        ),
        pytest.param(
            _entry(), _payload(citations=(), state="generation_incomplete"), 200, _OK,
            tax.GENERATION_INCOMPLETE, [tax.CITATION_FORMAT], id="cut-before-stop",
        ),
        pytest.param(
            _entry(gold_must_contain=["synthetic gadget"]), _payload(), 200, _OK,
            tax.CONTENT_MISS, [], id="gold-substring-missing",
        ),
        pytest.param(_entry(), httpx2.ReadTimeout("slow"), 200, _OK, tax.TIMEOUT, [], id="client-timeout"),
        pytest.param(
            _entry(), {"code": "deadline_exceeded", "message": "m"}, 504, _OK, tax.TIMEOUT, [],
            id="agent-deadline",
        ),
        pytest.param(
            _entry(), {"code": "prompt_budget_exceeded", "message": "m"}, 422, _OK, tax.PACKING_LOSS, [],
            id="agent-prompt-budget",
        ),
        pytest.param(
            _entry(), {"code": "invalid_request", "message": "m"}, 422, _OK, tax.ERROR, [],
            id="agent-other-422-stays-error",
        ),
        pytest.param(_entry(), httpx2.ConnectTimeout("down"), 200, _OK, tax.ERROR, [], id="unreachable"),
        pytest.param(
            _entry(), {"code": "upstream_error", "message": "m"}, 502, _OK, tax.ERROR, [], id="upstream",
        ),
        pytest.param(
            _entry("NEG-01", "abstain"), _payload(), 200, None, tax.UNSAFE_ANSWER, [tax.CITATION_FORMAT],
            id="trap-answered",
        ),
        pytest.param(
            _entry("NEG-01", "abstain"), _payload("I cannot say much here.", ()), 200, None,
            tax.CITATION_FORMAT, [], id="decline-not-recognized",
        ),
        pytest.param(
            _entry("NEG-01", "abstain"), _payload(DECLINE, (), "insufficient_evidence"), 200, None,
            tax.PASS, [], id="abstain-pass",
        ),
        pytest.param(
            _entry(), _payload(DECLINE, (), "insufficient_evidence"), 200, None,
            tax.UNATTRIBUTED, [tax.REFUSAL_GOLD_PRESENT], id="no-diagnostic-row",
        ),
    ],
)
def test_primary_class_of_real_eval_rows(entry, reply, status, diag, primary, also):
    row = _answer_row(entry, reply, status)
    got = tax.classify(row, diag)
    assert (got.primary, got.also) == (primary, also)


def test_retrieval_gap_keeps_the_gap_targets_and_a_pass_still_passes():
    diag = _diag("partial_retrieval", [("doc", GOLD, "retrieved_packed"), ("doc", OTHER, "not_retrieved")])
    got = tax.classify(_answer_row(_entry(), _payload()), diag)
    assert got.primary == tax.PASS
    assert got.gold_gaps == [f"doc:{OTHER}=not_retrieved"]


def test_pool_disagreement_is_flagged_only_when_both_signals_exist():
    pool_without_gold = [{"doc_id": OTHER}]
    row = _answer_row(_entry(), _payload(), pool=pool_without_gold)
    assert row["gold_retrieved"] is False
    assert tax.classify(row, _OK).pool_disagrees is True
    assert tax.classify(row, _diag("not_retrieved", gold_hit=False)).pool_disagrees is False
    no_pool = _answer_row(_entry(), _payload())
    assert no_pool["gold_retrieved"] is None
    assert tax.classify(no_pool, _OK).pool_disagrees is False


def test_split_run_counts_every_row_once_and_unrun_rows_never_pass():
    first = {"results": [_answer_row(_entry("MSG-01"), _payload())]}
    second = {"results": [_answer_row(_entry("MSG-02"), _payload())]}
    merged = tax.merge_answer_reports([("s1", first), ("s2", second)])
    rows = tax.build_taxonomy(["MSG-01", "MSG-02", "MSG-03"], merged, {})
    assert [(r.id, r.primary, r.source) for r in rows] == [
        ("MSG-01", tax.PASS, "s1"),
        ("MSG-02", tax.PASS, "s2"),
        ("MSG-03", tax.NOT_RUN, None),
    ]
    assert tax.summarize(rows)["classes"] == {tax.PASS: 2, tax.NOT_RUN: 1}

    with pytest.raises(tax.JoinError, match="MSG-01 appears in s1 and again"):
        tax.merge_answer_reports([("s1", first), ("again", first)])
    with pytest.raises(tax.JoinError, match="outside the expected golden set: MSG-02"):
        tax.build_taxonomy(["MSG-01"], merged, {})


def test_packing_profile_must_be_named_when_ambiguous():
    report = {"profiles": {"target": {"rows": [_OK]}, "w16384": {"rows": []}}}
    with pytest.raises(tax.JoinError, match="pass --profile"):
        tax.packing_rows(report, None)
    name, rows = tax.packing_rows(report, "target")
    assert (name, list(rows)) == ("target", ["MSG-01"])
    with pytest.raises(tax.JoinError, match="'local' not in"):
        tax.packing_rows(report, "local")


def _hit(doc: str, chunk_id: str) -> SearchHit:
    text = "ZZQ100I Synthetic message text.\nExplanation: the synthetic widget started."
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
        units=((0, len(text), "atomic"),),
    )


def test_cli_joins_real_producer_reports_without_any_text(tmp_path):
    """Producer-to-consumer round trip: a split answer run and a packing
    report written by the real producers, joined by the CLI. The output
    carries ids and classes, never query, answer or chunk text."""
    settings = Settings(**settings_kw(
        llm_max_model_len=2048,
        llm_reserved_output_tokens=128,
        llm_thinking_reserve_tokens_simple=0,
        llm_thinking_reserve_tokens_complex=0,
        llm_token_safety_margin=0,
    ))
    golden = tmp_path / "golden.jsonl"
    entries = {
        "MSG-01": _entry("MSG-01"),
        "MSG-02": _entry("MSG-02"),
        "NEG-01": _entry("NEG-01", "abstain"),
        "MSG-03": _entry("MSG-03"),
    }
    golden.write_text("".join(json.dumps(e) + "\n" for e in entries.values()))

    def diag_row(row_id: str, hits: list[SearchHit]) -> dict:
        entry = GoldenEntry(id=row_id, query=QUERY_TEXT, expected_doc_ids=[GOLD])
        return asdict(trace_row(row_id, entry, hits, settings))

    packing = {
        "representation": {"servable": True, "outcome": "compatible", "details": []},
        "profiles": {
            "target": {
                "rows": [
                    diag_row("MSG-01", [_hit(GOLD, "c1")]),
                    diag_row("MSG-02", [_hit(OTHER, "c2")]),
                    diag_row("MSG-03", [_hit(GOLD, "c3")]),
                ]
            }
        },
    }
    session1 = {
        "results": [
            _answer_row(entries["MSG-01"], _payload(DECLINE, (), "insufficient_evidence")),
            _answer_row(entries["NEG-01"], _payload()),
        ]
    }
    session2 = {"results": [_answer_row(entries["MSG-02"], _payload(DECLINE, (), "insufficient_evidence"))]}
    paths = {}
    for name, body in (("s1", session1), ("s2", session2), ("packing", packing)):
        paths[name] = tmp_path / f"{name}.json"
        paths[name].write_text(json.dumps(body))
    out, md = tmp_path / "taxonomy.json", tmp_path / "taxonomy.md"

    code = tax.main([
        "--answers", str(paths["s1"]), "--answers", str(paths["s2"]),
        "--packing", str(paths["packing"]), "--golden", str(golden),
        "--out", str(out), "--markdown", str(md),
    ])

    report = json.loads(out.read_text())
    assert {r["id"]: r["primary"] for r in report["rows"]} == {
        "MSG-01": tax.REFUSAL_GOLD_PRESENT,
        "MSG-02": tax.RETRIEVAL_MISS,
        "MSG-03": tax.NOT_RUN,
        "NEG-01": tax.UNSAFE_ANSWER,
    }
    assert report["summary"]["by_row_class"]["MSG"] == {
        tax.RETRIEVAL_MISS: 1, tax.REFUSAL_GOLD_PRESENT: 1, tax.NOT_RUN: 1,
    }
    assert code == 1  # a row was not run
    blob = out.read_text() + md.read_text()
    for text in (QUERY_TEXT, ANSWER_TEXT, DECLINE, "Synthetic message text", "synthetic widget"):
        assert text not in blob
