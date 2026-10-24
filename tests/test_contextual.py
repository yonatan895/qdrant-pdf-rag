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
from mainframe_rag.ingest.ibm_pdf import sha256_file
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


# Template digest per prompt version: the version rides the context cache
# key and the representation manifest, so a template edit without a bump
# would keep serving contexts generated under the old prompt. Bump
# CONTEXT_PROMPT_VERSION with any change here and pin the new digest.
_PROMPT_PINS = {"v2": "d9daab45b97178cd"}


def _prompt_digest() -> str:
    import hashlib
    import inspect

    template = "\0".join([
        ctx_mod.CONTEXT_SYSTEM_PROMPT,
        inspect.getsource(ctx_mod.build_context_messages),
        str(ctx_mod.MAX_COMPLETION_TOKENS),
    ])
    return hashlib.sha256(template.encode("utf-8")).hexdigest()[:16]


def test_prompt_template_change_requires_a_version_bump():
    assert _PROMPT_PINS.get(ctx_mod.CONTEXT_PROMPT_VERSION) == _prompt_digest(), (
        "context prompt template changed: bump CONTEXT_PROMPT_VERSION and pin "
        f"its digest {_prompt_digest()} in _PROMPT_PINS"
    )


def test_prompt_messages_carry_the_embed_header_fields():
    # The digest pin above owns the template wording and its version; here the
    # messages must carry the same identifying fields the header-only baseline embeds.
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
    for needle in ("SA22-0000-00", "Synthetic Reference", "Chapter 2 > IEA500I", "IEA500I body"):
        assert needle in user


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
    with pytest.raises(ValueError, match="unknown chunk ids"):
        ctx_mod.append_context_entries(tmp_path / "c.jsonl", binding, [c1], {"zzz": "gist"})
    assert not (tmp_path / "c.jsonl").exists()


def test_unschematized_and_malformed_records_are_misses_not_errors(tmp_path, caplog):
    """Records without the current schema (no model/input/cap binding) and
    malformed schema-2 records never hit and never raise; each is a skipped
    line, and the file is left untouched."""
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
    assert caplog.text.count("context_cache_skip_line") == len(bad) + 1
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


def test_complete_request_contract():
    http = FakeHttpClient([FakeResp("  Gist with\nnewline.  ")])
    client = ctx_mod.ContextLLMClient(_settings(), client=http)
    out = client.complete([ChatMessage(role="user", content="hi")])
    assert out == "Gist with newline."
    (post,) = http.posts
    assert post["url"] == "http://context.internal/v1/chat/completions"
    assert post["json"]["model"] == "test-gist-model"
    assert post["json"]["temperature"] == 0.0
    assert post["json"]["max_tokens"] == ctx_mod.MAX_COMPLETION_TOKENS
    assert post["headers"] == {}  # no key: header absent, not empty
    keyed = FakeHttpClient([FakeResp("Gist.")])
    ctx_mod.ContextLLMClient(_settings(context_llm_api_key="sk-test-context"), client=keyed).complete(
        [ChatMessage(role="user", content="hi")]
    )
    assert keyed.posts[0]["headers"] == {"Authorization": "Bearer sk-test-context"}
    failing = FakeHttpClient([FakeResp(status_code=500)])
    with pytest.raises(RuntimeError, match="HTTP 500"):
        ctx_mod.ContextLLMClient(_settings(), client=failing).complete(
            [ChatMessage(role="user", content="hi")]
        )


def test_generate_contexts_uses_cache_and_generates_misses():
    cache = {_binding().key(_chunk("c1")): "Cached gist."}
    http = FakeHttpClient([FakeResp("Fresh gist.")])
    full, new = _gen([_chunk("c1"), _chunk("c2")], http, cache)
    assert full == {"c1": "Cached gist.", "c2": "Fresh gist."}
    assert new == {"c2": "Fresh gist."}
    assert len(http.posts) == 1  # the hit made zero LLM calls
    # Second pass over the same input: all hits, zero calls.
    http2 = FakeHttpClient([])  # any POST raises
    full2, new2 = _gen([_chunk("c1"), _chunk("c2")], http2, cache)
    assert full2 == full and new2 == {} and http2.posts == []


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
    ("edit", "override"),
    [
        ({"text": "IEA500I changed body text"}, {}),  # chunk input under a stable chunk id
        ({}, {"title": "Other Title"}),  # header field
        ({}, {"sha": "other-sha"}),  # doc content digest
    ],
)
def test_changed_inputs_under_stable_chunk_id_miss(edit, override):
    cache: dict[str, str] = {}
    _gen([_chunk()], FakeHttpClient([FakeResp("Original gist.")]), cache)
    http = FakeHttpClient([FakeResp("Edited gist.")])
    full, _ = _gen([_chunk(**edit)], http, cache, **override)
    assert full == {"c1": "Edited gist."}
    assert len(http.posts) == 1
    if edit:
        assert next(iter(edit.values())) in http.posts[0]["json"]["messages"][1]["content"]


def test_unnormalized_cache_hit_is_regenerated():
    """A hit that violates the current normalization policy is not served."""
    assert ctx_mod.normalize_context("  a   b\nc  ", 100) == "a b c"
    assert ctx_mod.normalize_context("x" * 600, 500) == "x" * 500
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


def test_run_fails_closed_without_context_llm(monkeypatch, tmp_path):
    """Enabled without a context LLM can never silently embed header-only
    vectors (the hash-mode refusal is pinned in test_run_ingest's preflight
    matrix, case contextual_on_hash)."""
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
        sha256_file(synthetic_pdf), True, str(cache_path),
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
        doc_sha256=sha256_file(synthetic_pdf),
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
        sha256_file(synthetic_pdf), False, str(tmp_path / "ctx.jsonl"),
    )
    record, _, _chunks, vectors, contexts = run_ingest._parse_one(task)
    assert record.status != "error"
    assert vectors == []
    assert contexts == {}


def _resume_env(
    synthetic_pdf, tmp_path, monkeypatch, *, model="model-a", max_chars=500, embedder=None
):
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
    emb = embedder if embedder is not None else HashEmbedder()
    monkeypatch.setattr(run_ingest, "_get_embedder", lambda s: emb)
    cache_path = tmp_path / "inv.contexts.jsonl"

    def run(http, *, append=True, expect_error=False):
        monkeypatch.setattr(
            run_ingest, "_get_context_client", lambda s: ctx_mod.ContextLLMClient(s, client=http)
        )
        run_ingest._worker_context_cache = None
        run_ingest._worker_context_cache_path = None
        task = (
            str(synthetic_pdf), None, None, None, str(synthetic_pdf.parent),
            sha256_file(synthetic_pdf), True, str(cache_path),
        )
        record, parsed, chunks, vectors, contexts = run_ingest._parse_one(task)
        if expect_error:
            return record, contexts
        assert record.status != "error"
        if append and contexts:
            ctx_mod.append_context_entries(
                cache_path,
                ctx_mod.ContextBinding.from_settings(
                    settings,
                    doc_sha256=sha256_file(synthetic_pdf),
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
            {"v": "v2", "doc_sha256": sha256_file(synthetic_pdf), "chunk_id": c.chunk_id, "context": "L" * 130}
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
        sha256_file(synthetic_pdf), True, str(cache_path),
    )
    record, _, chunks, vectors, contexts = run_ingest._parse_one(task)
    assert record.status != "error"
    assert contexts == {}
    assert len(vectors) == len(chunks) > 0
    assert not cache_path.exists()


class _RecordingEmbedder:
    """HashEmbedder that records the exact dense and sparse inputs."""

    def __init__(self):
        from mainframe_rag.ingest.embed import HashEmbedder

        self._inner = HashEmbedder()
        self.dense_texts: list[str] = []
        self.sparse_texts: list[str] = []

    def dense(self, texts):
        self.dense_texts.extend(texts)
        return self._inner.dense(texts)

    def dense_query(self, queries):
        return self.dense(queries)

    def sparse(self, texts):
        self.sparse_texts.extend(texts)
        return self._inner.sparse(texts)

    def sparse_query(self, queries):
        return self._inner.sparse_query(queries)


def test_model_swap_embeds_new_prefix_and_leaves_sparse_unchanged(
    synthetic_pdf, tmp_path, monkeypatch
):
    """Matrix: assert the dense input, not just the cache key. After a model
    swap on a shared sidecar the dense text carries model B's gist and never
    model A's; the sparse (BM25) inputs and vectors are byte-identical to the
    model-A run and to a contextual-off run, so the swap cannot churn BM25."""
    from mainframe_rag.ingest import run_ingest

    emb_a = _RecordingEmbedder()
    _, _, run_a = _resume_env(synthetic_pdf, tmp_path, monkeypatch, model="model-a", embedder=emb_a)
    chunks, vec_a, _ = run_a(FakeHttpClient([FakeResp("Gist from A.")] * 100))
    assert len(emb_a.dense_texts) == len(chunks) > 0

    emb_b = _RecordingEmbedder()
    _, _, run_b = _resume_env(synthetic_pdf, tmp_path, monkeypatch, model="model-b", embedder=emb_b)
    http_b = FakeHttpClient([FakeResp("Gist from B.")] * 100)
    _, vec_b, ctx_b = run_b(http_b)
    assert len(http_b.posts) == len(chunks)
    assert set(ctx_b.values()) == {"Gist from B."}
    assert all("Gist from B." in t and "Gist from A." not in t for t in emb_b.dense_texts)
    assert all("Gist from A." in t for t in emb_a.dense_texts)

    # Contextual off over the same document: header-only dense, same sparse.
    emb_off = _RecordingEmbedder()
    settings_off = Settings(_env_file=None, embed_mode="hash", contextual_embed_enabled=False)
    monkeypatch.setattr(run_ingest, "_load_worker_settings", lambda: settings_off)
    monkeypatch.setattr(run_ingest, "_get_embedder", lambda s: emb_off)
    record, _, _, vec_off, ctx_off = run_ingest._parse_one(
        (
            str(synthetic_pdf), None, None, None, str(synthetic_pdf.parent),
            sha256_file(synthetic_pdf), True, str(tmp_path / "inv.contexts.jsonl"),
        )
    )
    assert record.status != "error" and ctx_off == {}
    assert emb_a.sparse_texts == emb_b.sparse_texts == emb_off.sparse_texts
    assert [v[1] for v in vec_a] == [v[1] for v in vec_b] == [v[1] for v in vec_off]
    assert all("Gist" not in t for t in emb_off.dense_texts)
    # Dense differs from header-only exactly where a prefix was applied.
    assert [v[0] for v in vec_b] != [v[0] for v in vec_off]


def test_interrupted_worker_persists_nothing_and_restart_regenerates(
    synthetic_pdf, tmp_path, monkeypatch
):
    """The model fails partway through a document: the worker returns an error
    record with no contexts (no half-embedded doc, no empty prefix), the
    parent appends nothing, and a restarted (cold) worker regenerates every
    chunk from a clean sidecar and then hits fully."""
    _, cache_path, run = _resume_env(synthetic_pdf, tmp_path, monkeypatch)
    probe = FakeHttpClient([FakeResp("probe")] * 100)
    chunks, _, _ = run(probe, append=False)
    n = len(chunks)
    assert n >= 2, "synthetic document must have several chunks to interrupt"

    flaky = FakeHttpClient([FakeResp("Partial.")] * (n - 1) + [FakeResp(status_code=503)])
    record, contexts = run(flaky, append=False, expect_error=True)
    assert record.status == "error" and record.error_type == "RuntimeError"
    assert contexts == {}
    assert len(flaky.posts) == n
    assert not cache_path.exists()  # nothing was persisted for the failed doc

    restarted = FakeHttpClient([FakeResp("Whole gist.")] * 100)
    _, _, ctx = run(restarted)
    assert len(restarted.posts) == n
    assert set(ctx.values()) == {"Whole gist."}
    again = FakeHttpClient([])
    _, _, ctx2 = run(again)
    assert again.posts == [] and ctx2 == ctx


def test_live_worker_retry_reuses_only_completed_gists(synthetic_pdf, tmp_path, monkeypatch):
    """Same worker process retries the interrupted doc: gists it already
    completed are hits (their identity matches), the failed chunk and the rest
    call the model once each, and the result is a complete map."""
    from mainframe_rag.ingest import run_ingest

    _, cache_path, run = _resume_env(synthetic_pdf, tmp_path, monkeypatch)
    probe = FakeHttpClient([FakeResp("probe")] * 100)
    chunks, _, _ = run(probe, append=False)
    n = len(chunks)
    assert n >= 3

    flaky = FakeHttpClient([FakeResp("Early.")] * 2 + [FakeResp(status_code=503)])
    record, _ = run(flaky, append=False, expect_error=True)
    assert record.status == "error"

    # Retry without resetting worker state (the real pool reuses processes).
    http = FakeHttpClient([FakeResp("Late.")] * 100)
    monkeypatch.setattr(
        run_ingest, "_get_context_client", lambda s: ctx_mod.ContextLLMClient(s, client=http)
    )
    task = (
        str(synthetic_pdf), None, None, None, str(synthetic_pdf.parent),
        sha256_file(synthetic_pdf), True, str(cache_path),
    )
    record2, _, chunks2, _, contexts = run_ingest._parse_one(task)
    assert record2.status != "error"
    assert len(http.posts) == n - 2
    assert [contexts[c.chunk_id] for c in chunks2] == ["Early."] * 2 + ["Late."] * (n - 2)


def test_append_after_torn_tail_does_not_corrupt_the_new_record(tmp_path):
    """A parent killed mid-append leaves a newline-less partial last line. The
    next append must start on a fresh line: otherwise it is glued onto the
    fragment and the new, valid record is lost to the loader."""
    path = tmp_path / "contexts.jsonl"
    c1, c2 = _chunk("c1"), _chunk("c2")
    binding = _binding()
    ctx_mod.append_context_entries(path, binding, [c1, c2], {"c1": "Kept gist."})
    full = json.dumps(binding.record(c2, "Torn gist."))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(full[: len(full) // 2])  # simulated crash mid-write
    assert not path.read_text().endswith("\n")

    ctx_mod.append_context_entries(path, binding, [c1, c2], {"c2": "Replacement gist."})
    loaded = ctx_mod.load_context_cache(path)
    assert loaded[binding.key(c1)] == "Kept gist."
    assert loaded[binding.key(c2)] == "Replacement gist."
