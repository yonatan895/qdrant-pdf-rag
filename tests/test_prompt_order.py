"""Prompt block ordering seam (Step 1 interfaces pass).

build_messages assembles NAMED blocks and orders them through
order_prompt_blocks (Settings.prompt_order). The default "retrieval" policy
is byte-identical to the historical prompt; issue #80 adds policies here.
These tests pin the seam: identity, fail-closed dispatch, exact assembly
(including the header ride-along and the empty-hits shape), and order
preservation.
"""

import pytest

from mainframe_rag.agent.answer import build_messages, order_prompt_blocks
from mainframe_rag.retrieve.query import SearchHit


def _hit(cite: str, text: str) -> SearchHit:
    return SearchHit(
        chunk_id="abc123",
        score=0.42,
        cite=cite,
        heading="Chapter 2",
        text=text,
        doc_id="SA22-0000-00",
        title="Synthetic Reference",
        page_label="1-6",
        chunk_type="message",
        product="z/OS",
        version="9.9",
        message_ids=("IEA500I",),
    )


def test_order_identity_returns_equal_new_list():
    blocks = [("question", "q"), ("excerpt", "e1"), ("tail", "t")]
    ordered = order_prompt_blocks(blocks)
    assert ordered == blocks
    assert ordered is not blocks
    assert blocks == [("question", "q"), ("excerpt", "e1"), ("tail", "t")]  # unmutated


def test_order_empty_list():
    assert order_prompt_blocks([]) == []


def test_order_unknown_policy_fails_closed():
    with pytest.raises(ValueError, match="retrieval"):
        order_prompt_blocks([("question", "q")], "prefix_cache")


def test_build_messages_funnels_order_through_policy():
    with pytest.raises(ValueError, match="retrieval"):
        build_messages("q", [_hit("cite, p. 1", "body")], order="nope")  # type: ignore[arg-type]


def test_default_assembly_is_exact():
    """Byte pin of the historical user message: question, headed excerpts
    in retrieval order, tail last."""
    hit1 = _hit("SA22-0000-00 Ref, Chapter 2 > IEA500I, p. 1-6", "First body.")
    hit2 = _hit("SA22-7777-01 Ref, Chapter 1 > IEB700I, p. 2-3", "Second body.")
    messages = build_messages("Do the thing?", [hit1, hit2], complexity="simple")
    user = messages[1].content
    tail = (
        "Please answer based strictly on the retrieved manual excerpts above and conclude "
        "with the 'Citations:' section copying the exact citation line for each excerpt used, "
        "for example:\nCitations:\nSA22-0000-00 Ref, Chapter 2 > IEA500I, p. 1-6"
    )
    assert user == (
        "Question: Do the thing?"
        "\n\nRetrieved manual excerpts:\n[1] SA22-0000-00 Ref, Chapter 2 > IEA500I, p. 1-6\nFirst body."
        "\n\n[2] SA22-7777-01 Ref, Chapter 1 > IEB700I, p. 2-3\nSecond body."
        f"\n\n{tail}"
    )
    assert messages[0].role == "system" and messages[0].content


def test_empty_hits_keeps_bare_section_header():
    messages = build_messages("q?", [], complexity="simple")
    user = messages[1].content
    assert "Retrieved manual excerpts:\n" in user
    assert "[1]" not in user
    assert user.rstrip().endswith("p. 1-17")  # fallback example cite in the tail


def test_context_block_precedes_question():
    messages = build_messages("q?", [_hit("c, p. 1", "b")], product="z/OS", version="3.2", complexity="simple")
    user = messages[1].content
    assert user.index("Sysplex context: product: z/OS, version: 3.2") < user.index("Question: q?")
    no_context = build_messages("q?", [_hit("c, p. 1", "b")], complexity="simple")[1].content
    assert no_context.startswith("Question: q?")


def test_excerpt_order_follows_input_not_cite_sort():
    """Retrieval policy preserves rank order even when cites sort otherwise."""
    hits = [
        _hit("ZZZ last-sorting cite, p. 9", "body one."),
        _hit("AAA first-sorting cite, p. 1", "body two."),
        _hit("MMM middle cite, p. 5", "body three."),
    ]
    user = build_messages("q?", hits, complexity="simple")[1].content
    assert user.index("body one.") < user.index("body two.") < user.index("body three.")
    assert user.index("[1] ZZZ") < user.index("[2] AAA") < user.index("[3] MMM")
