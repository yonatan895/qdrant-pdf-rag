"""Unit tests for eval_retrieval scoring: golden schema v2, per-class
aggregation, must_not gates, abstain handling, page diagnostic.

Hermetic: no Qdrant, no network — score_entry and summarize are pure."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from scripts.eval_retrieval import (
    GoldenEntry,
    check_baseline,
    default_baseline_path,
    load_golden,
    score_entry,
    summarize,
    update_baseline,
)

from mainframe_rag.retrieve.query import SearchHit


def _hit(
    doc_id: str = "SC23-6883-70",
    heading: str = "Chapter 4 > Mounting",
    page_label: str = "4-12",
    message_ids: tuple[str, ...] = (),
    score: float = 0.9,
    chunk_type: str = "narrative",
) -> SearchHit:
    return SearchHit(
        chunk_id=f"c-{doc_id}-{page_label}-{score}",
        score=score,
        cite=f"{doc_id} Reference, {heading}, p. {page_label}",
        heading=heading,
        text="excerpt text",
        doc_id=doc_id,
        title="Reference",
        page_label=page_label,
        chunk_type=chunk_type,
        message_ids=message_ids,
    )


def test_score_entry_answer_perfect():
    entry = GoldenEntry(query="NFS mount error return codes", expected_doc_ids=["SC23-6883-70"])
    row = score_entry([_hit()], entry)
    assert row["recall@1"] == 1.0 and row["recall@3"] == 1.0 and row["recall@5"] == 1.0
    assert row["mrr"] == 1.0
    assert "top_scores" not in row


def test_score_entry_heading_scoped():
    entry = GoldenEntry(
        query="NFS mount error return codes",
        expected_doc_ids=["SC23-6883-70"],
        expected_heading="mounting",
    )
    ok = score_entry([_hit(heading="Chapter 4 > Mounting")], entry)
    assert ok["mrr"] == 1.0
    miss = score_entry([_hit(heading="Chapter 9 > Editing")], entry)
    assert miss["mrr"] == 0.0 and miss["recall@5"] == 0.0


def test_score_entry_abstain_excluded_from_recall():
    entry = GoldenEntry(
        query="how do I fix the flux capacitor",
        expected_behavior="abstain",
        must_not_retrieve=["SA23-2230-60"],
    )
    row = score_entry([_hit(doc_id="SA23-2230-60", score=0.7)], entry)
    for key in ("recall@1", "recall@3", "recall@5", "mrr"):
        assert key not in row
    assert row["top_scores"] == [0.7]
    assert row["violations"][0] == {"type": "doc_id", "value": "SA23-2230-60", "rank": 1}


def test_golden_entry_abstain_rejects_expected_docs():
    with pytest.raises(ValidationError, match="abstain"):
        GoldenEntry(
            query="x",
            expected_behavior="abstain",
            expected_doc_ids=["SC23-6883-70"],
        )


def test_golden_entry_rejects_unknown_class():
    with pytest.raises(ValidationError):
        GoldenEntry(query="x", expected_doc_ids=["D1"], query_class="vibes")


def test_score_entry_must_not_doc_violation_ranked():
    entry = GoldenEntry(
        query="DFSORT tuning",
        expected_doc_ids=["SC23-6882-70"],
        must_not_retrieve=["SC23-6880-70"],
    )
    hits = [_hit(doc_id="SC23-6882-70"), _hit(doc_id="SC23-6880-70", score=0.5)]
    row = score_entry(hits, entry)
    assert row["violations"] == [{"type": "doc_id", "value": "SC23-6880-70", "rank": 2}]


def test_score_entry_must_not_window_is_top5():
    entry = GoldenEntry(
        query="DFSORT tuning",
        expected_doc_ids=["SC23-6882-70"],
        must_not_retrieve=["SC23-6880-70"],
    )
    hits = [_hit(doc_id="SC23-6882-70")] + [_hit(doc_id=f"OTHER-{i}", score=0.5 - i * 0.01) for i in range(5)]
    hits.append(_hit(doc_id="SC23-6880-70", score=0.2))  # rank 6: outside the window
    row = score_entry(hits, entry)
    assert "violations" not in row


def test_score_entry_must_not_message_id_via_payload():
    entry = GoldenEntry(
        query="IEA500I rejected",
        expected_doc_ids=["SA38-0677-70"],
        must_not_message_ids=["IEA501I"],
    )
    hits = [
        _hit(doc_id="SA38-0677-70", message_ids=("IEA500I",), score=0.9),
        _hit(doc_id="SA38-0677-70", message_ids=("IEA501I",), score=0.4),
    ]
    row = score_entry(hits, entry)
    assert row["violations"] == [
        {"type": "message_id", "value": ["IEA501I"], "rank": 2, "doc_id": "SA38-0677-70"}
    ]


def test_score_entry_must_not_allows_cocarrying_chunk():
    # Sibling-precision allowance: a chunk that documents BOTH the query's
    # own message id and the bait id is one adjacent-message page (e.g.
    # IOS207I/IOS208I share a page) — never a wrong-sibling answer.
    entry = GoldenEntry(
        query="IOS207I rejected a command",
        expected_doc_ids=["SA38-0676-70"],
        must_not_message_ids=["IOS208I"],
    )
    hits = [
        _hit(doc_id="SA38-0676-70", message_ids=("IOS207I",), score=1.0),
        _hit(doc_id="SA38-0676-07", message_ids=("IOS207I", "IOS208I"), score=0.6),
    ]
    row = score_entry(hits, entry)
    assert "violations" not in row


def test_score_entry_must_not_still_gates_sibling_only_chunk():
    # The allowance needs the query's own parseable id on the chunk; a
    # sibling-only chunk (bait without the query id) still violates.
    entry = GoldenEntry(
        query="IOS207I rejected a command",
        expected_doc_ids=["SA38-0676-70"],
        must_not_message_ids=["IOS208I"],
    )
    hits = [
        _hit(doc_id="SA38-0676-70", message_ids=("IOS207I",), score=1.0),
        _hit(doc_id="SA38-0676-07", message_ids=("IOS208I",), score=0.6),
    ]
    row = score_entry(hits, entry)
    assert row["violations"] == [
        {"type": "message_id", "value": ["IOS208I"], "rank": 2, "doc_id": "SA38-0676-07"}
    ]


def test_score_entry_must_not_unparseable_query_stays_strict():
    # Without a parseable query id the allowance cannot apply; every bait
    # chunk in the window violates (strict gate preserved).
    entry = GoldenEntry(
        query="the spool is short",
        expected_behavior="abstain",
        must_not_message_ids=["HASP309"],
    )
    hits = [_hit(doc_id="SA32-0989-03", message_ids=("HASP309",), score=0.7)]
    row = score_entry(hits, entry)
    assert row["violations"] == [
        {"type": "message_id", "value": ["HASP309"], "rank": 1, "doc_id": "SA32-0989-03"}
    ]


def test_score_entry_page_diagnostic():
    entry = GoldenEntry(
        query="NFS mount error return codes",
        expected_doc_ids=["SC23-6883-70"],
        expected_page="4-12",
    )
    assert score_entry([_hit(page_label="4-12")], entry)["page_hit@5"] == 1.0
    assert score_entry([_hit(page_label="4-13")], entry)["page_hit@5"] == 0.0
    # Page diagnostic is doc-restricted: right page label on a wrong doc is a miss.
    other = score_entry([_hit(doc_id="SC23-9999-99", page_label="4-12")], entry)
    assert other["page_hit@5"] == 0.0


def test_score_entry_page_not_applicable_for_abstain():
    entry = GoldenEntry(query="x", expected_behavior="abstain", expected_page="4-12")
    row = score_entry([_hit(page_label="4-12")], entry)
    assert "page_hit@5" not in row


def test_summarize_mixed_corpus():
    rows = [
        {**score_entry([_hit(doc_id="A")], GoldenEntry(query="q1", expected_doc_ids=["A"], query_class="message_id")), "kind": "identifier"},
        {**score_entry([_hit(doc_id="B", score=0.4)], GoldenEntry(query="q2", expected_doc_ids=["A"], query_class="message_id", must_not_retrieve=["B"])), "kind": "identifier"},
        {**score_entry([], GoldenEntry(query="q3", expected_behavior="abstain", query_class="negative")), "kind": "nl"},
        {"query": "q4", "error": "boom", "kind": "error"},
    ]
    report = summarize(rows, failures=0, elapsed_s=1.0, embed_mode="hash", collection="c")
    assert report["n"] == 4
    assert report["recall@1"] == 0.5  # abstain and error rows excluded from the denominator
    assert report["identifier"]["recall@1"] == 0.5 and report["nl"]["recall@1"] is None
    assert report["classes"]["message_id"]["n"] == 2
    assert report["classes"]["message_id"]["scored"] == 2
    assert report["classes"]["message_id"]["recall@1"] == 0.5
    assert report["classes"]["negative"] == {"n": 1, "scored": 0, "recall@1": None, "recall@3": None, "recall@5": None, "mrr": None}
    assert report["abstain"]["n"] == 1
    assert report["must_not"] == {"checked": 4, "violations": 1, "rate": 0.25}


def test_summarize_abstain_score_calibration():
    rows = [
        score_entry([_hit(score=0.42)], GoldenEntry(query="a", expected_behavior="abstain", query_class="negative")),
        score_entry([_hit(score=0.77)], GoldenEntry(query="b", expected_behavior="abstain", query_class="negative")),
    ]
    report = summarize(rows, failures=0, elapsed_s=0.0, embed_mode="hash", collection="c")
    assert report["abstain"] == {"n": 2, "top_score_mean": 0.595, "top_score_max": 0.77}
    assert report["recall@1"] == 0.0  # no scored rows


def test_check_baseline_zero_gate_ignores_baseline_values():
    report = {"failures": 0, "must_not": {"violations": 2, "checked": 10, "rate": 0.2}}
    regressions = check_baseline(report, {"must_not": {"violations": 2}})
    assert any("must_not.violations" in r for r in regressions)
    report_ok = {"failures": 0, "must_not": {"violations": 0, "checked": 10, "rate": 0.0}}
    assert check_baseline(report_ok, {"must_not": {"violations": 0}}) == []


def test_check_baseline_ratio_gates_unchanged():
    baseline = {"recall@1": 0.5, "recall@5": 0.75, "mrr": 0.625, "identifier": {"recall@1": 1.0}}
    good = {
        "failures": 0,
        "recall@1": 0.5, "recall@5": 0.75, "mrr": 0.625,
        "identifier": {"recall@1": 1.0},
        "must_not": {"violations": 0},
    }
    assert check_baseline(good, baseline) == []
    bad = {**good, "recall@1": 0.4}
    assert any(r.startswith("recall@1") for r in check_baseline(bad, baseline))


def test_update_baseline_roundtrip_with_new_keys(tmp_path: Path):
    report = summarize(
        [
            {**score_entry([_hit(doc_id="A")], GoldenEntry(query="q", expected_doc_ids=["A"], query_class="doc_number")), "kind": "identifier"},
            score_entry([], GoldenEntry(query="t", expected_behavior="abstain", query_class="negative")),
        ],
        failures=0,
        elapsed_s=0.1,
        embed_mode="hash",
        collection="c",
    )
    path = tmp_path / "baseline.json"
    update_baseline(report, path)
    recorded = json.loads(path.read_text(encoding="utf-8"))
    assert recorded["classes"]["doc_number"]["recall@1"] == 1.0
    assert recorded["abstain"]["n"] == 1
    assert check_baseline(report, recorded) == []


def test_default_baseline_path():
    assert default_baseline_path("hash") == Path("evals/baseline.json")
    assert default_baseline_path("vllm") == Path("evals/baseline-vllm.json")


def test_load_golden_abstain_roundtrip(tmp_path: Path):
    golden = tmp_path / "golden.jsonl"
    golden.write_text(
        "# comment line\n"
        + json.dumps({
            "id": "neg-01",
            "query": "fix flux capacitor",
            "query_class": "negative",
            "expected_behavior": "abstain",
            "must_not_retrieve": ["SA23-2230-60"],
            "source": "operator-history",
        })
        + "\n"
        + json.dumps({"query": "SC23-6883-70", "expected_doc_ids": ["SC23-6883-70"], "query_class": "doc_number"})
        + "\n",
        encoding="utf-8",
    )
    entries = load_golden(golden)
    assert len(entries) == 2
    assert entries[0].expected_behavior == "abstain"
    assert entries[1].expected_behavior == "answer"

    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"query": "x", "expected_behavior": "abstain", "expected_doc_ids": ["D"]}) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="abstain entries"):
        load_golden(bad)
