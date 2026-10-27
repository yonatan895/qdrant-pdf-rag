"""EMBED_MODE=hash tests (issue #8): deterministic, no network, CI/dev only."""

import math

from mainframe_rag.config import HASH_EMBED_DIM, Settings
from mainframe_rag.ingest.embed import build_embedder, hash_dense_embed, hash_sparse_embed
from mainframe_rag.ports import Embedder


def _hash_settings() -> Settings:
    return Settings(embed_mode="hash", _env_file=None)


def test_hash_embedder_is_deterministic_normalized_and_offline():
    text = "the widget torque buffer reached capacity WDG001I"
    vecs = hash_dense_embed(["IEA500I BEFORE IOS IOSCMDS COMMAND REJECTED", "torque the widget"])
    assert len(vecs) == 2
    for v in vecs:
        assert len(v) == HASH_EMBED_DIM
        assert abs(math.sqrt(sum(x * x for x in v)) - 1.0) < 1e-9
    assert hash_dense_embed([text]) == hash_dense_embed([text])
    assert hash_sparse_embed([text]) == hash_sparse_embed([text])
    assert hash_dense_embed(["torque wrench calibration"]) != hash_dense_embed(["mainframe channel subsystem"])
    assert hash_dense_embed([""]) == [[0.0] * HASH_EMBED_DIM]
    idx, vals = hash_sparse_embed(["torque the widget screws; torque specs"])[0]
    assert idx == sorted(idx) and len(idx) == len(vals)
    assert all(0 <= i < (1 << 31) for i in idx)
    assert all(v > 0 for v in vals)
    # Dispatch needs no EMBED_BASE_URL (no network).
    embedder = build_embedder(_hash_settings())
    assert isinstance(embedder, Embedder)
    assert len(embedder.dense(["IEA500I"])[0]) == HASH_EMBED_DIM


def test_vllm_mode_with_base_url_dispatches_to_vllm_embedder_not_hash():
    """#1034: vllm settings (even with embed_base_url set) must build the real
    VllmEmbedder, never HashEmbedder; construction needs no network."""
    from mainframe_rag.ingest.embed import HashEmbedder, VllmEmbedder

    s = Settings(embed_mode="vllm", embed_base_url="http://embed.invalid/v1",
                 embed_model="m", dense_dim=1024, _env_file=None)
    embedder = build_embedder(s)
    assert isinstance(embedder, VllmEmbedder)
    assert not isinstance(embedder, HashEmbedder)
    assert isinstance(build_embedder(_hash_settings()), HashEmbedder)


def test_hash_mode_needs_no_dense_dim_and_rejects_vllm_requirement():
    import pytest

    s = Settings(embed_mode="hash", dense_dim=None, _env_file=None)
    assert s.require_dense_dim() == HASH_EMBED_DIM
    with pytest.raises(RuntimeError, match="vLLM-only"):
        s.require_embed()


def test_vllm_embedder_encodes_queries_and_documents_with_their_own_bm25_encoder(monkeypatch):
    """Issue #836: FastEmbed BM25 has a document encoder (term-frequency
    saturation, length normalization) and a query encoder (each distinct
    term at weight 1). Ingest keeps the document encoder; a query must never
    go through it, or a repeated query term is weighted like a document's."""
    from types import SimpleNamespace

    import numpy as np

    from mainframe_rag.ingest import embed as embed_mod

    calls: list[tuple[str, list[str]]] = []

    class FakeBm25:
        def embed(self, texts):
            calls.append(("embed", list(texts)))
            return [SimpleNamespace(indices=np.array([7, 9]), values=np.array([1.899, 1.67])) for _ in texts]

        def query_embed(self, queries):
            calls.append(("query_embed", list(queries)))
            return [SimpleNamespace(indices=np.array([7, 9]), values=np.array([1, 1])) for _ in queries]

    monkeypatch.setattr(embed_mod, "_bm25_model", lambda name, cache_dir: FakeBm25())
    embedder = embed_mod.VllmEmbedder(Settings(embed_mode="vllm", _env_file=None))

    assert embedder.sparse(["IEFBR14 abend IEFBR14 abend code"]) == [([7, 9], [1.899, 1.67])]
    queries = embedder.sparse_query(["IEFBR14 abend IEFBR14 abend code"])
    assert queries == [([7, 9], [1.0, 1.0])]
    assert all(type(v) is float for _, values in queries for v in values)
    assert calls == [
        ("embed", ["IEFBR14 abend IEFBR14 abend code"]),
        ("query_embed", ["IEFBR14 abend IEFBR14 abend code"]),
    ]
    assert embedder.sparse_query([]) == [] and len(calls) == 2


def test_hash_sparse_query_matches_sparse_so_hash_baselines_hold():
    """The hash leg has one sparse encoding for both sides, like dense_query."""
    embedder = build_embedder(_hash_settings())
    text = "torque the widget screws; torque specs"
    assert embedder.sparse_query([text]) == embedder.sparse([text]) == hash_sparse_embed([text])


def _header_capturing_embedder(api_key):
    """VllmEmbedder double recording the headers kwarg of its /embeddings POST."""
    from types import SimpleNamespace

    from mainframe_rag.ingest.embed import VllmEmbedder

    seen: dict = {}

    class FakeHttp:
        def post(self, url, json, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(
                status_code=200,
                raise_for_status=lambda: None,
                json=lambda: {
                    "data": [{"index": i, "embedding": [0.1] * 8} for i in range(len(json["input"]))]
                },
            )

    settings = Settings(
        embed_mode="vllm",
        embed_base_url="http://mock:8000/v1",
        embed_model="mock-model",
        dense_dim=8,
        embed_api_key=api_key,
        _env_file=None,
    )
    return VllmEmbedder(settings, client=FakeHttp()), seen


def test_vllm_embedder_auth_header_present_only_when_key_set():
    embedder, seen = _header_capturing_embedder("sk-test-embed")
    embedder.dense(["hi"])
    assert seen["headers"] == {"Authorization": "Bearer sk-test-embed"}
    embedder, seen = _header_capturing_embedder(None)
    embedder.dense(["hi"])
    assert seen["headers"] == {}


def _counting_vllm_embedder(**overrides):
    """VllmEmbedder over a fake HTTP client that counts /embeddings POSTs."""
    from types import SimpleNamespace

    from mainframe_rag.ingest.embed import VllmEmbedder

    posts: list[list[str]] = []

    class FakeHttp:
        def post(self, url, json, **kwargs):
            posts.append(list(json["input"]))
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "data": [{"index": i, "embedding": [0.1] * 8} for i in range(len(json["input"]))]
                },
            )

    settings = Settings(
        embed_mode="vllm",
        embed_base_url="http://mock:8000/v1",
        embed_model="mock-model",
        dense_dim=8,
        dense_query_prefix="Q: ",
        _env_file=None,
        **overrides,
    )
    return VllmEmbedder(settings, client=FakeHttp()), posts


def test_embed_bound_refuses_before_the_post_and_never_truncates():
    """Issue #374: an over-bound input is refused with counts only — the
    endpoint is never called for the batch (not even for its in-bound
    members) and nothing is truncated. The bound counts the query prefix."""
    import pytest

    from mainframe_rag.ingest.bounds import EmbedInputTooLarge

    embedder, posts = _counting_vllm_embedder(embed_max_input_chars=50)
    with pytest.raises(EmbedInputTooLarge) as caught:
        embedder.dense(["short", "x" * 51, "y" * 60])
    assert posts == []
    assert (caught.value.limit, caught.value.largest, caught.value.count) == (50, 60, 2)
    assert "xxxx" not in str(caught.value), "message carries counts only, never input text"

    # At the bound is accepted and sent byte-identically.
    embedder.dense(["z" * 50])
    assert posts == [["z" * 50]]

    prefixed, qposts = _counting_vllm_embedder(embed_max_input_chars=10)
    prefixed.dense_query(["1234567"])  # "Q: " + 7 chars == 10
    assert qposts == [["Q: 1234567"]]
    with pytest.raises(EmbedInputTooLarge):
        prefixed.dense_query(["12345678"])
    assert len(qposts) == 1


def test_embed_bound_default_is_unbounded_legacy_behaviour():
    """The 43k-character JCL statement of issue #374 is still embedded whole
    when no bound is selected (no silent default change)."""
    embedder, posts = _counting_vllm_embedder()
    embedder.dense(["J" * 43301])
    assert posts == [["J" * 43301]]
