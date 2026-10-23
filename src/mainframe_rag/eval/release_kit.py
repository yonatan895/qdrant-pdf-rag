"""Authoring kit for the independent release set (issue #367).

Tooling only: this module never contains rows or questions. The maintainer
authors the set outside the repository; ``check`` validates a draft against the
registered schema (``acceptance.parse_release_set``) and the authoring rules in
docs/eval.md section 11, and prints ids and counts only, never query text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import sys
from collections import Counter
from pathlib import Path

from mainframe_rag.eval import acceptance as acc
from mainframe_rag.eval.datasets import (
    QUERY_CLASSES,
    DatasetError,
    parse_golden_text,
    read_golden_text,
)

PILOT_ANSWER, PILOT_ABSTAIN = 24, 6
RELEASE_MIN_ANSWER, RELEASE_ABSTAIN = 100, 40  # D1 (about 130 rows) and D4
DEV_SETS = ("golden.jsonl", "paraphrase.jsonl")  # pinned holdout is checked by the RC validator
DEFAULT_PRIVATE_DIR = Path.home() / ".config" / "mainframe-rag" / "release-set"

TODO = "TODO"


def template_rows() -> list[dict]:
    """Skeleton manifest plus one answer and one abstain row; placeholders only."""
    manifest = {
        "record": "manifest", "set_id": TODO, "version": "1", "stage": "pilot",
        "criteria_sha256": "0" * 64, "created_at": "YYYY-MM-DD", "corpus_revision": TODO,
        "authors": [TODO], "adjudicators": [TODO],
        "independence": {"authored_outside_tuning_loop": True, "saw_system_outputs": False, "statement": TODO},
    }
    base = {
        "record": "case", "id": "REL-000", "query": TODO, "query_class": "|".join(QUERY_CLASSES),
        "provenance": {"author": TODO, "authored_at": "YYYY-MM-DD", "source": TODO, "release": TODO},
        "adjudication": {"adjudicator": TODO, "adjudicated_at": "YYYY-MM-DD (a later day)", "verdict": "accepted"},
    }
    answer = dict(base, id="REL-001", expected_behavior="answer", expected_doc_ids=[TODO],
                  evidence=[{"doc_id": TODO, "release": TODO, "physical_page": 1, "locator": TODO,
                             "excerpt_sha256": "<sha256 of the adjudicated passage, never the text>"}],
                  required_facts=[TODO], required_conditions=[])
    abstain = dict(base, id="REL-002", expected_behavior="abstain",
                   abstain_reason="insufficient_evidence|wrong_premise", must_not_assert=[TODO])
    return [manifest, answer, abstain]


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _dev_rows(root: Path) -> tuple[set[str], set[str]]:
    ids: set[str] = set()
    queries: set[str] = set()
    for name in DEV_SETS:
        try:
            entries = parse_golden_text(read_golden_text(root / name))
        except (DatasetError, OSError, UnicodeError, SystemExit) as exc:
            raise acc.AcceptanceError(f"dev comparison set {name} unavailable or invalid") from exc
        ids.update(e.id.casefold() for e in entries if e.id)
        queries.update(acc.normalize_query(e.query) for e in entries)
    return ids, queries


def check_draft(path: Path, *, criteria_path: Path, comparison_root: Path, repo_root: Path) -> list[str]:
    """Return problems (ids and counts only). Empty means the draft passes every kit rule."""
    if _inside(path, repo_root):
        raise acc.AcceptanceError("the release set must live outside the repository working tree")
    mode = stat.S_IMODE(path.parent.stat().st_mode)
    problems: list[str] = []
    if mode & 0o077:
        problems.append(f"storage directory is accessible to group/other (mode {mode:o}); use chmod 700")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise acc.AcceptanceError("release set draft unreadable") from exc
    release = acc.parse_release_set(text)
    criteria, criteria_sha = acc.load_criteria(criteria_path)
    manifest = release.manifest
    if manifest.criteria_sha256 != criteria_sha:
        problems.append("manifest criteria_sha256 does not match the current criteria file")
    cases = release.cases
    answers = [c for c in cases if c.expected_behavior == "answer"]
    abstains = [c for c in cases if c.expected_behavior == "abstain"]
    if manifest.stage == "pilot":
        if (len(answers), len(abstains)) != (PILOT_ANSWER, PILOT_ABSTAIN):
            problems.append(f"pilot needs {PILOT_ANSWER} answer + {PILOT_ABSTAIN} abstain rows, "
                            f"found {len(answers)} + {len(abstains)}")
    else:
        if len(answers) < RELEASE_MIN_ANSWER:
            problems.append(f"release needs >= {RELEASE_MIN_ANSWER} answer rows, found {len(answers)}")
        if len(abstains) != RELEASE_ABSTAIN:
            problems.append(f"release needs exactly {RELEASE_ABSTAIN} abstain rows (D4), found {len(abstains)}")
    by_class = Counter(c.query_class for c in answers)
    for crit in criteria.stages[manifest.stage].criteria:
        behavior, _, qclass = crit.scope.partition(":")
        if crit.metric == "case_count" and behavior == "answer" and qclass and by_class[qclass] < crit.threshold:
            problems.append(f"{crit.id}: {by_class[qclass]} answer rows in {qclass}, need >= {crit.threshold:g}")
    for c in answers:
        if len(c.required_facts) != 1:
            problems.append(f"{c.id}: one fact per row (required_facts has {len(c.required_facts)}); "
                            "put side conditions in required_conditions")
        if any(e.excerpt_sha256 is None for e in c.evidence):
            problems.append(f"{c.id}: evidence needs excerpt_sha256 (source passage id)")
    dev_ids, dev_queries = _dev_rows(comparison_root)
    for c in cases:
        if c.id.casefold() in dev_ids:
            problems.append(f"{c.id}: id collides with a dev golden/paraphrase id")
        if acc.normalize_query(c.query) in dev_queries:
            problems.append(f"{c.id}: query duplicates a dev golden/paraphrase query")
    return problems


def pin(path: Path) -> str:
    """Write the adjacent ``<name>.sha256`` pin the RC readers require."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (path.parent / (path.name + ".sha256")).write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return digest


def main(argv: list[str] | None = None, *, workspace_root: Path | None = None) -> int:
    root = workspace_root if workspace_root is not None else Path.cwd()
    p = argparse.ArgumentParser(description="Release-set authoring kit (#367); prints ids and counts only")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("template", help="print a placeholder manifest and row skeletons")
    for name in ("check", "pin"):
        s = sub.add_parser(name)
        s.add_argument("--set", type=Path, default=DEFAULT_PRIVATE_DIR / "release_set.jsonl")
        if name == "check":
            s.add_argument("--criteria", type=Path, default=root / acc.DEFAULT_CRITERIA_PATH)
            s.add_argument("--comparison-root", type=Path, default=root / "evals")
    args = p.parse_args(argv)
    if args.cmd == "template":
        for row in template_rows():
            print(json.dumps(row))
        return 0
    try:
        problems = check_draft(args.set, criteria_path=args.criteria, comparison_root=args.comparison_root,
                               repo_root=root) if args.cmd == "check" else []
        if args.cmd == "pin":
            if _inside(args.set, root):
                raise acc.AcceptanceError("the release set must live outside the repository working tree")
            acc.parse_release_set(args.set.read_text(encoding="utf-8"))
            print(f"pinned sha256={pin(args.set)}")
            return 0
    except (acc.AcceptanceError, OSError, UnicodeError) as exc:
        print(f"release kit refused: {exc}", file=sys.stderr)
        return 2
    for line in problems:
        print(f"problem: {line}")
    print("release set draft ok" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
