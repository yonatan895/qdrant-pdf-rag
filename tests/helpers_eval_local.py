"""Shared builders for the eval/harness/retrieval/feedback/MCP suites.

Builders only: they remove literal payloads every suite copied, they never
switch a behavior pin. Each call site still names the path it locks, so a
change here cannot silently flip a fallback pin into a success pin
(``docs/testing.md#shared-doubles-share-builders-pin-behavior``).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
from pathlib import Path
from typing import Any

import pytest
from qdrant_client import models

from mainframe_rag.config import Settings
from mainframe_rag.retrieve.query import SearchHit


def eval_settings(**overrides: Any) -> Settings:
    """The hermetic retrieval settings base (no dotenv, fixed collection)."""
    base: dict[str, Any] = {
        "qdrant_url": "http://localhost:6333",
        "qdrant_collection": "mainframe_manuals",
        "dense_dim": 768,
        "embed_base_url": "http://localhost:8000/v1",
        "embed_model": "test-embed",
        "bm25_model": "Qdrant/bm25",
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


def hit(
    chunk_id: str,
    doc_id: str,
    score: float = 0.9,
    *,
    heading: str = "Heading",
    text: str = "Body text",
    page_label: str = "1",
    **overrides: Any,
) -> SearchHit:
    """A fused hit with the shape the ranking/diversify suites assert."""
    fields: dict[str, Any] = {
        "chunk_id": chunk_id,
        "score": score,
        "cite": f"{doc_id} Manual, {heading}, p. {page_label}",
        "heading": heading,
        "text": text,
        "doc_id": doc_id,
        "title": "Manual",
        "page_label": page_label,
        "chunk_type": "narrative",
        "message_ids": (),
        "rerank_score": None,
    }
    fields.update(overrides)
    return SearchHit(**fields)


def point(
    pid: str,
    doc_id: str,
    score: float = 1.0,
    *,
    page_label: str = "1",
    page_start: int | None = None,
    page_end: object = "absent",
    title: str = "Widget Guide",
    heading: str = "Chapter 1. Widgets",
    chunk_type: str = "narrative",
    text: str | None = None,
    **extra: Any,
) -> models.ScoredPoint:
    """A stored point; `page_start=None` and the default `page_end` are absent
    keys (a legacy payload), so physical-page fallbacks stay reachable."""
    payload: dict[str, Any] = {
        "doc_id": doc_id,
        "title": title,
        "heading_path": heading,
        "page_label": page_label,
        "chunk_type": chunk_type,
        "message_ids": [],
        "text": text if text is not None else f"text {pid}",
    }
    if page_start is not None:
        payload["page_start"] = page_start
    if page_end != "absent":
        payload["page_end"] = page_end
    payload.update(extra)
    return models.ScoredPoint(id=pid, version=1, score=score, payload=payload)


def retrieval_report() -> dict:
    """The canned retrieval report the gate/main suites share (hash venue)."""
    return {
        "n": 12,
        "scored": 12,
        "must_not": {"checked": 12, "violations": 0},
        "failures": 0,
        "elapsed_s": 0.25,
        "embed_mode": "hash",
        "collection": "test-corpus",
        "representation": {"servable": True, "outcome": "compatible", "details": [],
                           "physical": "test-corpus__gen1"},
        "recall@1": 0.5,
        "recall@3": 0.75,
        "recall@5": 0.75,
        "recall@8": 0.80,
        "mrr": 0.625,
        "ndcg@8": 0.70,
        "identifier": {"recall@1": 1.0, "recall@5": 1.0, "recall@8": 1.0, "mrr": 1.0, "ndcg@8": 1.0},
        "nl": {"recall@1": 0.333, "recall@5": 0.667, "recall@8": 0.70, "mrr": 0.5, "ndcg@8": 0.60},
        "rows": [],
    }


def lines_of(path: Path) -> list[bytes]:
    """Every JSONL line but the trailing newline (trailer included)."""
    return path.read_bytes().split(b"\n")[:-1]


def rehash(lines: list[bytes]) -> bytes:
    """Recompute the trailer hash over a hand-edited export."""
    body = b"".join(line + b"\n" for line in lines[:-1])
    trailer = json.loads(lines[-1])
    trailer["sha256"] = hashlib.sha256(body).hexdigest()
    return body + json.dumps(trailer, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


REPO_ROOT = Path(__file__).resolve().parents[1]


def task_workspace(tmp_path: Path, *scripts: str) -> Path:
    """The pinned Task plus selected real scripts in a throwaway repo.

    Returns the Task binary. Skips when the pinned runner is absent, and fails
    in the required lane (docs/task-runner.md): a skipped bridge is not a pass.
    """
    from tests.test_taskfile_contracts import REQUIRE_RUNNER, find_task

    task = find_task()
    if task is None:
        if REQUIRE_RUNNER:
            pytest.fail("pinned Task unavailable in required lane")
        pytest.skip("pinned Task unavailable")
    shutil.copy2(REPO_ROOT / "Taskfile.yml", tmp_path / "Taskfile.yml")
    shutil.copytree(REPO_ROOT / "taskfiles", tmp_path / "taskfiles")
    (tmp_path / "scripts").mkdir()
    for name in scripts:
        shutil.copy2(REPO_ROOT / "scripts" / name, tmp_path / "scripts" / name)
    (tmp_path / ".venv/bin").mkdir(parents=True)
    return task


