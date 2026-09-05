"""Paired off/on A/B mode tests (issue #82): variant settings, pure delta
comparison, attribution markdown, and the CLI fail-closed guards.

Hermetic: no Qdrant, no LLM, no network — ab_variant_settings, compare_ab,
and ab_markdown are pure; the CLI guards fire before any I/O."""

import pytest
from scripts.eval_retrieval import (
    AB_VARIANTS,
    GoldenEntry,
    ab_markdown,
    ab_variant_settings,
    compare_ab,
    main,
    score_entry,
    summarize,
)

from mainframe_rag.config import Settings
from mainframe_rag.retrieve.query import SearchHit


def _hit(
    doc_id: str = "SC23-6883-70",
    score: float = 0.9,
) -> SearchHit:
    return SearchHit(
        chunk_id=f"c-{doc_id}-{score}",
        score=score,
        cite=f"{doc_id} Reference",
        heading="Chapter 4 > Mounting",
        text="excerpt text",
        doc_id=doc_id,
        title="Reference",
        page_label="4-12",
        chunk_type="narrative",
        message_ids=(),
    )


def _report(rows: list[dict], failures: int = 0) -> dict:
    return summarize(rows, failures=failures, elapsed_s=1.0, embed_mode="hash", collection="c")


def _entry(query: str, doc_id: str = "A", query_class: str | None = None) -> GoldenEntry:
    return GoldenEntry(query=query, expected_doc_ids=[doc_id], query_class=query_class)  # type: ignore[arg-type]


def test_ab_variant_settings_turns_on_only_the_requested_flag():
    for variant, flags in AB_VARIANTS.items():
        s = ab_variant_settings(Settings(_env_file=None), variant)
        assert s.hyde_enabled is flags.get("hyde_enabled", False)
        assert s.stepback_enabled is flags.get("stepback_enabled", False)
        # Nothing else leaks into the variant pass.
        assert s.embed_mode == Settings(_env_file=None).embed_mode


def test_combined_variant_enables_both_flags():
    s = ab_variant_settings(Settings(_env_file=None), "combined")
    assert s.hyde_enabled and s.stepback_enabled


def test_compare_ab_identical_runs_zero_delta():
    rows = [
        {**score_entry([_hit(doc_id="A")], _entry("q1", "A", "message_id")), "kind": "identifier"},
        {**score_entry([_hit(doc_id="A", score=0.5)], _entry("q2", "A", "diagnostic")), "kind": "nl"},
    ]
    report = _report(rows)
    delta = compare_ab(report, report, variant="hyde")
    assert delta["variant"] == "hyde"
    assert delta["moved_queries"] == 0
    assert delta["per_query"] == []
    assert delta["regressions"] == []
    for scope in ("all", "identifier", "nl"):
        # recall@3 exists only at the all scope (summarize computes the
        # identifier/nl sub-scores without it) — None cells stay None.
        assert all(
            cell["off"] == cell["on"] and cell["delta"] in (0.0, None)
            for cell in delta["aggregate"][scope].values()
        )


def test_compare_ab_attributes_every_moved_query():
    entry_a = _entry("q1", "A", "message_id")
    entry_b = _entry("q2", "A", "diagnostic")
    entry_c = _entry("q3", "A", "diagnostic")
    off_rows = [
        {**score_entry([_hit(doc_id="B", score=0.9)], entry_a), "kind": "identifier"},
        {**score_entry([_hit(doc_id="B", score=0.9), _hit(doc_id="A", score=0.5)], entry_b), "kind": "nl"},
        {**score_entry([_hit(doc_id="A", score=0.9)], entry_c), "kind": "nl"},
    ]
    on_rows = [
        {**score_entry([_hit(doc_id="A", score=0.9)], entry_a), "kind": "identifier"},
        {**score_entry([], entry_b), "kind": "nl"},
        {**score_entry([_hit(doc_id="A", score=0.9)], entry_c), "kind": "nl"},
    ]
    delta = compare_ab(_report(off_rows), _report(on_rows), variant="hyde")
    assert delta["moved_queries"] == 2
    by_query = {row["query"]: row for row in delta["per_query"]}
    assert by_query["q1"]["off"]["recall@1"] == 0.0 and by_query["q1"]["on"]["recall@1"] == 1.0
    assert by_query["q2"]["off"]["recall@5"] == 1.0 and by_query["q2"]["on"]["recall@5"] == 0.0
    assert by_query["q2"]["off"]["mrr"] == 0.5 and by_query["q2"]["on"]["mrr"] == 0.0
    assert "q3" not in by_query  # unmoved rows are not attributed
    assert delta["aggregate"]["all"]["recall@1"]["delta"] == 0.334  # 0.667 - 0.333 (per-scope rounding)


def test_compare_ab_attributes_one_sided_error():
    entry_a = _entry("q1", "A", "diagnostic")
    off_rows = [{**score_entry([_hit(doc_id="A")], entry_a), "kind": "nl"}]
    on_rows = [{"query": "q1", "error": "boom", "kind": "error"}]
    delta = compare_ab(_report(off_rows), _report(on_rows), variant="stepback")
    assert delta["moved_queries"] == 1
    assert delta["per_query"][0]["error_side"] == "on"


def test_compare_ab_ignores_double_error_rows():
    off_rows = [{"query": "q1", "error": "boom", "kind": "error"}]
    on_rows = [{"query": "q1", "error": "boom", "kind": "error"}]
    delta = compare_ab(_report(off_rows), _report(on_rows), variant="hyde")
    assert delta["moved_queries"] == 0


def test_compare_ab_flags_identifier_degradation():
    # Identifier-heavy queries bypass rewriting via should_rewrite; if the
    # identifier class moves at all, the bypass leaked. regression list must
    # carry it even though the query class is tiny.
    off_rows = [{**score_entry([_hit(doc_id="A")], _entry("IEA500I rejected", "A", "message_id")), "kind": "identifier"}]
    on_rows = [{**score_entry([_hit(doc_id="B", score=0.4)], _entry("IEA500I rejected", "A", "message_id")), "kind": "identifier"}]
    delta = compare_ab(_report(off_rows), _report(on_rows), variant="hyde")
    assert any("identifier.recall@1" in r for r in delta["regressions"])
    assert any("identifier.mrr" in r for r in delta["regressions"])


def test_compare_ab_flags_on_pass_violations():
    entry = _entry("q1", "A")
    entry_w = GoldenEntry(
        query="q2", expected_doc_ids=["A"], must_not_retrieve=["BAIT"]
    )
    off_rows = [
        {**score_entry([_hit(doc_id="A")], entry), "kind": "nl"},
        {**score_entry([_hit(doc_id="A")], entry_w), "kind": "nl"},
    ]
    on_rows = [
        {**score_entry([_hit(doc_id="A")], entry), "kind": "nl"},
        {**score_entry([_hit(doc_id="A"), _hit(doc_id="BAIT", score=0.5)], entry_w), "kind": "nl"},
    ]
    delta = compare_ab(_report(off_rows), _report(on_rows), variant="hyde")
    assert any("must_not violations on the hyde pass: 1" in r for r in delta["regressions"])


def test_ab_markdown_tables_and_regressions():
    off_rows = [{**score_entry([_hit(doc_id="B")], _entry("q1", "A", "diagnostic")), "kind": "nl"}]
    on_rows = [{**score_entry([_hit(doc_id="A")], _entry("q1", "A", "diagnostic")), "kind": "nl"}]
    delta = compare_ab(_report(off_rows), _report(on_rows), variant="hyde")
    text = ab_markdown(delta)
    assert text.startswith("## A/B: hyde off vs on")
    assert "| scope | metric | off | on | delta |" in text
    assert "| all | recall@1 |" in text
    assert "moved queries: 1" in text
    assert "q1" in text
    assert delta["regressions"] == []


def test_ab_markdown_lists_regressions():
    report = _report([{**score_entry([], _entry("q1", "A")), "kind": "nl"}])
    delta = compare_ab(report, report, variant="stepback")
    assert delta["regressions"] == []  # identical runs never regress
    regressions = compare_ab(
        report,
        {**report, "must_not": {"checked": 1, "violations": 2, "rate": 2.0}},
        variant="stepback",
    )
    text = ab_markdown(regressions)
    assert "### Regressions" in text
    assert "must_not violations on the stepback pass: 2" in text


def _assert_cli_error(capsys, match: str) -> None:
    err = capsys.readouterr().err
    assert match in err


def test_cli_rejects_ab_with_baseline_gate(capsys):
    with pytest.raises(SystemExit):
        main(["--ab", "hyde", "--check", "evals/baseline.json"])
    _assert_cli_error(capsys, "paired")


def test_cli_rejects_ab_with_update_baseline(capsys):
    with pytest.raises(SystemExit):
        main(["--ab", "hyde", "--update-baseline", "evals/baseline.json"])
    _assert_cli_error(capsys, "paired")


def test_cli_rejects_ab_with_label_draft(capsys):
    with pytest.raises(SystemExit):
        main(["--ab", "hyde", "--label-draft"])
    _assert_cli_error(capsys, "paired")


def test_cli_rejects_ab_when_settings_already_enable_rewrite(monkeypatch, capsys):
    from scripts import eval_retrieval

    monkeypatch.setattr(
        eval_retrieval,
        "load_settings",
        lambda: Settings(_env_file=None, hyde_enabled=True),
    )
    with pytest.raises(SystemExit):
        main(["--ab", "hyde", "--no-check"])
    _assert_cli_error(capsys, "off pass")


def test_cli_rejects_unknown_ab_variant():
    with pytest.raises(SystemExit):
        main(["--ab", "splash", "--no-check"])


def test_run_ab_all_zero_is_exit_2_not_a_null_result(monkeypatch, capsys, tmp_path):
    # Both passes scoring recall@1 = 0.0 means the golden set and the
    # collection look mismatched (the off pass alone gates at ~0.45 on the
    # real venues): exit 2 so a mismatch cannot read as a clean 0-delta pass.
    from types import SimpleNamespace

    from scripts import eval_retrieval

    zero_report = {
        "n": 2, "failures": 0, "elapsed_s": 0.0, "embed_mode": "hash", "collection": "c",
        "recall@1": 0.0, "recall@3": 0.0, "recall@5": 0.0, "recall@8": 0.0,
        "ndcg@8": 0.0,
        "mrr": 0.0, "identifier": {"recall@1": 0.0, "mrr": 0.0},
        "nl": {"recall@1": 0.0, "mrr": 0.0}, "must_not": {"violations": 0},
        "rows": [
            {"query": "q1", "id": "a", "recall@1": 0.0, "recall@5": 0.0, "mrr": 0.0,
             "hit_doc_ids": []},
            {"query": "q2", "id": "b", "recall@1": 0.0, "recall@5": 0.0, "mrr": 0.0,
             "hit_doc_ids": []},
        ],
    }
    monkeypatch.setattr(eval_retrieval, "evaluate", lambda golden, settings: zero_report)
    monkeypatch.setattr(eval_retrieval, "write_run_manifest", lambda *a, **k: {"git_sha": "x"})
    args = SimpleNamespace(ab="hyde", out=None, summary=None)
    ret = eval_retrieval._run_ab(args, Settings(_env_file=None), [])
    assert ret == 2
    assert "mismatched" in capsys.readouterr().err
