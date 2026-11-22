"""Unit tests for eval_retrieval scoring: golden schema v2, per-class
aggregation, must_not gates, abstain handling, page diagnostic.

Hermetic: no Qdrant, no network — score_entry and summarize are pure."""

import json
from pathlib import Path

import httpx2
import pytest
from pydantic import ValidationError

from mainframe_rag.eval.datasets import GoldenEntry, load_golden
from mainframe_rag.eval.retrieval import (
    _set,
    check_baseline,
    is_relevant_hit,
    is_sibling_exception,
    score_entry,
    summarize,
    update_baseline,
)
from mainframe_rag.retrieve.query import SearchHit
from tests.helpers_eval_local import retrieval_report as _report


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


def test_shared_predicates_single_source_for_eval_harness_verify():
    entry = GoldenEntry(
        query="IOS207I message", expected_doc_ids=["SA38-0677-70"], expected_heading="mounting"
    )
    assert is_relevant_hit("SA38-0677-70", "Chapter 4 > Mounting", entry) is True
    assert is_relevant_hit("SA38-0677-70", "Chapter 9 > Editing", entry) is False
    assert is_relevant_hit("OTHER-DOC", "Chapter 4 > Mounting", entry) is False
    assert is_sibling_exception({"IOS207I"}, {"IOS207I", "IOS208I"}) is True
    assert is_sibling_exception({"IOS207I"}, {"IOS208I"}) is False
    assert is_sibling_exception(set(), {"IOS208I"}) is False


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
    assert report["classes"]["negative"] == {
        "n": 1,
        "scored": 0,
        "recall@1": None,
        "recall@3": None,
        "recall@5": None,
        "recall@8": None,
        "mrr": None,
        "ndcg@8": None,
    }
    assert report["abstain"]["n"] == 1
    assert report["must_not"] == {"checked": 4, "violations": 1, "rate": 0.25}


# The gate's refusal reasons are one decision table: every kind of regression
# is a row, with the fixed substring callers see.
def _gate_report(**overrides) -> dict:
    report = _report()
    for key, value in overrides.items():
        _set(report, key, value)
    return report


def _empty_scope_reports() -> list[dict]:
    from mainframe_rag.eval.retrieval import score_entry

    rows = ([], [score_entry([], GoldenEntry(query="unknown", expected_behavior="abstain"))])
    return [summarize(r, failures=0, elapsed_s=0, embed_mode="hash", collection="c") for r in rows]


_REGRESSION_CASES = {
    "recall-regression": (
        _gate_report(**{"recall@5": 0.70}),
        {"recall@1": 0.5, "recall@5": 0.80, "mrr": 0.60, "identifier": {"recall@1": 1.0}},
        [("recall@5",)], 1),
    "query-failures": (
        _gate_report(failures=2),
        {"recall@1": 0.5, "recall@5": 0.75, "mrr": 0.60, "identifier": {"recall@1": 1.0}},
        [("failures: 2 > 0",)], 1),
    # Absolute identifier gates: a drop of any size fails, never a ratio. Each
    # reason names its own metric and the gate that fired, together.
    "identifier-and-message-id-absolute": (
        _gate_report(**{"identifier.recall@1": 0.99, "classes.message_id.recall@1": 0.99}),
        {"identifier": {"recall@1": 0.5}, "classes": {"message_id": {"recall@1": 0.5}}},
        [("identifier.recall@1", "absolute gate"),
         ("classes.message_id.recall@1", "absolute gate")], None),
    # Issue #286: the real-corpus baseline sits below 1.0 and goes green at its
    # own value, while any identifier/message-id drop is still flagged.
    "real-corpus-at-its-own-baseline": (
        _gate_report(**{"identifier.recall@1": 0.692, "classes.message_id.recall@1": 0.8}),
        {"_meta": {"collection": "real_manuals", "embed_mode": "vllm"},
         "identifier": {"recall@1": 0.692}, "classes": {"message_id": {"recall@1": 0.8}}},
        [], 0),
    "real-corpus-drop": (
        _gate_report(**{"identifier.recall@1": 0.65, "classes.message_id.recall@1": 0.75}),
        {"_meta": {"collection": "real_manuals", "embed_mode": "vllm"},
         "identifier": {"recall@1": 0.692}, "classes": {"message_id": {"recall@1": 0.8}}},
        [("identifier.recall@1", "real-corpus gate"),
         ("classes.message_id.recall@1", "real-corpus gate")], None),
    # A must_not baseline is a zero gate: its recorded values are not a ratio.
    "must-not-violations": (
        _gate_report(must_not={"violations": 2, "checked": 10, "rate": 0.2}),
        {"must_not": {"violations": 2}},
        [("must_not.violations",)], None),
    "must-not-clean": (
        _gate_report(must_not={"violations": 0, "checked": 10, "rate": 0.0}),
        {"must_not": {"violations": 0}},
        [], 0),
    # Issue #270/#367: a requested class must still be present and scoreable.
    "missing-required-class": (
        _report(),
        {"classes": {"table": {"n": 1, "scored": 1, "recall@5": 1.0}}},
        [("table",)], None),
}


@pytest.mark.parametrize("case", sorted(_REGRESSION_CASES))
def test_check_baseline_flags_each_regression_kind(case):
    report, baseline, needles, count = _REGRESSION_CASES[case]
    regressions = check_baseline(report, baseline)
    if count is not None:
        assert len(regressions) == count, regressions
    if not needles:
        assert regressions == []
    for group in needles:
        # Every text of a group must share ONE reason string: the metric name
        # and the gate that fired are the same verdict, never two loose hits.
        assert any(all(text in r for text in group) for r in regressions), (group, regressions)


def test_requested_gate_rejects_empty_and_abstain_only_reports():
    """Issue #367: requested gates need actual eligible observations."""
    for report in _empty_scope_reports():
        assert check_baseline(report, {"recall@1": 0.0})


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


@pytest.mark.parametrize("failure", [RuntimeError("original failure"),
                                   httpx2.HTTPError("original failure")])
def test_measurement_preserves_calls_errors_and_next_run(monkeypatch, failure):
    from types import SimpleNamespace

    import qdrant_client

    from mainframe_rag.eval.retrieval import evaluate
    from mainframe_rag.ingest import embed
    from mainframe_rag.retrieve import query, rerank

    settings = SimpleNamespace(qdrant_url="http://unused.invalid", qdrant_api_key=None,
                               qdrant_timeout_s=17, qdrant_collection="original", embed_mode="hash")
    client, embedder, reranker = object(), object(), object()
    constructors, calls = [], []
    monkeypatch.setattr(qdrant_client, "QdrantClient",
                        lambda **kwargs: constructors.append(kwargs) or client)
    monkeypatch.setattr(embed, "build_embedder", lambda actual: embedder if actual is settings else None)
    monkeypatch.setattr(rerank, "build_reranker", lambda actual: reranker if actual is settings else None)
    from mainframe_rag.agent.serving import ServingGeneration
    from mainframe_rag.eval import retrieval as retrieval_mod

    generations = []
    monkeypatch.setattr(
        retrieval_mod, "serving_generation",
        lambda c, s: generations.append((c, s)) or ServingGeneration("original__gen1", "compatible"),
    )
    entries = [GoldenEntry(query="broken", expected_doc_ids=["missing"]),
               GoldenEntry(query="answer", expected_doc_ids=["SC23-6883-70"]),
               GoldenEntry(query="abstain", expected_behavior="abstain")]

    def search(*args, **kwargs):
        calls.append((args, kwargs))
        if args[3] == "broken":
            raise failure
        return ([_hit()] if args[3] == "answer" else []), "nl", {}

    monkeypatch.setattr(query, "search", search)
    report = evaluate(entries, settings)
    limits = httpx2.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=2.0)
    assert constructors == [
        {"url": "http://unused.invalid", "api_key": None, "timeout": 17, "limits": limits}
    ]
    # The serving gate's verdict binds the run to the validated physical.
    assert generations == [(client, settings)]
    assert calls == [((client, embedder, "original__gen1", entry.query),
                      {"limit": 8, "settings": settings, "reranker": reranker}) for entry in entries]
    assert report["representation"] == {"servable": True, "outcome": "compatible",
                                        "details": [], "physical": "original__gen1"}
    assert report["collection"] == "original"
    assert report["n"] == 3 and report["failures"] == 1 and report["scored"] == 1
    assert report["recall@1"] == 1.0 and report["mrr"] == 1.0
    assert report["abstain"]["n"] == 1
    calls.clear()
    next_report = evaluate(entries[1:2], settings)
    assert next_report["n"] == next_report["scored"] == 1
    assert next_report["failures"] == 0 and len(calls) == 1


def test_retrieval_defaults_resolve_each_normalized_invocation(monkeypatch):
    from mainframe_rag.config import Settings
    from mainframe_rag.eval.datasets import default_retrieval_paths

    for modes in (("hash", " VLLM "), (" VLLM ", "hash")):
        for mode in modes:
            monkeypatch.setenv("EMBED_MODE", mode)
            normalized = Settings(_env_file=None).embed_mode
            suffix = "-vllm" if mode == " VLLM " else ""
            assert default_retrieval_paths(normalized) == (
                Path("evals/golden.jsonl"), Path(f"evals/baseline{suffix}.json"))
            assert default_retrieval_paths(normalized, "paraphrase") == (
                Path("evals/paraphrase.jsonl"), Path(f"evals/baseline-paraphrase{suffix}.json"))


# ------------------------------------------- passage-level gold (#1317 PR 2)
def _chunk_hit(
    chunk_id: str,
    doc_id: str = "SC23-6883-70",
    heading: str = "Chapter 4 > Mounting",
    page_label: str = "4-12",
    score: float = 0.9,
) -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        score=score,
        cite=f"{doc_id} Reference, {heading}, p. {page_label}",
        heading=heading,
        text="excerpt text",
        doc_id=doc_id,
        title="Reference",
        page_label=page_label,
        chunk_type="narrative",
        message_ids=(),
    )


def _adjudicated_entry(**overrides) -> GoldenEntry:
    base = {
        "id": "GEN-0001",
        "query": "Which lever opens the outlet valve?",
        "expected_doc_ids": ["DOC-A"],
        "expected_heading": "Part A > Valves",
        "expected_page": "4",
        "query_class": "diagnostic",
        "source": "generated",
        "status": "adjudicated",
        "reviewers": ["alice"],
        "reviewed_by": "alice",
        "reviewed_at": "2026-11-21",
        "gold_chunk_ids": ["chunk-src", "chunk-sib1", "chunk-sib2"],
        "gold_chunk_ids_truncated": False,
        "section_chunk_count": 3,
        "lexical_overlap": 0.45,
    }
    base.update(overrides)
    return GoldenEntry(**base)


def test_golden_entry_passage_gold_validation_and_backward_compatibility():
    # Existing doc-only entries still work unchanged.
    plain = GoldenEntry(query="plain question", expected_doc_ids=["DOC-A"])
    assert plain.gold_chunk_ids == []
    assert plain.gold_chunk_ids_truncated is False
    assert plain.section_chunk_count is None
    assert plain.lexical_overlap is None

    # Adjudicated and spot-checked generated entries parse cleanly.
    adj = _adjudicated_entry()
    assert adj.gold_chunk_ids == ["chunk-src", "chunk-sib1", "chunk-sib2"]
    spot = _adjudicated_entry(status="spot_checked", reviewed_by=None, reviewed_at=None)
    assert spot.status == "spot_checked"

    # Abstain entries cannot carry gold_chunk_ids.
    with pytest.raises(ValidationError, match="gold_chunk_ids"):
        GoldenEntry(query="trap", expected_behavior="abstain", gold_chunk_ids=["c1"])
    # Duplicate or blank chunk ids are rejected.
    with pytest.raises(ValidationError):
        _adjudicated_entry(gold_chunk_ids=["c1", "c1"])
    with pytest.raises(ValidationError):
        _adjudicated_entry(gold_chunk_ids=["c1", "  "])
    # section_chunk_count cannot be smaller than gold_chunk_ids.
    with pytest.raises(ValidationError):
        _adjudicated_entry(gold_chunk_ids=["c1", "c2"], section_chunk_count=1)
    # lexical_overlap must be finite in [0, 1].
    with pytest.raises(ValidationError):
        _adjudicated_entry(lexical_overlap=1.5)
    with pytest.raises(ValidationError):
        _adjudicated_entry(lexical_overlap=float("nan"))
    # reviewed_at must be an ISO calendar date when supplied.
    with pytest.raises(ValidationError):
        _adjudicated_entry(reviewed_at="not-a-date")


def test_readers_refuse_escaped_candidate_rejected_spot_check_and_unreviewed_generated(tmp_path):
    from mainframe_rag.eval import datasets as ds

    escaped = tmp_path / "escaped.jsonl"
    escaped.write_text(
        '{"query": "secret question", "expected_doc_ids": ["A"], "status": "\\u0063andidate"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ds.CandidateDatasetError) as exc:
        ds.load_golden(escaped)
    assert str(exc.value) == ds.CANDIDATE_REFUSAL

    rejected = tmp_path / "rejected.jsonl"
    rejected.write_text(
        '{"query": "secret question", "expected_doc_ids": ["A"], "status": "rejected"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ds.CandidateDatasetError) as exc:
        ds.load_golden(rejected)
    assert str(exc.value) == ds.REJECTED_REFUSAL
    assert "secret question" not in str(exc.value)

    review = tmp_path / "review.jsonl"
    review.write_text(
        json.dumps({"record": "spot_check_header", "kind": "gold-spot-check-review", "version": 1}) + "\n"
        + json.dumps({"record": "spot_check_item", "id": "GEN-1", "query": "secret question"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ds.CandidateDatasetError) as exc:
        ds.load_golden(review)
    assert str(exc.value) == ds.SPOT_CHECK_REFUSAL
    assert "secret question" not in str(exc.value)

    # Generated rows stripped of status, reviewers, or adjudication metadata are refused before parsing.
    for bad_fields in (
        {"status": None},
        {"status": "draft"},
        {"reviewers": []},
        {"reviewed_by": None},
        {"reviewed_at": "2026-99-99"},
    ):
        row = {**_adjudicated_entry().model_dump(), **bad_fields, "query": "secret question"}
        bad_path = tmp_path / f"bad-{next(iter(bad_fields))}.jsonl"
        bad_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        with pytest.raises(ds.CandidateDatasetError) as exc:
            ds.load_golden(bad_path)
        assert str(exc.value) == ds.UNREVIEWED_GENERATED_REFUSAL
        assert "secret question" not in str(exc.value)

    # Valid adjudicated and spot_checked generated files load cleanly.
    valid = tmp_path / "valid.jsonl"
    valid.write_text(
        json.dumps(_adjudicated_entry().model_dump()) + "\n"
        + json.dumps(_adjudicated_entry(
            id="GEN-0002", status="spot_checked", reviewed_by=None, reviewed_at=None,
        ).model_dump()) + "\n",
        encoding="utf-8",
    )
    loaded = ds.load_golden(valid)
    assert [e.status for e in loaded] == ["adjudicated", "spot_checked"]


def test_score_entry_scores_chunk_section_and_doc_levels_and_keeps_plain_rows_unchanged():
    # Plain entry without gold_chunk_ids emits no passage keys.
    plain = GoldenEntry(query="q", expected_doc_ids=["DOC-A"], query_class="diagnostic")
    plain_row = score_entry([_chunk_hit("c-other", doc_id="DOC-A")], plain)
    assert "chunk_recall@1" not in plain_row and "section_recall@1" not in plain_row

    # Passage-gold entry: sibling at rank 1, source chunk at rank 4.
    entry = _adjudicated_entry()
    hits = [
        _chunk_hit("chunk-sib1", doc_id="DOC-A", heading="Part A > Valves", score=0.9),
        _chunk_hit("chunk-wrong-1", doc_id="DOC-B", heading="Other", score=0.8),
        _chunk_hit("chunk-wrong-2", doc_id="DOC-C", heading="Other", score=0.7),
        _chunk_hit("chunk-src", doc_id="DOC-A", heading="Part A > Valves", score=0.6),
    ]
    row = score_entry(hits, entry)
    # Doc/heading relevance still hits at rank 1.
    assert row["recall@1"] == 1.0 and row["mrr"] == 1.0
    # Section-chunk relevance hits at rank 1 via chunk-sib1.
    assert row["section_recall@1"] == 1.0
    assert row["section_recall@5"] == 1.0
    assert row["section_mrr"] == 1.0
    assert row["section_rank"] == 1
    # Exact source chunk hits at rank 4.
    assert row["chunk_recall@1"] == 0.0
    assert row["chunk_recall@3"] == 0.0
    assert row["chunk_recall@5"] == 1.0
    assert row["chunk_recall@8"] == 1.0
    assert row["chunk_mrr"] == 0.25
    assert row["chunk_rank"] == 4
    assert row["overlap_band"] == "medium"
    assert row["gold_chunk_ids_truncated"] is False
    assert row["unoutlined_section"] is False


def test_summarize_passage_reports_three_levels_overlap_bands_truncation_and_unoutlined():
    from mainframe_rag.eval.retrieval import summary_markdown

    e_low = _adjudicated_entry(
        id="GEN-1", query_class="diagnostic", lexical_overlap=0.15,
        gold_chunk_ids=["c1", "c1-sib"], gold_chunk_ids_truncated=True, section_chunk_count=80,
    )
    e_high = _adjudicated_entry(
        id="GEN-2", query_class="syntax", expected_heading=None, lexical_overlap=0.85,
        gold_chunk_ids=["c2", "c2-sib"], gold_chunk_ids_truncated=False, section_chunk_count=2,
    )
    r1 = {**score_entry([_chunk_hit("c1-sib", doc_id="DOC-A", heading="Part A > Valves")], e_low), "kind": "nl"}
    r2 = {**score_entry([_chunk_hit("c2", doc_id="DOC-A", heading="")], e_high), "kind": "nl"}
    report = summarize([r1, r2], failures=0, elapsed_s=0.1, embed_mode="hash", collection="c")

    passage = report["passage"]
    assert passage["checked"] == 2
    assert passage["truncated"] == 1
    assert passage["unoutlined"] == 1
    assert passage["chunk"]["recall@1"] == 0.5 and passage["chunk"]["mrr"] == 0.5
    assert passage["section"]["recall@1"] == 1.0 and passage["section"]["mrr"] == 1.0
    assert passage["doc"]["recall@1"] == 1.0 and passage["doc"]["mrr"] == 1.0
    assert passage["by_overlap_band"]["low"]["n"] == 1
    assert passage["by_overlap_band"]["low"]["chunk"]["recall@1"] == 0.0
    assert passage["by_overlap_band"]["high"]["n"] == 1
    assert passage["by_overlap_band"]["high"]["chunk"]["recall@1"] == 1.0
    assert passage["by_class"]["diagnostic"]["truncated"] == 1
    assert passage["by_class"]["syntax"]["unoutlined"] == 1

    md = summary_markdown(report)
    assert "Passage-level" in md and "truncated=1" in md and "unoutlined=1" in md
    assert "low" in md and "high" in md


def test_baseline_recording_refuses_generated_rows_even_after_adjudication(tmp_path, monkeypatch, capsys):
    import scripts.eval_retrieval as er
    import scripts.gate_l1 as gl
    import scripts.harness as hn

    from mainframe_rag import config as cfg
    from mainframe_rag.config import Settings
    from mainframe_rag.eval import datasets as ds

    golden = tmp_path / "adjudicated.jsonl"
    golden.write_text(json.dumps(_adjudicated_entry().model_dump()) + "\n", encoding="utf-8")
    baseline = tmp_path / "baseline.json"
    monkeypatch.delenv("VENUE", raising=False)
    settings = Settings(embed_mode="hash", qdrant_collection="synthetic", _env_file=None)
    monkeypatch.setattr(er, "load_settings", lambda: settings)
    monkeypatch.setattr(cfg, "load_settings", lambda: settings)

    assert er.main(["--golden", str(golden), "--update-baseline", str(baseline)]) == 2
    assert ds.GENERATED_BASELINE_REFUSAL in capsys.readouterr().err
    assert not baseline.exists()

    rc, md = gl.run_gate(golden_path=golden, baseline_path=baseline, update_baseline_flag=True)
    assert rc == 2 and ds.GENERATED_BASELINE_REFUSAL in md
    assert not baseline.exists()

    assert hn.main(["--golden", str(golden), "--baseline", str(baseline), "--update-baseline"]) == 2
    assert ds.GENERATED_BASELINE_REFUSAL in capsys.readouterr().err
    assert not baseline.exists()
