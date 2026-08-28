"""EMBED_MODE=hash tests (issue #8): deterministic, no network, CI/dev only."""

import math

from mainframe_rag.config import HASH_EMBED_DIM, Settings
from mainframe_rag.ingest.embed import build_embedder, hash_dense_embed, hash_sparse_embed
from mainframe_rag.ports import Embedder


def _hash_settings() -> Settings:
    return Settings(embed_mode="hash", _env_file=None)


def test_dense_dim_and_norm():
    vecs = hash_dense_embed(["IEA500I BEFORE IOS IOSCMDS COMMAND REJECTED", "torque the widget"])
    assert len(vecs) == 2
    for v in vecs:
        assert len(v) == HASH_EMBED_DIM
        assert abs(math.sqrt(sum(x * x for x in v)) - 1.0) < 1e-9


def test_deterministic_across_calls():
    text = "the widget torque buffer reached capacity WDG001I"
    assert hash_dense_embed([text]) == hash_dense_embed([text])
    assert hash_sparse_embed([text]) == hash_sparse_embed([text])


def test_empty_text_is_zero_vector():
    assert hash_dense_embed([""]) == [[0.0] * HASH_EMBED_DIM]


def test_sparse_shape_and_determinism():
    idx, vals = hash_sparse_embed(["torque the widget screws; torque specs"][0])[0]
    assert idx == sorted(idx)
    assert all(0 <= i < (1 << 31) for i in idx)
    assert all(v > 0 for v in vals)
    assert len(idx) == len(vals)


def test_dispatch_uses_hash_without_network():
    s = _hash_settings()
    embedder = build_embedder(s)  # must not require EMBED_BASE_URL
    assert isinstance(embedder, Embedder)
    dense = embedder.dense(["IEA500I"])
    assert len(dense[0]) == HASH_EMBED_DIM
    assert embedder.sparse(["IEA500I"])[0][0] == sorted(embedder.sparse(["IEA500I"])[0][0])


def test_hash_mode_needs_no_dense_dim_env():
    s = Settings(embed_mode="hash", dense_dim=None, _env_file=None)
    assert s.require_dense_dim() == HASH_EMBED_DIM


def test_hash_mode_rejects_vllm_endpoint_requirement():
    s = Settings(embed_mode="hash", _env_file=None)
    import pytest

    with pytest.raises(RuntimeError, match="vLLM-only"):
        s.require_embed()


def test_different_texts_differ():
    a = hash_dense_embed(["torque wrench calibration"])[0]
    b = hash_dense_embed(["mainframe channel subsystem"])[0]
    assert a != b
