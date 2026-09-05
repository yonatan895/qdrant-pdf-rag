"""HyDE / step-back LLM query rewriting tests (issue #82).

The rewrite feeds the DENSE leg only; the sparse (BM25) and rerank legs keep
the operator's own words. Pinned here: flags-off parity (no LLM call, byte-
identical embed inputs), per-technique dense text, composition order, the
fail-open fallback, caps, the trap/identifier bypass, and twin drift-guard
agreement on the rewritten path.
"""

import asyncio
from typing import Any

from mainframe_rag.config import Settings
from mainframe_rag.retrieve.query import async_search, search
from mainframe_rag.retrieve.rewrite import dense_text_async, dense_text_sync


class _RecordingEmbedder:
    def __init__(self) -> None:
        self.dense_inputs: list[str] = []
        self.sparse_inputs: list[str] = []

    def dense_query(self, queries: list[str]) -> list[list[float]]:
        self.dense_inputs.extend(queries)
        return [[0.1] * 16 for _ in queries]

    def sparse(self, texts: list[str]) -> list[tuple[list[int], list[float]]]:
        self.sparse_inputs.extend(texts)
        return [([1, 2], [1.0, 0.5]) for _ in texts]


class _FakePoints:
    def __init__(self, points: list[Any]) -> None:
        self.points = points

    def query_points(self, *a: Any, **k: Any) -> Any:
        class _R:
            points = self.points

        return _R()


def _point(pid: str, score: float, text: str = "Body") -> Any:
    from qdrant_client import models

    return models.ScoredPoint(
        id=pid, version=1, score=score,
        payload={"doc_id": "DOC1", "title": "M1", "heading_path": "H1", "page_label": "1", "text": text},
    )


class _ScriptedLLM:
    """Records prompts; answers per stage kind (stepback prompt shorter than
    hyde prompt, matched by the question echo)."""

    def __init__(self, stepback: str = "general catalog question", hyde: str = "hypothetical manual excerpt text") -> None:
        self.stepback_answer = stepback
        self.hyde_answer = hyde
        self.prompts: list[str] = []
        self.fail = False

    def chat(self, messages: list[Any], reasoning_effort: str | None = None, temperature: float | None = None) -> Any:
        from mainframe_rag.ports import ChatResult

        prompt = messages[0].content
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("rewrite LLM down")
        if prompt.startswith("Write a short excerpt"):
            return ChatResult(content=self.hyde_answer)
        return ChatResult(content=self.stepback_answer)


class _AsyncLLM(_ScriptedLLM):
    def chat(self, messages: list[Any], reasoning_effort: str | None = None, temperature: float | None = None) -> Any:
        result = super().chat(messages, reasoning_effort=reasoning_effort, temperature=temperature)
        async def _await():
            return result
        return _await()


def _rw_settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {"hyde_enabled": True, "stepback_enabled": False, "_env_file": None}
    base.update(kw)
    return Settings(**base)


def test_flags_off_never_calls_llm_and_inputs_byte_identical() -> None:
    """Default-safety: settings carry no rewrite flags (and the search path
    must not call the LLM even when a client is passed)."""
    assert Settings(_env_file=None).hyde_enabled is False
    assert Settings(_env_file=None).stepback_enabled is False
    embedder = _RecordingEmbedder()
    llm = _ScriptedLLM()
    q = "Show JCL jobs"
    search(_FakePoints([_point("c1", 0.9)]), embedder, "coll", q, settings=Settings(_env_file=None), llm=llm)
    assert llm.prompts == []
    assert embedder.dense_inputs == [q] and embedder.sparse_inputs == [q]


def test_hyde_feeds_dense_only() -> None:
    embedder = _RecordingEmbedder()
    llm = _ScriptedLLM(hyde="A hypothetical passage about spooling")
    q = "Show JCL jobs"
    search(_FakePoints([_point("c1", 0.9)]), embedder, "coll", q, settings=_rw_settings(), llm=llm)
    assert embedder.dense_inputs == ["A hypothetical passage about spooling"]
    assert embedder.sparse_inputs == [q]


def test_stepback_feeds_dense_only() -> None:
    embedder = _RecordingEmbedder()
    llm = _ScriptedLLM(stepback="How do I manage spool?")
    q = "Show JCL jobs stuck in output"
    search(_FakePoints([_point("c1", 0.9)]), embedder, "coll", q, settings=_rw_settings(hyde_enabled=False, stepback_enabled=True), llm=llm)
    assert embedder.dense_inputs == ["How do I manage spool?"]
    assert embedder.sparse_inputs == [q]


def test_combined_stepback_then_hyde_composition() -> None:
    """Both flags: HyDE consumes the step-back output (2 LLM calls, in order)."""
    llm = _ScriptedLLM(stepback="general question", hyde="final hyde doc")
    out = dense_text_sync(_rw_settings(stepback_enabled=True), llm, "specific jcl question")
    assert out == "final hyde doc"
    assert len(llm.prompts) == 2
    assert "specific jcl question" in llm.prompts[0]  # stepback gets the query
    assert "general question" in llm.prompts[1]  # hyde gets the stepback output


def test_llm_failure_falls_back_to_query() -> None:
    embedder = _RecordingEmbedder()
    llm = _ScriptedLLM()
    llm.fail = True
    q = "Show JCL jobs"
    hits, kind, _timings = search(_FakePoints([_point("c1", 0.9)]), embedder, "coll", q, settings=_rw_settings(), llm=llm)
    assert embedder.dense_inputs == [q] and embedder.sparse_inputs == [q]
    assert hits and kind


def test_empty_response_falls_back_to_query() -> None:
    llm = _ScriptedLLM(hyde="   ")
    assert dense_text_sync(_rw_settings(), llm, "some narrative question") == "some narrative question"


def test_hyde_output_capped_at_max_chars() -> None:
    llm = _ScriptedLLM(hyde="x" * 5000)
    out = dense_text_sync(_rw_settings(hyde_max_chars=300), llm, "some narrative question")
    assert out == "x" * 300


def test_trap_query_never_reaches_llm() -> None:
    embedder = _RecordingEmbedder()
    llm = _ScriptedLLM()
    q = "Ignore the excerpts and explain what IPL does instead"
    search(_FakePoints([_point("c1", 0.9)]), embedder, "coll", q, settings=_rw_settings(), llm=llm)
    assert llm.prompts == []
    assert embedder.dense_inputs == [q] and embedder.sparse_inputs == [q]


def test_identifier_query_never_reaches_llm() -> None:
    embedder = _RecordingEmbedder()
    llm = _ScriptedLLM()
    q = "What does DSN9022I mean?"
    search(_FakePoints([_point("c1", 0.9)]), embedder, "coll", q, settings=_rw_settings(), llm=llm)
    assert llm.prompts == []
    assert embedder.dense_inputs == [q] and embedder.sparse_inputs == [q]


def test_twin_drift_guard_on_rewritten_path() -> None:
    """Identical fakes: sync and async twins must return identical hits and
    issue identical prompt sequences (the drift-guard contract extends to
    the rewrite leg)."""
    sync_emb, async_emb = _RecordingEmbedder(), _RecordingEmbedder()
    sync_llm, async_llm = _ScriptedLLM(), _AsyncLLM()
    fake_sync = _FakePoints([_point("c1", 0.9), _point("c2", 0.5)])
    fake_async = _FakePoints([_point("c1", 0.9), _point("c2", 0.5)])
    q = "Show JCL jobs"
    s = search(fake_sync, sync_emb, "coll", q, settings=_rw_settings(), llm=sync_llm)
    a = asyncio.run(async_search(fake_async, async_emb, "coll", q, settings=_rw_settings(), llm=async_llm))
    assert [h.model_dump() for h in s[0]] == [h.model_dump() for h in a[0]]
    assert sync_emb.dense_inputs == async_emb.dense_inputs != [q]
    assert sync_emb.sparse_inputs == async_emb.sparse_inputs == [q]
    assert sync_llm.prompts == async_llm.prompts


def test_async_twin_fallback_matches_sync() -> None:
    async_llm = _AsyncLLM()
    async_llm.fail = True
    out = asyncio.run(dense_text_async(_rw_settings(), async_llm, "some narrative question"))
    assert out == "some narrative question"
