"""Unit tests for scripts/replay_sweep.py (hermetic, no GPU/network)."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from scripts.capture_pool import legs_to_record
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

from mainframe_rag.config import Settings
from mainframe_rag.eval.datasets import GoldenEntry
from tests.conftest import _point


def _cp(pid, doc, page="1", ctype="narrative", page_start=5):
    """Shared point; page_start=None makes a legacy point without a physical page."""
    base = _point(pid)
    payload = dict(base.payload or {})
    payload.update({"doc_id": doc, "page_label": page, "chunk_type": ctype, "page_start": page_start})
    if page_start is None:
        del payload["page_start"]
    return base.model_copy(update={"payload": payload})


def _settings(**overrides):
    kw = {
        "qdrant_url": "http://localhost:6333",
        "qdrant_collection": "mainframe_manuals",
        "dense_dim": 768,
        "embed_base_url": "http://localhost:8000/v1",
        "embed_model": "test-embed",
        "bm25_model": "Qdrant/bm25",
        "_env_file": None,
    }
    kw.update(overrides)
    return Settings(**kw)


def _record(dense_ids, ce=None, meta=None, second_leg=None, docs=None):
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


def test_replay_rerank_refuses_unscored_pool():
    record = _record(["c1", "c2"], {}, {"ce_scored": True})
    config = SweepConfig(label="alpha", rerank=True, fuse_limit=50)
    with pytest.raises(ValueError, match="no CE score"):
        replay_record(record, config, _settings())


def test_replay_fuse_limit_truncates_candidates():
    record = _record([f"c{i}" for i in range(5)])
    base = SweepConfig(label="prod")
    assert len(replay_record(record, replace(base, fuse_limit=5), _settings())) == 5
    assert len(replay_record(record, replace(base, fuse_limit=2), _settings())) == 2


def test_replay_prefetch_limit_trims_deep_capture():
    record = _record([f"c{i}" for i in range(5)])
    hits = replay_record(record, replace(SweepConfig(label="t"), prefetch_limit=1), _settings())
    assert [h.chunk_id for h in hits] == ["c0"]


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
        {"split_mode": "comparative"},
    )
    config = SweepConfig(label="t")
    assert replay_record(rec, config, _settings())[0].chunk_id == "a"
    rec["_meta"]["split_mode"] = "diagnostic"
    assert replay_record(rec, config, _settings())[0].chunk_id == "b"


def test_score_hits_doc_level_recall_and_must_not():
    entry = GoldenEntry(
        query="q", expected_doc_ids=["D"], must_not_retrieve=["X"], query_class="syntax"
    )
    row = score_hits([SimpleNamespace(doc_id="X"), SimpleNamespace(doc_id="D")], entry)
    assert row is not None
    assert row["recall@1"] == 0.0 and row["recall@5"] == 1.0 and row["mrr"] == 0.5
    assert row["violations"] == ["X"]
    abstain = GoldenEntry(query="trap", expected_behavior="abstain", query_class="negative")
    assert score_hits([], abstain) is None


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


def test_replay_refuses_prefetch_deeper_than_recorded_pool():
    deep = replace(SweepConfig(label="t"), prefetch_limit=100)
    with pytest.raises(ValueError, match="exceeds the recorded pool depth 50"):
        replay_record(_record(["c0"], meta={"depth": 50}), deep, _settings())
    # Legacy pool (no recorded depth) proves only its longest leg.
    with pytest.raises(ValueError, match="recorded pool depth 2"):
        replay_record(_record(["c0", "c1"]), deep, _settings())
    shallower = replace(SweepConfig(label="t"), prefetch_limit=2)
    assert [h.chunk_id for h in replay_record(_record(["c0", "c1"]), shallower, _settings())] == ["c0", "c1"]


def test_unsupported_configs_are_skipped_not_scored():
    records = [_record(["c0"], meta={"depth": 50}), {"query": "broken", "error": "x"}]
    deep = replace(SweepConfig(label="deep"), prefetch_limit=100)
    assert unsupported_reason(records, SweepConfig(label="p")) is None
    assert unsupported_reason(records, deep) == "pool depth 50 < prefetch 100"
    assert unsupported_reason([_record(["c0"])], deep) == "pool depth 1 < prefetch 100"
    result = run_config(records, {}, deep, _settings())
    assert result == {"label": "deep", "scoring": "doc", "skipped": "pool depth 50 < prefetch 100"}


def test_rerank100_replays_a_deep_scored_pool():
    ids = [f"c{i:03d}" for i in range(120)]
    ce = {cid: float(i) / 1000 for i, cid in enumerate(ids[:100])}  # capture stops CE at 100
    record = _record(ids, ce=ce, meta={"depth": 120, "ce_scored": True, "ce_depth": 100})
    config = next(c for c in candidate_configs(_settings()) if c.label == "rerank100")
    assert config.rerank and config.prefetch_limit == 100
    hits = replay_record(record, config, _settings())
    assert hits[0].chunk_id == "c099"  # the deepest scored candidate wins on CE alone (alpha 1.0)


def test_score_hits_section_level_requires_expected_heading():
    entry = GoldenEntry(query="q", expected_doc_ids=["D"], expected_heading="Status subparameter",
                        query_class="syntax")
    wrong = SimpleNamespace(doc_id="D", heading="Book > DISP parameter > Example of the DEST parameter")
    right = SimpleNamespace(doc_id="D", heading="Book > DISP parameter > Status subparameter")
    doc_row = score_hits([wrong, right], entry)
    assert doc_row is not None and doc_row["recall@1"] == 1.0  # doc-level ignores the heading
    section_row = score_hits([wrong, right], entry, section_level=True)
    assert section_row is not None
    assert section_row["recall@1"] == 0.0 and section_row["mrr"] == 0.5


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
    record = legs_to_record("q", "nl", [{"effective_text": "q", "dense": pts, "sparse": []}], {}, {})
    config = SweepConfig(label="p", max_per_page=1, max_per_doc=3)
    # The same-page "c" is deferred behind the next page, not ranked second.
    assert [h.chunk_id for h in replay_record(record, config, _settings())] == ["a", "b", "c"]
    legacy_pts = [_cp(p, "D", "", page_start=None) for p in "acb"]
    legacy = legs_to_record("q", "nl", [{"effective_text": "q", "dense": legacy_pts, "sparse": []}], {}, {})
    # Without a physical page every unlabelled chunk shares one bucket: RRF order stands.
    assert [h.chunk_id for h in replay_record(legacy, config, _settings())] == ["a", "c", "b"]


def test_join_headings_refuses_a_moved_physical_page():
    pts = [_cp("c1", "D", "", page_start=5)]
    record = legs_to_record("q", "nl", [{"effective_text": "q", "dense": pts, "sparse": []}], {}, {})
    same = {"c1": {"doc_id": "D", "page_label": "", "page_start": 5, "heading_path": "H"}}
    assert join_headings([record], _RetrieveClient(same), "local") == {"c1": "H"}
    moved = {"c1": {**same["c1"], "page_start": 6}}
    with pytest.raises(HeadingJoinError, match="1 with a different"):
        join_headings([record], _RetrieveClient(moved), "local")
