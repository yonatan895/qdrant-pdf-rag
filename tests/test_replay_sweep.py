"""Unit tests for scripts/replay_sweep.py (hermetic, no GPU/network)."""

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from scripts import replay_sweep
from scripts.capture_pool import legs_to_record, replay_pool
from scripts.replay_sweep import (
    HeadingJoinError,
    SweepConfig,
    candidate_configs,
    join_headings,
    replay_record,
    run_config,
    score_hits,
    summarize,
    unsupported_reason,
)

from mainframe_rag.eval.datasets import GoldenEntry
from tests.conftest import _point
from tests.helpers_eval_local import eval_settings as _settings


def _cp(pid, doc, page="1", ctype="narrative", page_start=5):
    """Shared point; page_start=None makes a legacy point without a physical page."""
    base = _point(pid)
    payload = dict(base.payload or {})
    payload.update({"doc_id": doc, "page_label": page, "chunk_type": ctype, "page_start": page_start})
    if page_start is None:
        del payload["page_start"]
    return base.model_copy(update={"payload": payload})


def _record(dense_ids, ce=None, meta=None, second_leg=None, docs=None):
    # Every capture records its per-leg depth; 200 is the capture default.
    meta = {"depth": 200, **(meta or {})}
    docs = docs or {}
    if second_leg is None and docs:
        legs = [{"effective_text": "q", "dense": [_cp(pid, docs.get(pid, pid)) for pid in dense_ids],
                 "sparse": []}]
        return legs_to_record("q", "nl", legs, ce or {}, meta or {})
    legs = [{"effective_text": "q", "dense": [_cp(pid, pid) for pid in dense_ids], "sparse": []}]
    if second_leg is not None:
        legs.append({"effective_text": "q2", "dense": [_cp(pid, pid) for pid in second_leg], "sparse": []})
    return legs_to_record("q", "nl", legs, ce or {}, meta or {})


def test_replay_alpha_one_follows_ce_alpha_zero_keeps_rrf_order():
    record = _record(["c1", "c2", "c3"], {"c1": 0.1, "c2": 0.9, "c3": 0.5}, {"ce_scored": True})
    config = SweepConfig(label="alpha", rerank=True, fuse_limit=50)
    assert [h.chunk_id for h in replay_record(record, config, _settings())] == ["c2", "c3", "c1"]
    kept = replay_record(record, replace(config, alpha=0.0), _settings())
    assert [h.chunk_id for h in kept] == ["c1", "c2", "c3"]
    # A rerank replay over a pool without CE scores refuses rather than guesses.
    with pytest.raises(ValueError, match="no CE score"):
        replay_record(_record(["c1", "c2"], {}, {"ce_scored": True}), config, _settings())


def test_replay_limits_truncate_and_refuse_beyond_the_recorded_pool():
    record = _record([f"c{i}" for i in range(5)])
    base = SweepConfig(label="prod")
    assert len(replay_record(record, replace(base, fuse_limit=5), _settings())) == 5
    assert len(replay_record(record, replace(base, fuse_limit=2), _settings())) == 2
    hits = replay_record(record, replace(base, prefetch_limit=1), _settings())
    assert [h.chunk_id for h in hits] == ["c0"]

    deep = replace(SweepConfig(label="deep"), prefetch_limit=100)
    with pytest.raises(ValueError, match="exceeds the recorded pool depth 50"):
        replay_record(_record(["c0"], meta={"depth": 50}), deep, _settings())
    # A pool without a recorded depth predates depth recording: refused.
    undated = _record(["c0", "c1"])
    del undated["_meta"]["depth"]
    with pytest.raises(ValueError, match="predates depth recording"):
        replay_record(undated, deep, _settings())
    # run_config skips (never scores) an unsupported config.
    records = [_record(["c0"], meta={"depth": 50}), {"query": "broken", "error": "x"}]
    assert unsupported_reason(records, SweepConfig(label="p")) is None
    assert unsupported_reason(records, deep) == "pool depth 50 < prefetch 100"
    assert run_config(records, {}, deep, _settings()) == {
        "label": "deep", "scoring": "doc", "skipped": "pool depth 50 < prefetch 100"
    }


def test_replay_split_modes_merge_differently():
    """Comparative takes best evidence per doc (tie keeps leg order);
    diagnostic rank-sums 2:1 symptom-first — the recorded mode decides."""
    legs = ([_cp("a", "A"), _cp("b", "B")], [_cp("b", "B"), _cp("c", "C")])
    rec = legs_to_record(
        "A versus B",
        "nl",
        [
            {"effective_text": "q", "dense": legs[0], "sparse": []},
            {"effective_text": "q2", "dense": legs[1], "sparse": []},
        ],
        {},
        {"split_mode": "comparative", "depth": 200},
    )
    config = SweepConfig(label="t")
    assert replay_record(rec, config, _settings())[0].chunk_id == "a"
    rec["_meta"]["split_mode"] = "diagnostic"
    assert replay_record(rec, config, _settings())[0].chunk_id == "b"


def test_score_hits_doc_level_section_level_and_must_not():
    entry = GoldenEntry(
        query="q", expected_doc_ids=["D"], must_not_retrieve=["X"], query_class="syntax"
    )
    row = score_hits([SimpleNamespace(doc_id="X"), SimpleNamespace(doc_id="D")], entry)
    assert row is not None
    assert row["recall@1"] == 0.0 and row["recall@5"] == 1.0 and row["mrr"] == 0.5
    assert row["violations"] == ["X"]
    abstain = GoldenEntry(query="trap", expected_behavior="abstain", query_class="negative")
    assert score_hits([], abstain) is None

    entry = GoldenEntry(query="q", expected_doc_ids=["D"], expected_heading="Status subparameter",
                        query_class="syntax")
    wrong = SimpleNamespace(doc_id="D", heading="Book > DISP parameter > Example of the DEST parameter")
    right = SimpleNamespace(doc_id="D", heading="Book > DISP parameter > Status subparameter")
    doc_row = score_hits([wrong, right], entry)
    assert doc_row is not None and doc_row["recall@1"] == 1.0  # doc-level ignores the heading
    section_row = score_hits([wrong, right], entry, section_level=True)
    assert section_row is not None
    assert section_row["recall@1"] == 0.0 and section_row["mrr"] == 0.5


def test_summarize_means_classes_and_violations():
    rows = [
        {"id": "a", "query_class": "syntax", "recall@1": 1.0, "recall@5": 1.0, "mrr": 1.0},
        {"id": "b", "query_class": "syntax", "recall@1": 0.0, "recall@5": 1.0, "mrr": 0.5,
         "violations": ["X"]},
    ]
    summary = summarize(rows)
    assert summary["n"] == 2
    assert summary["recall@1"] == 0.5
    assert summary["classes"]["syntax"]["mrr"] == 0.75
    assert summary["violations"] == 1


def test_rerank100_replays_a_deep_scored_pool():
    ids = [f"c{i:03d}" for i in range(120)]
    ce = {cid: float(i) / 1000 for i, cid in enumerate(ids[:100])}  # capture stops CE at 100
    record = _record(ids, ce=ce, meta={"depth": 120, "ce_scored": True, "ce_depth": 100})
    config = next(c for c in candidate_configs(_settings()) if c.label == "rerank100")
    assert config.rerank and config.prefetch_limit == 100
    hits = replay_record(record, config, _settings())
    assert hits[0].chunk_id == "c099"  # the deepest scored candidate wins on CE alone (alpha 1.0)


def test_replay_headings_change_scoring_not_ranking():
    record = _record(["c1", "c2", "c3"], docs={"c1": "D", "c2": "D", "c3": "E"})
    headings = {"c1": "Book > A", "c2": "Book > Status subparameter", "c3": "Other > C"}
    config = SweepConfig(label="p", max_per_doc=3, max_per_page=3)
    plain = replay_record(record, config, _settings())
    joined = replay_record(record, config, _settings(), headings)
    assert [h.chunk_id for h in joined] == [h.chunk_id for h in plain]
    assert [h.heading for h in joined] == ["Book > A", "Book > Status subparameter", "Other > C"]
    entry = GoldenEntry(query="q", expected_doc_ids=["D"], expected_heading="status", query_class="syntax")
    result = run_config([record], {"q": entry}, config, _settings(), headings)
    assert result["scoring"] == "section" and result["mrr"] == 0.5


class _RetrieveClient:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def retrieve(self, collection, ids, with_payload, with_vectors):
        self.calls.append((collection, list(ids), with_payload, with_vectors))
        return [SimpleNamespace(id=i, payload=self.payloads[i]) for i in ids if i in self.payloads]


def test_join_headings_maps_ids_and_fails_closed_on_a_different_ingest():
    records = [_record(["c1", "c2"], docs={"c1": "D", "c2": "D"}), {"query": "x", "error": "boom"}]
    good = {
        "c1": {"doc_id": "D", "page_label": "1", "page_start": 5, "heading_path": "Book > A"},
        "c2": {"doc_id": "D", "page_label": "1", "page_start": 5, "heading_path": "Book > B"},
    }
    client = _RetrieveClient(good)
    assert join_headings(records, client, "local") == {"c1": "Book > A", "c2": "Book > B"}
    assert client.calls[0][2] == ["doc_id", "page_label", "page_start", "heading_path"]  # never chunk text
    with pytest.raises(HeadingJoinError, match="1 captured chunk ids missing and 0 with a different"):
        join_headings(records, _RetrieveClient({"c1": good["c1"]}), "local")
    moved = dict(good, c2={**good["c2"], "page_label": "9"})
    with pytest.raises(HeadingJoinError, match="0 captured chunk ids missing and 1 with a different"):
        join_headings(records, _RetrieveClient(moved), "local")


def test_replay_page_cap_buckets_by_physical_page_like_live():
    """Unlabelled pages share the printed label ''; live diversification
    (query._page_key) buckets by physical page, so replay must too."""
    pts = [_cp("a", "D", "", page_start=10), _cp("c", "D", "", page_start=10), _cp("b", "D", "", page_start=11)]
    record = legs_to_record("q", "nl", [{"effective_text": "q", "dense": pts, "sparse": []}], {}, {"depth": 200})
    config = SweepConfig(label="p", max_per_page=1, max_per_doc=3)
    # The same-page "c" is deferred behind the next page, not ranked second.
    assert [h.chunk_id for h in replay_record(record, config, _settings())] == ["a", "b", "c"]
    unpaged_pts = [_cp(p, "D", "", page_start=None) for p in "acb"]
    unpaged = legs_to_record("q", "nl", [{"effective_text": "q", "dense": unpaged_pts, "sparse": []}], {}, {"depth": 200})
    # Without a physical page every unlabelled chunk shares one bucket: RRF order stands.
    assert [h.chunk_id for h in replay_record(unpaged, config, _settings())] == ["a", "c", "b"]


def test_join_headings_refuses_a_moved_physical_page():
    pts = [_cp("c1", "D", "", page_start=5)]
    record = legs_to_record("q", "nl", [{"effective_text": "q", "dense": pts, "sparse": []}], {}, {"depth": 200})
    same = {"c1": {"doc_id": "D", "page_label": "", "page_start": 5, "heading_path": "H"}}
    assert join_headings([record], _RetrieveClient(same), "local") == {"c1": "H"}
    moved = {"c1": {**same["c1"], "page_start": 6}}
    with pytest.raises(HeadingJoinError, match="1 with a different"):
        join_headings([record], _RetrieveClient(moved), "local")


def _fusion_record(dense, sparse, text="q", kind="nl"):
    """One leg with explicit dense/BM25 lists; every chunk is its own doc."""
    legs = [{"effective_text": text, "dense": [_cp(p, p) for p in dense], "sparse": [_cp(p, p) for p in sparse]}]
    return legs_to_record(text, kind, legs, {}, {"depth": 200})


def test_replay_rrf_k_override_reaches_fusion():
    """#845: `a` is rank 1 in one list only, `m` rank 5 in both. With k=2 the
    lone rank 1 wins (1/3 > 2/7); with k=60 the double rank 5 wins (2/65 > 1/61)."""
    rec = _fusion_record(["a", "d1", "d2", "d3", "m"], ["s1", "s2", "s3", "s4", "m"])
    assert replay_record(rec, SweepConfig(label="k2", rrf_k=2), _settings())[0].chunk_id == "a"
    assert replay_record(rec, SweepConfig(label="k60", rrf_k=60), _settings())[0].chunk_id == "m"
    # None keeps the Settings value.
    assert replay_record(rec, SweepConfig(label="p"), _settings(rrf_k=60))[0].chunk_id == "m"


def test_replay_sparse_weight_overrides_follow_the_leg_kind():
    """An NL leg ties dense and BM25 rank 1 at (1, 1); doubling the NL sparse
    weight lifts the BM25 hit. Identifier legs keep (1, 3) unless their own
    override applies, and the NL override never touches them."""
    nl = _fusion_record(["a"], ["b"])
    assert replay_record(nl, SweepConfig(label="p"), _settings())[0].chunk_id == "a"
    assert replay_record(nl, SweepConfig(label="w", sparse_weight_nl=2.0), _settings())[0].chunk_id == "b"
    assert replay_record(nl, SweepConfig(label="i", sparse_weight_identifier=1.0), _settings())[0].chunk_id == "a"

    ident = _fusion_record(["a"], ["b"], text="What does IEA500I mean?", kind="identifier")
    assert replay_record(ident, SweepConfig(label="p"), _settings())[0].chunk_id == "b"
    assert replay_record(ident, SweepConfig(label="w", sparse_weight_nl=5.0), _settings())[0].chunk_id == "b"
    assert replay_record(ident, SweepConfig(label="i", sparse_weight_identifier=1.0), _settings())[0].chunk_id == "a"


def test_rrf_grid_sweeps_each_kind_against_production():
    from scripts.replay_sweep import RRF_GRID_K, RRF_GRID_SPARSE_WEIGHT, rrf_grid_configs

    configs = rrf_grid_configs(_settings())
    assert configs[0] == candidate_configs(_settings())[0]
    grid = configs[1:]
    assert len(grid) == 2 * len(RRF_GRID_K) * len(RRF_GRID_SPARSE_WEIGHT) == 40
    assert len({c.label for c in configs}) == len(configs)
    assert {c.rrf_k for c in grid} == {2, 5, 10, 30, 60}
    # Exactly one kind's weight varies per config; the other stays production.
    assert all((c.sparse_weight_nl is None) != (c.sparse_weight_identifier is None) for c in grid)
    assert {c.sparse_weight_nl for c in grid} - {None} == {c.sparse_weight_identifier for c in grid} - {None} == {
        1.0, 2.0, 3.0, 5.0
    }


def test_run_config_reports_identifier_and_nl_separately():
    entries = {
        "q": GoldenEntry(id="NL-1", query="q", query_class="syntax", expected_doc_ids=["a"]),
        "What does IEA500I mean?": GoldenEntry(
            id="ID-1", query="What does IEA500I mean?", query_class="message_id", expected_doc_ids=["a"]
        ),
    }
    records = [
        _fusion_record(["a"], ["b"]),
        _fusion_record(["a"], ["b"], text="What does IEA500I mean?", kind="identifier"),
    ]
    result = run_config(records, entries, SweepConfig(label="p"), _settings())
    # NL ties keep dense first (a, relevant); the identifier leg's 3x BM25 puts b first.
    assert result["kinds"] == {
        "identifier": {"n": 1, "recall@1": 0.0, "recall@5": 1.0, "mrr": 0.5},
        "nl": {"n": 1, "recall@1": 1.0, "recall@5": 1.0, "mrr": 1.0},
    }
    assert [r["kind"] for r in result["rows"]] == ["nl", "identifier"]


def test_rerank_grid_is_the_pre_registered_862_axes_against_production():
    from scripts.replay_sweep import rerank_grid_configs

    configs = rerank_grid_configs(_settings())
    assert configs[0] == candidate_configs(_settings())[0]
    assert len({c.label for c in configs}) == len(configs) and all(len(c.label) <= 12 for c in configs)
    off = [c for c in configs[1:] if not c.rerank]
    grid = [c for c in configs[1:] if c.rerank]
    assert [c.rrf_k for c in off] == [2, 60]
    assert len(grid) == 2 * 3 * 5
    assert {(c.rrf_k, c.prefetch_limit, c.alpha) for c in grid} == {
        (k, depth, alpha) for k in (2, 60) for depth in (20, 30, 50)
        for alpha in (0.0, 0.25, 0.5, 0.75, 1.0)}
    # Candidate depth is both the per-leg prefetch and the fused set reranked,
    # exactly what RERANK_CANDIDATES sets in production; never beyond 50.
    assert all(c.fuse_limit == c.prefetch_limit <= 50 for c in grid)


def test_rerank_grid_depth_bounds_which_candidates_the_cross_encoder_can_promote():
    from scripts.replay_sweep import rerank_grid_configs

    ids = [f"c{i:03d}" for i in range(30)]
    ce = {cid: 0.1 for cid in ids}
    ce["c025"] = 0.99   # beyond the depth-20 candidate set
    ce["c010"] = 0.9
    record = _record(ids, ce=ce, meta={"depth": 50, "ce_scored": True, "ce_depth": 50})
    by_label = {c.label: c for c in rerank_grid_configs(_settings())}
    assert replay_record(record, by_label["k60d20a1"], _settings())[0].chunk_id == "c010"
    assert replay_record(record, by_label["k60d30a1"], _settings())[0].chunk_id == "c025"
    # alpha 0 keeps the first-stage order whatever the cross-encoder says.
    assert replay_record(record, by_label["k60d30a0"], _settings())[0].chunk_id == "c000"


def test_rerank_grid_without_ce_scored_pools_warns_with_a_fixed_message(capsys):
    """#588: a rerank grid over pools with no CE scores replays RRF-only; say so."""
    from scripts.replay_sweep import NO_CE_SCORED_WARNING, warn_if_no_ce_scored

    plain = _record(["a", "b"])
    scored = _record(["a", "b"], ce={"a": 1.0, "b": 0.5}, meta={"ce_scored": True})
    assert warn_if_no_ce_scored("rerank", [plain]) is True
    assert capsys.readouterr().err.strip() == NO_CE_SCORED_WARNING
    assert warn_if_no_ce_scored("rerank", [plain, scored]) is False
    assert warn_if_no_ce_scored("default", [plain]) is False
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------- replay schema
# (tests/test_replay.py retired into this file: one module owns the captured
# pool's schema, its replay, and the sweep over it.)


def _capture_row(pid: str, dense_rank: int | None = None, sparse_rank: int | None = None,
                 ce: Any = 0.5) -> dict[str, Any]:
    return {"id": pid, "doc_id": pid, "page": "1", "dense_rank": dense_rank,
            "sparse_rank": sparse_rank, "ce": ce}


def test_replay_pool_orders_legs_by_rank_and_defaults_optional_fields():
    rows = [
        {"id": "b", "doc_id": "B", "page": "2", "dense_rank": 1, "sparse_rank": 0},  # no ce, no chunk_type
        {"id": "a", "doc_id": "A", "page": "1", "dense_rank": 0, "ce": 0.5},
        {"id": "c", "doc_id": "C", "page": "3", "sparse_rank": 1, "ce": 0.1},  # sparse-only
    ]
    dense, sparse, ce_by_id = replay_pool(rows)
    assert [p.id for p in dense] == ["a", "b"]
    assert [p.id for p in sparse] == ["b", "c"]
    assert ce_by_id == {"b": None, "a": 0.5, "c": 0.1}  # CE-less rows are kept, marked unscored
    assert all(p.payload["chunk_type"] == "narrative" for p in dense)


@pytest.mark.parametrize("rows", [
    "not-a-list",
    ["just-a-string"],
    [{"no": "id"}],
    [_capture_row("a", dense_rank=0), _capture_row("a", sparse_rank=0)],  # duplicate id
    [_capture_row("a", dense_rank=0), _capture_row("b", dense_rank=0)],  # duplicate rank in a leg
    [_capture_row("lonely")],  # no rank on either leg
    [_capture_row("flag", dense_rank=True)],
    [_capture_row("nance", dense_rank=0, ce=float("nan"))],
    [{**_capture_row("badtype", dense_rank=0), "chunk_type": 7}],
    [{**_capture_row("baddoc", dense_rank=0), "doc_id": ""}],
    [{**_capture_row("badpage", dense_rank=0), "page": ""}],
])
def test_replay_schema_rejects_corrupt_capture(rows):
    with pytest.raises((ValueError, TypeError)):
        replay_pool(rows)  # type: ignore[arg-type]


# ------------------------------------------------------------------ main's exit code


def _sweep_result(label: str, failures: int | None = None, skipped: str | None = None) -> dict:
    """One config's result exactly as ``run_config`` shapes it: a scored row
    carries ``failures``; a skipped row reports the reason and no counts."""
    if skipped is not None:
        return {"label": label, "scoring": "doc", "skipped": skipped}
    return {"label": label, "scoring": "doc", "failures": failures, "unmatched": 0, **summarize([]), "rows": []}


@pytest.fixture
def sweep_cli(tmp_path, monkeypatch):
    """Drives ``main()`` for real with the dataset loading and scoring stubbed,
    so the exit code comes from ``main()``'s own tally of the results it was given."""

    def run(results: list[dict]) -> int:
        pools = tmp_path / "pools.jsonl"
        pools.write_text(json.dumps(_record(["P1"], docs={"P1": "D1"})) + "\n", encoding="utf-8")
        golden = tmp_path / "golden.jsonl"
        golden.write_text(json.dumps({"id": "q1", "query": "q", "expected_doc_ids": ["D1"],
                                      "query_class": "syntax"}) + "\n", encoding="utf-8")
        entries = [GoldenEntry(id="q1", query="q", expected_doc_ids=["D1"], query_class="syntax")]
        monkeypatch.setattr(replay_sweep, "load_golden", lambda path: entries)
        monkeypatch.setattr(replay_sweep, "candidate_configs", lambda settings: [SweepConfig(label="stub")])
        monkeypatch.setattr("mainframe_rag.config.load_settings", lambda: _settings())
        answers = iter(results)
        monkeypatch.setattr(replay_sweep, "run_config", lambda *args, **kwargs: next(answers))
        return replay_sweep.main(["--pools", str(pools), "--golden", str(golden)])

    return run


def test_main_exits_1_when_rows_failured(sweep_cli) -> None:
    assert sweep_cli([_sweep_result("stub", failures=1)]) == 1


def test_main_exits_0_when_no_failures(sweep_cli) -> None:
    assert sweep_cli([_sweep_result("stub", failures=0)]) == 0


def test_main_exits_0_when_configs_only_skipped(sweep_cli) -> None:
    assert sweep_cli([_sweep_result("stub", skipped="pool depth 50 < prefetch 100")]) == 0


def test_main_exits_1_over_a_captured_error_row_and_still_reports(tmp_path, monkeypatch, capsys):
    """End to end over a real pools file: ``run_config`` counts the captured
    error row, so ``main`` exits 1, and the table and ``--json`` still cover
    every config (the exit code adds a signal, it withholds no artefact)."""
    monkeypatch.delenv("VENUE", raising=False)
    monkeypatch.setattr("mainframe_rag.config.load_settings", lambda: _settings())
    pools = tmp_path / "pools.jsonl"
    records = [_record(["P1"], docs={"P1": "D1"}), {"query": "broken", "error": "timeout"}]
    pools.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    golden = tmp_path / "golden.jsonl"
    golden.write_text(json.dumps({"id": "q1", "query": "q", "expected_doc_ids": ["D1"],
                                  "query_class": "syntax"}) + "\n", encoding="utf-8")
    out = tmp_path / "sweep.json"

    assert replay_sweep.main(["--pools", str(pools), "--golden", str(golden), "--json", str(out)]) == 1
    results = json.loads(out.read_text(encoding="utf-8"))
    assert [r["label"] for r in results] == [c.label for c in candidate_configs(_settings())]
    assert {(r["failures"], r["n"]) for r in results} == {(1, 1)}
    assert len(capsys.readouterr().out.splitlines()) == 2 + len(results)


# ------------------------------------- passage gold & #1310 attribution (#1317)
def _passage_entry(
    row_id: str,
    query: str,
    doc: str,
    gold_chunk_ids: list[str],
    *,
    query_class: str = "diagnostic",
    heading: str | None = "Part A > Valves",
    truncated: bool = False,
    overlap: float | None = 0.5,
) -> GoldenEntry:
    return GoldenEntry(
        id=row_id,
        query=query,
        expected_doc_ids=[doc],
        expected_heading=heading,
        query_class=query_class,
        source="generated",
        status="adjudicated",
        reviewers=["alice"],
        reviewed_by="alice",
        reviewed_at="2026-11-21",
        gold_chunk_ids=gold_chunk_ids,
        gold_chunk_ids_truncated=truncated,
        section_chunk_count=len(gold_chunk_ids) + (5 if truncated else 0),
        lexical_overlap=overlap,
    )


def test_replay_score_hits_and_run_config_support_gold_chunk_ids_without_heading_join():
    # Plain entries without gold_chunk_ids emit no passage summary block.
    plain = GoldenEntry(id="P-1", query="q", expected_doc_ids=["D1"], query_class="syntax")
    plain_rec = _record(["c1"], docs={"c1": "D1"})
    plain_res = run_config([plain_rec], {"q": plain}, SweepConfig(label="p"), _settings())
    assert "passage" not in plain_res

    # Entry with gold_chunk_ids: sibling c_sib at rank 1, source c_src at rank 3.
    entry = _passage_entry("GEN-1", "q", "D1", ["c_src", "c_sib"], overlap=0.2)
    rec = _record(["c_sib", "c_other", "c_src"], docs={"c_sib": "D1", "c_other": "D2", "c_src": "D1"})
    # Ensure distinct physical pages so max_per_page=1 does not defer c_src.
    rec["chunks"]["c_sib"]["page_start"] = 1
    rec["chunks"]["c_other"]["page_start"] = 2
    rec["chunks"]["c_src"]["page_start"] = 3
    res = run_config([rec], {"q": entry}, SweepConfig(label="p", max_per_doc=3), _settings())
    assert res["recall@1"] == 1.0
    passage = res["passage"]
    assert passage["checked"] == 1
    assert passage["chunk"]["recall@1"] == 0.0
    assert passage["chunk"]["recall@5"] == 1.0
    assert passage["chunk"]["mrr"] == round(1 / 3, 3)
    assert passage["section"]["recall@1"] == 1.0
    assert passage["section"]["mrr"] == 1.0
    assert passage["by_overlap_band"]["low"]["n"] == 1


def test_attribution_classifies_hit_a_b_c_and_unclassifiable_across_1310_cuts():
    from scripts.replay_sweep import attribute_config, attribution_configs

    # 35 unique chunks on distinct docs/pages so RRF order equals dense order.
    ids = [f"c{i:02d}" for i in range(35)]
    base_docs = {cid: f"DOC-{i:02d}" for i, cid in enumerate(ids)}

    def make_rec(query: str) -> dict[str, Any]:
        legs = [{"effective_text": query,
                 "dense": [_cp(cid, base_docs[cid], page=str(i), page_start=i) for i, cid in enumerate(ids)],
                 "sparse": []}]
        return legs_to_record(query, "nl", legs, {}, {"depth": 40})

    entries = {
        # Source c00 is rank 1 -> hit at every cut.
        "q-hit": _passage_entry("GEN-HIT", "q-hit", "DOC-00", ["c00", "c01"], overlap=0.8),
        # Source c25 is rank 26 (below 8/3 and 16/5, hit at 32/5), sibling c02 is rank 3 -> 'a' at 8/3 & 16/5 with source_in_pool=True!
        "q-a": _passage_entry("GEN-A", "q-a", "DOC-25", ["c25", "c02"], overlap=0.45, truncated=True),
        # Source c10 is rank 11, no sibling in hits -> 'b' at 8/3, 'hit' at 16/5 & 32/5!
        "q-b": _passage_entry("GEN-B", "q-b", "DOC-10", ["c10", "c99"], overlap=0.15, heading=None),
        # Source c_absent not in pool at all -> 'c' at every cut.
        "q-c": _passage_entry("GEN-C", "q-c", "DOC-99", ["c_absent", "c_absent_sib"], query_class="syntax"),
        # Doc-only entry without gold_chunk_ids -> unclassifiable.
        "q-plain": GoldenEntry(id="DOC-ONLY", query="q-plain", expected_doc_ids=["DOC-00"], query_class="diagnostic"),
        # Abstain entry -> unclassifiable.
        "q-abstain": GoldenEntry(id="NEG-1", query="q-abstain", expected_behavior="abstain", query_class="negative"),
    }
    records = [make_rec(q) for q in entries]

    configs = attribution_configs(_settings())
    assert [(c.label, c.limit, c.max_per_doc, c.max_per_page) for c in configs] == [
        ("16/5", 16, 5, 1),
        ("8/3", 8, 3, 1),
        ("32/5", 32, 5, 1),
    ]
    by_cut = {c.label: attribute_config(records, entries, c, _settings()) for c in configs}

    assert by_cut["8/3"]["counts"] == {
        "hit": 1, "a": 1, "b": 1, "c": 1, "unclassifiable": 2, "a_also_in_pool": 1,
    }
    assert by_cut["8/3"]["row_ids"] == {
        "hit": ["GEN-HIT"], "a": ["GEN-A"], "b": ["GEN-B"], "c": ["GEN-C"],
        "unclassifiable": ["DOC-ONLY", "NEG-1"],
    }
    # At 16/5, GEN-B (rank 11) moves from b -> hit; GEN-A stays 'a'.
    assert by_cut["16/5"]["counts"]["hit"] == 2
    assert by_cut["16/5"]["counts"]["a"] == 1
    assert by_cut["16/5"]["counts"]["b"] == 0
    # At 32/5, GEN-A (rank 26) moves from a -> hit.
    assert by_cut["32/5"]["counts"]["hit"] == 3
    assert by_cut["32/5"]["counts"]["a"] == 0
    assert by_cut["16/5"]["truncated"] == 1 and by_cut["16/5"]["truncated_ids"] == ["GEN-A"]
    assert by_cut["16/5"]["unoutlined"] == 1 and by_cut["16/5"]["unoutlined_ids"] == ["GEN-B"]


def test_replay_sweep_cli_attribute_mode_is_text_free_and_honors_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("VENUE", raising=False)
    monkeypatch.setattr("mainframe_rag.config.load_settings", lambda: _settings())
    sentinel_query = "How does secret_quibble_lever_99 open the valve?"
    sentinel_heading = "Secret_Heading_Zorblatt > Sub"
    entry = _passage_entry("GEN-77", sentinel_query, "D1", ["c0", "c1"], heading=sentinel_heading)
    pools = tmp_path / "pools.jsonl"
    rec = legs_to_record(
        sentinel_query, "nl",
        [{"effective_text": sentinel_query, "dense": [_cp("c1", "D1", "1", page_start=1)], "sparse": []}],
        {}, {"depth": 40},
    )
    pools.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    golden = tmp_path / "adjudicated.jsonl"
    golden.write_text(json.dumps(entry.model_dump()) + "\n", encoding="utf-8")
    out = tmp_path / "attr.json"

    rc = replay_sweep.main(["--pools", str(pools), "--golden", str(golden), "--attribute", "--json", str(out)])
    assert rc == 0
    captured = capsys.readouterr()
    raw_json = out.read_text(encoding="utf-8")
    # Text-free contract: no query text or heading leaks to stdout or JSON output.
    for leak in (sentinel_query, "secret_quibble_lever_99", sentinel_heading, "Secret_Heading_Zorblatt"):
        assert leak not in captured.out
        assert leak not in raw_json
    parsed = json.loads(raw_json)
    assert [r["label"] for r in parsed] == ["16/5", "8/3", "32/5"]
    assert parsed[0]["counts"]["a"] == 1 and parsed[0]["row_ids"]["a"] == ["GEN-77"]


def test_replay_sweep_refuses_a_generated_set_json_path_inside_a_git_work_tree(tmp_path, monkeypatch, capsys):
    """The --json refusal named as uncovered in #1330: checked before any
    replay, one fixed message, no file, for the sweep and for --attribute."""
    from mainframe_rag.eval.datasets import GENERATED_REPORT_OUTPUT_REFUSAL

    monkeypatch.delenv("VENUE", raising=False)
    monkeypatch.setattr("mainframe_rag.config.load_settings", lambda: _settings())
    query = "How does secret_quibble_lever_99 open the valve?"
    pools = tmp_path / "pools.jsonl"
    rec = legs_to_record(
        query, "nl",
        [{"effective_text": query, "dense": [_cp("c0", "D1", "1", page_start=1)], "sparse": []}],
        {}, {"depth": 40},
    )
    pools.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    golden = tmp_path / "adjudicated.jsonl"
    golden.write_text(
        json.dumps(_passage_entry("GEN-77", query, "D1", ["c0", "c1"]).model_dump()) + "\n", encoding="utf-8")
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    out = checkout / "attr.json"
    replayed: list[object] = []
    monkeypatch.setattr(replay_sweep, "attribute_config", lambda *a, **k: replayed.append(a))
    monkeypatch.setattr(replay_sweep, "run_config", lambda *a, **k: replayed.append(a))

    base = ["--pools", str(pools), "--golden", str(golden), "--json", str(out)]
    assert replay_sweep.main([*base, "--attribute"]) == 2
    assert replay_sweep.main(base) == 2
    captured = capsys.readouterr()
    assert captured.err.count(GENERATED_REPORT_OUTPUT_REFUSAL) == 2
    assert "secret_quibble_lever_99" not in captured.out + captured.err
    assert not out.exists() and replayed == []
