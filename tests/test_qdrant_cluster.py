"""Hermetic check of the one fixture behavior the `qa:ha` verdict depends on.

The disposable multi-peer fixture is test infrastructure; its behavior is
proven by `tests/test_ha_cluster.py` (integration, `qa:ha`). Only the pinned
image attestation stays here: without it `qa:ha` could pass against an
unpinned or substituted Qdrant image.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from scripts import qdrant_cluster as cluster
from scripts import qdrant_pin


def test_require_docker_rejects_digest_mismatch_without_pull(monkeypatch):
    payload = json.dumps([{
        "Id": "sha256:" + "b" * 64,
        "RepoDigests": ["qdrant/qdrant@sha256:" + "c" * 64],
    }])
    calls = []

    def inspect(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=payload)

    monkeypatch.setattr(qdrant_pin.subprocess, "run", inspect)
    with pytest.raises(cluster.QdrantClusterError):
        cluster.require_docker("docker.io/qdrant/qdrant@sha256:" + "a" * 64)
    assert len(calls) == 1
    assert calls[0][:3] == ["docker", "image", "inspect"]


def test_require_docker_returns_immutable_id_after_digest_attestation(monkeypatch):
    reference = "docker.io/qdrant/qdrant@sha256:" + "a" * 64
    image_id = "sha256:" + "b" * 64
    monkeypatch.setattr(qdrant_pin.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0,
        stdout=json.dumps([{"Id": image_id, "RepoDigests": [reference.removeprefix("docker.io/")]}])))
    assert cluster.require_docker(reference) == image_id
