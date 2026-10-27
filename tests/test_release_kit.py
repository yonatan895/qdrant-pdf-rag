"""Release-set authoring kit (issue #367). Synthetic fixtures only; no real rows."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from mainframe_rag.eval import acceptance as acc
from mainframe_rag.eval import release_kit as kit

ROOT = Path(__file__).resolve().parents[1]
CLASSES = ["message_id"] * 5 + ["version"] * 5 + ["syntax"] * 5 + ["table"] * 5 + ["diagnostic"] * 4
SHA = "a" * 64


def _case(i: int, qclass: str, behavior: str = "answer") -> dict:
    row = {
        "record": "case", "id": f"REL-{i:03d}", "query": f"synthetic kit question {i} regarding gadget {i}",
        "query_class": qclass, "expected_behavior": behavior,
        "provenance": {"author": "Ann", "authored_at": "2026-10-01", "source": "synthetic", "release": "R1"},
        "adjudication": {"adjudicator": "Ann", "adjudicated_at": "2026-10-09", "verdict": "accepted"},
    }
    if behavior == "answer":
        row.update(expected_doc_ids=[f"DOC-{i}"], required_facts=[f"fact {i}"],
                   evidence=[{"doc_id": f"DOC-{i}", "release": "R1", "physical_page": 1, "locator": "s",
                              "excerpt_sha256": SHA}])
    else:
        row.update(abstain_reason="wrong_premise", must_not_assert=[f"invented {i}"])
    return row


def _rows(crit_sha: str, stage: str = "pilot", abstain: int = 6) -> list[dict]:
    manifest = {"record": "manifest", "set_id": "synthetic", "version": "1", "stage": stage,
                "criteria_sha256": crit_sha, "created_at": "2026-10-01", "corpus_revision": "rev",
                "authors": ["Ann"], "adjudicators": ["Ann"],
                "independence": {"authored_outside_tuning_loop": True, "saw_system_outputs": False,
                                 "statement": "synthetic"}}
    rows = [manifest] + [_case(i, c) for i, c in enumerate(CLASSES)]
    return rows + [_case(100 + i, "negative", "abstain") for i in range(abstain)]


@pytest.fixture
def env(tmp_path):
    """Private dir outside the repo, a synthetic comparison root and the real criteria file."""
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    cmp_root = tmp_path / "evals"
    cmp_root.mkdir()
    gold = {"id": "DEV-1", "query": "a dev question about widgets", "expected_doc_ids": ["D"],
            "query_class": "syntax", "expected_behavior": "answer"}
    (cmp_root / "golden.jsonl").write_text(json.dumps(gold) + "\n")
    (cmp_root / "paraphrase.jsonl").write_text(json.dumps(dict(gold, id="DEV-P", query="dev paraphrase")) + "\n")
    crit = ROOT / "evals" / "acceptance-criteria.json"
    return private, cmp_root, crit, hashlib.sha256(crit.read_bytes()).hexdigest()


def _check(env, rows, name="release_set.jsonl"):
    private, cmp_root, crit, _ = env
    path = private / name
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return kit.check_draft(path, criteria_path=crit, comparison_root=cmp_root, repo_root=ROOT)


def test_valid_pilot_draft_passes_with_same_author_and_adjudicator(env):
    assert _check(env, _rows(env[3])) == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda r: r.pop(), "pilot needs 24 answer + 6 abstain"),
    (lambda r: r[1].update(required_facts=["a", "b"]), "one fact per row"),
    (lambda r: r[1]["evidence"][0].pop("excerpt_sha256"), "excerpt_sha256"),
    (lambda r: r[2].update(id="dev-1"), "collides with a dev golden"),
    (lambda r: r[2].update(query="A  dev question about WIDGETS"), "duplicates a dev golden"),
    (lambda r: r[0].update(criteria_sha256="b" * 64), "criteria_sha256"),
])
def test_draft_problems(env, mutate, needle):
    rows = _rows(env[3])
    mutate(rows)
    assert any(needle in p for p in _check(env, rows))


def test_class_coverage_is_read_from_the_criteria(env):
    rows = _rows(env[3])
    for r in rows[1:6]:
        r["query_class"] = "comparative"  # empties message_id
    assert any("P-COV-message_id" in p for p in _check(env, rows))


def test_release_stage_needs_exactly_forty_abstain(env):
    rows = _rows(env[3], stage="release", abstain=6)
    problems = _check(env, rows)
    assert any("exactly 40 abstain" in p for p in problems)
    assert any(">= 100 answer" in p for p in problems)


def test_problem_output_never_echoes_query_text(env, capsys):
    private, cmp_root, crit, sha = env
    rows = _rows(sha)
    rows[2]["query"] = "a dev question about widgets"
    (private / "release_set.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    rc = kit.main(["check", "--set", str(private / "release_set.jsonl"), "--criteria", str(crit),
                   "--comparison-root", str(cmp_root)], workspace_root=ROOT)
    out = capsys.readouterr().out
    assert rc == 1 and "REL-001" in out and "widgets" not in out


def test_set_inside_the_repository_is_refused(env):
    _, cmp_root, crit, _ = env
    with pytest.raises(acc.AcceptanceError, match="outside the repository"):
        kit.check_draft(ROOT / "release_set.jsonl", criteria_path=crit, comparison_root=cmp_root, repo_root=ROOT)


def test_group_accessible_storage_is_a_problem(env):
    os.chmod(env[0], 0o750)
    assert any("chmod 700" in p for p in _check(env, _rows(env[3])))


def test_pin_is_accepted_by_the_rc_reader(env, monkeypatch):
    private, _cmp_root, _crit, sha = env
    path = private / "release_set.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in _rows(sha)) + "\n")
    digest = kit.pin(path)
    assert (private / "release_set.jsonl.sha256").read_text().split() == [digest, "release_set.jsonl"]
    monkeypatch.setenv("VENUE", "rc")
    release, got = acc.load_release_set(path)
    assert got == digest and len(release.cases) == 30


def test_template_lists_every_schema_field():
    manifest, answer, abstain = kit.template_rows()
    assert set(manifest) == set(acc.Manifest.model_fields)
    assert set(answer) <= set(acc.ReleaseCase.model_fields) and set(abstain) <= set(acc.ReleaseCase.model_fields)
    assert {"evidence", "required_facts"} <= set(answer) and {"abstain_reason", "must_not_assert"} <= set(abstain)
    for row in kit.template_rows():
        json.dumps(row)
