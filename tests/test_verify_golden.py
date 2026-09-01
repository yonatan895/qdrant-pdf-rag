"""Unit tests for scripts/verify_golden.py pure logic (no Qdrant/network)."""

import json
from pathlib import Path

from scripts.eval_retrieval import GoldenEntry
from scripts.verify_golden import (
    CorpusFacts,
    DocFacts,
    find_duplicate_queries,
    load_entries,
    verify_entry,
)


def _facts() -> CorpusFacts:
    return CorpusFacts(
        docs={
            "SC23-6883-70": DocFacts(
                pages={"4-12", "5-1"},
                headings=["chapter 4 > mounting", "chapter 9 > editing"],
                message_ids={"EZD1205I"},
                title="NFS Guide",
            ),
            "SA23-2230-60": DocFacts(pages={"2-1"}, headings=["chapter 2"], message_ids=set(), title="Old book"),
            "SA38-0677-70": DocFacts(pages={"7-1"}, headings=["iea500i"], message_ids={"IEA500I", "IEA501I"}, title="Messages"),
            "SA38-0674-70": DocFacts(pages={"3-1"}, headings=["csv"], message_ids={"IEA501I"}, title="Other messages"),
        },
        msg_docs={"IEA501I": {"SA38-0674-70"}, "EZD1205I": {"SC23-6883-70"}},
        points=4,
    )


def test_verify_entry_clean_pass():
    entry = GoldenEntry(
        id="doc-01",
        query="SC23-6883-70",
        query_class="doc_number",
        expected_doc_ids=["SC23-6883-70"],
        expected_heading="Mounting",
        expected_page="4-12",
        source="operator-history",
    )
    fails, warns = verify_entry(entry, _facts())
    assert fails == [] and warns == []


def test_verify_entry_fails_on_unknown_docs():
    entry = GoldenEntry(query="x", expected_doc_ids=["NOPE-1"], must_not_retrieve=["NOPE-2"])
    fails, _ = verify_entry(entry, _facts())
    assert any("NOPE-1" in f for f in fails)
    assert any("NOPE-2" in f for f in fails)


def test_verify_entry_fails_on_missing_heading_and_page():
    entry = GoldenEntry(
        query="q",
        expected_doc_ids=["SC23-6883-70"],
        expected_heading="Nonexistent",
        expected_page="9-99",
    )
    fails, _ = verify_entry(entry, _facts())
    assert any("heading" in f for f in fails)
    assert any("page" in f for f in fails)


def test_verify_entry_message_id_must_be_in_expected_doc_payload():
    entry = GoldenEntry(
        id="msg-01",
        query="IEA500I rejected before IOS init",
        query_class="message_id",
        expected_doc_ids=["SA38-0677-70"],
        source="operator-history",
    )
    fails, warns = verify_entry(entry, _facts())
    assert fails == [] and warns == []  # IEA500I is in SA38-0677-70's payload

    entry_bad = GoldenEntry(
        id="msg-02",
        query="EZD1205I odd code",
        query_class="message_id",
        expected_doc_ids=["SA38-0677-70"],
        source="operator-history",
    )
    fails, _ = verify_entry(entry_bad, _facts())
    assert any("EZD1205I" in f for f in fails)


def test_verify_entry_broken_sibling_trap_fails():
    entry = GoldenEntry(
        id="msg-02",
        query="IEA501I code",
        query_class="message_id",
        expected_doc_ids=["SA38-0674-70"],
        must_not_message_ids=["IEA501I"],
        source="operator-history",
    )
    fails, _ = verify_entry(entry, _facts())
    assert any("trap is broken" in f for f in fails)


def test_verify_entry_weak_trap_warns():
    facts = _facts()
    facts.msg_docs["IEA501I"] = {f"D{i}" for i in range(20)}
    entry = GoldenEntry(
        id="msg-03",
        query="IEA501I code",
        query_class="message_id",
        expected_doc_ids=["SC23-6883-70"],
        must_not_message_ids=["IEA501I"],
        source="operator-history",
    )
    _, warns = verify_entry(entry, facts)
    assert any("weak trap" in w for w in warns)


def test_verify_entry_nonexistent_sibling_trap_fails():
    entry = GoldenEntry(
        id="msg-04",
        query="q",
        query_class="message_id",
        expected_doc_ids=["SC23-6883-70"],
        must_not_message_ids=["ZZZ9999I"],
        source="operator-history",
    )
    fails, _ = verify_entry(entry, _facts())
    assert any("does not exist" in f for f in fails)


def test_verify_entry_abstain_without_targets_or_note_warns():
    entry = GoldenEntry(id="neg-01", query="q", query_class="negative", expected_behavior="abstain", source="operator-history")
    _, warns = verify_entry(entry, _facts())
    assert any("abstain" in w for w in warns)
    entry_noted = GoldenEntry(
        id="neg-02", query="q", query_class="negative", expected_behavior="abstain",
        source="operator-history", note="out-of-corpus topic by design",
    )
    _, warns2 = verify_entry(entry_noted, _facts())
    assert not any("abstain" in w for w in warns2)


def test_verify_entry_hygiene_warns():
    entry = GoldenEntry(query="q", expected_doc_ids=["SC23-6883-70"])
    _, warns = verify_entry(entry, _facts())
    joined = " | ".join(warns)
    assert "missing id" in joined and "missing query_class" in joined and "missing source" in joined


def test_find_duplicate_queries():
    entries = [
        GoldenEntry(query="Same Query", expected_doc_ids=["SC23-6883-70"]),
        GoldenEntry(query="same query ", expected_doc_ids=["SC23-6883-70"]),
        GoldenEntry(query="other", expected_doc_ids=["SC23-6883-70"]),
    ]
    assert find_duplicate_queries(entries) == [("same query", 2)]


def test_load_entries_collects_per_line_errors(tmp_path: Path):
    path = tmp_path / "golden.jsonl"
    path.write_text(
        json.dumps({"query": "ok", "expected_doc_ids": ["SC23-6883-70"]}) + "\n"
        + "not json\n"
        + json.dumps({"query": "x", "expected_behavior": "abstain", "expected_doc_ids": ["D"]}) + "\n",
        encoding="utf-8",
    )
    entries, errors = load_entries(path)
    assert len(entries) == 1
    assert len(errors) == 2
    assert "line 2" in errors[0] and "line 3" in errors[1]


def test_verify_entry_needs_no_network():
    """Guard against regressions that silently dial the network: verify_entry
    is pure over CorpusFacts."""
    entry = GoldenEntry(query="q", expected_doc_ids=["SC23-6883-70"])
    fails, warns = verify_entry(entry, _facts())
    assert fails == []
    assert len(warns) == 3
    assert any("query_class" in w and "message_id" in w for w in warns)
