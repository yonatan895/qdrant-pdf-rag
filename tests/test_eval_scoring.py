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
