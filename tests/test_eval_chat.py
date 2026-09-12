"""Unit tests for the multi-turn condensation A/B helpers (scripts/eval_chat.py).

Hermetic: follow-up templates, per-arm entry construction, and pure
aggregation only. The live tier runs via `make eval-chat` on the RC stack.
"""

from __future__ import annotations

from scripts.eval_chat import (
    ARM_CONDENSED,
    ARM_LITERAL,
    DEFAULT_FOLLOW_UP,
    arm_entry,
    follow_up_query,
    select_entries,
    session_messages,
    summarize_arms,
    summary_markdown,
)
from scripts.eval_retrieval import GoldenEntry


def _entry(entry_id: str = "g-1", query_class: str = "message_id") -> GoldenEntry:
    return GoldenEntry(
        id=entry_id,
        query="What does IEA500I mean?",
        expected_doc_ids=["SA22-0000-00"],
        expected_heading="IEA500I",
        query_class=query_class,
        must_not_retrieve=["SA22-9999-99"],
        must_not_message_ids=["IEA501I"],
    )


def test_follow_up_uses_class_template_with_default():
    assert (
        follow_up_query(_entry(query_class="message_id"))
        == "What does it mean and how do I resolve it?"
    )
    assert follow_up_query(_entry(query_class="diagnostic")) == "How do I recover from it?"
    assert follow_up_query(_entry(query_class=None)) == DEFAULT_FOLLOW_UP


def test_session_messages_end_with_the_follow_up():
    messages = session_messages(_entry())
    assert [m.role for m in messages] == ["user", "assistant", "user"]
    assert messages[0].content == "What does IEA500I mean?"
    assert messages[-1].content == follow_up_query(_entry())


def test_arm_entry_keeps_session_expectations_and_rebinds_id():
    original = _entry()
    arm = arm_entry(original, "How do I resolve it?")
    assert arm.id == "g-1::How do I resolve it?"
    assert arm.query == "How do I resolve it?"
    assert arm.expected_doc_ids == original.expected_doc_ids
    assert arm.expected_heading == original.expected_heading
    assert arm.must_not_retrieve == original.must_not_retrieve
    assert arm.must_not_message_ids == original.must_not_message_ids


def test_select_entries_filters_and_limits_deterministically():
    entries = [
        _entry("b"),
        _entry("a"),
        GoldenEntry(id="abstain", query="x", expected_behavior="abstain", must_not_retrieve=["d"]),
        _entry("c"),
    ]
    selected = select_entries(entries, limit=2)
    assert [e.id for e in selected] == ["a", "b"]


def test_summarize_arms_means_and_deltas():
    rows = [
        {
            "arm": ARM_LITERAL,
            "recall@1": 0.0,
            "recall@3": 0.0,
            "recall@5": 0.2,
            "recall@8": 0.4,
            "mrr": 0.1,
            "retrieval_ms": 100,
        },
        {
            "arm": ARM_LITERAL,
            "recall@1": 0.2,
            "recall@3": 0.4,
            "recall@5": 0.4,
            "recall@8": 0.6,
            "mrr": 0.3,
            "retrieval_ms": 200,
        },
        {
            "arm": ARM_CONDENSED,
            "recall@1": 1.0,
            "recall@3": 1.0,
            "recall@5": 1.0,
            "recall@8": 1.0,
            "mrr": 0.9,
            "retrieval_ms": 130,
            "condense_ms": 400,
        },
        {
            "arm": ARM_CONDENSED,
            "recall@1": 0.8,
            "recall@3": 1.0,
            "recall@5": 0.8,
            "recall@8": 1.0,
            "mrr": 0.7,
            "retrieval_ms": 150,
            "condense_ms": 800,
        },
        {"arm": ARM_CONDENSED, "error": "boom"},
    ]
    summary = summarize_arms(rows)
    assert summary[ARM_LITERAL]["n"] == 2
    assert summary[ARM_LITERAL]["recall@1"] == 0.1
    assert summary[ARM_CONDENSED]["n"] == 2
    assert summary[ARM_CONDENSED]["recall@1"] == 0.9
    assert summary["delta_recall@1"] == 0.8
    assert summary["condense_p50_ms"] == 800
    # retrieval p50 uses the scored condensed rows only (two values)
    assert summary[ARM_CONDENSED]["retrieval_p50_ms"] in (130, 150)


def test_summary_markdown_names_both_arms_and_the_decision_rule():
    rows = [
        {"arm": ARM_LITERAL, "recall@1": 0.0, "recall@5": 0.0, "mrr": 0.0, "retrieval_ms": 10},
        {
            "arm": ARM_CONDENSED,
            "recall@1": 1.0,
            "recall@5": 1.0,
            "mrr": 1.0,
            "retrieval_ms": 12,
            "condense_ms": 300,
        },
    ]
    report = {
        "rows": rows,
        "failures": 0,
        "summary": summarize_arms(rows),
        "settings": {"collection": "real_manuals", "embed_mode": "vllm", "llm_model": "m"},
    }
    body = summary_markdown(report)
    assert "| literal |" in body
    assert "| condensed |" in body
    assert "separate, dedicated decision" in body
