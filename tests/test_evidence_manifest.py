"""Evidence-manifest citation validation (issue #364).

Pins that the citation allowlist and the [n] -> excerpt mapping come from the
final supplied-evidence manifest, never the retrieval list: a
retrieved-but-omitted excerpt, or the tail's example cite, can never be
accepted as grounding, and bracket inference reads the fence-processed answer
surface only.
"""

from __future__ import annotations

import pytest

from mainframe_rag.agent.answer import (
    EvidenceEntry,
    PromptEvidence,
    build_messages,
    parse_answer,
)
from mainframe_rag.agent.answer_core import (
    AnswerCoreDeps,
    AnswerCoreInput,
    execute_answer_core,
    execute_answer_core_stream,
)
from mainframe_rag.agent.tokenizer import FallbackTokenizer
from mainframe_rag.config import Settings
from mainframe_rag.ports import ChatMessage, ChatResult, TokenUsage
from mainframe_rag.retrieve.query import SearchHit
from tests.fakes import make_evidence

# Chunk body sized so the 1000-char context budget packs exactly two full
# excerpts (header ~65 chars + 340 body -> 406/excerpt; the third would leave
# a sub-200 tail, so packing stops without a partial).
_SUPPLIED_TEXT = "S" * 340
_PARTIAL_TEXT = "P" * 500


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        llm_base_url="http://llm.internal/v1",
        llm_model_reasoning="test-reasoning-model",
        embed_mode="hash",
        allow_hash_mode=True,
        **overrides,
    )


def _hit(i: int, text: str | None = None) -> SearchHit:
    return SearchHit(
        chunk_id=f"chunk-{i}",
        score=1.0 - i * 0.05,
        cite=f"SA22-0000-0{i} Synthetic Reference, Chapter {i} > IEA500I, p. 1-{i}",
        heading=f"Chapter {i} > IEA500I",
        text=text if text is not None else f"Excerpt {i} body " * 20,
        doc_id=f"SA22-0000-0{i}",
        title="Synthetic Reference",
        page_label=f"1-{i}",
        chunk_type="message",
        message_ids=("IEA500I",),
    )


class CitingLLM:
    """Records the outgoing messages and returns a fixed answer body."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.messages: list[ChatMessage] | None = None

    def chat(self, messages, reasoning_effort=None, temperature=None):
        self.messages = messages
        return ChatResult(content=self.content, finish_reason="stop", usage=TokenUsage())


class StreamingCitingLLM:
    """Streaming twin of CitingLLM: same content, one token + done."""

    def __init__(self, content: str) -> None:
        self.content = content

    async def chat_stream(self, messages, reasoning_effort=None, temperature=None):
        yield {"type": "token", "delta": self.content, "ttft_ms": 1}
        yield {"type": "done", "finish_reason": "stop", "usage": TokenUsage(), "ttft_ms": 1}


class OvershootTokenizer(FallbackTokenizer):
    """Reports a prompt far over any window so the verify loop trims every
    packed excerpt away (the zero-evidence case the issue calls out)."""

    def count_messages(self, messages) -> int:
        return 10**9


def _deps(settings: Settings, llm, **kwargs) -> AnswerCoreDeps:
    return AnswerCoreDeps(
        settings=settings,
        llm=llm,
        qdrant=None,
        embedder=None,
        **kwargs,
    )


def _user_content(messages: list[ChatMessage]) -> str:
    return next(m.content for m in messages if m.role == "user")


# ---------------------------------------------------------------------------
# Manifest construction
# ---------------------------------------------------------------------------


def test_manifest_lists_only_supplied_excerpts():
    """8 retrieved, 2 supplied: entries carry the final labels, omission is
    explicit, and the prompt itself cannot contain the omitted excerpt."""
    hits = [_hit(i, _SUPPLIED_TEXT) for i in range(1, 9)]
    prepared = build_messages(
        "IEA500I", hits, complexity="simple", max_context_chars=1000
    )

    entries = prepared.evidence.entries
    assert [e.prompt_index for e in entries] == [1, 2]
    assert [e.cite for e in entries] == [hits[0].cite, hits[1].cite]
    assert prepared.evidence.omitted_indices == (3, 4, 5, 6, 7, 8)
    assert all(not e.truncated for e in entries)
    assert all(e.included_chars == e.source_chars for e in entries)
    assert "[3]" not in _user_content(prepared.messages)


def test_manifest_records_partial_tail_boundaries():
    """A partially packed excerpt stays evidence, with its retained range and
    truncation flag recorded honestly."""
    hits = [_hit(i, _PARTIAL_TEXT) for i in range(1, 5)]
    prepared = build_messages(
        "IEA500I", hits, complexity="simple", max_context_chars=1000
    )

    entries = prepared.evidence.entries
    assert [e.prompt_index for e in entries] == [1, 2]
    tail = entries[-1]
    assert tail.truncated is True
    assert 0 < tail.included_chars < tail.source_chars
    assert tail.source_chars == len(_PARTIAL_TEXT)
    assert prepared.evidence.omitted_indices == (3, 4)


def test_reordered_blocks_keep_the_same_evidence_labels():
    """prompt_order reorders blocks but never relabels excerpts; the manifest
    and the [n] mapping survive the stable_cache policy."""
    hits = [_hit(1, _SUPPLIED_TEXT), _hit(2, _SUPPLIED_TEXT)]
    prepared = build_messages("IEA500I", hits, complexity="simple", order="stable_cache")

    assert [e.prompt_index for e in prepared.evidence.entries] == [1, 2]
    parsed = parse_answer("See [2].", prepared.evidence)
    assert parsed.citations == [hits[1].cite]
    assert parsed.inferred_indices == [2]


def test_manifest_keeps_distinct_identity_for_duplicate_cites():
    """Two chunks can share one display citation; the manifest keeps both
    identities so internal provenance is never collapsed to the string."""
    shared = _hit(1).cite
    hits = [
        _hit(1, "First body"),
        _hit(2, "Second body").model_copy(update={"cite": shared}),
    ]
    prepared = build_messages("IEA500I", hits, complexity="simple")

    entries = prepared.evidence.entries
    assert [e.cite for e in entries] == [shared, shared]
    assert entries[0].chunk_id != entries[1].chunk_id
    assert prepared.evidence.allowed_citations == frozenset({shared})


def test_manifest_maps_nonconsecutive_labels_without_rank_arithmetic():
    """The [n] mapping is the manifest's label map, never positional
    arithmetic: a future packer may retain nonconsecutive excerpts."""
    cites = [_hit(1).cite, _hit(2).cite]
    evidence = PromptEvidence(
        entries=(
            EvidenceEntry(
                prompt_index=2,
                chunk_id="c2",
                doc_id="D2",
                cite=cites[0],
                truncated=False,
                included_chars=1,
                source_chars=1,
                est_tokens=1,
            ),
            EvidenceEntry(
                prompt_index=5,
                chunk_id="c5",
                doc_id="D5",
                cite=cites[1],
                truncated=False,
                included_chars=1,
                source_chars=1,
                est_tokens=1,
            ),
        ),
        omitted_indices=(1, 3, 4),
    )
    parsed = parse_answer("See [5]. Also [2].", evidence)
    assert parsed.citations == [cites[1], cites[0]]
    assert parsed.inferred_indices == [5, 2]


def test_manifest_empty_when_verification_trims_every_excerpt():
    """Zero trimmable evidence against an irreducible fixed budget is an
    explicit budget failure (issue #368), not an empty manifest: the old
    silent over-budget return is gone. The error carries counts, never
    prompt text."""
    from mainframe_rag.agent.answer import PromptBudgetExceeded

    settings = _settings(
        llm_max_model_len=2000,
        llm_reserved_output_tokens=200,
        llm_token_safety_margin=50,
    )
    with pytest.raises(PromptBudgetExceeded) as exc_info:
        build_messages(
            "IEA500I",
            [_hit(1), _hit(2)],
            tokenizer=OvershootTokenizer(),
            settings=settings,
            complexity="simple",
        )
    assert exc_info.value.used > exc_info.value.limit
    assert "IEA500I" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Claimed path: retrieved-but-omitted sources are rejected
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_retrieved_but_omitted_explicit_citation_rejected():
    hits = [_hit(i, _SUPPLIED_TEXT) for i in range(1, 9)]
    llm = CitingLLM(f"Reissue the command.\n\nCitations:\n{hits[7].cite}\n")
    out = await execute_answer_core(
        AnswerCoreInput(query="IEA500I", hits=hits, query_kind="identifier"),
        _deps(_settings(prompt_max_context_chars=1000), llm),
    )

    assert out.citations == []
    assert out.citations_inferred is False
    assert out.parsed.cites_rejected_unmapped == 1
    assert out.answer == "Reissue the command."
    # The claim is about the actual outgoing prompt, not the budget math:
    # excerpt 8 was never supplied.
    assert llm.messages is not None
    prompt = _user_content(llm.messages)
    assert "[1]" in prompt and "[2]" in prompt
    assert "[3]" not in prompt and "[8]" not in prompt


@pytest.mark.anyio
async def test_retrieved_but_omitted_bracket_marker_rejected():
    hits = [_hit(i, _SUPPLIED_TEXT) for i in range(1, 9)]
    llm = CitingLLM("Reissue the command. See [8].")
    out = await execute_answer_core(
        AnswerCoreInput(query="IEA500I", hits=hits, query_kind="identifier"),
        _deps(_settings(prompt_max_context_chars=1000), llm),
    )

    assert out.citations == []
    assert out.inferred_indices == []
    assert out.citations_inferred is False
    assert out.parsed.inline_bracket_present is True


@pytest.mark.anyio
async def test_supplied_explicit_citation_still_accepted():
    hits = [_hit(i, _SUPPLIED_TEXT) for i in range(1, 9)]
    supplied = hits[1]
    llm = CitingLLM(f"Reissue the command.\n\nCitations:\n{supplied.cite}\n")
    out = await execute_answer_core(
        AnswerCoreInput(query="IEA500I", hits=hits, query_kind="identifier"),
        _deps(_settings(prompt_max_context_chars=1000), llm),
    )

    assert out.citations == [supplied.cite]
    assert out.citations_inferred is False
    assert [e.prompt_index for e in out.evidence.entries] == [1, 2]
    # Retrieval list is retained separately for diagnostics.
    assert out.hits == hits


@pytest.mark.anyio
async def test_supplied_bracket_marker_maps_to_prompt_label():
    hits = [_hit(i, _SUPPLIED_TEXT) for i in range(1, 9)]
    llm = CitingLLM("Reissue the command. See [2].")
    out = await execute_answer_core(
        AnswerCoreInput(query="IEA500I", hits=hits, query_kind="identifier"),
        _deps(_settings(prompt_max_context_chars=1000), llm),
    )

    assert out.citations == [hits[1].cite]
    assert out.citations_inferred is True
    assert out.inferred_indices == [2]


@pytest.mark.anyio
async def test_example_cite_is_not_evidence_when_all_excerpts_trimmed():
    """An irreducible budget fails before generation (issue #368): the tail's
    worked example never becomes evidence, and the model is never called
    with an over-budget prompt."""
    from mainframe_rag.agent.answer import PromptBudgetExceeded

    hits = [_hit(1), _hit(2)]
    llm = CitingLLM("Answer text.\n\nCitations:\n")
    with pytest.raises(PromptBudgetExceeded):
        await execute_answer_core(
            AnswerCoreInput(query="IEA500I", hits=hits, query_kind="identifier"),
            _deps(_settings(), llm, tokenizer=OvershootTokenizer()),
        )
    assert llm.messages is None  # no model call happened


@pytest.mark.anyio
async def test_prior_turn_citation_is_not_current_evidence():
    """A cite the assistant used in an earlier turn is conversation history,
    not evidence supplied for the new answer."""
    current = _hit(1, _SUPPLIED_TEXT)
    previous = _hit(2)
    messages = [
        ChatMessage(role="user", content="What is IEA500I?"),
        ChatMessage(
            role="assistant",
            content=f"Documented earlier.\n\nCitations:\n{previous.cite}",
        ),
        ChatMessage(role="user", content="How do I resolve it?"),
    ]
    llm = CitingLLM(f"Reissue the command.\n\nCitations:\n{previous.cite}\n")
    out = await execute_answer_core(
        AnswerCoreInput(
            query="How do I resolve it?",
            messages=messages,
            is_chat=True,
            hits=[current],
            query_kind="identifier",
        ),
        _deps(_settings(), llm),
    )

    assert out.citations == []
    assert out.parsed.cites_rejected_unmapped == 1
    assert [e.cite for e in out.evidence.entries] == [current.cite]


@pytest.mark.anyio
async def test_omitted_citation_rejected_identically_in_stream_and_json():
    """Buffered and streaming consumers share one finalize path (issue #364
    point 6): the omitted cite is rejected on both, with identical fields."""
    hits = [_hit(i, _SUPPLIED_TEXT) for i in range(1, 9)]
    omitted = hits[7]
    content = f"Reissue the command.\n\nCitations:\n{omitted.cite}\n"

    json_out = await execute_answer_core(
        AnswerCoreInput(query="IEA500I", hits=hits, query_kind="identifier"),
        _deps(_settings(prompt_max_context_chars=1000), CitingLLM(content)),
    )
    items = [
        item
        async for item in execute_answer_core_stream(
            AnswerCoreInput(query="IEA500I", hits=hits, query_kind="identifier"),
            _deps(_settings(prompt_max_context_chars=1000), StreamingCitingLLM(content)),
        )
    ]
    stream_out = items[-1]["output"]

    assert [i["type"] for i in items] == ["token", "final"]
    for field in ("answer", "citations", "citations_inferred", "inferred_indices"):
        assert getattr(stream_out, field) == getattr(json_out, field)
    assert stream_out.citations == []
    assert [e.prompt_index for e in stream_out.evidence.entries] == [1, 2]


# ---------------------------------------------------------------------------
# Marker surface and grammar
# ---------------------------------------------------------------------------


def _marker_cases():
    cites = [_hit(1).cite, _hit(2).cite]
    return cites, make_evidence(cites)


@pytest.mark.parametrize(
    "content,expected_indices,inline",
    [
        ("Points from [1].", [1], True),
        ("Points from [ 2 ].", [2], True),
        ("Points from [1,2].", [1, 2], True),
        ("Points from [1, 2].", [1, 2], True),
        ("Summarized in [1] and [2].", [1, 2], True),
        ("z/OS (3.1), APARs (1, 2), option (2).", [], False),
        ("Broken marker [1.5].", [], False),
        ("See [abc].", [], False),
        ("See [0].", [], True),
        ("See [99].", [], True),
    ],
)
def test_bracket_marker_grammar(content, expected_indices, inline):
    cites, evidence = _marker_cases()
    parsed = parse_answer(content, evidence)
    assert parsed.citations == [cites[i - 1] for i in expected_indices]
    assert parsed.inferred_indices == expected_indices
    assert parsed.citations_inferred == bool(expected_indices)
    assert parsed.inline_bracket_present is inline


def test_marker_in_dropped_thinking_fence_is_not_provenance():
    _, evidence = _marker_cases()
    parsed = parse_answer("```thinking\nconsider [1] carefully\n```\nFinal answer.", evidence)
    assert parsed.citations == []
    assert parsed.citations_inferred is False
    assert parsed.inline_bracket_present is False


def test_marker_in_extracted_script_fence_is_not_provenance():
    _, evidence = _marker_cases()
    parsed = parse_answer("```jcl\n// STEP [1]\n```\nFinal answer.", evidence)
    assert parsed.citations == []
    assert parsed.script == "// STEP [1]"
    assert parsed.inline_bracket_present is False


def test_example_cite_alone_in_citations_block_is_not_supplied():
    """With zero supplied evidence, an explicit cite line is rejected even
    when the prompt's own example shows that exact string."""
    example = _hit(1).cite
    parsed = parse_answer(
        f"Answer text.\n\nCitations:\n{example}\n", make_evidence(set())
    )
    assert parsed.citations == []
    assert parsed.citations_inferred is False
    assert parsed.cites_rejected_unmapped == 1
