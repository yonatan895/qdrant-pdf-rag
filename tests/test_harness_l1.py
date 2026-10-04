"""Unit tests for canonical evaluation statistics, retrieval and promotion,
plus operational harness baseline round-trip and snapshot behavior.

Hermetic: no Qdrant, no vLLM — every helper under test is pure."""

from __future__ import annotations

import json

import pytest
from scripts.harness import (
    baseline_path_for,
    load_baseline,
    pin_snapshot,
    resolve_snapshot_action,
    restore_snapshot,
    save_baseline,
    snapshot_fingerprint,
)

from mainframe_rag.eval.datasets import GoldenEntry
from mainframe_rag.eval.promotion import DEFAULT_CLASS_FLOOR, gate_verdict
from mainframe_rag.eval.retrieval import aggregate, collect_rows, score_row
from mainframe_rag.eval.statistics import ci95, ci95_paired, ci_excludes_zero


def test_collect_rows_uses_exact_order_depth_and_propagates_errors(monkeypatch):
    from mainframe_rag.retrieve import query
    from mainframe_rag.retrieve.query import SearchHit

    client, embedder, settings = object(), object(), object()
    calls = []
    entries = [GoldenEntry(id="row-a", query="first", expected_doc_ids=["A"]),
               GoldenEntry(id="row-b", query="second", expected_doc_ids=["B"])]
    hit = SearchHit(chunk_id="original", doc_id="A", text="Original evidence", score=1.0,
                    cite="A p. 1", heading="Example", title="Original", page_label="1",
                    chunk_type="narrative", message_ids=())

    def search(*args, **kwargs):
        calls.append((args, kwargs))
        return [hit], "nl", {}

    monkeypatch.setattr(query, "search", search)
    rows = collect_rows(entries, client, embedder, "original", settings)
    assert calls == [((client, embedder, "original", entry.query),
                      {"limit": 8, "settings": settings}) for entry in entries]
    assert [row["id"] for row in rows] == ["row-a", "row-b"]
    assert [row["recall@5"] for row in rows] == [1.0, 0.0]

    def fail(*args, **kwargs):
        raise RuntimeError("original query failure")

    monkeypatch.setattr(query, "search", fail)
    with pytest.raises(RuntimeError, match="original query failure"):
        collect_rows(entries, client, embedder, "original", settings)


# ---------------------------------------------------------------- bootstrap
def test_ci95_seeded_deterministic():
    values = [0.5, 0.6, 0.4, 0.7, 0.3, 0.55]
    a = ci95(values, resamples=500, seed=7)
    b = ci95(values, resamples=500, seed=7)
    assert a == b
    lo, hi = a
    assert 0.0 <= lo <= hi <= 1.0


def test_ci95_known_distribution_orders_as_expected():
    tight = ci95([0.5] * 100, resamples=200, seed=1)
    wide = ci95([0.0, 1.0] * 50, resamples=200, seed=1)
    assert tight[1] - tight[0] < wide[1] - wide[0]
    lo, hi = tight
    assert abs(lo - 0.5) < 0.01 and abs(hi - 0.5) < 0.01


def test_ci95_paired_positive_shift_excludes_zero():
    pairs = [(b + 0.2, b) for b in [0.1 * i for i in range(30)]]
    ci = ci95_paired(pairs, resamples=500, seed=3)
    assert ci is not None and ci[0] > 0.0
    assert ci_excludes_zero(ci, improvement=True)


def test_ci95_paired_noise_straddles_zero():
    # symmetric +/-: mean delta 0, CI straddles zero -> no merge signal
    pairs = [(0.5 + d, 0.5 - d) for d in [0.1, -0.1] * 15]
    ci = ci95_paired(pairs, resamples=1000, seed=5)
    assert not ci_excludes_zero(ci, improvement=True)
    assert not ci_excludes_zero(ci, improvement=False)


def test_ci95_empty_is_none():
    assert ci95([]) is None
    assert ci95_paired([]) is None


def test_ci95_regression_pinned_seeded_values():
    # Pin the exact seeded bounds (not just determinism-vs-itself): stdlib
    # random.Random(0) is stable, so these are cross-machine constants. A
    # change here means the resampling behavior changed — update deliberately.
    values = [0.42, 0.51, 0.38, 0.60, 0.45, 0.49, 0.52, 0.40]
    assert ci95(values, resamples=1000, seed=0) == (0.42625, 0.52125)


# ------------------------------------------------------------------- L1 rows
class _Hit:
    def __init__(self, doc_id, heading="Chapter", page_label="1", score=0.9, message_ids=()):
        self.doc_id = doc_id
        self.heading = heading
        self.page_label = page_label
        self.score = score
        self.message_ids = tuple(message_ids)


def _entry(**kw):
    base = {
        "id": "MSG-01",
        "query": "What does IEA500I report?",
        "query_class": "message_id",
        "expected_behavior": "answer",
        "expected_doc_ids": ["SA38-0673-70"],
    }
    base.update(kw)
    return GoldenEntry(**base)


def test_score_row_full_graded_match():
    e = _entry(expected_heading="IEA500I", expected_page="2-14")
    hits = [_Hit("SA38-0673-70", heading="IEA500I", page_label="2-14", score=1.0)]
    row = score_row(hits, e)
    assert row["recall@5"] == 1.0 and row["recall@8"] == 1.0 and row["mrr"] == 1.0
    assert row["ndcg@8"] == 1.0  # max gain at rank 1 == ideal


def test_score_row_doc_only_hit_partial_ndcg_strict_recall():
    e = _entry(expected_heading="IEA500I", expected_page="2-14")
    hits = [_Hit("SA38-0673-70", heading="Other", page_label="9-9", score=0.9)]
    row = score_row(hits, e)
    # recall stays strict (doc + heading substring, same as the retrieval eval);
    # nDCG gives graded credit for the doc-level hit (gain 1 of max 3)
    assert row["recall@5"] == 0.0
    assert 0.0 < row["ndcg@8"] < 1.0


def test_score_row_abstain_excluded_but_traps_checked():
    e = _entry(id="NEG-08", expected_behavior="abstain", expected_doc_ids=[],
               must_not_message_ids=["IEC070I"])
    hits = [_Hit("SA38-0674-70", message_ids=("IEC070I",), score=0.7)]
    row = score_row(hits, e)
    assert "recall@5" not in row and "ndcg@8" not in row
    assert row["violations"]  # sibling-only bait chunk violates


def test_score_row_cocarrying_chunk_not_a_violation():
    e = _entry(id="MSG-25", query="IOS207I rejected a command",
               must_not_message_ids=["IOS208I"])
    hits = [
        _Hit("SA38-0676-70", message_ids=("IOS207I",), score=1.0),
        _Hit("SA38-0676-07", message_ids=("IOS207I", "IOS208I"), score=0.6),
    ]
    row = score_row(hits, e)
    assert "violations" not in row


def test_score_row_ndcg_dedupes_per_doc_never_exceeds_one():
    # N chunks of the SAME expected doc must not push nDCG above 1 (the
    # doc-level ranking dedupes; IDCG assumes every expected doc at max gain)
    e = _entry(expected_doc_ids=["SA38-0673-70"])
    hits = [_Hit("SA38-0673-70", score=1.0 - 0.01 * i) for i in range(8)]
    row = score_row(hits, e)
    assert 0.0 < row["ndcg@8"] <= 1.0
    assert row["ndcg@8"] == 1.0  # single expected doc found at rank 1 = perfect


def test_score_row_ndcg_multi_doc_ideal():
    # Gold has 3 docs; deduped doc ranking is D1(rank1), D9(rank2, gain 0),
    # D2(rank3): DCG = 1/log2(2) + 1/log2(4) = 1.5; IDCG (3 ideal positions,
    # max gain 1) = 1/log2(2)+1/log2(3)+1/log2(4) = 2.1309 -> nDCG = 0.7039
    e = _entry(expected_doc_ids=["D1", "D2", "D3"])
    hits = [_Hit("D1", score=1.0), _Hit("D9", score=0.9), _Hit("D2", score=0.8)]
    row = score_row(hits, e)
    assert row["ndcg@8"] == pytest.approx(0.7039, abs=0.001)


def test_aggregate_per_class_never_aggregate_only():
    rows = [
        score_row([_Hit("D1")], _entry(id="A-1", query_class="message_id", expected_doc_ids=["D1"])),
        score_row([], _entry(id="A-2", query_class="message_id", expected_doc_ids=["D1"])),
        score_row([_Hit("D2")], _entry(id="B-1", query_class="syntax", expected_doc_ids=["D2"])),
    ]
    agg = aggregate(rows)
    assert set(agg["classes"]) == {"message_id", "syntax"}
    assert agg["classes"]["message_id"]["recall@5"] == 0.5
    assert agg["classes"]["syntax"]["recall@5"] == 1.0
    assert agg["overall"]["recall@5"] == 0.6667  # aggregate rounds to 4dp
    assert set(agg["per_query"]) == {"A-1", "A-2", "B-1"}


def test_aggregate_trap_precision_absolute():
    rows = [
        score_row([_Hit("D1")], _entry(id="A-1", expected_doc_ids=["D1"])),
        score_row([_Hit("X1", message_ids=("IEC070I",))],
                  _entry(id="NEG-08", query_class="negative", expected_behavior="abstain",
                         expected_doc_ids=[], must_not_message_ids=["IEC070I"])),
    ]
    agg = aggregate(rows)
    assert agg["traps"]["failed"] == ["NEG-08"]
    assert agg["traps"]["precision"] == 0.5


# ------------------------------------------------------------- gate verdict
def _summary(recall5=0.8, mrr=0.7, trap_failed=None, classes=None):
    n = 10
    per_query = {f"E{i:02d}": {"recall@5": recall5, "mrr": mrr} for i in range(n)}
    return {
        "overall": {"n": n, "scored": n, "recall@5": recall5, "mrr": mrr},
        "classes": classes or {"message_id": {"n": n, "scored": n, "recall@5": recall5, "mrr": mrr}},
        "traps": {"checked": n, "failed": trap_failed or [], "precision": 1.0},
        "per_query": per_query,
    }


def _baseline(summary, floor=DEFAULT_CLASS_FLOOR):
    return {"_meta": {"class_regression_floor": floor}, **summary}


def test_gate_no_baseline_records():
    verdict, reasons = gate_verdict(_summary(), None)
    assert verdict == "baseline"
    assert any("first baseline" in r for r in reasons)


def test_gate_identical_run_holds_no_improvement():
    verdict, reasons = gate_verdict(_summary(), _baseline(_summary()))
    assert verdict == "hold"
    assert any("CI overlap" in r for r in reasons)


def test_gate_improvement_merges():
    base = _summary(recall5=0.6, mrr=0.5)
    cand = _summary(recall5=0.8, mrr=0.7)
    verdict, reasons = gate_verdict(cand, _baseline(base), resamples=400)
    assert verdict == "merge", reasons
    assert any("recall@5" in r for r in reasons)


def test_gate_trap_failure_holds_even_with_improvement():
    base = _summary(recall5=0.6)
    cand = _summary(recall5=0.9, trap_failed=["NEG-08"])
    verdict, reasons = gate_verdict(cand, _baseline(base), resamples=400)
    assert verdict == "hold"
    assert any("P0" in r for r in reasons)


def test_gate_class_regression_holds_despite_overall_gain():
    base = _summary(recall5=0.6, classes={
        "message_id": {"n": 10, "scored": 10, "recall@5": 0.9, "mrr": 0.9},
        "comparative": {"n": 10, "scored": 10, "recall@5": 0.3, "mrr": 0.3},
    })
    cand = _summary(recall5=0.7, classes={
        "message_id": {"n": 10, "scored": 10, "recall@5": 0.95, "mrr": 0.95},
        "comparative": {"n": 10, "scored": 10, "recall@5": 0.2, "mrr": 0.2},
    })
    verdict, reasons = gate_verdict(cand, _baseline(base), resamples=400)
    assert verdict == "hold"
    assert any("comparative" in r for r in reasons)


def test_gate_small_regression_within_floor_merges():
    base = _summary(recall5=0.6, classes={
        "message_id": {"n": 10, "scored": 10, "recall@5": 0.5, "mrr": 0.5},
    })
    cand = _summary(recall5=0.8, classes={
        "message_id": {"n": 10, "scored": 10, "recall@5": 0.45, "mrr": 0.45},
    })
    verdict, _ = gate_verdict(cand, _baseline(base), resamples=400)
    assert verdict == "merge"


def test_gate_golden_entry_mismatch_holds():
    # A dev run (golden only) must not be scored against an RC baseline
    # (golden + holdout): the paired deltas would cover a truncated venue.
    base = _baseline(_summary())
    base["_meta"]["golden_entries"] = 177
    cand = _summary(recall5=0.9, mrr=0.8)
    verdict, reasons = gate_verdict(cand, base, resamples=400)
    assert verdict == "hold"
    assert any("golden entry-set mismatch" in r for r in reasons)


def test_gate_matching_golden_count_still_gates():
    base = _baseline(_summary(recall5=0.6, mrr=0.5))
    base["_meta"]["golden_entries"] = 10
    cand = _summary(recall5=0.8, mrr=0.7)
    verdict, reasons = gate_verdict(cand, base, resamples=400)
    assert verdict == "merge", reasons


# ------------------------------------------------------- baseline round-trip
def test_baseline_round_trip(tmp_path):
    summary = _summary()
    path = tmp_path / "harness-vllm.json"
    save_baseline(path, summary, embed_mode="vllm",
                  snapshot={"points_count": 840396, "snapshot_name": "mainframe_manuals-1.snapshot"})
    loaded = load_baseline(path)
    assert loaded is not None
    assert loaded["_meta"]["embed_mode"] == "vllm"
    assert loaded["_meta"]["snapshot"]["points_count"] == 840396
    assert loaded["_meta"]["golden_entries"] == summary["traps"]["checked"]
    assert loaded["per_query"] == summary["per_query"]
    # the stored per-query values must be exactly what the gate pairs against
    verdict, _ = gate_verdict(_summary(recall5=0.95, mrr=0.9), loaded, resamples=300)
    assert verdict == "merge"


def test_baseline_path_mode_keyed(tmp_path):
    assert baseline_path_for(tmp_path, "hash").name == "harness.json"
    assert baseline_path_for(tmp_path, "vllm").name == "harness-vllm.json"


def test_baseline_json_is_utf8_and_stable(tmp_path):
    summary = _summary()
    path = tmp_path / "b.json"
    save_baseline(path, summary, embed_mode="vllm", snapshot={"points_count": 1})
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["overall"]["recall@5"] == summary["overall"]["recall@5"]


# ------------------------------------------------------- snapshot policy matrix
class _Snap:
    def __init__(self, name: str, creation_time: str):
        self.name = name
        self.creation_time = creation_time


class _Collection:
    def __init__(self, points: int):
        self.points_count = points


class _FakeQdrant:
    """Minimal qdrant-client stand-in for the pin/restore helpers."""

    def __init__(self, points: int, snaps: list[_Snap]):
        self.points = points
        self.snaps = list(snaps)
        self.deleted: list[str] = []
        self.recovered: list[str] = []

    def get_collection(self, _c):
        return _Collection(self.points)

    def list_snapshots(self, _c):
        return list(self.snaps)

    def create_snapshot(self, _c, wait=True):
        s = _Snap(f"c-{len(self.snaps)}-2026-09-03-00-00-0{len(self.snaps)}.snapshot", f"2026-09-03T00:00:0{len(self.snaps)}")
        self.snaps.append(s)
        return s

    def delete_snapshot(self, _c, name):
        self.deleted.append(name)
        self.snaps = [s for s in self.snaps if s.name != name]

    def recover_snapshot(self, _c, location, wait=True):
        self.recovered.append(location)
        return True


def test_fingerprint_orders_by_creation_time_not_name():
    # Names sort by checksum segment; ordering must follow creation_time.
    c = _FakeQdrant(100, [
        _Snap("c-zzz-2026-09-02-10-00-00.snapshot", "2026-09-02T10:00:00"),
        _Snap("c-aaa-2026-09-02-09-00-00.snapshot", "2026-09-02T09:00:00"),
    ])
    fp = snapshot_fingerprint(c, "coll")
    assert fp["snapshot_names"] == [
        "c-aaa-2026-09-02-09-00-00.snapshot",
        "c-zzz-2026-09-02-10-00-00.snapshot",
    ]


def test_fingerprint_prefers_recorded_pin():
    c = _FakeQdrant(100, [
        _Snap("c-a-old.snapshot", "2026-09-02T09:00:00"),
        _Snap("c-b-pin.snapshot", "2026-09-02T11:00:00"),
    ])
    fp = snapshot_fingerprint(c, "coll", prefer_name="c-b-pin.snapshot")
    assert fp["snapshot_names"][0] == "c-b-pin.snapshot"


def test_resolve_drift_match_skips_restore():
    fp = {"points_count": 840396, "snapshot_names": ["pin.snapshot"]}
    recorded = {"points_count": 840396, "snapshot_name": "pin.snapshot"}
    assert resolve_snapshot_action(fp, recorded, restore="drift", record_mode=False) == ("skip", "pin.snapshot")


def test_resolve_drift_restores_recorded_pin_despite_drift():
    # Production shape: main() passes prefer_name=recorded, so a stray alone
    # cannot push the recorded pin out of first place (that combination is
    # the skip path). Reaching restore-with-stray requires a DRIFTED points
    # count — and the restore must still target the recorded pin, not the
    # stray that a naive fingerprint-first policy would pick.
    fp = {"points_count": 900000, "snapshot_names": ["pin.snapshot", "stray.snapshot"]}
    recorded = {"points_count": 840396, "snapshot_name": "pin.snapshot"}
    action, name = resolve_snapshot_action(fp, recorded, restore="drift", record_mode=False)
    assert (action, name) == ("restore", "pin.snapshot")


def test_resolve_drift_fails_when_recorded_pin_deleted():
    # Blocker 3: a deleted pin on a gate run must fail closed — a gate run
    # never pins live state, even at the same points count.
    fp = {"points_count": 840396, "snapshot_names": ["other.snapshot"]}
    recorded = {"points_count": 840396, "snapshot_name": "pin.snapshot"}
    action, reason = resolve_snapshot_action(fp, recorded, restore="drift", record_mode=False)
    assert action == "fail"
    assert "never pins live state" in reason


def test_resolve_drift_fails_without_recorded_pin():
    fp = {"points_count": 840396, "snapshot_names": ["whatever.snapshot"]}
    action, _ = resolve_snapshot_action(fp, None, restore="drift", record_mode=False)
    assert action == "fail"


def test_resolve_drift_record_mode_pins():
    fp = {"points_count": 840396, "snapshot_names": []}
    assert resolve_snapshot_action(fp, None, restore="drift", record_mode=True) == ("pin", None)


def test_resolve_never_skips():
    fp = {"points_count": 1, "snapshot_names": []}
    action, _ = resolve_snapshot_action(fp, None, restore="never", record_mode=False)
    assert action == "skip"


def test_resolve_always_restores_recorded_not_live():
    fp = {"points_count": 840396, "snapshot_names": ["stray.snapshot", "pin.snapshot"]}
    recorded = {"points_count": 840396, "snapshot_name": "pin.snapshot"}
    assert resolve_snapshot_action(fp, recorded, restore="always", record_mode=False) == ("restore", "pin.snapshot")
    action, _ = resolve_snapshot_action(fp, None, restore="always", record_mode=False)
    assert action == "fail"


def test_pin_snapshot_creates_when_missing():
    c = _FakeQdrant(840396, [])
    fp = pin_snapshot(c, "coll")
    assert fp["snapshot_name"].startswith("c-0-")
    assert fp["points_count"] == 840396


def test_pin_snapshot_adopts_only_on_exact_match():
    # Re-adoption happens only under the skip path's exact condition:
    # recorded pin first AND points count equal. The fake's prefer_name puts
    # the recorded pin first, mirroring the production call shape.
    recorded = "c-pin-2026-09-01.snapshot"
    c = _FakeQdrant(840396, [
        _Snap("c-x-2026-09-02.snapshot", "2026-09-02T00:00:00"),
        _Snap(recorded, "2026-09-01T00:00:00"),
    ])
    fp = pin_snapshot(c, "coll", keep=recorded, expected_points=840396)
    assert fp["snapshot_name"] == recorded
    assert fp["points_count"] == 840396
    assert c.deleted == ["c-x-2026-09-02.snapshot"]
    assert c.recovered == []  # pin never recovers


def test_pin_snapshot_drifted_record_creates_new_pin_and_prunes_old():
    # The review's remaining hole: a record run against a DRIFTED index
    # (new ingest changed the points count) must snapshot the CURRENT state
    # and delete the previous pin — recording {new points, old name} would
    # be a baseline no restore can reproduce.
    recorded = "c-old-2026-09-01.snapshot"
    c = _FakeQdrant(900000, [_Snap(recorded, "2026-09-01T00:00:00")])
    fp = pin_snapshot(c, "coll", keep=recorded, expected_points=840396)
    assert fp["points_count"] == 900000
    assert fp["snapshot_name"] != recorded
    assert c.deleted == [recorded]
    assert [s.name for s in c.snaps] == [fp["snapshot_name"]]


def test_pin_snapshot_no_recorded_pin_creates_fresh():
    # A first baseline (no recorded fingerprint at all) pins current state
    # and prunes anything pre-existing — never adopts an unknown snapshot.
    c = _FakeQdrant(840396, [_Snap("c-preexisting.snapshot", "2026-09-01T00:00:00")])
    fp = pin_snapshot(c, "coll", keep=None, expected_points=None)
    assert fp["snapshot_name"] != "c-preexisting.snapshot"
    assert c.deleted == ["c-preexisting.snapshot"]


def test_restore_snapshot_uses_recorded_name_and_dir_setting():
    c = _FakeQdrant(840396, [_Snap("c-pin.snapshot", "2026-09-01T00:00:00")])
    fp = restore_snapshot(c, "coll", "c-pin.snapshot", snapshots_dir="/snapshots")
    assert fp == {"points_count": 840396, "snapshot_name": "c-pin.snapshot"}
    assert c.recovered == ["file:///snapshots/coll/c-pin.snapshot"]


def test_restore_snapshot_fails_closed_when_missing():
    c = _FakeQdrant(840396, [])
    with pytest.raises(RuntimeError, match="missing"):
        restore_snapshot(c, "coll", "gone.snapshot")


def test_restore_snapshot_fails_when_index_does_not_roll_back():
    # recover reporting success without actually rolling the collection
    # back (post-restore points != recorded pin) must fail the run — a gate
    # on a mutated index is worse than no gate.
    c = _FakeQdrant(900000, [_Snap("c-pin.snapshot", "2026-09-01T00:00:00")])
    with pytest.raises(RuntimeError, match="did not roll back"):
        restore_snapshot(c, "coll", "c-pin.snapshot", expected_points=840396)


# --------------------------------------------------------- metric cap / ideal
def test_mrr_capped_at_limit():
    e = _entry(expected_doc_ids=["SA38-0673-70"])
    hits = [_Hit(f"OTHER{i}", score=0.9) for i in range(8)] + [_Hit("SA38-0673-70", score=0.5)]
    row = score_row(hits, e)
    assert row["mrr"] == 0.0  # relevant at rank 9 is outside MRR@8
    assert row["recall@8"] == 0.0


def test_ndcg_graded_ideal_single_max_gain_for_multi_doc():
    # Two expected docs with a singular heading gold: the ideal gives max
    # gain (2) to ONE doc and plain gain (1) to the other — not max to both.
    e = _entry(expected_doc_ids=["D1", "D2"], expected_heading="IEA794I")
    hits = [_Hit("D1", heading="IEA794I", score=1.0)]  # D2 missed
    row = score_row(hits, e)
    # deduped: D1 rank1 gain2; ideal [2,1]: (2/1) / (2/1 + 1/1.585) = 0.7604
    assert row["ndcg@8"] == pytest.approx(0.7604, abs=0.001)


def test_promotion_and_harness_share_canonical_owners():
    from scripts import harness

    from mainframe_rag.eval import promotion, statistics

    assert harness.gate_verdict is promotion.gate_verdict
    assert harness.DEFAULT_CLASS_FLOOR is promotion.DEFAULT_CLASS_FLOOR
    assert promotion.ci95_paired is statistics.ci95_paired
    assert promotion.ci_excludes_zero is statistics.ci_excludes_zero


@pytest.fixture
def harness_input_workspace(tmp_path):
    """Actual Task/CLI/scorers/verdicts; original data and inert external boundaries."""
    import hashlib
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    from tests.test_taskfile_contracts import REQUIRE_RUNNER, find_task

    task = find_task()
    if task is None:
        if REQUIRE_RUNNER:
            pytest.fail("pinned Task unavailable in required lane")
        pytest.skip("pinned Task unavailable")
    repo = Path(__file__).resolve().parents[1]
    shutil.copy2(repo / "Taskfile.yml", tmp_path / "Taskfile.yml")
    shutil.copytree(repo / "taskfiles", tmp_path / "taskfiles")
    (tmp_path / "scripts").mkdir()
    shutil.copy2(repo / "scripts/harness.py", tmp_path / "scripts/harness.py")
    (tmp_path / ".venv/bin").mkdir(parents=True)
    launcher = tmp_path / ".venv/bin/python"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import importlib.util, json, sys\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        f"sys.path.insert(0, {str(repo / 'src')!r})\n"
        "spec = importlib.util.spec_from_file_location('actual_harness_cli', sys.argv[1])\n"
        "cli = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(cli)\n"
        "import qdrant_client\n"
        "from mainframe_rag import manifest\n"
        "from mainframe_rag.ingest import embed\n"
        "from mainframe_rag.retrieve import query\n"
        "def effect(kind, value=None):\n"
        "    with Path('effects.jsonl').open('a') as f: f.write(json.dumps([kind, value])+'\\n')\n"
        "def client(**kwargs):\n"
        "    effect('client'); return object()\n"
        "qdrant_client.QdrantClient = client\n"
        "embed.build_embedder = lambda *a: object()\n"
        "manifest.write_run_manifest = lambda *a: {'git_sha': 'synthetic'}\n"
        "cli.snapshot_fingerprint = lambda *a, prefer_name=None: {'points_count': 7, 'snapshot_names': [prefer_name or 'fresh-pin']}\n"
        "cli.pin_snapshot = lambda *a, **k: {'points_count': 7, 'snapshot_name': 'fresh-pin'}\n"
        "def search(client, embedder, collection, text, **kwargs):\n"
        "    effect('query', text)\n"
        "    doc = {'Synthetic dev query': 'synthetic-dev', 'Synthetic holdout query': 'synthetic-hold'}[text]\n"
        "    return [SimpleNamespace(doc_id=doc, heading='Chapter', page_label='1', score=1.0, message_ids=())], 'nl', {}\n"
        "query.search = search\n"
        "raise SystemExit(cli.main(sys.argv[2:]))\n"
    )
    launcher.chmod(0o755)
    evals = tmp_path / "evals"
    evals.mkdir()
    for filename, identity, query in (("golden.jsonl", "dev", "Synthetic dev query"),
                                      ("holdout.jsonl", "hold", "Synthetic holdout query")):
        path = evals / filename
        path.write_text(json.dumps({"id": identity, "query": query, "query_class": "syntax",
                                    "expected_doc_ids": [f"synthetic-{identity}"]}) + "\n")
    holdout = evals / "holdout.jsonl"
    holdout.with_name("holdout.jsonl.sha256").write_text(hashlib.sha256(holdout.read_bytes()).hexdigest() + "  evals/holdout.jsonl\n")
    benchmarks = tmp_path / "benchmarks"
    benchmarks.mkdir()
    for filename, identities, pin in (("harness.json", ["dev"], "hash-pin"),
                                      ("harness-vllm.json", ["dev", "hold"], "vllm-pin")):
        (benchmarks / filename).write_text(json.dumps({
            "_meta": {"golden_entries": len(identities), "snapshot": {"points_count": 7, "snapshot_name": pin}},
            "classes": {"syntax": {"recall@5": 0.0, "mrr": 0.0}},
            "per_query": {identity: {"recall@5": 0.0, "mrr": 0.0} for identity in identities},
        }))
    before = {p: p.read_bytes() for p in benchmarks.iterdir()}
    def run(mode, venue, *, direct=False, record=False, extra=()):
        output = tmp_path / ("direct.json" if direct else "bundles/harness-report.json")
        output.unlink(missing_ok=True)
        (tmp_path / "effects.jsonl").unlink(missing_ok=True)
        env = {"PATH": os.defpath, "HOME": str(tmp_path), "QDRANT_COLLECTION": "synthetic",
               "EMBED_MODE": "hash", "VENUE": "dev", "HARNESS_BASELINE": "ignored ambient.json"}
        if direct:
            env.update(EMBED_MODE=mode, VENUE=venue)
            command = [str(launcher), "scripts/harness.py", "--update-baseline" if record else "--gate",
                       "--out", str(output), *extra]
        else:
            command = [task, "--taskfile", str(tmp_path / "Taskfile.yml"),
                       "eval:harness:baseline" if record else "eval:harness:gate",
                       f"EMBED_MODE={mode}", f"VENUE={venue}", *extra]
        result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30, check=False)
        report = json.loads(output.read_text()) if output.exists() else None
        effects_path = tmp_path / "effects.jsonl"
        effects = [json.loads(line) for line in effects_path.read_text().splitlines()] if effects_path.exists() else []
        return result, report, effects
    return run, tmp_path, before


@pytest.mark.parametrize("mode,venue,ids,pin,verdict", [
    ("hash", "dev", ["dev"], "hash-pin", "merge"),
    ("hash", "rc", ["dev"], "hash-pin", "merge"),
    ("", "rc", ["dev"], "hash-pin", "merge"),
    ("vllm", "rc", ["dev", "hold"], "vllm-pin", "merge"),
    ("VLLM", "rc", ["dev", "hold"], "vllm-pin", "merge"),
    (" VLLM ", "rc", ["dev", "hold"], "vllm-pin", "merge"),
    ("vllm", "dev", ["dev"], "vllm-pin", "hold"),
])
def test_harness_task_and_cli_select_same_mode_inputs(harness_input_workspace, mode, venue, ids, pin, verdict):
    run, _, before = harness_input_workspace
    for direct in (False, True):
        result, report, effects = run(mode, venue, direct=direct)
        assert (result.returncode == 0) == (verdict == "merge"), result.stdout + result.stderr
        assert list(report["summary"]["per_query"]) == ids
        assert report["snapshot"]["snapshot_name"] == pin
        assert report["verdict"] == verdict
        assert [row[1] for row in effects if row[0] == "query"] == [
            "Synthetic dev query" if identity == "dev" else "Synthetic holdout query" for identity in ids]
        assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("mode,ids,filename", [
    ("hash", ["dev"], "harness.json"),
    ("VLLM", ["dev", "hold"], "harness-vllm.json"),
])
def test_harness_task_records_selected_dataset_then_next_gate(harness_input_workspace, mode, ids, filename):
    run, root, before = harness_input_workspace
    result, report, _ = run(mode, "rc", record=True, extra=("HARNESS_BASELINE=ignored CLI.json",))
    assert result.returncode == 0, result.stdout + result.stderr
    selected = root / "benchmarks" / filename
    stored = json.loads(selected.read_text())
    assert list(stored["per_query"]) == ids
    assert stored["_meta"]["golden_entries"] == len(ids)
    assert stored["_meta"]["embed_mode"] == mode.lower()
    assert stored["overall"]["recall@5"] == 1.0
    for path, original in before.items():
        if path != selected:
            assert path.read_bytes() == original
    after = selected.read_bytes()
    result, report, _ = run(mode, "rc")
    assert result.returncode != 0  # No improvement over itself: valid promotion hold.
    assert report["verdict"] == "hold"
    assert report["reasons"] == ["no primary metric improved beyond CI overlap (recall@5, mrr)"]
    assert list(report["summary"]["per_query"]) == ids
    assert selected.read_bytes() == after
    assert not (root / "ignored CLI.json").exists()
    assert not (root / "ignored ambient.json").exists()


def test_harness_direct_explicit_dataset_and_baseline_remain_authoritative(harness_input_workspace):
    run, root, before = harness_input_workspace
    explicit = root / "custom אב;$(touch SENTINEL).json"
    explicit.write_bytes((root / "benchmarks/harness-vllm.json").read_bytes())
    original = explicit.read_bytes()
    result, report, _ = run("hash", "rc", direct=True, extra=(
        "--golden", "evals/golden.jsonl", "--golden", "evals/holdout.jsonl", "--baseline", str(explicit)))
    assert result.returncode == 0, result.stdout + result.stderr
    assert list(report["summary"]["per_query"]) == ["dev", "hold"]
    assert report["snapshot"]["snapshot_name"] == "vllm-pin"
    assert explicit.read_bytes() == original
    assert {path: path.read_bytes() for path in before} == before
    assert not (root / "SENTINEL").exists()


@pytest.mark.parametrize("fault", ["missing_baseline", "bad_pin"])
def test_harness_task_refuses_invalid_inputs_before_external_effects(harness_input_workspace, fault):
    run, root, before = harness_input_workspace
    if fault == "missing_baseline":
        (root / "benchmarks/harness-vllm.json").unlink()
    else:
        (root / "evals/holdout.jsonl").write_text("corrupt before parsing\n")
    result, report, effects = run("VLLM", "rc")
    assert result.returncode != 0
    assert report is None
    assert effects == []
    assert (root / "benchmarks/harness.json").read_bytes() == before[root / "benchmarks/harness.json"]
