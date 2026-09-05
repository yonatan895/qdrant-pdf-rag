"""Extraction-rules versioning tests (issue #124).

The #120 failure: a retrieval-side MSG_RE widening desynced queries (new
rules) from payloads (old rules) because nothing versioned the extraction
rules and content-unchanged docs never re-ingest. The version must be
computed (never hand-bumped), gate both skip layers, and fail closed on
collection mismatch.

Hermetic: no Qdrant, no network — fakes for the port, temp files for the
hash seam."""

import json

import pytest

from mainframe_rag.ingest.inventory import InventoryRecord, should_skip
from mainframe_rag.ingest.qdrant_io import upsert_chunks
from mainframe_rag.ingest.rules_version import (
    extraction_rules_version,
    hash_rule_files,
    rule_module_paths,
)


# ---------------------------------------------------------------- version
def test_version_is_stable_and_short() -> None:
    v1 = extraction_rules_version()
    v2 = extraction_rules_version()
    assert v1 == v2
    assert len(v1) == 16
    int(v1, 16)  # hex


def test_version_covers_all_payload_producers() -> None:
    names = {p.name for p in rule_module_paths()}
    assert names == {"regexes.py", "ibm_pdf.py", "chrome.py", "chunk.py", "classify.py"}


def test_hash_changes_when_rules_change(tmp_path) -> None:
    a = tmp_path / "regexes.py"
    a.write_text("MSG_RE = 1\n")
    h1 = hash_rule_files((a,))
    a.write_text("MSG_RE = 2\n")
    assert hash_rule_files((a,)) != h1
    # The file name is folded in: same bytes under another name differ.
    b = tmp_path / "other.py"
    b.write_text("MSG_RE = 1\n")
    assert hash_rule_files((b,)) != h1


# ---------------------------------------------------------------- inventory skip
def test_should_skip_gates_on_rules_version() -> None:
    rec = InventoryRecord(path="x.pdf", sha256="a" * 64, status="upserted", rules_version="v1")
    assert should_skip(rec, "a" * 64, rules_version="v1") is True
    # Rules changed since the record was written: re-ingest despite the
    # identical file bytes — the whole point of the fix.
    assert should_skip(rec, "a" * 64, rules_version="v2") is False
    # Pre-versioning records carry no field and never skip (fail closed).
    old = InventoryRecord(path="x.pdf", sha256="a" * 64, status="upserted")
    assert should_skip(old, "a" * 64, rules_version="v1") is False
    # Sha mismatch still refuses regardless of version.
    assert should_skip(rec, "b" * 64, rules_version="v1") is False


# ---------------------------------------------------------------- payload
def test_upsert_payload_carries_rules_v() -> None:
    from types import SimpleNamespace

    from mainframe_rag.ingest.ibm_pdf import ParsedDoc

    class FakeClient:
        def __init__(self) -> None:
            self.points: list = []

        def upsert(self, collection, *, points, wait=True):
            self.points.extend(points)
            return SimpleNamespace()

    from pathlib import Path

    parsed = ParsedDoc(
        path=Path("x.pdf"), sha256="a" * 64, doc_id="SA22-0000-00", title="t",
        product="p", version="1", vendor="v", page_count=1,
    )
    from mainframe_rag.ingest.chunk import Chunk

    chunk = Chunk(
        chunk_id="c1", doc_id="SA22-0000-00", heading_path="h",
        page_start=0, page_label="1", chunk_type="narrative",
        text="t", message_ids=[], members=[], ordinal=0,
    )
    settings = _settings()
    client = FakeClient()
    upsert_chunks(client, settings, parsed, [chunk], [([0.0] * 8, ([0], [1.0]))])
    assert client.points[0].payload["rules_v"] == extraction_rules_version()


def _settings():
    from mainframe_rag.config import Settings

    return Settings(_env_file=None)


# ---------------------------------------------------------------- ingest gate
def test_ingest_fails_closed_on_rules_mismatch(tmp_path, synthetic_pdf, monkeypatch):
    from mainframe_rag.ingest import run_ingest
    from tests.test_run_ingest import _FakeQdrant

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    fake = _FakeQdrant(stored_sha="c" * 64, stored_rules_v="0123456789abcdef")
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    with pytest.raises(RuntimeError, match="extraction-rules mismatch"):
        run_ingest.main([
            "--src", str(synthetic_pdf.parent),
            "--progress", str(tmp_path / "inv.jsonl"), "--workers", "1",
        ])


def test_ingest_fails_closed_on_legacy_collection(tmp_path, synthetic_pdf, monkeypatch):
    """A collection whose points predate versioning is a mismatch, never a
    pass — it cannot prove which rules extracted its payloads."""
    from mainframe_rag.ingest import run_ingest
    from tests.test_run_ingest import _FakeQdrant

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    fake = _FakeQdrant(stored_sha="c" * 64, stored_rules_v="")
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    with pytest.raises(RuntimeError, match="predates extraction-rules versioning"):
        run_ingest.main([
            "--src", str(synthetic_pdf.parent),
            "--progress", str(tmp_path / "inv.jsonl"), "--workers", "1",
        ])


def test_reingest_flag_reextracts_matching_sha(tmp_path, synthetic_pdf, monkeypatch, capsys):
    """--reingest after a rules change: the startup gate passes, the
    qdrant sha-equal early return is bypassed, and the doc is deleted and
    re-upserted (same file bytes, stale payloads)."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.ibm_pdf import sha256_file
    from tests.test_run_ingest import _FakeQdrant

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    fake = _FakeQdrant(stored_sha=sha256_file(synthetic_pdf), stored_rules_v="0123456789abcdef")
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    rc = run_ingest.main([
        "--src", str(synthetic_pdf.parent),
        "--progress", str(tmp_path / "inv.jsonl"), "--workers", "1",
        "--reingest",
    ])
    assert rc == 0
    assert fake.deletes >= 1
    assert fake.upserts, "the doc must be re-upserted, not skipped"
    done = [l for l in capsys.readouterr().err.splitlines() if l.strip().startswith("{")]
    assert any(json.loads(l).get("action") == "done" for l in done)


def test_reingest_flag_passes_the_startup_gate(tmp_path, synthetic_pdf, monkeypatch):
    """--reingest is also the documented override for the mismatch gate:
    the same run that raises without it completes with it."""
    from mainframe_rag.ingest import run_ingest
    from tests.test_run_ingest import _FakeQdrant

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    fake = _FakeQdrant(stored_sha="c" * 64, stored_rules_v="0123456789abcdef")
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    rc = run_ingest.main([
        "--src", str(synthetic_pdf.parent),
        "--progress", str(tmp_path / "inv.jsonl"), "--workers", "1",
        "--reingest",
    ])
    assert rc == 0


# ---------------------------------------------------------------- manifest
def test_manifest_records_rules_version(tmp_path, monkeypatch) -> None:
    from mainframe_rag.config import Settings
    from mainframe_rag.manifest import write_run_manifest

    monkeypatch.chdir(tmp_path)
    manifest = write_run_manifest(
        "eval", Settings(_env_file=None, embed_mode="hash"), {"n": 1},
        runs_dir=tmp_path / "runs",
    )
    assert manifest["extraction_rules_version"] == extraction_rules_version()
    line = json.loads((tmp_path / "runs" / "eval_runs.jsonl").read_text().splitlines()[0])
    assert line["extraction_rules_version"] == manifest["extraction_rules_version"]
