#!/usr/bin/env python3
"""Mechanical verification of golden-set expectations against a live collection.

The golden corpus is only as good as its expectations. Every entry is checked
against the actual Qdrant payload before any human semantic pass:

  FAIL (gates the corpus, nonzero exit)
    - expected_doc_ids that do not exist in the collection (typos)
    - expected_heading not found in any chunk of the expected docs
    - expected_page not among the expected doc's page labels
    - message-ID queries whose parsed ID is not in the expected docs' payloads
    - must_not_retrieve docs that do not exist (typos)
    - must_not_message_ids that ARE present in an expected doc (broken trap:
      the sibling lives inside the doc you claim answers the query)
    - duplicate query text
  WARN (hygiene; escalate with --strict when the corpus is complete)
    - missing id / query_class / source
    - abstain entry without any must_not and without a note
    - must_not_message_ids appearing in more than RARITY_LIMIT docs (weak trap)
    - stratification below the corpus targets (--strict)

    QDRANT_URL=http://127.0.0.1:6333 python scripts/verify_golden.py
    python scripts/verify_golden.py --golden evals/holdout.jsonl --strict
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root: scripts.* imports

from mainframe_rag.config import load_settings
from mainframe_rag.retrieve.filters import parse_query
from scripts.eval_retrieval import QUERY_CLASSES, GoldenEntry, is_sibling_exception

RARITY_LIMIT = 10  # a must_not message ID in more docs than this is a weak trap
PAGE_SIZE = 1000


@dataclass
class DocFacts:
    pages: set[str] = field(default_factory=set)
    headings: list[str] = field(default_factory=list)
    message_ids: set[str] = field(default_factory=set)
    title: str = ""


@dataclass
class CorpusFacts:
    docs: dict[str, DocFacts] = field(default_factory=dict)
    msg_docs: dict[str, set[str]] = field(default_factory=dict)
    points: int = 0


def build_corpus_facts(client, collection: str) -> CorpusFacts:
    """One scroll pass over the collection: doc-level pages/headings/IDs and
    the message-id -> docs map used for trap-rarity checks."""
    facts = CorpusFacts()
    offset = None
    while True:
        points, offset = client.scroll(
            collection,
            limit=PAGE_SIZE,
            offset=offset,
            with_payload=["doc_id", "title", "heading_path", "page_label", "message_ids"],
            with_vectors=False,
        )
        for point in points:
            payload = point.payload or {}
            doc_id = str(payload.get("doc_id") or "")
            if not doc_id:
                continue
            doc = facts.docs.setdefault(doc_id, DocFacts())
            doc.title = doc.title or str(payload.get("title") or "")
            doc.pages.add(str(payload.get("page_label") or ""))
            heading = str(payload.get("heading_path") or "")
            if heading:
                doc.headings.append(heading.lower())
            for msg in payload.get("message_ids") or []:
                doc.message_ids.add(str(msg))
                facts.msg_docs.setdefault(str(msg), set()).add(doc_id)
        facts.points += len(points)
        if offset is None:
            break
    return facts


def verify_entry(entry: GoldenEntry, facts: CorpusFacts) -> tuple[list[str], list[str]]:
    """Returns (failures, warnings) for one entry against corpus facts."""
    fails: list[str] = []
    warns: list[str] = []

    missing_docs = [d for d in entry.expected_doc_ids if d not in facts.docs]
    if missing_docs:
        fails.append(f"expected doc(s) not in collection: {missing_docs}")
    missing_traps = [d for d in entry.must_not_retrieve if d not in facts.docs]
    if missing_traps:
        fails.append(f"must_not doc(s) not in collection: {missing_traps}")

    expected_docs = [facts.docs[d] for d in entry.expected_doc_ids if d in facts.docs]
    if entry.expected_heading:
        needle = entry.expected_heading.lower()
        if not any(needle in h for doc in expected_docs for h in doc.headings):
            fails.append(f"heading {entry.expected_heading!r} not found in expected docs' chunks")
    if entry.expected_page and expected_docs and not any(
        entry.expected_page in doc.pages for doc in expected_docs
    ):
        fails.append(f"page {entry.expected_page!r} not among expected docs' page labels")

    if entry.query_class == "message_id":
        for msg in parse_query(entry.query).message_ids:
            if expected_docs and not any(msg in doc.message_ids for doc in expected_docs):
                fails.append(f"message id {msg!r} from query not present in expected docs' payloads")

    for msg in entry.must_not_message_ids:
        hit_docs = facts.msg_docs.get(msg, set())
        if not hit_docs:
            fails.append(f"must_not message id {msg!r} does not exist in the collection (typo?)")
            continue
        # A must_not ID inside an expected doc is only broken when the doc does
        # not also carry the query's own message ID: same-volume sibling
        # precision assertions (IOS207I vs IOS208I) are legitimate.
        query_ids = set(parse_query(entry.query).message_ids)
        for d in sorted(hit_docs & set(entry.expected_doc_ids)):
            doc_ids = facts.docs[d].message_ids if d in facts.docs else set()
            if not is_sibling_exception(query_ids, doc_ids):
                fails.append(
                    f"must_not message id {msg!r} is present inside expected doc {d} "
                    "which does not carry the query's own message id; trap is broken"
                )
        if len(hit_docs) > RARITY_LIMIT:
            warns.append(
                f"must_not message id {msg!r} appears in {len(hit_docs)} docs (> {RARITY_LIMIT}); weak trap"
            )

    if (
        entry.expected_behavior == "abstain"
        and not entry.must_not_retrieve
        and not entry.must_not_message_ids
        and not entry.note
    ):
        warns.append("abstain entry without must_not targets or note: what makes abstention verifiable?")

    if not entry.id:
        warns.append("missing id (recommended for tracking/review)")
    if not entry.query_class:
        warns.append(f"missing query_class (expected one of {', '.join(QUERY_CLASSES)})")
    if not entry.source:
        warns.append("missing source (operator-history|payload-draft)")

    return fails, warns


def find_duplicate_queries(entries: list[GoldenEntry]) -> list[tuple[str, int]]:
    counts = Counter(e.query.strip().lower() for e in entries)
    return [(q, n) for q, n in counts.items() if n > 1]


def load_entries(path: Path) -> tuple[list[GoldenEntry], list[str]]:
    """Parse a golden JSONL; per-line validation errors become FAILs instead
    of aborting the whole run."""
    entries: list[GoldenEntry] = []
    errors: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entries.append(GoldenEntry.model_validate_json(line))
        except Exception as exc:  # noqa: BLE001
            msg = f"line {lineno}: {line[:100]} ({str(exc)[:140]})"
            print(f"FAIL [{path.name}] {msg}", file=sys.stderr)
            errors.append(msg)
    return entries, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--golden", type=Path, action="append", default=None,
        help="golden JSONL path (repeatable; default: evals/golden.jsonl + evals/holdout.jsonl)",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="treat hygiene warnings (id/class/source, stratification) as failures",
    )
    args = parser.parse_args(argv)
    golden_paths = args.golden or [Path("evals/golden.jsonl"), Path("evals/holdout.jsonl")]

    settings = load_settings()
    from qdrant_client import QdrantClient

    client = QdrantClient(
        url=settings.qdrant_url, api_key=settings.qdrant_api_key, timeout=settings.qdrant_timeout_s
    )
    print(f"[*] Scanning collection {settings.qdrant_collection!r} at {settings.qdrant_url} ...", file=sys.stderr)
    facts = build_corpus_facts(client, settings.qdrant_collection)
    print(f"[*] {len(facts.docs)} docs / {facts.points} points indexed for verification", file=sys.stderr)

    entries: list[GoldenEntry] = []
    parse_errors: list[str] = []
    for path in golden_paths:
        if not path.exists():
            print(f"FAIL missing golden file: {path}", file=sys.stderr)
            parse_errors.append(f"missing golden file: {path}")
            continue
        file_entries, file_errors = load_entries(path)
        print(f"[*] {path}: {len(file_entries)} entries", file=sys.stderr)
        entries.extend(file_entries)
        parse_errors.extend(file_errors)

    total_fails: list[str] = list(parse_errors)
    total_warns: list[str] = []
    for entry in entries:
        fails, warns = verify_entry(entry, facts)
        label = entry.id or entry.query[:40]
        for f in fails:
            print(f"FAIL [{label}] {f}", file=sys.stderr)
        for w in warns:
            print(f"WARN [{label}] {w}", file=sys.stderr)
        total_fails.extend(fails)
        total_warns.extend(warns)

    for q, n in find_duplicate_queries(entries):
        print(f"FAIL duplicate query ({n}x): {q}", file=sys.stderr)
        total_fails.append(f"duplicate query ({n}x): {q}")

    # Stratification report (per-file; the corpus targets are enforced under
    # --strict once authoring is complete).
    class_counts: Counter = Counter((e.query_class or "(none)") for e in entries)
    behavior_counts: Counter = Counter(e.expected_behavior for e in entries)
    print("[*] stratification:", dict(sorted(class_counts.items())), file=sys.stderr)
    print("[*] behaviors:", dict(sorted(behavior_counts.items())), file=sys.stderr)
    if args.strict:
        if len(entries) < 150:
            total_fails.append(f"corpus has {len(entries)} entries; target is >=150")
        for cls in QUERY_CLASSES:
            if class_counts.get(cls, 0) == 0:
                total_fails.append(f"query_class {cls!r} has no entries")
        for key in ("id", "query_class", "source"):
            missing = sum(1 for e in entries if not getattr(e, key))
            if missing:
                total_fails.append(f"{missing} entries missing {key}")

    print(
        f"[*] verify-golden: {len(entries)} entries, {len(total_fails)} FAIL, {len(total_warns)} WARN",
        file=sys.stderr,
    )
    return 1 if total_fails else 0


if __name__ == "__main__":
    sys.exit(main())
