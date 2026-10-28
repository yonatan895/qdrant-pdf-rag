"""Unit tests for the multi-turn condensation A/B helpers (eval.chat).

Hermetic: follow-up templates, per-arm entry construction, and pure
aggregation only. The live tier runs via `sh scripts/tools/run-task.sh eval:chat` on the RC stack.
"""

from __future__ import annotations

import httpx2

from mainframe_rag.eval.chat import (
    DEFAULT_FOLLOW_UP,
    arm_entry,
    follow_up_query,
    select_entries,
    session_messages,
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
            assert kwargs == {
                "url": "http://synthetic.invalid",
                "api_key": None,
                "timeout": 9,
                "limits": httpx2.Limits(
                    max_connections=100, max_keepalive_connections=20, keepalive_expiry=2.0
                ),
            }

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
        # Measured through the serving gate's validated physical.
        assert collection == "synthetic__gen1" and kwargs["limit"] == 8
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
    from mainframe_rag.agent.serving import ServingGeneration

    verdicts = []
    monkeypatch.setattr(
        chat, "serving_generation",
        lambda c, s: verdicts.append(s) or ServingGeneration("synthetic__gen1", "compatible"),
    )
    entries = [GoldenEntry(id=name, query=name, expected_doc_ids=["doc"])
               for name in ("rewrite", "broken", "fallback")]
    report = chat.evaluate_sessions(entries, settings)
    assert verdicts == [settings]
    assert report["representation"]["servable"] is True
    assert report["representation"]["physical"] == "synthetic__gen1"
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

