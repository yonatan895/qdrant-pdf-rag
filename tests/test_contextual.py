"""Contextual retrieval prefixes (issue #78): prompt, client, cache, wiring.

All LLM contact is faked; the success path is forced with mocks (a test that
only passes because the network call failed is invalid). Live-network
validation rides `make eval EMBED_MODE=vllm`, never this file.
"""

from pathlib import Path

import pytest

from mainframe_rag.config import Settings
from mainframe_rag.ingest import context as ctx_mod
from mainframe_rag.ingest.chunk import Chunk
from mainframe_rag.ports import ChatMessage


def _settings(**kw):
    base = {
        "_env_file": None,
        "contextual_embed_enabled": True,
        "context_llm_base_url": "http://context.internal/v1",
        "context_llm_model": "test-gist-model",
    }
    base.update(kw)
    return Settings(**base)


def _chunk(chunk_id="c1", text="IEA500I BEFORE IOS IOSCMDS COMMAND REJECTED"):
    return Chunk(
        chunk_id=chunk_id,
        doc_id="SA22-0000-00",
        heading_path="Chapter 2 > IEA500I",
        page_start=5,
        page_label="1-6",
        chunk_type="message",
        text=text,
        message_ids=["IEA500I"],
        members=[],
        ordinal=0,
    )


class FakeHttpClient:
    """httpx2.Client double: records requests, replays queued responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []

    def post(self, url, json=None):
        self.posts.append({"url": url, "json": json})
        if not self.responses:
            raise AssertionError("unexpected extra LLM call")
        return self.responses.pop(0)


class FakeResp:
    def __init__(self, content="Situating gist.", status_code=200):
        self._content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def test_prompt_template_is_versioned_and_mirrors_embed_header():
    assert ctx_mod.CONTEXT_PROMPT_VERSION == "v1"
    assert "1" in ctx_mod.CONTEXT_SYSTEM_PROMPT or "two sentences" in ctx_mod.CONTEXT_SYSTEM_PROMPT
    messages = ctx_mod.build_context_messages(
        product="z/OS",
        version="3.1",
        doc_id="SA22-0000-00",
        title="Synthetic Reference",
        heading_path="Chapter 2 > IEA500I",
        body="IEA500I body",
    )
    assert [m.role for m in messages] == ["system", "user"]
    user = messages[1].content
    # Same identifying fields the header-only baseline embeds.
    for needle in ("SA22-0000-00", "Synthetic Reference", "Chapter 2 > IEA500I", "IEA500I body"):
        assert needle in user


def test_normalize_context_collapses_and_truncates():
    assert ctx_mod.normalize_context("  a   b\nc  ", 100) == "a b c"
    assert ctx_mod.normalize_context("x" * 600, 500) == "x" * 500


def test_cache_key_includes_template_version():
    assert ctx_mod.cache_key("sha", "cid").startswith("v1:")


def test_cache_round_trip_last_wins_and_skips_corrupt_lines(tmp_path, caplog):
    path = tmp_path / "contexts.jsonl"
    assert ctx_mod.load_context_cache(path) == {}
    ctx_mod.append_context_entries(path, "sha1", {"c1": "first", "c2": "second"})
    ctx_mod.append_context_entries(path, "sha1", {"c1": "updated"})
    path.write_text(path.read_text() + "not json\n", encoding="utf-8")
    with caplog.at_level("WARNING", logger="ingest"):
        loaded = ctx_mod.load_context_cache(path)
    assert loaded[ctx_mod.cache_key("sha1", "c1")] == "updated"
    assert loaded[ctx_mod.cache_key("sha1", "c2")] == "second"
    assert "context_cache_skip_line" in caplog.text


def test_resolve_cache_path_explicit_wins_and_sibling_default(tmp_path):
    explicit = Settings(_env_file=None, context_cache_path="/tmp/x.jsonl")
    assert ctx_mod.resolve_cache_path(explicit, tmp_path / "inventory.jsonl") == Path("/tmp/x.jsonl")
    defaulted = Settings(_env_file=None)
    assert (
        ctx_mod.resolve_cache_path(defaulted, tmp_path / "inventory.jsonl").name
        == "inventory.contexts.jsonl"
    )


def test_complete_posts_short_deterministic_completion():
    http = FakeHttpClient([FakeResp("  Gist with\nnewline.  ")])
    client = ctx_mod.ContextLLMClient(_settings(), client=http)
    out = client.complete([ChatMessage(role="user", content="hi")])
    assert out == "Gist with newline."
    (post,) = http.posts
    assert post["url"] == "http://context.internal/v1/chat/completions"
    assert post["json"]["model"] == "test-gist-model"
    assert post["json"]["temperature"] == 0.0
    assert post["json"]["max_tokens"] == ctx_mod.MAX_COMPLETION_TOKENS


def test_complete_http_error_propagates():
    http = FakeHttpClient([FakeResp(status_code=500)])
    client = ctx_mod.ContextLLMClient(_settings(), client=http)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        client.complete([ChatMessage(role="user", content="hi")])


def test_generate_contexts_uses_cache_and_generates_misses():
    http = FakeHttpClient([FakeResp("Fresh gist.")])
    client = ctx_mod.ContextLLMClient(_settings(), client=http)
    cache = {ctx_mod.cache_key("sha", "c1"): "Cached gist."}
    full, new = ctx_mod.generate_contexts(
        [_chunk("c1"), _chunk("c2")],
        doc_sha256="sha",
        product="z/OS",
        version="3.1",
        title="Synthetic Reference",
        client=client,
        cache=cache,
        max_chars=500,
    )
    assert full == {"c1": "Cached gist.", "c2": "Fresh gist."}
    assert new == {"c2": "Fresh gist."}
    assert len(http.posts) == 1  # the hit made zero LLM calls


def test_generate_contexts_empty_gist_fails_loud():
    http = FakeHttpClient([FakeResp("   ")])
    client = ctx_mod.ContextLLMClient(_settings(), client=http)
    with pytest.raises(RuntimeError, match="empty gist"):
        ctx_mod.generate_contexts(
            [_chunk("c1")],
            doc_sha256="sha",
            product=None,
            version=None,
            title="T",
            client=client,
            cache={},
            max_chars=500,
        )


def test_embed_batch_prefixes_dense_only():
    from mainframe_rag.ingest.embed import embed_batch

    seen: dict[str, list[str]] = {"dense": [], "sparse": []}

    class RecordingEmbedder:
        def dense(self, texts):
            seen["dense"] = list(texts)
            return [[0.1] * 4 for _ in texts]

        def dense_query(self, queries):
            return self.dense(queries)

        def sparse(self, texts):
            seen["sparse"] = list(texts)
            return [([3], [1.0]) for _ in texts]

    chunk = _chunk()
    embed_batch([chunk], "z/OS", "3.1", "Synthetic Reference", RecordingEmbedder(), {"c1": "Gist."})
    assert "Gist." in seen["dense"][0]
    assert "Gist." not in seen["sparse"][0]
    # Sparse input is byte-identical to the legacy header-only string.
    from mainframe_rag.ingest.embed import chunk_embed_text

    assert seen["sparse"][0] == chunk_embed_text(chunk, "z/OS", "3.1", "Synthetic Reference")


def test_upsert_payload_stores_context_only_when_present():
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc
    from mainframe_rag.ingest.qdrant_io import upsert_chunks

    class RecordingClient:
        def __init__(self):
            self.points = []

        def upsert(self, collection_name, *, points, wait=True):
            self.points.extend(points)
            return True

    parsed = ParsedDoc(
        path="manual.pdf",
        doc_id="SA22-0000-00",
        sha256="abc123",
        vendor="IBM",
        product="z/OS",
        version="3.1",
        title="Synthetic Reference",
        page_count=10,
    )
    chunk = _chunk()
    vectors = [([0.1] * 4, ([3], [1.0]))]

    bare = RecordingClient()
    upsert_chunks(bare, Settings(_env_file=None, dense_dim=4), parsed, [chunk], vectors)
    assert "context" not in bare.points[0].payload

    with_ctx = RecordingClient()
    upsert_chunks(
        with_ctx, Settings(_env_file=None, dense_dim=4), parsed, [chunk], vectors, {"c1": "Gist."}
    )
    assert with_ctx.points[0].payload["context"] == "Gist."
    assert with_ctx.points[0].payload["text"] == chunk.text


def test_run_fails_closed_on_hash_mode(monkeypatch, tmp_path):
    """Enabled + hash embedder can never silently embed header-only vectors."""
    from mainframe_rag.ingest import run_ingest

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("CONTEXTUAL_EMBED_ENABLED", "true")
    monkeypatch.setenv("CONTEXT_LLM_BASE_URL", "http://context.internal/v1")
    monkeypatch.setenv("CONTEXT_LLM_MODEL", "test-gist-model")
    with pytest.raises(RuntimeError, match="embed_mode=vllm"):
        run_ingest.run(
            src=tmp_path, progress=tmp_path / "inventory.jsonl",
            workers=1, limit=None, dry_run=False,
        )


def test_run_fails_closed_without_context_llm(monkeypatch, tmp_path):
    from mainframe_rag.ingest import run_ingest

    monkeypatch.setenv("EMBED_MODE", "vllm")
    monkeypatch.setenv("CONTEXTUAL_EMBED_ENABLED", "true")
    monkeypatch.delenv("CONTEXT_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("CONTEXT_LLM_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="CONTEXT_LLM_BASE_URL"):
        run_ingest.run(
            src=tmp_path, progress=tmp_path / "inventory.jsonl",
            workers=1, limit=None, dry_run=False,
        )


def test_parse_one_contextual_path_end_to_end(synthetic_pdf, tmp_path, monkeypatch):
    """Worker with the flag on: cache hit skips the LLM, miss generates, and
    the dense vectors carry the prefix while sparse stays raw."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.embed import HashEmbedder

    settings = Settings(
        _env_file=None,
        embed_mode="vllm",
        contextual_embed_enabled=True,
        context_llm_base_url="http://context.internal/v1",
        context_llm_model="test-gist-model",
    )
    monkeypatch.setattr(run_ingest, "_load_worker_settings", lambda: settings)
    monkeypatch.setattr(run_ingest, "_get_embedder", lambda s: HashEmbedder())

    http = FakeHttpClient([FakeResp("Generated gist.")] * 100)
    monkeypatch.setattr(
        run_ingest, "_get_context_client", lambda s: ctx_mod.ContextLLMClient(s, client=http)
    )
    cache_path = tmp_path / "ctx.jsonl"
    monkeypatch.setattr(run_ingest, "_get_context_cache", lambda p: {})

    task = (
        str(synthetic_pdf), None, None, None, str(synthetic_pdf.parent),
        "dummy_sha", True, str(cache_path),
    )
    record, parsed, chunks, vectors, contexts = run_ingest._parse_one(task)
    assert record.status != "error"
    assert len(contexts) == len(chunks) > 0
    assert all(v == "Generated gist." for v in contexts.values())
    assert len(vectors) == len(chunks)
    assert len(http.posts) == len(chunks)

    # Second run with a primed cache makes zero LLM calls (acceptance #2).
    primed = {
        ctx_mod.cache_key("dummy_sha", c.chunk_id): contexts[c.chunk_id] for c in chunks
    }
    http2 = FakeHttpClient([])
    monkeypatch.setattr(
        run_ingest, "_get_context_client", lambda s: ctx_mod.ContextLLMClient(s, client=http2)
    )
    monkeypatch.setattr(run_ingest, "_get_context_cache", lambda p: dict(primed))
    record2, _, chunks2, vectors2, contexts2 = run_ingest._parse_one(task)
    assert record2.status != "error"
    assert contexts2 == contexts
    assert http2.posts == []

    # Results ride spawn IPC: plain strings pickle cleanly.
    import pickle

    pickle.dumps((record2, parsed, chunks2, vectors2, contexts2))


def test_dry_run_makes_no_context_calls(synthetic_pdf, tmp_path, monkeypatch):
    """The --dry-run contract (parse + chunk only) covers the context LLM."""
    from mainframe_rag.ingest import run_ingest

    settings = Settings(
        _env_file=None,
        embed_mode="hash",
        contextual_embed_enabled=True,
        context_llm_base_url="http://context.internal/v1",
        context_llm_model="test-gist-model",
    )
    monkeypatch.setattr(run_ingest, "_load_worker_settings", lambda: settings)

    def boom(*a, **k):
        raise AssertionError("no LLM client may be built on a dry run")

    monkeypatch.setattr(run_ingest, "_get_context_client", boom)
    task = (
        str(synthetic_pdf), None, None, None, str(synthetic_pdf.parent),
        "dummy_sha", False, str(tmp_path / "ctx.jsonl"),
    )
    record, _, _chunks, vectors, contexts = run_ingest._parse_one(task)
    assert record.status != "error"
    assert vectors == []
    assert contexts == {}
