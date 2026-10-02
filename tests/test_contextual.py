"""Contextual retrieval prefixes (issue #78): prompt, client, cache, wiring.

All LLM contact is faked; the success path is forced with mocks (a test that
only passes because the network call failed is invalid). Live-network
validation rides `sh scripts/tools/run-task.sh eval:retrieval EMBED_MODE=vllm`, never this file.
"""

import json
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


def _chunk(
    chunk_id="c1",
    text="IEA500I BEFORE IOS IOSCMDS COMMAND REJECTED",
    heading_path="Chapter 2 > IEA500I",
):
    return Chunk(
        chunk_id=chunk_id,
        doc_id="SA22-0000-00",
        heading_path=heading_path,
        page_start=5,
        page_label="1-6",
        chunk_type="message",
        text=text,
        message_ids=["IEA500I"],
        members=[],
        ordinal=0,
    )


def _binding(chunk_sha="sha", **kw):
    base = {
        "doc_sha256": chunk_sha,
        "product": "z/OS",
        "version": "3.1",
        "title": "Synthetic Reference",
        "model": "test-gist-model",
        "max_chars": 500,
    }
    base.update(kw)
    return ctx_mod.ContextBinding(**base)


def _gen(chunks, http, cache, *, model="test-gist-model", max_chars=500, sha="sha", **kw):
    """generate_contexts through the real ContextLLMClient over a fake HTTP double."""
    client = ctx_mod.ContextLLMClient(_settings(context_llm_model=model), client=http)
    args = {
        "doc_sha256": sha,
        "product": "z/OS",
        "version": "3.1",
        "title": "Synthetic Reference",
    }
    args.update(kw)
    return ctx_mod.generate_contexts(
        chunks, client=client, cache=cache, max_chars=max_chars, **args
    )


class FakeHttpClient:
    """httpx2.Client double: records requests, replays queued responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []

    def post(self, url, json=None, headers=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
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
    assert ctx_mod.CONTEXT_PROMPT_VERSION == "v2"
    # v2 contract (issue #78 reviewer sequence): the gist must ADD what the
    # header lacks, never restate it — pin both sides so the template cannot
    # silently regress to v1 semantics.
    assert "Never repeat the manual title" in ctx_mod.CONTEXT_SYSTEM_PROMPT
    assert "name the manual and section" not in ctx_mod.CONTEXT_SYSTEM_PROMPT
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


def test_cache_key_binds_every_generation_input():
    chunk = _chunk()
    base = _binding().key(chunk)
    assert base.startswith("ctx2:")
    assert _binding().key(chunk) == base  # deterministic
    variants = {
        "doc sha": _binding(chunk_sha="sha2").key(chunk),
        "model": _binding(model="other-model").key(chunk),
        "max_chars": _binding(max_chars=10).key(chunk),
        "title": _binding(title="Other").key(chunk),
        "product": _binding(product="CICS").key(chunk),
        "version": _binding(version="9.9").key(chunk),
        "body": _binding().key(_chunk(text="changed body")),
        "heading": _binding().key(_chunk(heading_path="Chapter 9")),
        "chunk id": _binding().key(_chunk("c2")),
    }
    for name, key in variants.items():
        assert key != base, name
    assert len(set(variants.values())) == len(variants)


def test_cache_round_trip_last_wins_and_skips_corrupt_lines(tmp_path, caplog):
    path = tmp_path / "contexts.jsonl"
    c1, c2 = _chunk("c1"), _chunk("c2")
    binding = _binding("sha1")
    assert ctx_mod.load_context_cache(path) == {}
    ctx_mod.append_context_entries(path, binding, [c1, c2], {"c1": "first", "c2": "second"})
    ctx_mod.append_context_entries(path, binding, [c1, c2], {"c1": "updated"})
    path.write_text(path.read_text() + "not json\n", encoding="utf-8")
    with caplog.at_level("WARNING", logger="ingest"):
        loaded = ctx_mod.load_context_cache(path)
    assert loaded[binding.key(c1)] == "updated"
    assert loaded[binding.key(c2)] == "second"
    assert "context_cache_skip_line" in caplog.text


def test_append_rejects_entries_for_unknown_chunks(tmp_path):
    with pytest.raises(ValueError, match="unknown chunk ids"):
        ctx_mod.append_context_entries(
            tmp_path / "c.jsonl", _binding(), [_chunk("c1")], {"zzz": "gist"}
        )
    assert not (tmp_path / "c.jsonl").exists()


def test_legacy_and_malformed_records_are_misses_not_errors(tmp_path, caplog):
    """Pre-#416 records (no model/input/cap binding) and malformed schema-2
    records never hit and never raise; the file is left untouched."""
    chunk = _chunk()
    binding = _binding()
    good = binding.record(chunk, "Good gist.")
    legacy = {"v": "v2", "doc_sha256": "sha", "chunk_id": "c1", "context": "A" * 130}
    bad = [
        {**good, "max_chars": True},
        {**good, "max_chars": "500"},
        {**good, "model": ""},
        {**good, "model": None},
        {**good, "input_sha256": 7},
        {**good, "schema": 99},
        {**good, "context": "x" * 600},  # longer than its own cap
        {**good, "context": ""},
        {**good, "context": ["not", "str"]},
        ["not", "a", "dict"],
    ]
    path = tmp_path / "contexts.jsonl"
    lines = [json.dumps(legacy)] + [json.dumps(b) for b in bad]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    before = path.read_text()
    with caplog.at_level("WARNING", logger="ingest"):
        assert ctx_mod.load_context_cache(path) == {}
    assert "context_cache_legacy_records_ignored" in caplog.text
    assert caplog.text.count("context_cache_skip_line") == len(bad)
    assert path.read_text() == before  # user's cache never rewritten

    # A valid record appended after the legacy/garbage still loads and hits.
    ctx_mod.append_context_entries(path, binding, [chunk], {"c1": "Good gist."})
    cache = ctx_mod.load_context_cache(path)
    assert cache == {binding.key(chunk): "Good gist."}
    http = FakeHttpClient([])
    full, new = _gen([chunk], http, cache)
    assert full == {"c1": "Good gist."} and new == {} and http.posts == []


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


def test_complete_sends_bearer_when_key_set():
    http = FakeHttpClient([FakeResp("Gist.")])
    client = ctx_mod.ContextLLMClient(
        _settings(context_llm_api_key="sk-test-context"), client=http
    )
    client.complete([ChatMessage(role="user", content="hi")])
    (post,) = http.posts
    assert post["headers"] == {"Authorization": "Bearer sk-test-context"}


def test_complete_omits_auth_when_key_unset():
    http = FakeHttpClient([FakeResp("Gist.")])
    client = ctx_mod.ContextLLMClient(_settings(), client=http)
    client.complete([ChatMessage(role="user", content="hi")])
    (post,) = http.posts
    assert post["headers"] == {}


def test_complete_http_error_propagates():
    http = FakeHttpClient([FakeResp(status_code=500)])
    client = ctx_mod.ContextLLMClient(_settings(), client=http)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        client.complete([ChatMessage(role="user", content="hi")])


def test_generate_contexts_uses_cache_and_generates_misses():
    cache = {_binding().key(_chunk("c1")): "Cached gist."}
    http = FakeHttpClient([FakeResp("Fresh gist.")])
    full, new = _gen([_chunk("c1"), _chunk("c2")], http, cache)
    assert full == {"c1": "Cached gist.", "c2": "Fresh gist."}
    assert new == {"c2": "Fresh gist."}
    assert len(http.posts) == 1  # the hit made zero LLM calls


def test_same_input_second_pass_makes_zero_calls():
    cache: dict[str, str] = {}
    http = FakeHttpClient([FakeResp("Gist A."), FakeResp("Gist B.")])
    full1, new1 = _gen([_chunk("c1"), _chunk("c2")], http, cache)
    assert len(http.posts) == 2
    http2 = FakeHttpClient([])  # any POST raises
    full2, new2 = _gen([_chunk("c1"), _chunk("c2")], http2, cache)
    assert full2 == full1 == {"c1": "Gist A.", "c2": "Gist B."}
    assert new2 == {} and http2.posts == []
    assert new1 == full1


def test_issue_416_repro_model_cap_and_body_change_miss_the_cache():
    """The exact reported scenario: a cache primed by model A (130 chars) must
    not answer for model B with max_chars=10 and a changed body."""
    cache: dict[str, str] = {}
    _gen([_chunk("c1")], FakeHttpClient([FakeResp("A" * 130)]), cache, model="model-a")
    http_b = FakeHttpClient([FakeResp("B" * 130)])
    full, new = _gen(
        [_chunk("c1", text="changed body")], http_b, cache, model="model-b", max_chars=10
    )
    assert len(http_b.posts) == 1
    assert http_b.posts[0]["json"]["model"] == "model-b"
    assert "changed body" in http_b.posts[0]["json"]["messages"][1]["content"]
    assert full == new == {"c1": "B" * 10}


def test_model_swap_same_chunk_misses_and_calls_new_model():
    cache: dict[str, str] = {}
    _gen([_chunk()], FakeHttpClient([FakeResp("From A.")]), cache, model="model-a")
    http_b = FakeHttpClient([FakeResp("From B.")])
    full, _ = _gen([_chunk()], http_b, cache, model="model-b")
    assert full == {"c1": "From B."}
    assert len(http_b.posts) == 1
    # Model A's entry is still intact for a switch back: zero calls.
    http_a = FakeHttpClient([])
    full_a, _ = _gen([_chunk()], http_a, cache, model="model-a")
    assert full_a == {"c1": "From A."} and http_a.posts == []


def test_max_chars_change_is_policy_correct_in_both_directions():
    cache: dict[str, str] = {}
    long_gist = ("word " * 40).strip()  # 199 chars after normalization
    _gen([_chunk()], FakeHttpClient([FakeResp(long_gist)]), cache, max_chars=100)
    (cached_100,) = cache.values()
    assert len(cached_100) <= 100

    smaller = FakeHttpClient([FakeResp(long_gist)])
    full_small, _ = _gen([_chunk()], smaller, cache, max_chars=30)
    assert len(smaller.posts) == 1
    assert 0 < len(full_small["c1"]) <= 30

    larger = FakeHttpClient([FakeResp(long_gist)])
    full_large, _ = _gen([_chunk()], larger, cache, max_chars=300)
    assert len(larger.posts) == 1
    assert full_large["c1"] == long_gist  # not stuck at the old 100-char cut

    # Each cap now hits its own entry with zero calls.
    for cap, expect in ((100, cached_100), (30, full_small["c1"]), (300, long_gist)):
        silent = FakeHttpClient([])
        full, _ = _gen([_chunk()], silent, cache, max_chars=cap)
        assert full == {"c1": expect} and silent.posts == []


@pytest.mark.parametrize(
    "edit",
    [
        {"text": "IEA500I changed body text"},
        {"heading_path": "Chapter 9 > IEA999I"},
    ],
)
def test_changed_chunk_input_under_stable_chunk_id_misses(edit):
    cache: dict[str, str] = {}
    _gen([_chunk()], FakeHttpClient([FakeResp("Original gist.")]), cache)
    edited = _chunk(**edit)
    http = FakeHttpClient([FakeResp("Edited gist.")])
    full, _ = _gen([edited], http, cache)
    assert full == {"c1": "Edited gist."}
    assert len(http.posts) == 1
    assert next(iter(edit.values())) in http.posts[0]["json"]["messages"][1]["content"]


@pytest.mark.parametrize(
    "override",
    [{"title": "Other Title"}, {"product": "CICS"}, {"version": "9.9"}, {"sha": "other-sha"}],
)
def test_changed_header_fields_or_doc_sha_miss(override):
    cache: dict[str, str] = {}
    _gen([_chunk()], FakeHttpClient([FakeResp("Original gist.")]), cache)
    http = FakeHttpClient([FakeResp("Fresh gist.")])
    full, _ = _gen([_chunk()], http, cache, **override)
    assert full == {"c1": "Fresh gist."}
    assert len(http.posts) == 1


def test_unnormalized_cache_hit_is_regenerated():
    """A hit that violates the current normalization policy is not served."""
    cache = {_binding().key(_chunk()): "has   odd\nwhitespace"}
    http = FakeHttpClient([FakeResp("Clean gist.")])
    full, _ = _gen([_chunk()], http, cache)
    assert full == {"c1": "Clean gist."} and len(http.posts) == 1


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
    binding = ctx_mod.ContextBinding.from_settings(
        settings,
        doc_sha256="dummy_sha",
        product=parsed.product,
        version=parsed.version,
        title=parsed.title,
    )
    primed = {binding.key(c): contexts[c.chunk_id] for c in chunks}
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


def test_rerun_reads_sidecar_file_with_zero_llm_calls(synthetic_pdf, tmp_path, monkeypatch):
    """Acceptance #2 at file level: run 1 writes the sidecar through the
    parent's append path; run 2 loads it through the real worker cache
    loader (cold worker state, as in a separate process) and makes zero
    LLM calls with identical contexts and vectors. The primed-dict test
    above cannot catch a file-format skew between append and load."""
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
    cache_path = tmp_path / "inv.contexts.jsonl"

    def fresh_worker_cache():
        run_ingest._worker_context_cache = None
        run_ingest._worker_context_cache_path = None

    def task():
        return (
            str(synthetic_pdf), None, None, None, str(synthetic_pdf.parent),
            "dummy_sha", True, str(cache_path),
        )

    http1 = FakeHttpClient([FakeResp(f"Gist {i}.") for i in range(100)])
    monkeypatch.setattr(
        run_ingest, "_get_context_client", lambda s: ctx_mod.ContextLLMClient(s, client=http1)
    )
    fresh_worker_cache()
    record1, parsed1, chunks1, vectors1, contexts1 = run_ingest._parse_one(task())
    assert record1.status != "error"
    assert len(http1.posts) == len(chunks1) > 0
    # Parent append, exactly like run_ingest.main: the full map, last-wins load.
    ctx_mod.append_context_entries(
        cache_path,
        ctx_mod.ContextBinding.from_settings(
            settings,
            doc_sha256="dummy_sha",
            product=parsed1.product,
            version=parsed1.version,
            title=parsed1.title,
        ),
        chunks1,
        contexts1,
    )
    assert cache_path.exists()

    http2 = FakeHttpClient([])  # any POST raises: zero calls allowed
    monkeypatch.setattr(
        run_ingest, "_get_context_client", lambda s: ctx_mod.ContextLLMClient(s, client=http2)
    )
    fresh_worker_cache()
    record2, _, _chunks2, vectors2, contexts2 = run_ingest._parse_one(task())
    assert record2.status != "error"
    assert http2.posts == []
    assert contexts2 == contexts1
    assert vectors2 == vectors1
    lines = [line for line in cache_path.read_text().splitlines() if line.strip()]
    assert len(lines) == len(chunks1)


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


def _resume_env(synthetic_pdf, tmp_path, monkeypatch, *, model="model-a", max_chars=500):
    """Worker harness for file-level resume: real _parse_one + real sidecar
    loader (cold worker state per call) + the parent's append path."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.embed import HashEmbedder

    settings = Settings(
        _env_file=None,
        embed_mode="vllm",
        contextual_embed_enabled=True,
        context_llm_base_url="http://context.internal/v1",
        context_llm_model=model,
        context_max_chars=max_chars,
    )
    monkeypatch.setattr(run_ingest, "_load_worker_settings", lambda: settings)
    monkeypatch.setattr(run_ingest, "_get_embedder", lambda s: HashEmbedder())
    cache_path = tmp_path / "inv.contexts.jsonl"

    def run(http, *, append=True):
        monkeypatch.setattr(
            run_ingest, "_get_context_client", lambda s: ctx_mod.ContextLLMClient(s, client=http)
        )
        run_ingest._worker_context_cache = None
        run_ingest._worker_context_cache_path = None
        task = (
            str(synthetic_pdf), None, None, None, str(synthetic_pdf.parent),
            "dummy_sha", True, str(cache_path),
        )
        record, parsed, chunks, vectors, contexts = run_ingest._parse_one(task)
        assert record.status != "error"
        if append and contexts:
            ctx_mod.append_context_entries(
                cache_path,
                ctx_mod.ContextBinding.from_settings(
                    settings,
                    doc_sha256="dummy_sha",
                    product=parsed.product,
                    version=parsed.version,
                    title=parsed.title,
                ),
                chunks,
                contexts,
            )
        return chunks, vectors, contexts

    return settings, cache_path, run


def test_resume_after_model_swap_regenerates_through_worker(synthetic_pdf, tmp_path, monkeypatch):
    """Run 1 with model A fills the sidecar. Run 2, same sidecar and same
    chunks but model B, must call model B for every chunk and embed B's text;
    a third run with model B is then all hits (zero calls)."""
    _, _, run_a = _resume_env(synthetic_pdf, tmp_path, monkeypatch, model="model-a")
    http_a = FakeHttpClient([FakeResp("Gist from A.")] * 100)
    chunks_a, _, ctx_a = run_a(http_a)
    assert len(http_a.posts) == len(chunks_a) > 0
    assert set(ctx_a.values()) == {"Gist from A."}

    _, _, run_b = _resume_env(synthetic_pdf, tmp_path, monkeypatch, model="model-b")
    http_b = FakeHttpClient([FakeResp("Gist from B.")] * 100)
    chunks_b, _, ctx_b = run_b(http_b)
    assert len(http_b.posts) == len(chunks_b)
    assert {p["json"]["model"] for p in http_b.posts} == {"model-b"}
    assert set(ctx_b.values()) == {"Gist from B."}

    http_b2 = FakeHttpClient([])
    _, _, ctx_b2 = run_b(http_b2)
    assert http_b2.posts == [] and ctx_b2 == ctx_b
    # The old model's entries remain valid under their own identity.
    _, _, run_a2 = _resume_env(synthetic_pdf, tmp_path, monkeypatch, model="model-a")
    http_a2 = FakeHttpClient([])
    _, _, ctx_a2 = run_a2(http_a2)
    assert http_a2.posts == [] and ctx_a2 == ctx_a


def test_resume_after_cap_change_regenerates_through_worker(synthetic_pdf, tmp_path, monkeypatch):
    _, _, run_500 = _resume_env(synthetic_pdf, tmp_path, monkeypatch, max_chars=500)
    http1 = FakeHttpClient([FakeResp("word " * 60)] * 100)
    chunks, _, ctx1 = run_500(http1)
    assert len(http1.posts) == len(chunks) > 0
    assert all(len(v) > 100 for v in ctx1.values())

    _, _, run_50 = _resume_env(synthetic_pdf, tmp_path, monkeypatch, max_chars=50)
    http2 = FakeHttpClient([FakeResp("word " * 60)] * 100)
    _, _, ctx2 = run_50(http2)
    assert len(http2.posts) == len(chunks)
    assert all(0 < len(v) <= 50 for v in ctx2.values())


def test_resume_with_legacy_sidecar_regenerates_without_deleting_it(
    synthetic_pdf, tmp_path, monkeypatch
):
    """A user's pre-#416 sidecar (v/doc_sha256/chunk_id/context) must neither
    crash the worker nor be served: every chunk is regenerated, the legacy
    lines stay on disk, and the next run is all hits."""
    _, cache_path, run = _resume_env(synthetic_pdf, tmp_path, monkeypatch)
    probe = FakeHttpClient([FakeResp("probe")] * 100)
    chunks, _, _ = run(probe, append=False)
    legacy_lines = [
        json.dumps(
            {"v": "v2", "doc_sha256": "dummy_sha", "chunk_id": c.chunk_id, "context": "L" * 130}
        )
        for c in chunks
    ]
    cache_path.write_text("\n".join(legacy_lines) + "\n", encoding="utf-8")

    http = FakeHttpClient([FakeResp("Fresh gist.")] * 100)
    _, _, contexts = run(http)
    assert len(http.posts) == len(chunks)
    assert set(contexts.values()) == {"Fresh gist."}
    on_disk = cache_path.read_text().splitlines()
    assert on_disk[: len(legacy_lines)] == legacy_lines  # never rewritten/deleted

    again = FakeHttpClient([])
    _, _, contexts2 = run(again)
    assert again.posts == [] and contexts2 == contexts


def test_contextual_off_worker_path_is_unchanged(synthetic_pdf, tmp_path, monkeypatch):
    """Flag off: no client, no sidecar read or write, no contexts, and the
    header-only vectors are produced exactly as before."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.embed import HashEmbedder

    settings = Settings(_env_file=None, embed_mode="hash", contextual_embed_enabled=False)
    monkeypatch.setattr(run_ingest, "_load_worker_settings", lambda: settings)
    monkeypatch.setattr(run_ingest, "_get_embedder", lambda s: HashEmbedder())

    def boom(*a, **k):
        raise AssertionError("contextual-off must not touch the context cache or client")

    monkeypatch.setattr(run_ingest, "_get_context_client", boom)
    monkeypatch.setattr(run_ingest, "_get_context_cache", boom)
    cache_path = tmp_path / "never.jsonl"
    task = (
        str(synthetic_pdf), None, None, None, str(synthetic_pdf.parent),
        "dummy_sha", True, str(cache_path),
    )
    record, _, chunks, vectors, contexts = run_ingest._parse_one(task)
    assert record.status != "error"
    assert contexts == {}
    assert len(vectors) == len(chunks) > 0
    assert not cache_path.exists()
