"""Extraction-rules versioning tests (issue #124).

The #120 failure: a retrieval-side MSG_RE widening desynced queries (new
rules) from payloads (old rules) because nothing versioned the extraction
rules and content-unchanged docs never re-ingest. The version must be
computed (never hand-bumped), gate both skip layers, and fail closed on
collection mismatch.

Hermetic: no Qdrant, no network — fakes for the port, temp files for the
hash seam."""

import json
from pathlib import Path

import pytest

from mainframe_rag.ingest.inventory import InventoryRecord, should_skip
from mainframe_rag.ingest.qdrant_io import upsert_chunks
from mainframe_rag.ingest.rules_version import (
    extraction_rules_version,
    hash_rule_files,
    rule_module_paths,
)
from tests.helpers_builder import staging_builder  # noqa: F401 — fixture

# Real runs publish (in-place ingest retired, #360); these tests cover the
# builder against a fixed collection through tests/helpers_builder.py.
pytestmark = pytest.mark.usefixtures("staging_builder")


# ---------------------------------------------------------------- version
def test_version_is_stable_and_covers_all_payload_producers() -> None:
    v1 = extraction_rules_version()
    assert v1 == extraction_rules_version()
    int(v1, 16)  # hex
    names = {p.name for p in rule_module_paths()}
    assert names == {
        "regexes.py", "ibm_pdf.py", "chrome.py", "chunk.py", "classify.py",
        "walk.py", "identity.py", "payload.py",
    }


# The version is the bytes of the rule modules, so ANY edit to one (even an
# exception rename) marks every stored corpus reembed_required. #776 did that
# silently (94d7c565 -> ef1c7f19, restored here). A real extraction change
# updates this pin deliberately under its own approved concern (AGENTS.md:
# no silent baseline changes); unrelated code belongs outside rule modules.
RULES_VERSION_PIN = "94d7c565b4cdceaa"


def test_rules_version_changes_only_deliberately() -> None:
    assert extraction_rules_version() == RULES_VERSION_PIN, (
        "a rule module changed: every stored corpus now needs a re-embed. Revert the "
        "edit or move non-extraction code out of the rule modules; update the pin only "
        "under an approved extraction change."
    )


def _package_imports(path) -> set[str]:
    """Every `mainframe_rag.*` module a source file imports, at any depth
    (function-local and TYPE_CHECKING imports included: conservative)."""
    import ast

    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("mainframe_rag"):
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("mainframe_rag"))
    return found


def test_rule_modules_import_only_rule_modules() -> None:
    """The hashed set is closed under package imports. A rule module that
    imports payload-shaping code from outside the set lets that code change
    stored payloads without changing the version, the silent desync #124
    exists to prevent (walk.py labels, identity.py keys and the payload
    mapping in qdrant_io.py were outside it)."""
    import importlib.util

    rules = {p.resolve() for p in rule_module_paths()}
    escapes = []
    for path in sorted(rules):
        for module in sorted(_package_imports(path)):
            spec = importlib.util.find_spec(module)
            origin = spec.origin if spec is not None else None
            if origin is None or Path(origin).resolve() not in rules:
                escapes.append(f"{path.name} imports {module}")
    assert escapes == [], escapes


def test_stored_payload_is_owned_by_a_rule_module() -> None:
    """upsert_chunks stores exactly point_payload's mapping (carrying
    rules_v), and that mapping, like the contextual-prefix normalization
    recipe (review of #701), lives in a hashed module: adding or deriving a
    stored field anywhere else would bypass the version."""
    import inspect

    from mainframe_rag.ingest import context
    from mainframe_rag.ingest.payload import normalize_context, point_payload

    hashed = {p.resolve() for p in rule_module_paths()}
    assert Path(inspect.getfile(point_payload)).resolve() in hashed
    assert Path(inspect.getfile(normalize_context)).resolve() in hashed
    assert context.normalize_context is normalize_context
    client, parsed, chunk = _upsert_one({"c1": "situating gist"})
    assert client.points[0].payload == point_payload(
        parsed, chunk, extraction_rules_version(), "situating gist"
    )
    assert client.points[0].payload["context"] == "situating gist"
    assert client.points[0].payload["rules_v"] == extraction_rules_version()


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
    # Issue #179: --reingest never skips; the same record skips without the flag.
    assert should_skip(rec, "a" * 64, rules_version="v1", force_reingest=True) is False
    assert should_skip(rec, "a" * 64, rules_version="v1", force_reingest=False) is True


# ---------------------------------------------------------------- payload
def _upsert_one(contexts=None):
    from types import SimpleNamespace

    from mainframe_rag.ingest.ibm_pdf import ParsedDoc

    class FakeClient:
        def __init__(self) -> None:
            self.points: list = []

        def upsert(self, collection, *, points, wait=True):
            self.points.extend(points)
            return SimpleNamespace()

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
    upsert_chunks(client, settings, parsed, [chunk], [([0.0] * 8, ([0], [1.0]))], contexts)
    return client, parsed, chunk


def _settings():
    from mainframe_rag.config import Settings

    return Settings(_env_file=None)


# ---------------------------------------------------------------- ingest gate
def _walked_source_rev(path) -> str:
    from mainframe_rag.ingest.ibm_pdf import parse_pdf
    from mainframe_rag.ingest.identity import source_rev_key

    parsed = parse_pdf(path)
    return source_rev_key(parsed.vendor, parsed.product, parsed.version, parsed.sha256)


def _seed_stale_doc(fake, collection, doc_id, sha, rules_v, source_rev, n=3):
    """Materialize stale-rules points of a current-format revision: the
    fake's seeded sampling marker models the startup-gate sample only —
    doc-level reads observe stored points, so stale-payload tests store
    real ones. Points without a source revision are unsupported data and
    refuse instead (test_ingest_revisions.py)."""
    from qdrant_client import models

    fake._points.setdefault(collection, []).extend([
        models.PointStruct(
            id=f"stale-{i}",
            vector={"dense": [0.1] * 256,
                    "bm25": models.SparseVector(indices=[1], values=[1.0])},
            payload={"doc_id": doc_id, "sha256": sha, "source_rev": source_rev,
                     "rules_v": rules_v, "text": f"stale payload {i}"},
        )
        for i in range(n)
    ])


def _main_collection():
    from mainframe_rag.config import Settings as _Settings

    return _Settings(_env_file=None).qdrant_collection


def _seed_current_manifest(fake) -> None:
    """Commit the current run contract into the fake's completions: the
    representation preflight passes, so tests below the gate (rules,
    skips) exercise their claimed path instead of the unversioned-target
    refusal."""
    from mainframe_rag.config import Settings as _Settings
    from mainframe_rag.ingest.completion import completion_collection_name
    from mainframe_rag.ingest.representation import write_manifest

    settings = _Settings(_env_file=None, embed_mode="hash")
    write_manifest(
        fake, completion_collection_name(settings), settings,
        extraction_rules_version(),
    )
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
    _seed_current_manifest(fake)
    _seed_stale_doc(fake, _main_collection(), "SA22-0000-00",
                    sha256_file(synthetic_pdf), "0123456789abcdef",
                    _walked_source_rev(synthetic_pdf))
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


# ---------------------------------------------------------------- qdrant skip
def test_qdrant_skip_gates_on_rules_version(tmp_path, synthetic_pdf, monkeypatch, capsys) -> None:
    """The third skip layer (live-found on the real_manuals re-stamp): a
    sha-equal doc whose stored rules_v differs must be deleted and
    re-upserted by a plain rerun — same file bytes, stale payloads."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.ibm_pdf import sha256_file
    from tests.test_run_ingest import _FakeQdrant

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    sha = sha256_file(synthetic_pdf)

    # Mixed collection (startup sample reads current, the doc itself is
    # rules-stale — exactly the state a partially-interrupted stamp run
    # leaves): NOT skipped — delete + re-upsert. The manifest matches the
    # current contract (crashed-migration state, not pre-manifest data),
    # so the representation preflight passes and the rules gate fires.
    fake = _FakeQdrant(stored_sha=sha, stored_rules_v="",
                       sample_rules_v=extraction_rules_version())
    _seed_current_manifest(fake)
    _seed_stale_doc(fake, _main_collection(), "SA22-0000-00", sha, "",
                    _walked_source_rev(synthetic_pdf))
    # A current-rules point from another doc: the startup sample (limit 1,
    # no filter) must observe the mixed collection as current-gated.
    _seed_stale_doc(fake, _main_collection(), "OTHER-DOC", "f" * 64,
                    extraction_rules_version(), "|||" + "f" * 64)
    # Order matters: the gate samples the first stored point.
    fake._points[_main_collection()].reverse()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    assert run_ingest.main([
        "--src", str(synthetic_pdf.parent),
        "--progress", str(tmp_path / "inv.jsonl"), "--workers", "1",
    ]) == 0
    assert fake.deletes >= 1 and fake.upserts, "stale sha-equal doc must re-upsert"

    # Matching generation with a WRITTEN completion + verified points:
    # publish once, then rerun with a fresh inventory — the second run
    # parses again but skips at the verified-completion gate (no new
    # main-collection upserts). A bare sha/rules marker without a
    # completion must never skip (issue #359 — pinned separately in
    # test_ingest_completion.py).
    fake2 = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake2)
    progress = tmp_path / "inv2.jsonl"
    assert run_ingest.main([
        "--src", str(synthetic_pdf.parent),
        "--progress", str(progress), "--workers", "1",
    ]) == 0
    from mainframe_rag.config import Settings as _Settings

    main_collection = _Settings(_env_file=None).qdrant_collection
    first_main = [n for collection, n in fake2.upsert_calls if collection == main_collection]
    assert first_main, "first run must publish main-collection points"
    progress.unlink()  # fresh inventory, warm Qdrant
    assert run_ingest.main([
        "--src", str(synthetic_pdf.parent),
        "--progress", str(progress), "--workers", "1",
    ]) == 0
    second_main = [n for collection, n in fake2.upsert_calls if collection == main_collection]
    assert second_main == first_main, "verified completion must skip without new main upserts"
