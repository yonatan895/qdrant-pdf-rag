"""Release-set mechanics and pre-registered acceptance scoring (issue #367).

Synthetic fixtures only: invented queries, doc ids and dates. Nothing here
implies any manual content; the real independent set is SME-authored outside
this repository.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mainframe_rag.eval import acceptance as acc
from mainframe_rag.eval.datasets import (
    DatasetError,
    VenueError,
    parse_golden_text,
    read_golden_text,
    require_rc_for_golden,
    resolve_golden_paths,
)

CRITERIA_PATH = Path(__file__).resolve().parents[1] / "evals" / "acceptance-criteria.json"
PILOT_CLASSES = ["message_id"] * 5 + ["version"] * 5 + ["syntax"] * 5 + ["table"] * 5 + ["diagnostic"] * 4


def _criteria_doc(status: str = "registered", **stage_over) -> dict:
    doc = json.loads(CRITERIA_PATH.read_text())
    if status == "registered":
        doc.update(status="registered", registered_by="sme-reviewer", registered_at="2026-10-04")
    doc["stages"]["pilot"].update(stage_over)
    return doc


def _write_criteria(tmp: Path, doc: dict) -> tuple[Path, str]:
    path = tmp / "criteria.json"
    data = json.dumps(doc).encode()
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest()


def _case(i: int, qclass: str, behavior: str = "answer", author: str = "Ann", adjudicator: str = "Bob") -> dict:
    row = {
        "record": "case", "id": f"C{i:03d}", "query": f"synthetic question number {i} about widget {i}",
        "query_class": qclass, "expected_behavior": behavior,
        "provenance": {"author": author, "authored_at": "2026-10-01", "source": "synthetic fixture", "release": "R1"},
        "adjudication": {"adjudicator": adjudicator, "adjudicated_at": "2026-10-02", "verdict": "accepted"},
    }
    if behavior == "answer":
        row.update(
            expected_doc_ids=[f"DOC-{i}"],
            evidence=[{"doc_id": f"DOC-{i}", "release": "R1", "physical_page": i + 1, "locator": f"sec {i}"}],
            required_facts=[f"fact {i}"],
        )
    else:
        row.update(abstain_reason="wrong_premise", must_not_assert=[f"invented step {i}"])
    return row


def _manifest(criteria_sha: str, stage: str = "pilot") -> dict:
    return {
        "record": "manifest", "set_id": "synthetic-pilot", "version": "1", "stage": stage,
        "criteria_sha256": criteria_sha, "created_at": "2026-10-02", "corpus_revision": "rev-synthetic",
        "authors": ["Ann"], "adjudicators": ["Bob"],
        "independence": {"authored_outside_tuning_loop": True, "saw_system_outputs": False,
                         "statement": "synthetic fixture"},
    }


def _pilot_rows(criteria_sha: str) -> list[dict]:
    rows = [_manifest(criteria_sha)]
    rows += [_case(i, qc) for i, qc in enumerate(PILOT_CLASSES)]
    rows += [_case(100 + i, "negative", "abstain") for i in range(6)]
    assert sum(r.get("expected_behavior") == "answer" for r in rows) == 24
    return rows


def _write_set(tmp: Path, rows: list[dict], name: str = "release_set.jsonl") -> Path:
    path = tmp / name
    data = ("\n".join(json.dumps(r) for r in rows) + "\n").encode()
    path.write_bytes(data)
    (tmp / (name + ".sha256")).write_text(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
    return path


def _outcomes(release: acc.ReleaseSet, set_sha: str, crit_sha: str, kind: str = "production",
              fail_ids: tuple[str, ...] = (), repeats: int = 1, drop: tuple[str, ...] = ()) -> str:
    profile = {"kind": kind, "label": "fixture"}
    if kind == "production":
        profile.update(reasoning_model="m-r", embed_model="m-e", rerank_model="none", corpus_collection="c",
                       corpus_revision="rev-synthetic", image="img@sha256:0", git_sha="abc", temperature=0.2)
    lines = [{"record": "run", "set_sha256": set_sha, "criteria_sha256": crit_sha, "repeats": repeats,
              "executed_at": "2026-10-05", "profile": profile}]
    for case in release.cases:
        for r in range(1, repeats + 1):
            if case.id in drop:
                continue
            o = {"record": "outcome", "case_id": case.id, "repeat": r, "status": "scored",
                 "adjudicator": "Bob", "completed": True, "evidence_supplied": True}
            good = case.id not in fail_ids
            if case.expected_behavior == "answer":
                o.update(refused=False, useful=good, supported=True, traceable=True)
                if not good:
                    o["failure_stage"] = "unsupported_answer"
            else:
                o.update(refused=True, appropriate_abstention=good, fabricated_instruction=not good)
            lines.append(o)
    return "\n".join(json.dumps(x) for x in lines)


@pytest.fixture
def rc(monkeypatch):
    monkeypatch.setenv("VENUE", "rc")


@pytest.fixture
def pilot(tmp_path, rc):
    cpath, csha = _write_criteria(tmp_path, _criteria_doc())
    spath = _write_set(tmp_path, _pilot_rows(csha))
    release, ssha = acc.load_release_set(spath)
    criteria, _ = acc.load_criteria(cpath)
    return release, ssha, criteria, csha


def _score(pilot, **kw):
    release, ssha, criteria, csha = pilot
    run, outs = acc.parse_outcomes(_outcomes(release, ssha, csha, **kw))
    return acc.score(release, ssha, criteria, csha, run, outs)


# ---- proposed criteria file ----------------------------------------------


def test_committed_criteria_are_proposed_and_valid():
    criteria, _ = acc.load_criteria(CRITERIA_PATH)
    assert criteria.status == "proposed" and criteria.registered_by is None
    assert criteria.certifying_profiles == ["production"]
    pilot = {c.id: c for c in criteria.stages["pilot"].criteria}
    assert (pilot["P1"].threshold, pilot["P1"].min_n) == (0.8333, 24)  # 20/24
    assert pilot["P2"].min_n == 6 and pilot["P3"].threshold == 0 and pilot["P4"].threshold == 0
    assert criteria.stages["release"].repeats == 3 and criteria.stages["release"].statistical_claim


@pytest.mark.parametrize("patch", [
    {"status": "registered"},  # registered without who/when
    {"certifying_profiles": ["production", "standin"]},
])
def test_criteria_reject_incoherent_registration(tmp_path, patch):
    doc = json.loads(CRITERIA_PATH.read_text()) | patch
    path, _ = _write_criteria(tmp_path, doc)
    with pytest.raises(acc.AcceptanceError):
        acc.load_criteria(path)


def test_criteria_reject_unknown_metric_and_bad_pairing(tmp_path):
    for bad in ({"metric": "vibes"}, {"statistic": "wilson_lower", "op": "<="}, {"scope": "answer:nonsense"}):
        doc = json.loads(CRITERIA_PATH.read_text())
        doc["stages"]["pilot"]["criteria"][0].update(bad)
        path, _ = _write_criteria(tmp_path, doc)
        with pytest.raises(acc.AcceptanceError):
            acc.load_criteria(path)


# ---- release-set validator ------------------------------------------------


def test_valid_set_loads_and_hash_identifies_bytes(pilot, tmp_path):
    release, ssha, _, _ = pilot
    assert len(release.cases) == 30
    assert ssha == hashlib.sha256((tmp_path / "release_set.jsonl").read_bytes()).hexdigest()


def _mutated(tmp_path, csha, mutate) -> str:
    rows = _pilot_rows(csha)
    mutate(rows)
    return "\n".join(json.dumps(r) for r in rows)


@pytest.mark.parametrize("mutate,needle", [
    (lambda r: r[1]["adjudication"].update(adjudicator="Ann"), "different people"),
    (lambda r: r[1].update(evidence=[]), "evidence"),
    (lambda r: r[1]["evidence"][0].update(physical_page=0), "physical_page"),
    (lambda r: r[1]["provenance"].pop("source"), "source"),
    (lambda r: r[1]["adjudication"].update(verdict="pending"), "verdict"),
    (lambda r: r[1].update(expected_doc_ids=["OTHER"]), "evidence doc ids"),
    (lambda r: r[25].update(must_not_assert=[]), "must_not_assert"),
    (lambda r: r[25].update(expected_doc_ids=["X"]), "expected docs"),
    (lambda r: r[2].update(id=r[1]["id"]), "duplicate case ids"),
    (lambda r: r[2].update(query=r[1]["query"].upper() + "  "), "duplicate queries"),
    (lambda r: r[0]["independence"].update(saw_system_outputs=True), "saw_system_outputs"),
    (lambda r: r[0].update(authors=["Zed"]), "author is not declared"),
    (lambda r: r[1].update(surprise=1), "surprise"),
    (lambda r: r.pop(0), "manifest"),
])
def test_validator_rejects(tmp_path, mutate, needle):
    with pytest.raises(acc.AcceptanceError, match=needle):
        acc.parse_release_set(_mutated(tmp_path, "0" * 64, mutate))


def test_overlap_with_tuning_queries_is_detected(pilot):
    release = pilot[0]
    assert acc.find_overlaps(release, ["  SYNTHETIC question number 3 about WIDGET 3"]) == ["C003"]
    assert acc.find_overlaps(release, ["unrelated"]) == []


# ---- venue and tuning protection -----------------------------------------


def test_release_set_refused_without_rc(tmp_path, monkeypatch):
    path = _write_set(tmp_path, _pilot_rows("0" * 64))
    monkeypatch.delenv("VENUE", raising=False)
    with pytest.raises(VenueError):
        acc.load_release_set(path)
    with pytest.raises(VenueError, match="release set"):
        require_rc_for_golden([path], venue="dev")
    with pytest.raises(VenueError):
        resolve_golden_paths([path], venue="dev")


def test_release_set_pin_failures_refuse(tmp_path, rc):
    path = _write_set(tmp_path, _pilot_rows("0" * 64))
    pin = tmp_path / "release_set.jsonl.sha256"
    good = pin.read_text()
    pin.write_text(good.replace(good[:4], "ffff", 1))
    with pytest.raises(acc.AcceptanceError, match="mismatch"):
        acc.load_release_set(path)
    pin.write_text(good.split()[0] + "  other.jsonl\n")
    with pytest.raises(acc.AcceptanceError, match="naming its dataset"):
        acc.load_release_set(path)
    pin.unlink()
    with pytest.raises(acc.AcceptanceError, match="unavailable"):
        acc.load_release_set(path)
    pin.write_text(good)  # next ordinary run succeeds after repair
    assert acc.load_release_set(path)[0].cases


def test_golden_readers_never_open_the_release_set_even_under_rc(tmp_path, rc):
    path = _write_set(tmp_path, _pilot_rows("0" * 64))
    with pytest.raises(DatasetError, match="not a golden"):
        read_golden_text(path)
    alias = tmp_path / "golden.jsonl"
    alias.symlink_to(path)
    with pytest.raises(DatasetError, match="not a golden"):
        read_golden_text(alias)


def test_renamed_copy_is_caught_by_record_guard(tmp_path, rc):
    text = "\n".join(json.dumps(r) for r in _pilot_rows("0" * 64))
    with pytest.raises(SystemExit, match="release-set records"):
        parse_golden_text(text)


# ---- scoring --------------------------------------------------------------


def test_pilot_all_pass_registered_production_is_accepted(pilot):
    report = _score(pilot)
    assert report["verdict"] == "accepted" and report["certifying"]
    assert acc.exit_code(report) == 0
    assert {r["status"] for r in report["criteria_results"]} == {"pass"}
    assert report["diagnostics"]["answer_all_request_pass_rate"] == 1.0


def test_pilot_boundary_20_of_24_passes_19_fails(pilot):
    ids = [c.id for c in pilot[0].cases if c.expected_behavior == "answer"]
    assert _score(pilot, fail_ids=tuple(ids[:4]))["verdict"] == "accepted"
    report = _score(pilot, fail_ids=tuple(ids[:5]))
    assert report["verdict"] == "rejected" and acc.exit_code(report) == 1
    failed = {r["id"] for r in report["criteria_results"] if r["status"] == "fail"}
    assert "P1" in failed
    assert report["diagnostics"]["answer_failure_attribution"] == {"unsupported_answer": 5}


def test_any_fabrication_or_critical_failure_rejects(pilot):
    abstain = next(c.id for c in pilot[0].cases if c.expected_behavior == "abstain")
    report = _score(pilot, fail_ids=(abstain,))
    failed = {r["id"] for r in report["criteria_results"] if r["status"] == "fail"}
    assert {"P2", "P3"} <= failed and acc.exit_code(report) == 1

    release, ssha, criteria, csha = pilot
    run, outs = acc.parse_outcomes(_outcomes(release, ssha, csha))
    outs[0].critical_failures = ["scope"]
    report = acc.score(release, ssha, criteria, csha, run, outs)
    failed = {r["id"] for r in report["criteria_results"] if r["status"] == "fail"}
    assert "P4" in failed and "P1" not in failed  # 23/24 still clears the rate bar


def test_missing_outcomes_cannot_pass_by_omission(pilot):
    drop = tuple(c.id for c in pilot[0].cases[:2])
    report = _score(pilot, drop=drop)
    done = {r["id"]: r["status"] for r in report["criteria_results"]}
    assert done["outcomes_complete"] == "insufficient"
    assert report["verdict"] in ("incomplete", "rejected") and acc.exit_code(report) == 1


def test_skipped_error_and_incomplete_rows_count_as_failures(pilot):
    release, ssha, criteria, csha = pilot
    run, outs = acc.parse_outcomes(_outcomes(release, ssha, csha))
    for o in outs[:5]:
        o.status = "skipped"
    report = acc.score(release, ssha, criteria, csha, run, outs)
    assert report["verdict"] == "rejected"
    assert report["diagnostics"]["answer_failure_attribution"]["skipped_or_error"] == 5
    for o in outs[:5]:
        o.status, o.completed = "scored", False  # e.g. finish_reason=length
    assert acc.score(release, ssha, criteria, csha, run, outs)["verdict"] == "rejected"


def test_false_refusal_is_a_usefulness_miss(pilot):
    release, ssha, criteria, csha = pilot
    run, outs = acc.parse_outcomes(_outcomes(release, ssha, csha))
    for o in outs[:6]:
        o.refused, o.useful, o.supported, o.traceable = True, None, None, None
    report = acc.score(release, ssha, criteria, csha, run, outs)
    failed = {r["id"] for r in report["criteria_results"] if r["status"] == "fail"}
    assert {"P1", "P5"} <= failed


def test_coverage_criteria_make_missing_classes_insufficient_never_pass(tmp_path, rc):
    cpath, csha = _write_criteria(tmp_path, _criteria_doc())
    rows = _pilot_rows(csha)
    rows = [r for r in rows if r.get("query_class") != "table"]
    spath = _write_set(tmp_path, rows)
    release, ssha = acc.load_release_set(spath)
    criteria, _ = acc.load_criteria(cpath)
    run, outs = acc.parse_outcomes(_outcomes(release, ssha, csha))
    report = acc.score(release, ssha, criteria, csha, run, outs)
    status = {r["id"]: r["status"] for r in report["criteria_results"]}
    assert status["P-COV-table"] == "fail" and report["verdict"] == "rejected"


def test_standin_and_proposed_are_never_certifying(tmp_path, rc):
    cpath, csha = _write_criteria(tmp_path, _criteria_doc())
    spath = _write_set(tmp_path, _pilot_rows(csha))
    release, ssha = acc.load_release_set(spath)
    criteria, _ = acc.load_criteria(cpath)
    run, outs = acc.parse_outcomes(_outcomes(release, ssha, csha, kind="standin"))
    report = acc.score(release, ssha, criteria, csha, run, outs)
    assert report["verdict"] == "accepted" and not report["certifying"]
    assert acc.exit_code(report) == 2

    pcpath, pcsha = _write_criteria(tmp_path, _criteria_doc("proposed"))
    spath = _write_set(tmp_path, _pilot_rows(pcsha))
    release, ssha = acc.load_release_set(spath)
    criteria, _ = acc.load_criteria(pcpath)
    run, outs = acc.parse_outcomes(_outcomes(release, ssha, pcsha))
    report = acc.score(release, ssha, criteria, pcsha, run, outs)
    assert not report["certifying"] and "PROPOSED" in report["non_certifying_reasons"][0]
    assert acc.exit_code(report) == 2


def test_bindings_are_enforced(pilot):
    release, ssha, criteria, csha = pilot
    run, outs = acc.parse_outcomes(_outcomes(release, "1" * 64, csha))
    with pytest.raises(acc.AcceptanceError, match="release-set hash"):
        acc.score(release, ssha, criteria, csha, run, outs)
    run, outs = acc.parse_outcomes(_outcomes(release, ssha, "2" * 64))
    with pytest.raises(acc.AcceptanceError, match="different criteria"):
        acc.score(release, ssha, criteria, csha, run, outs)
    # registered criteria changed after the set was adjudicated against them
    release2 = acc.parse_release_set(_mutated(None, "3" * 64, lambda r: None))
    run, outs = acc.parse_outcomes(_outcomes(release2, ssha, csha))
    with pytest.raises(acc.AcceptanceError, match="adjudicated against different criteria"):
        acc.score(release2, ssha, criteria, csha, run, outs)


def test_structural_outcome_defects_refuse(pilot):
    release, ssha, criteria, csha = pilot
    text = _outcomes(release, ssha, csha)
    lines = text.splitlines()
    with pytest.raises(acc.AcceptanceError, match="duplicate outcome"):
        run, outs = acc.parse_outcomes("\n".join(lines + [lines[1]]))
        acc.score(release, ssha, criteria, csha, run, outs)
    with pytest.raises(acc.AcceptanceError, match="unknown case"):
        run, outs = acc.parse_outcomes("\n".join(lines + [lines[1].replace("C000", "NOPE")]))
        acc.score(release, ssha, criteria, csha, run, outs)
    with pytest.raises(acc.AcceptanceError, match="adjudication fields missing"):
        run, outs = acc.parse_outcomes("\n".join([lines[0], lines[1].replace('"useful": true', '"useful": null')]))
        acc.score(release, ssha, criteria, csha, run, outs)
    with pytest.raises(acc.AcceptanceError, match="non-finite"):
        acc.parse_outcomes(lines[0] + '\n{"record": "outcome", "case_id": "C000", "repeat": NaN}')
    with pytest.raises(acc.AcceptanceError, match="production profile"):
        acc.parse_outcomes(lines[0].replace('"embed_model": "m-e"', '"embed_model": ""'))


# ---- release stage: statistics, repeats, flips ----------------------------


def _release_fixture(tmp_path, n_answer=100, n_abstain=30):
    doc = _criteria_doc()
    doc["stages"]["release"]["criteria"] = [c for c in doc["stages"]["release"]["criteria"]
                                            if not c["id"].startswith("R-CLS")]
    path, csha = _write_criteria(tmp_path, doc)
    rows = [_manifest(csha, "release")]
    rows += [_case(i, ["syntax", "diagnostic"][i % 2]) for i in range(n_answer)]
    rows += [_case(1000 + i, "negative", "abstain") for i in range(n_abstain)]
    release, ssha = acc.load_release_set(_write_set(tmp_path, rows))
    return release, ssha, acc.load_criteria(path)[0], csha


def test_wilson_bound_matches_reference_values():
    assert acc.wilson_bound(30, 30, 0.95, "lower") == pytest.approx(0.9173, abs=1e-3)
    assert acc.wilson_bound(0, 100, 0.95, "upper") == pytest.approx(0.0263, abs=1e-3)


def test_release_stage_uses_wilson_bound_not_point_estimate(tmp_path, rc):
    fx = _release_fixture(tmp_path)
    ids = [c.id for c in fx[0].cases if c.expected_behavior == "answer"]
    # 88/100 observed: the point estimate clears 0.80 but the bound is the rule
    report = _score(fx, repeats=3, fail_ids=tuple(ids[:12]))
    r1 = next(r for r in report["criteria_results"] if r["id"] == "R1")
    assert r1["value"] == 0.88 and r1["bound"] < 0.88 and r1["status"] == "pass"
    report = _score(fx, repeats=3, fail_ids=tuple(ids[:16]))  # 84% -> bound below 0.80
    assert next(r for r in report["criteria_results"] if r["id"] == "R1")["status"] == "fail"
    assert report["statistical_claim"] and report["repeats"] == 3


def test_release_stage_below_minimum_n_is_insufficient(tmp_path, rc):
    fx = _release_fixture(tmp_path, n_answer=30, n_abstain=6)
    report = _score(fx, repeats=3)
    status = {r["id"]: r["status"] for r in report["criteria_results"]}
    assert status["R1"] == status["R2"] == "insufficient"
    assert report["verdict"] == "incomplete" and acc.exit_code(report) == 1


def test_flip_rate_detects_unstable_cases(tmp_path, rc):
    fx = _release_fixture(tmp_path)
    release, ssha, criteria, csha = fx
    run, outs = acc.parse_outcomes(_outcomes(release, ssha, csha, repeats=3))
    flipped = [c.id for c in release.cases if c.expected_behavior == "answer"][:15]
    for o in outs:
        if o.case_id in flipped and o.repeat == 2:
            o.useful = False
    report = acc.score(release, ssha, criteria, csha, run, outs)
    flip = next(r for r in report["criteria_results"] if r["id"] == "R8")
    assert flip["value"] == pytest.approx(15 / 130, abs=1e-4) and flip["status"] == "fail"


def test_repeat_count_must_match_registration(tmp_path, rc):
    fx = _release_fixture(tmp_path)
    run, outs = acc.parse_outcomes(_outcomes(fx[0], fx[1], fx[3], repeats=1))
    with pytest.raises(acc.AcceptanceError, match="pre-register 3"):
        acc.score(fx[0], fx[1], fx[2], fx[3], run, outs)


# ---- CLI ------------------------------------------------------------------


def _cli_files(tmp_path):
    cpath, csha = _write_criteria(tmp_path, _criteria_doc())
    spath = _write_set(tmp_path, _pilot_rows(csha))
    release, ssha = acc.load_release_set(spath)
    opath = tmp_path / "outcomes.jsonl"
    opath.write_text(_outcomes(release, ssha, csha))
    return cpath, spath, opath


def test_cli_scores_and_writes_report(tmp_path, rc, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # no evals/ here: nothing to overlap with
    cpath, spath, opath = _cli_files(tmp_path)
    out = tmp_path / "report.json"
    code = acc.main(["--set", str(spath), "--criteria", str(cpath), "--outcomes", str(opath), "--out", str(out)])
    assert code == 0 and "accepted" in capsys.readouterr().out
    assert json.loads(out.read_text())["verdict"] == "accepted"
    assert acc.main(["--set", str(spath), "--validate-only"]) == 0


def test_cli_refuses_in_dev_and_on_tuning_overlap(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VENUE", "rc")
    cpath, spath, opath = _cli_files(tmp_path)
    monkeypatch.delenv("VENUE", raising=False)
    assert acc.main(["--set", str(spath), "--criteria", str(cpath), "--outcomes", str(opath)]) == 2
    assert "VENUE=rc" in capsys.readouterr().err
    monkeypatch.setenv("VENUE", "rc")
    (tmp_path / "evals").mkdir()
    (tmp_path / "evals" / "golden.jsonl").write_text(
        json.dumps({"query": "synthetic question number 3 about widget 3", "expected_doc_ids": ["D"]}) + "\n")
    assert acc.main(["--set", str(spath), "--validate-only"]) == 2
    assert "overlap" in capsys.readouterr().err
