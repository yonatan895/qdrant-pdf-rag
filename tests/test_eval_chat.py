"""Unit tests for the multi-turn condensation A/B helpers (eval.chat).

Hermetic: follow-up templates, per-arm entry construction, and pure
aggregation only. The live tier runs via `sh scripts/tools/run-task.sh eval:chat` on the RC stack.
"""

from __future__ import annotations

from mainframe_rag.eval.chat import (
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
from mainframe_rag.eval.datasets import GoldenEntry


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


def test_evaluate_sessions_preserves_both_arms_and_condensation_accounting(monkeypatch):
    from types import SimpleNamespace

    import qdrant_client

    from mainframe_rag.eval import chat
    from mainframe_rag.ingest import embed
    from mainframe_rag.retrieve import rerank

    events = []
    settings = SimpleNamespace(qdrant_url="http://synthetic.invalid", qdrant_api_key=None,
                               qdrant_timeout_s=9, qdrant_collection="synthetic",
                               embed_mode="hash", llm_model_reasoning="synthetic-model")

    class Storage:
        def __init__(self, **kwargs):
            assert kwargs == {"url": "http://synthetic.invalid", "api_key": None, "timeout": 9}

        def close(self):
            events.append(("close", "storage"))

    class LLM:
        def __init__(self, actual):
            assert actual is settings

        def close(self):
            events.append(("close", "llm"))

    async def condense(llm, messages, actual):
        assert isinstance(llm, LLM) and actual is settings
        assert [message.role for message in messages] == ["user", "assistant", "user"]
        opener = messages[0].content
        events.append(("condense", opener))
        if opener == "broken":
            raise RuntimeError("synthetic condensation failure")
        return "rewritten identifier" if opener == "rewrite" else ""

    def search(client, embedder, collection, query, **kwargs):
        assert isinstance(client, Storage) and embedder is sentinel
        assert collection == "synthetic" and kwargs["limit"] == 8
        assert kwargs["settings"] is settings and kwargs["reranker"] is None
        events.append(("retrieve", query))
        return [], "identifier", {"synthetic_ms": 1}

    sentinel = object()
    monkeypatch.setattr(qdrant_client, "QdrantClient", Storage)
    monkeypatch.setattr(embed, "build_embedder", lambda actual: sentinel)
    monkeypatch.setattr(rerank, "build_reranker", lambda actual: None)
    monkeypatch.setattr(chat, "HttpxLLMClient", LLM)
    monkeypatch.setattr(chat, "condense_query", condense)
    monkeypatch.setattr(chat, "retrieve_search", search)
    entries = [GoldenEntry(id=name, query=name, expected_doc_ids=["doc"])
               for name in ("rewrite", "broken", "fallback")]
    report = chat.evaluate_sessions(entries, settings)
    follow_up = "Tell me more about this."
    assert events == [
        ("retrieve", follow_up), ("condense", "rewrite"), ("retrieve", "rewritten identifier"),
        ("retrieve", follow_up), ("condense", "broken"),
        ("retrieve", follow_up), ("condense", "fallback"), ("retrieve", follow_up),
        ("close", "llm"), ("close", "storage"),
    ]
    assert [(row["session"], row["arm"]) for row in report["rows"]] == [
        ("rewrite", "literal"), ("rewrite", "condensed"),
        ("broken", "literal"), ("broken", "condensed"),
        ("fallback", "literal"), ("fallback", "condensed"),
    ]
    assert report["rows"][3]["error"] == "synthetic condensation failure"
    assert report["rows"][1]["follow_up"] == "rewritten identifier"
    assert report["rows"][5]["follow_up"] == follow_up
    assert report["failures"] == 1
    assert report["summary"]["literal"]["n"] == 3
    assert report["summary"]["condensed"]["n"] == 2
    assert report["summary"]["delta_recall@1"] == 0.0


def test_chat_consumes_canonical_datasets_and_scoring():
    from mainframe_rag.eval import chat, datasets, retrieval

    assert chat.GoldenEntry is datasets.GoldenEntry
    assert chat.score_entry is retrieval.score_entry
