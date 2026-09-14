"""Issue #361 step 1: source-revision identity planning gate.

Fail-closed collision detection + deterministic duplicate handling, all
before any delete/upsert. Unit pins use literal expected keys (never
make-key == make-key self-comparisons); integration tests fire the real
planning path (dry-run and a recording fake — never the fallback path).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from mainframe_rag.config import HASH_EMBED_DIM
from mainframe_rag.ingest.identity import (
    RevisionCollisionError,
    corpus_relpath,
    find_collisions,
    normalize_label,
    plan_duplicates,
    prescan_doc_ids,
    source_rev_key,
)


def _write_text_pdf(path: Path, lines: list[str]) -> Path:
    """Original prose PDFs at runtime (never fixtures): one page per ~40
    lines so multi-page stems behave like real manuals."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72.0
    for line in lines:
        if y > 720:
            page = doc.new_page()
            y = 72.0
        page.insert_text((72, y), line, fontsize=11)
        y += 14.0
    doc.set_metadata({"title": path.stem, "author": "identity-gate test"})
    doc.save(path)
    doc.close()
    return path


def _stderr_actions(capsys) -> list[dict]:
    actions = []
    for line in capsys.readouterr().err.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "action" in obj:
            actions.append(obj)
    return actions


# ---------------------------------------------------------------------------
# normalize_label: the one normalization, pinned literally
# ---------------------------------------------------------------------------


def test_normalize_label_matrix():
    assert normalize_label("IBM") == "ibm"
    assert normalize_label("  z/OS  ") == "z/os"
    assert normalize_label("V1\t R1") == "v1 r1"
    assert normalize_label("Red  Hat") == "red hat"
    assert normalize_label(None) == ""
    assert normalize_label("") == ""


def test_source_rev_key_literal():
    sha = "ab" * 32
    assert source_rev_key("IBM", "z/OS", "3.1", sha) == f"ibm|z/os|3.1|{sha}"
    # Missing labels are empty segments, never an empty-key collision: the
    # content hash always disambiguates.
    assert source_rev_key(None, None, None, sha) == f"|||{sha}"
    assert source_rev_key("unknown", "unknown", "", sha) == f"unknown|unknown||{sha}"


def test_source_rev_key_separates_revisions_not_case():
    sha = "ab" * 32
    other_sha = "cd" * 32
    assert source_rev_key("IBM", "z/OS", "3.1", sha) != source_rev_key("IBM", "z/OS", "3.1", other_sha)
    assert source_rev_key("IBM", "z/OS", "3.1", sha) != source_rev_key("IBM", "z/OS", "3.2", sha)
    assert source_rev_key("IBM", "z/OS", "3.1", sha) != source_rev_key("BMC", "z/OS", "3.1", sha)
    # Case/whitespace variants are the SAME revision, not a collision.
    assert source_rev_key("ibm", "Z/os", "3.1", sha) == source_rev_key("IBM", "z/OS", "3.1", sha)
    assert source_rev_key(" IBM ", "z/OS", "3.1", sha) == source_rev_key("IBM", "z/OS", "3.1", sha)


def test_corpus_relpath_is_mount_portable():
    assert corpus_relpath("/mnt/a/corpus/x/y.pdf", Path("/mnt/a/corpus")) == "x/y.pdf"
    assert corpus_relpath("/mnt/b/corpus/x/y.pdf", Path("/mnt/b/corpus")) == "x/y.pdf"
    # Outside the root: bare filename, never the absolute layout.
    assert corpus_relpath("/mnt/secret/other.pdf", Path("/mnt/a/corpus")) == "other.pdf"


# ---------------------------------------------------------------------------
# plan_duplicates: byte-identical copies elect the lexicographic winner
# ---------------------------------------------------------------------------

_ENTRIES = [
    ("/corpus/B/x.pdf", "s1"),
    ("/corpus/A/x.pdf", "s1"),
    ("/corpus/C/y.pdf", "s2"),
    ("/corpus/D/z.pdf", "s1"),
]


def test_plan_duplicates_deterministic_winner():
    kept, duplicates = plan_duplicates(_ENTRIES, Path("/corpus"))
    assert kept == [("/corpus/A/x.pdf", "s1"), ("/corpus/C/y.pdf", "s2")]
    assert [(d.loser_rel, d.winner_rel) for d in duplicates] == [
        ("B/x.pdf", "A/x.pdf"),
        ("D/z.pdf", "A/x.pdf"),
    ]


def test_plan_duplicates_order_independent():
    kept_fwd, dups_fwd = plan_duplicates(_ENTRIES, Path("/corpus"))
    kept_rev, dups_rev = plan_duplicates(list(reversed(_ENTRIES)), Path("/corpus"))
    assert sorted(kept_fwd) == sorted(kept_rev)
    assert sorted((d.loser_rel, d.winner_rel) for d in dups_fwd) == sorted(
        (d.loser_rel, d.winner_rel) for d in dups_rev
    )


def test_plan_duplicates_unique_corpus_is_untouched():
    entries = [(f"/corpus/{c}.pdf", f"s{i}") for i, c in enumerate("abcd")]
    kept, duplicates = plan_duplicates(entries, Path("/corpus"))
    assert kept == entries and duplicates == []


# ---------------------------------------------------------------------------
# find_collisions: same doc_id + distinct sha aborts; nothing else does
# ---------------------------------------------------------------------------


def test_find_collisions_basic_and_sorted():
    resolved = [
        ("/corpus/b/reference.pdf", "sha2", "reference"),
        ("/corpus/a/reference.pdf", "sha1", "reference"),
        ("/corpus/c/other.pdf", "sha3", "other"),
    ]
    (collision,) = find_collisions(resolved, Path("/corpus"))
    assert collision.doc_id == "reference"
    assert collision.members == (("a/reference.pdf", "sha1"), ("b/reference.pdf", "sha2"))


def test_find_collisions_clean_cases():
    # Same doc_id + same sha (post-dedup residue) is fine.
    assert find_collisions(
        [("/corpus/a/r.pdf", "s1", "r"), ("/corpus/b/r.pdf", "s1", "r")], Path("/corpus")
    ) == []
    # Distinct doc_ids never collide.
    assert find_collisions(
        [("/corpus/a.pdf", "s1", "d1"), ("/corpus/b.pdf", "s2", "d2")], Path("/corpus")
    ) == []
    # Unreadable inputs (None) neither cause nor hide collisions.
    assert find_collisions(
        [("/corpus/a.pdf", "s1", None), ("/corpus/b.pdf", "s1", None)], Path("/corpus")
    ) == []
    assert find_collisions(
        [("/corpus/a.pdf", "s1", "d"), ("/corpus/b.pdf", "s2", None)], Path("/corpus")
    ) == []


def test_find_collisions_order_independent_and_sha16():
    sha1, sha2 = "a" * 64, "b" * 64
    resolved = [("/corpus/a.pdf", sha1, "d"), ("/corpus/b.pdf", sha2, "d")]
    fwd = find_collisions(resolved, Path("/corpus"))
    rev = find_collisions(list(reversed(resolved)), Path("/corpus"))
    assert fwd == rev
    assert fwd[0].members == (("a.pdf", "a" * 16), ("b.pdf", "b" * 16))


def test_collision_report_names_inputs_not_layout_or_text():
    err = RevisionCollisionError(
        find_collisions(
            [("/mnt/secret/corpus/a/reference.pdf", "aa" * 32, "reference"),
             ("/mnt/secret/corpus/b/reference.pdf", "bb" * 32, "reference")],
            Path("/mnt/secret/corpus"),
        )
    )
    text = str(err)
    assert "'reference'" in text
    assert "a/reference.pdf" in text and "b/reference.pdf" in text
    assert "/mnt/secret" not in text
    assert "--vendor/--product/--version" in text


def test_prescan_doc_ids_never_raise():
    assert prescan_doc_ids([]) == {}
    # Filename-form resolves without any readable file behind it.
    assert prescan_doc_ids(["/nonexistent/SA22-0000-00_note.pdf"]) == {
        "/nonexistent/SA22-0000-00_note.pdf": "SA22-0000-00"
    }
    assert prescan_doc_ids(["/nonexistent/missing.pdf"]) == {"/nonexistent/missing.pdf": None}


def test_prescan_doc_id_unreadable_file_maps_none(tmp_path):
    bad = tmp_path / "reference.pdf"
    bad.write_bytes(b"not a pdf at all")
    assert prescan_doc_ids([str(bad)]) == {str(bad): None}


# ---------------------------------------------------------------------------
# Integration: the real planning path (dry-run needs no Qdrant)
# ---------------------------------------------------------------------------


def test_same_stem_collision_aborts_before_any_work(tmp_path, capsys):
    from mainframe_rag.ingest.run_ingest import main

    _write_text_pdf(tmp_path / "a" / "reference.pdf", ["Acme widget guide, edition one."])
    _write_text_pdf(tmp_path / "b" / "reference.pdf", ["Acme widget guide, edition two."])
    progress = tmp_path / "inventory.jsonl"
    with pytest.raises(RevisionCollisionError, match="reference"):
        main(["--src", str(tmp_path), "--progress", str(progress),
              "--workers", "1", "--dry-run"])
    # Aborted in planning: no pool spawned, no inventory line, no upserts.
    assert not progress.exists()
    assert all(a.get("action") not in ("dry", "upserted", "skip") for a in _stderr_actions(capsys))


def test_text_derived_collision_aborts(tmp_path):
    """Different stems, one form number inside the text: prescan must open
    the PDFs (filename matching alone would miss this)."""
    from mainframe_rag.ingest.run_ingest import main

    _write_text_pdf(tmp_path / "alpha-manual.pdf", ["SA22-7777-77", "Alpha edition prose."])
    _write_text_pdf(tmp_path / "beta-manual.pdf", ["SA22-7777-77", "Beta edition prose."])
    with pytest.raises(RevisionCollisionError, match="SA22-7777-77"):
        main(["--src", str(tmp_path), "--progress", str(tmp_path / "inv.jsonl"),
              "--workers", "1", "--dry-run"])


def test_filename_form_collision_aborts(tmp_path):
    from mainframe_rag.ingest.run_ingest import main

    _write_text_pdf(tmp_path / "a" / "SA22-9999-00_note.pdf", ["First note text."])
    _write_text_pdf(tmp_path / "b" / "SA22-9999-00_note.pdf", ["Second note text."])
    with pytest.raises(RevisionCollisionError, match="SA22-9999-00"):
        main(["--src", str(tmp_path), "--progress", str(tmp_path / "inv.jsonl"),
              "--workers", "1", "--dry-run"])


def test_duplicate_copies_ingest_winner_once(tmp_path, capsys):
    from mainframe_rag.ingest.run_ingest import main

    first = _write_text_pdf(tmp_path / "b" / "reference.pdf", ["Acme widget guide, shared edition."])
    (tmp_path / "a").mkdir(parents=True, exist_ok=True)
    shutil.copy(first, tmp_path / "a" / "reference.pdf")
    progress = tmp_path / "inventory.jsonl"
    assert main(["--src", str(tmp_path), "--progress", str(progress),
                 "--workers", "1", "--dry-run"]) == 0
    records = [json.loads(l) for l in progress.read_text().splitlines() if l.strip()]
    assert len(records) == 1
    assert records[0]["path"].endswith("a/reference.pdf")
    assert records[0]["status"] == "dry"
    dups = [a for a in _stderr_actions(capsys) if a.get("action") == "duplicate"]
    assert len(dups) == 1
    assert dups[0]["winner"].endswith("a/reference.pdf")
    # Rerun elects the same winner (deterministic, no stored state).
    assert main(["--src", str(tmp_path), "--progress", str(progress),
                 "--workers", "1", "--dry-run"]) == 0
    dups2 = [a for a in _stderr_actions(capsys) if a.get("action") == "duplicate"]
    assert [d["winner"] for d in dups2] == [dups[0]["winner"]]


def test_unreadable_file_neither_causes_nor_hides_collision(tmp_path):
    """A corrupt twin of a healthy stem must not abort planning (prescan
    maps it to None); the worker still error-records it per file."""
    from mainframe_rag.ingest.run_ingest import main

    _write_text_pdf(tmp_path / "a" / "reference.pdf", ["Acme widget guide, healthy."])
    (tmp_path / "b").mkdir(parents=True, exist_ok=True)
    (tmp_path / "b" / "reference.pdf").write_bytes(b"not a pdf at all")
    progress = tmp_path / "inventory.jsonl"
    assert main(["--src", str(tmp_path), "--progress", str(progress),
                 "--workers", "1", "--dry-run"]) == 1
    records = [json.loads(l) for l in progress.read_text().splitlines() if l.strip()]
    by_status = {}
    for rec in records:
        by_status.setdefault(rec["status"], []).append(rec)
    assert len(by_status["dry"]) == 1 and by_status["dry"][0]["path"].endswith("a/reference.pdf")
    assert len(by_status["error"]) == 1 and by_status["error"][0]["path"].endswith("b/reference.pdf")


def test_nondry_collision_writes_nothing(tmp_path, monkeypatch):
    """The claimed path: a real (non-dry) run aborts in planning with zero
    upserts and zero deletes — the second writer never reaches its
    delete_by_doc."""

    class _RecordingFake:
        def __init__(self):
            self.upserts: list[int] = []
            self.deletes = 0

        def collection_exists(self, name):
            return True

        def get_collection(self, name):
            return SimpleNamespace(
                config=SimpleNamespace(
                    params=SimpleNamespace(vectors={"dense": SimpleNamespace(size=HASH_EMBED_DIM)})
                )
            )

        def create_collection(self, name, **kwargs):
            return True

        def create_payload_index(self, *a, **k):
            return SimpleNamespace()

        def scroll(self, *a, **k):
            return [], None

        def upsert(self, name, *, points, wait=True):
            self.upserts.append(len(points))

        def delete(self, name, *, points_selector, wait=True):
            self.deletes += 1

    from mainframe_rag.ingest import run_ingest

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    fake = _RecordingFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    _write_text_pdf(tmp_path / "a" / "reference.pdf", ["Edition one prose."])
    _write_text_pdf(tmp_path / "b" / "reference.pdf", ["Edition two prose."])
    with pytest.raises(RevisionCollisionError, match="reference"):
        run_ingest.main(["--src", str(tmp_path), "--progress", str(tmp_path / "inv.jsonl"),
                         "--workers", "1"])
    assert fake.upserts == [] and fake.deletes == 0


def test_parse_one_stamps_source_rev_literal(tmp_path, monkeypatch):
    """Inventory provenance for the 361B migration: exact key composition."""
    from mainframe_rag.config import Settings
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.ibm_pdf import sha256_file

    pdf = _write_text_pdf(tmp_path / "manual.pdf", ["Acme widget guide."])
    sha = sha256_file(pdf)
    monkeypatch.setattr(
        run_ingest, "_load_worker_settings", lambda: Settings(_env_file=None)
    )
    (record, _, _, _, _) = run_ingest._parse_one(
        (str(pdf), "IBM", "  z/OS ", "3.1", str(tmp_path), sha, False, None)
    )
    assert record.source_rev == f"ibm|z/os|3.1|{sha}"


def test_upsert_payload_carries_source_rev():
    from mainframe_rag.ingest.chunk import Chunk
    from mainframe_rag.ingest.ibm_pdf import ParsedDoc
    from mainframe_rag.ingest.qdrant_io import upsert_chunks
    from tests.test_qdrant_io import RecordingClient, _settings

    class _Capture(RecordingClient):
        def __init__(self):
            super().__init__()
            self.points = []

        def upsert(self, collection_name, *, points, wait=True):
            self.points.extend(points)
            return True

    client = _Capture()
    parsed = ParsedDoc(
        path="manual.pdf",
        doc_id="SC14-7315-70",
        sha256="abc123",
        vendor="IBM",
        product="z/OS",
        version="3.2",
        title="Sample Manual",
        page_count=10,
    )
    chunk = Chunk(
        chunk_id="00000000-0000-0000-0000-000000000001",
        doc_id="SC14-7315-70",
        heading_path="Chapter 1",
        page_start=1,
        page_label="1",
        chunk_type="narrative",
        text="Body text.",
        message_ids=[],
        members=[],
        ordinal=0,
    )
    assert upsert_chunks(client, _settings(4), parsed, [chunk], [([0.1] * 4, ([1], [1.0]))]) == 1
    assert client.points[0].payload["source_rev"] == "ibm|z/os|3.2|abc123"
