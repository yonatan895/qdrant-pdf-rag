"""Unit tests for the shared answer core (M2 extraction).

Route handlers pass precomputed hits, so these tests drive the branches the
routes do not: the core's own retrieval leg (success and typed failure), the
condensation gate, and the empty-hits stream shape.
"""

from __future__ import annotations

import inspect

import pytest

from mainframe_rag.agent.answer import (
    REASON_MALFORMED_FRAME,
    REASON_MISSING_FINISH,
    TruncatedStreamError,
)
from mainframe_rag.agent.answer_core import (
    AnswerCoreDeps,
    AnswerCoreInput,
    LLMChatError,
    RetrievalError,
    chat_body_chars,
    execute_answer_core,
    execute_answer_core_stream,
    resolve_search_query,
)
from mainframe_rag.agent.core_ports import RetrievalResult
from mainframe_rag.agent.model_adapter import ModelAdapter
from mainframe_rag.config import Settings
from mainframe_rag.ports import ChatMessage, ChatResult, TokenUsage
from mainframe_rag.retrieve.query import SearchHit


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        llm_base_url="http://llm.internal/v1",
        llm_model_reasoning="test-reasoning-model",
        embed_mode="hash",
        allow_hash_mode=True,
        **overrides,
    )


def _hit() -> SearchHit:
    return SearchHit(
        chunk_id="abc123",
        score=0.42,
        cite="SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6",
        heading="Chapter 2 > IEA500I",
        text="IEA500I synthetic text",
        doc_id="SA22-0000-00",
        title="Synthetic Reference",
        page_label="1-6",
        chunk_type="message",
        product="z/OS",
        version="9.9",
        message_ids=("IEA500I",),
    )


class CoreFakeLLM:
    def __init__(self, content: str | None = None, raise_exc: Exception | None = None):
        self.content = content or (
            "Reissue the command.\n\nCitations:\n"
            "- SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6\n"
        )
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def chat(self, messages, reasoning_effort=None, temperature=None):
        self.calls.append({"messages": messages, "reasoning_effort": reasoning_effort})
        if self.raise_exc is not None:
            raise self.raise_exc
        return ChatResult(content=self.content, finish_reason="stop", usage=TokenUsage())


def _deps(settings: Settings, llm, retrieve) -> AnswerCoreDeps:
    async def retrieve_adapter(query, *, product, version, settings):
        result = retrieve(None, None, settings.qdrant_collection, query,
                          product=product, version=version, settings=settings, limit=8)
        if inspect.isawaitable(result):
            result = await result
        return RetrievalResult(*result)

    return AnswerCoreDeps(settings=settings, llm=ModelAdapter(llm), retrieve=retrieve_adapter)


def test_chat_body_chars_counts_messages_and_context():
    messages = [
        ChatMessage(role="user", content="abc"),
        ChatMessage(role="assistant", content="de"),
    ]
    assert chat_body_chars(messages) == 5
    assert chat_body_chars(messages, "xyz") == 8
    assert chat_body_chars(messages, "") == 5


@pytest.mark.anyio
async def test_core_retrieval_branch_feeds_prompt_and_output():
    calls: list[str] = []

    def retrieve(qdrant, embedder, collection, query, **kwargs):
        calls.append(query)
        return [_hit()], "identifier", {"embed_ms": 3, "qdrant_ms": 4}

    llm = CoreFakeLLM()
    out = await execute_answer_core(
        AnswerCoreInput(query="IEA500I rejected"),
        _deps(_settings(), llm, retrieve),
    )

    assert calls == ["IEA500I rejected"]
    assert out.query_kind == "identifier"
    assert out.timings == {"embed_ms": 3, "qdrant_ms": 4}
    assert out.citations == [_hit().cite]
    assert out.answer == "Reissue the command."
    assert llm.calls[0]["messages"][-1].role == "user"


@pytest.mark.anyio
async def test_core_retrieval_failure_raises_typed_error():
    def retrieve(*_a, **_k):
        raise RuntimeError("qdrant exploded")

    with pytest.raises(RetrievalError) as excinfo:
        await execute_answer_core(
            AnswerCoreInput(query="IEA500I"),
            _deps(_settings(), CoreFakeLLM(), retrieve),
        )
    assert isinstance(excinfo.value.original, RuntimeError)


@pytest.mark.anyio
async def test_core_stream_retrieval_failure_raises_typed_error():
    def retrieve(*_a, **_k):
        raise RuntimeError("qdrant exploded")

    with pytest.raises(RetrievalError):
        async for _item in execute_answer_core_stream(
            AnswerCoreInput(query="IEA500I"),
            _deps(_settings(), CoreFakeLLM(), retrieve),
        ):
            pass


@pytest.mark.anyio
async def test_core_llm_failure_raises_typed_error():
    def retrieve(*_a, **_k):
        return [_hit()], "identifier", {}

    with pytest.raises(LLMChatError):
        await execute_answer_core(
            AnswerCoreInput(query="IEA500I"),
            _deps(_settings(), CoreFakeLLM(raise_exc=RuntimeError("boom")), retrieve),
        )


@pytest.mark.anyio
async def test_core_stream_empty_hits_yields_only_final():
    def retrieve(*_a, **_k):
        return [], "nl", {"embed_ms": 1, "qdrant_ms": 1}

    items = [
        item
        async for item in execute_answer_core_stream(
            AnswerCoreInput(query="random obscure thing"),
            _deps(_settings(), CoreFakeLLM(), retrieve),
        )
    ]
    assert [item["type"] for item in items] == ["final"]
    output = items[0]["output"]
    assert output.answer == "No supporting manual excerpts were found for this question."
    assert output.citations == []
    assert output.hits == []


@pytest.mark.anyio
async def test_resolve_search_query_condenses_only_when_enabled():
    messages = [
        ChatMessage(role="user", content="What causes IEA500I?"),
        ChatMessage(role="assistant", content="It is an IOS command rejection."),
        ChatMessage(role="user", content="How do I resolve this?"),
    ]

    class CondensingLLM(CoreFakeLLM):
        def chat(self, messages, reasoning_effort=None, temperature=None):
            self.calls.append({"messages": messages, "reasoning_effort": reasoning_effort})
            return ChatResult(
                content="IEA500I recovery procedure", finish_reason="stop", usage=TokenUsage()
            )

    off_input = AnswerCoreInput(query="How do I resolve this?", messages=messages, is_chat=True)
    llm_off = CondensingLLM()
    assert (
        await resolve_search_query(off_input, _deps(_settings(), llm_off, None))
        == "How do I resolve this?"
    )
    assert llm_off.calls == []

    on_input = AnswerCoreInput(query="How do I resolve this?", messages=messages, is_chat=True)
    llm_on = CondensingLLM()
    assert (
        await resolve_search_query(
            on_input, _deps(_settings(chat_condense_enabled=True), llm_on, None)
        )
        == "IEA500I recovery procedure"
    )
    assert len(llm_on.calls) == 1


@pytest.mark.anyio
async def test_core_reasoning_effort_explicit_override_sync():
    """Explicit reasoning_effort overrides complexity-based default in execute_answer_core."""
    def retrieve(*_a, **_k):
        return [_hit()], "nl", {"embed_ms": 1, "qdrant_ms": 1}

    # Query without override -> simple query defaults to "low"
    llm1 = CoreFakeLLM()
    await execute_answer_core(
        AnswerCoreInput(query="simple query"),
        _deps(_settings(), llm1, retrieve),
    )
    assert llm1.calls[0]["reasoning_effort"] == "low"

    # Simple query with explicit override="high" -> gets "high"
    llm2 = CoreFakeLLM()
    await execute_answer_core(
        AnswerCoreInput(query="simple query", reasoning_effort="high"),
        _deps(_settings(), llm2, retrieve),
    )
    assert llm2.calls[0]["reasoning_effort"] == "high"

    # Complex query with explicit override="low" -> gets "low"
    llm3 = CoreFakeLLM()
    await execute_answer_core(
        AnswerCoreInput(query="diagnose abend S0C4 with registers and spool dump", reasoning_effort="low"),
        _deps(_settings(), llm3, retrieve),
    )
    assert llm3.calls[0]["reasoning_effort"] == "low"

    # Invalid override ignored -> falls back to complexity default ("low" for simple)
    llm4 = CoreFakeLLM()
    await execute_answer_core(
        AnswerCoreInput(query="simple query", reasoning_effort="ultra"),  # type: ignore[arg-type]
        _deps(_settings(), llm4, retrieve),
    )
    assert llm4.calls[0]["reasoning_effort"] == "low"


@pytest.mark.anyio
async def test_core_reasoning_effort_explicit_override_stream():
    """Explicit reasoning_effort overrides complexity-based default in execute_answer_core_stream."""
    def retrieve(*_a, **_k):
        return [_hit()], "nl", {"embed_ms": 1, "qdrant_ms": 1}

    llm = CoreFakeLLM()
    async for _ in execute_answer_core_stream(
        AnswerCoreInput(query="simple query", reasoning_effort="medium"),
        _deps(_settings(), llm, retrieve),
    ):
        pass
    assert llm.calls[0]["reasoning_effort"] == "medium"


@pytest.mark.anyio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("precomputed", [False, True])
async def test_core_normalizes_chat_for_retrieval_complexity_and_prompt(stream, precomputed):
    queries = []
    classifications = []
    active = "What does IEA500I mean?"

    def retrieve(_qdrant, _embedder, _collection, query, **_kwargs):
        queries.append(query)
        return [_hit()], "identifier", {}

    def classify(query):
        classifications.append(query)
        return "simple"

    llm = CoreFakeLLM()
    deps = _deps(_settings(), llm, retrieve)
    deps.classify_query_complexity_fn = classify
    source = AnswerCoreInput(
        query="Different redundant query S0C4",
        messages=[ChatMessage(role="user", content=f" {active}\n"),
                  ChatMessage(role="assistant", content="LATER_NON_USER S0C4")],
        is_chat=True,
        hits=[_hit()] if precomputed else None,
    )
    if stream:
        events = [item async for item in execute_answer_core_stream(source, deps)]
        assert events[-1]["type"] == "final"
    else:
        result = await execute_answer_core(source, deps)
        assert result.finish_reason == "stop"
    assert queries == ([] if precomputed else [active])
    assert classifications == [active]
    assert len(llm.calls) == 1
    assert f"Question: {active}\n" in llm.calls[0]["messages"][-1].content
    assert "LATER_NON_USER" not in llm.calls[0]["messages"][-1].content
    assert source.query == "Different redundant query S0C4"  # Caller input is not mutated.


@pytest.mark.anyio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("messages", [None, [], [ChatMessage(role="assistant", content="No user")],
                                       [ChatMessage(role="user", content=" \t ")]])
async def test_core_invalid_chat_refuses_even_with_precomputed_hits(stream, messages):
    from mainframe_rag.agent.chat_turn import InvalidChatTurn

    def retrieve(*_args, **_kwargs):
        raise AssertionError("invalid input must not retrieve")

    llm = CoreFakeLLM()
    deps = _deps(_settings(), llm, retrieve)
    source = AnswerCoreInput(query="A redundant query cannot rescue invalid chat", messages=messages,
                             is_chat=True, hits=[_hit()])
    with pytest.raises(InvalidChatTurn):
        if stream:
            [item async for item in execute_answer_core_stream(source, deps)]
        else:
            await execute_answer_core(source, deps)
    assert llm.calls == []


class _DoneSeamLLM:
    """chat_stream double yielding one grounded token, then a configurable
    terminal item: used to pin the core's explicit-finish gate (issue #365).
    A falsy done finish or a missing done must never finalize as "stop"."""

    def __init__(self, done_item):
        self._done_item = done_item

    async def chat_stream(self, messages, reasoning_effort=None, temperature=None):
        yield {"type": "token", "delta": "Reissue the command."}
        if self._done_item is not None:
            yield self._done_item

    def chat(self, *a, **k):
        raise AssertionError("buffered chat must not run on the stream path")


def _stream_deps(llm) -> AnswerCoreDeps:
    def retrieve(*_a, **_k):
        return [_hit()], "identifier", {}

    return _deps(_settings(), llm, retrieve)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "done_item, reason",
    [
        ({"type": "done", "finish_reason": None, "usage": TokenUsage()}, REASON_MISSING_FINISH),
        ({"type": "done", "usage": TokenUsage()}, REASON_MISSING_FINISH),
        ({"type": "done", "finish_reason": "", "usage": TokenUsage()}, REASON_MALFORMED_FRAME),
        ({"type": "done", "finish_reason": 42, "usage": TokenUsage()}, REASON_MALFORMED_FRAME),
    ],
)
async def test_core_stream_rejects_falsy_done_finish(done_item, reason):
    """The shared core validates the terminal done finish with buffered-parser
    parity (issue #365): missing/null finish and misshapen values raise fixed
    truncation errors instead of finalizing the prefix as accepted "stop"."""
    items = []
    with pytest.raises(TruncatedStreamError) as excinfo:
        async for item in execute_answer_core_stream(
            AnswerCoreInput(query="IEA500I rejected"), _stream_deps(_DoneSeamLLM(done_item))
        ):
            items.append(item)
    assert excinfo.value.reason == reason
    # The provisional token was already yielded and cannot be retracted; only
    # the terminal outcome is refused (the app takes its event: error path).
    assert [i["type"] for i in items] == ["token"]


@pytest.mark.anyio
async def test_core_stream_without_done_raises_missing_finish():
    """A token-only stream with no terminal done item is incomplete: the core
    must not fall back to an initial "stop" (issue #365)."""
    items = []
    with pytest.raises(TruncatedStreamError) as excinfo:
        async for item in execute_answer_core_stream(
            AnswerCoreInput(query="IEA500I rejected"), _stream_deps(_DoneSeamLLM(None))
        ):
            items.append(item)
    assert excinfo.value.reason == REASON_MISSING_FINISH
    assert [i["type"] for i in items] == ["token"]


@pytest.mark.anyio
async def test_core_stream_explicit_done_still_finalizes():
    """Guard against over-strictness: a custom chat_stream double with an
    explicit string done finish finalizes normally (issue #365)."""
    items = [
        item
        async for item in execute_answer_core_stream(
            AnswerCoreInput(query="IEA500I rejected"),
            _stream_deps(
                _DoneSeamLLM(
                    {"type": "done", "finish_reason": "stop", "usage": TokenUsage()}
                )
            ),
        )
    ]
    assert [i["type"] for i in items] == ["token", "final"]
    assert items[-1]["output"].finish_reason == "stop"


@pytest.mark.anyio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_structured_model_results_match_buffered_and_fallback_stream(asynchronous):
    """Sync/async boundary adaptation preserves structured completion metadata."""
    expected = ChatResult(content="Synthetic answer.", finish_reason="stop",
                          usage=TokenUsage(prompt_tokens=7, completion_tokens=3, total_tokens=10), ttft_ms=17)

    class StructuredModel:
        def chat(self, messages, reasoning_effort=None, temperature=None):
            async def result():
                return expected
            return result() if asynchronous else expected

    deps = _stream_deps(StructuredModel())
    source = AnswerCoreInput(query="IEA500I rejected")
    buffered = await execute_answer_core(source, deps)
    events = [item async for item in execute_answer_core_stream(source, deps)]
    final = events[-1]["output"]
    assert buffered.answer == final.answer == "Synthetic answer."
    assert buffered.finish_reason == final.finish_reason == "stop"
    assert buffered.usage == final.usage == expected.usage
    assert buffered.ttft_ms == final.ttft_ms == expected.ttft_ms
    assert buffered.citations == final.citations == []
    assert buffered.verification_state == final.verification_state
    assert [item["type"] for item in events] == ["token", "final"]


@pytest.mark.anyio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
async def test_unstructured_completion_fails_then_next_structured_answer_succeeds(asynchronous, streaming):
    class Model:
        response = "Unstructured answer must not become a completed result."

        def chat(self, messages, reasoning_effort=None, temperature=None):
            async def result():
                return self.response
            return result() if asynchronous else self.response

    model = Model()
    deps = _stream_deps(model)
    source = AnswerCoreInput(query="IEA500I rejected")
    events = []
    if streaming:
        with pytest.raises(TypeError, match="model completion must be ChatResult"):
            async for item in execute_answer_core_stream(source, deps):
                events.append(item)
        assert events == []
    else:
        with pytest.raises(LLMChatError) as error:
            await execute_answer_core(source, deps)
        assert isinstance(error.value.original, TypeError)

    model.response = ChatResult(content="Synthetic answer.", finish_reason="length", usage=TokenUsage())
    if streaming:
        events = [item async for item in execute_answer_core_stream(source, deps)]
        output = events[-1]["output"]
    else:
        output = await execute_answer_core(source, deps)
    assert output.finish_reason == "length"
    assert output.verification_state == "generation_incomplete"
    model.response = ChatResult(content="Synthetic answer.", finish_reason="stop", usage=TokenUsage())
    output = await execute_answer_core(source, deps)
    assert output.answer == "Synthetic answer." and output.finish_reason == "stop"


@pytest.mark.anyio
async def test_closing_core_stream_releases_operation_without_closing_shared_model():
    closed = []

    class Model(CoreFakeLLM):
        async def chat_stream(self, messages, reasoning_effort=None, temperature=None):
            try:
                yield {"type": "token", "delta": "Provisional."}
                yield {"type": "done", "finish_reason": "stop"}
            finally:
                closed.append("operation")

        def close(self):
            raise AssertionError("core does not own the shared client")

    deps = _stream_deps(Model())
    stream = execute_answer_core_stream(AnswerCoreInput(query="IEA500I"), deps)
    assert (await anext(stream))["type"] == "token"
    await stream.aclose()
    assert closed == ["operation"]
    # Cleanup must preserve the next ordinary operation, not only cancellation.
    result = [event async for event in execute_answer_core_stream(
        AnswerCoreInput(query="IEA500I"), deps
    )]
    assert result[-1]["type"] == "final"
    assert closed == ["operation", "operation"]


def _assert_core_import_boundary():
    """Follow deferred imports too: a cold import alone misses function-local dependencies."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src"
    pending = ["mainframe_rag.agent.answer_core", "mainframe_rag.agent.model_adapter"]
    visited = set()
    forbidden = ("mainframe_rag.agent.app", "mainframe_rag.agent.sse",
                 "mainframe_rag.webui", "mainframe_rag.mcp")
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        assert not any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden), name
        path = root.joinpath(*name.split(".")).with_suffix(".py")
        if not path.is_file():
            path = root.joinpath(*name.split("."), "__init__.py")
        if not path.is_file():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                pending.extend(alias.name for alias in node.names
                               if alias.name.startswith("mainframe_rag."))
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.level == 0, "resolve relative imports in the boundary walker before use"
                if node.module.startswith("mainframe_rag."):
                    pending.append(node.module)
                    pending.extend(node.module + "." + alias.name for alias in node.names)


def test_core_import_graph_excludes_transports_and_application_singleton():
    _assert_core_import_boundary()


@pytest.mark.parametrize(
    "module, injected, forbidden",
    [
        ("answer_core", "import mainframe_rag.agent.app", "mainframe_rag.agent.app"),
        ("model_adapter", "def deferred():\n    from mainframe_rag.webui import routes",
         "mainframe_rag.webui.routes"),
        ("core_ports", "from mainframe_rag.agent import sse", "mainframe_rag.agent.sse"),
    ],
)
def test_core_import_checker_rejects_forbidden_dependency(monkeypatch, module, injected, forbidden):
    """Challenge the actual graph checker without importing or modifying application code."""
    import re
    from pathlib import Path

    target = Path(__file__).resolve().parents[1] / "src/mainframe_rag/agent" / (module + ".py")
    original_read = Path.read_text

    def with_forbidden_import(path, *args, **kwargs):
        source = original_read(path, *args, **kwargs)
        return source + "\n" + injected + "\n" if path == target else source

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", with_forbidden_import)
        with pytest.raises(AssertionError, match=re.escape(forbidden)):
            _assert_core_import_boundary()
    _assert_core_import_boundary()


def test_core_type_boundary_rejects_storage_injection_and_write_use(tmp_path):
    """The same checker used by qa:typecheck must accept reads and reject admin access."""
    import subprocess
    import sys

    source = tmp_path / "capabilities.py"
    imports = """
from mainframe_rag.agent.answer_core import AnswerCoreDeps
from mainframe_rag.agent.core_ports import AnswerModel, Retriever
from mainframe_rag.config import Settings
from mainframe_rag.ports import QdrantBatchSearch, QdrantPoints, QdrantSearch
from qdrant_client import QdrantClient, AsyncQdrantClient
"""
    source.write_text(imports + """
def good(settings: Settings, model: AnswerModel, read: Retriever) -> AnswerCoreDeps:
    return AnswerCoreDeps(settings=settings, llm=model, retrieve=read)

def real_clients(sync: QdrantClient, async_client: AsyncQdrantClient) -> None:
    sync_read: QdrantSearch = sync
    async_read: QdrantSearch = async_client
    sync_batch: QdrantBatchSearch = sync
    async_batch: QdrantBatchSearch = async_client
""")
    command = [sys.executable, "-m", "mypy", "--strict", "--follow-imports=silent",
               "--no-incremental", str(source)]
    good = subprocess.run(command, capture_output=True, text=True, check=False)
    assert good.returncode == 0, good.stdout + good.stderr
    source.write_text(imports + """
def bad(settings: Settings, model: AnswerModel, writer: QdrantPoints,
        read: QdrantSearch, deps: AnswerCoreDeps) -> None:
    AnswerCoreDeps(settings=settings, llm=model, retrieve=writer)
    read.upsert("corpus", points=[])
    deps.qdrant.upsert("corpus", points=[])
""")
    bad = subprocess.run(command, capture_output=True, text=True, check=False)
    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert 'incompatible type "QdrantPoints"; expected "Retriever"' in bad.stdout
    assert '"QdrantSearch" has no attribute "upsert"' in bad.stdout
    assert '"AnswerCoreDeps" has no attribute "qdrant"' in bad.stdout
    assert "Found 3 errors" in bad.stdout


@pytest.mark.anyio
async def test_cancelling_pending_model_read_closes_operation_and_propagates():
    import asyncio

    waiting = asyncio.Event()
    closed = []

    class Model(CoreFakeLLM):
        async def chat_stream(self, messages, reasoning_effort=None, temperature=None):
            try:
                yield {"type": "token", "delta": "Provisional."}
                waiting.set()
                await asyncio.Event().wait()
            finally:
                closed.append("operation")

    deps = _stream_deps(Model())
    stream = execute_answer_core_stream(AnswerCoreInput(query="IEA500I"), deps)
    assert (await anext(stream))["type"] == "token"
    pending = asyncio.create_task(anext(stream))
    await asyncio.wait_for(waiting.wait(), timeout=2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert closed == ["operation"]
    assert (await execute_answer_core(AnswerCoreInput(query="IEA500I"), deps)).finish_reason == "stop"


_STREAM_BAD_SEQUENCES = [
    [{"type": "token", "delta": "provisional"}, {"type": "error", "error": "synthetic"},
     {"type": "done", "finish_reason": "stop"}],
    [{"type": "done", "finish_reason": "length"}, {"type": "done", "finish_reason": "stop"}],
    [{"type": "done", "finish_reason": "stop"}, {"type": "token", "delta": "late"}],
    [{"type": "metadata"}, {"type": "done", "finish_reason": "stop"}],
    [{"type": "token", "delta": False}, {"type": "done", "finish_reason": "stop"}],
    [{"type": "token", "delta": 0}, {"type": "done", "finish_reason": "stop"}],
    [{"type": "token", "delta": None}, {"type": "done", "finish_reason": "stop"}],
    [{"type": "token", "delta": []}, {"type": "done", "finish_reason": "stop"}],
    [{"type": "done", "finish_reason": "stop", "usage": False}],
    [{"type": "done", "finish_reason": "stop", "usage": 0}],
    [{"type": "done", "finish_reason": "stop", "usage": {}}],
    [{"type": "done", "finish_reason": "stop", "usage": None}],
    [{"type": "token", "delta": "text", "ttft_ms": False}, {"type": "done", "finish_reason": "stop"}],
    [{"type": "done", "finish_reason": False}],
    [{"type": "token", "delta": "text", "error": "synthetic"}, {"type": "done", "finish_reason": "stop"}],
    [False, {"type": "done", "finish_reason": "stop"}],
]


@pytest.mark.anyio
@pytest.mark.parametrize('seam', ['adapter', 'core-legacy', 'core-typed'])
@pytest.mark.parametrize('frames', _STREAM_BAD_SEQUENCES)
async def test_stream_grammar_rejects_invalid_sequences_and_next_operation_recovers(seam, frames):
    from mainframe_rag.agent.core_ports import ModelDone, ModelToken

    class Model(CoreFakeLLM):
        def __init__(self):
            self.frames = frames
            self.closed = 0

        async def chat_stream(self, *args, **kwargs):
            try:
                for frame in self.frames:
                    yield frame
            finally:
                self.closed += 1

        async def stream(self, *args, **kwargs):
            # Exercise the public typed port directly, bypassing ModelAdapter.
            try:
                for frame in self.frames:
                    if not isinstance(frame, dict) or 'error' in frame:
                        yield frame
                    elif frame.get('type') == 'token':
                        yield ModelToken(frame.get('delta'), frame.get('ttft_ms'))
                    elif frame.get('type') == 'done':
                        yield ModelDone(frame.get('finish_reason'), frame.get('usage', TokenUsage()),
                                        frame.get('ttft_ms'))
                    else:
                        yield frame
            finally:
                self.closed += 1

    model = Model()
    deps = _stream_deps(model)
    if seam == 'core-typed':
        deps.llm = model

    seen = []

    async def collect():
        if seam == 'adapter':
            return [event async for event in ModelAdapter(model).stream([], 'low', 0.0)]
        async for event in execute_answer_core_stream(AnswerCoreInput(query='IEA500I'), deps):
            seen.append(event)
        return seen.copy()

    with pytest.raises(TruncatedStreamError) as error:
        await collect()
    assert error.value.reason == REASON_MALFORMED_FRAME
    assert model.closed == 1
    assert not any(event['type'] == 'final' for event in seen)
    seen.clear()
    model.frames = [{"type": "token", "delta": ""}, {"type": "token", "delta": "Synthetic answer."},
                    {"type": "done", "finish_reason": "stop"}]
    healthy = await collect()
    assert model.closed == 2
    if seam == 'adapter':
        assert isinstance(healthy[-1], ModelDone) and healthy[-1].finish_reason == 'stop'
    else:
        assert [event['type'] for event in healthy] == ['token', 'final']
        assert healthy[-1]['output'].finish_reason == 'stop'


@pytest.mark.anyio
@pytest.mark.parametrize('typed', [False, True])
async def test_single_incomplete_terminal_remains_incomplete(typed):
    from mainframe_rag.agent.core_ports import ModelDone, ModelToken

    class Model(CoreFakeLLM):
        async def chat_stream(self, *args, **kwargs):
            yield {"type": "token", "delta": "Partial answer."}
            yield {"type": "done", "finish_reason": "length"}

        async def stream(self, *args, **kwargs):
            yield ModelToken("Partial answer.")
            yield ModelDone("length", TokenUsage())

    model = Model()
    deps = _stream_deps(model)
    if typed:
        deps.llm = model
    events = [event async for event in execute_answer_core_stream(AnswerCoreInput(query='IEA500I'), deps)]
    assert events[-1]['type'] == 'final'
    assert events[-1]['output'].finish_reason == 'length'
    assert events[-1]['output'].verification_state == 'generation_incomplete'
