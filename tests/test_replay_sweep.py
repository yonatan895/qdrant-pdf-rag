"""Unit tests for scripts/replay_sweep.py (hermetic, no GPU/network)."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from scripts.capture_pool import legs_to_record
from scripts.eval_retrieval import GoldenEntry
from scripts.replay_sweep import SweepConfig, replay_record, score_hits, summarize

from mainframe_rag.config import Settings
from tests.conftest import _point


def _cp(pid, doc, page="1", ctype="narrative"):
    base = _point(pid)
    payload = dict(base.payload or {})
    payload.update({"doc_id": doc, "page_label": page, "chunk_type": ctype})
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


def _record(dense_ids, ce=None, meta=None, second_leg=None):
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
