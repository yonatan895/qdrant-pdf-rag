"""Issue #362 step 1: representation manifest, record-only.

The manifest defines the stored-representation contract and commits it per
run; enforcement stays off (362B). Pins are literal (never
digest == digest self-comparisons across the same call path — the one
worker-vs-parent agreement test compares two independent constructions,
which is the invariant 362B relies on).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from mainframe_rag.config import Settings
from mainframe_rag.ingest.representation import (
    build_manifest,
    manifest_digest,
    manifest_point_id,
    read_manifest,
    write_manifest,
)
from mainframe_rag.ingest.rules_version import extraction_rules_version

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES = "testrules12345678"


def _settings(**overrides):
    base = {
        "_env_file": None,
        "embed_mode": "hash",
        "embed_model": None,
        "embed_model_revision": "",
        "dense_dim": None,
        "contextual_embed_enabled": False,
        "context_llm_model": None,
        "context_max_chars": 500,
        "bm25_model": "Qdrant/bm25",
        "bm25_weights_revision": "22b8d2af71a76161e18dd432d2cee0eefa66e412",
        "dense_query_prefix": "Q:",
    }
    base.update(overrides)
    return Settings(**base)


def test_build_manifest_literal():
    m = build_manifest(_settings(), RULES)
    assert m.model_dump(mode="json") == {
        "schema_version": 1,
        "extraction_rules": "testrules12345678",
        "identity_schema": "source_rev",
        "embed_mode": "hash",
        "embed_model": None,
        "embed_model_revision": "",
        "dense_dim": 256,
        "contextual_enabled": False,
        "context_llm_model": None,
        "context_prompt_version": "v2",
        "context_max_chars": 500,
        "sparse_model": "Qdrant/bm25",
        "sparse_weights_revision": "22b8d2af71a76161e18dd432d2cee0eefa66e412",
        "dense_query_prefix": "Q:",
    }


def test_manifest_digest_literal_and_stable():
    assert manifest_digest(_settings(), RULES) == "efb582c923ae147d"
    assert manifest_digest(_settings(), RULES) == manifest_digest(_settings(), RULES)


def test_manifest_point_id_literal():
    assert manifest_point_id("mainframe_manuals__completions") == "f6276259-bb0c-5705-9d2a-82cf55a3c168"
    assert manifest_point_id("other__completions") != manifest_point_id("mainframe_manuals__completions")


def test_manifest_digest_sensitivity_matrix():
    """Every contracted field moves the digest — including the record-only
    query prefix (recorded for audit/evaluation, never a re-embed trigger;
    the re-embed-vs-record-only split is enforced in 362B)."""
    base = manifest_digest(_settings(), RULES)
    variants: list[tuple[dict, str]] = [
        ({}, "otherrules1234567"),
        ({"embed_mode": "vllm", "embed_model": "m", "dense_dim": 768}, RULES),
        ({"embed_model": "some-model"}, RULES),
        ({"embed_model_revision": "rev-9"}, RULES),
        ({"contextual_enabled": True, "context_llm_model": "gist"}, RULES),
        ({"context_max_chars": 600}, RULES),
        ({"bm25_model": "other/bm25"}, RULES),
        ({"bm25_weights_revision": "00" * 20}, RULES),
        ({"dense_query_prefix": "other prefix"}, RULES),
    ]
    for i, (kw, rules) in enumerate(variants):
        assert manifest_digest(_settings(**kw), rules) != base, f"variant {i} ({kw}) must move the digest"


def test_manifest_digest_dim_fallback_never_raises():
    """vLLM mode without DENSE_DIM still digests (dim None) — dry runs in
    unconfigured environments must not crash on provenance."""
    s = Settings(_env_file=None, embed_mode="vllm", dense_dim=None)
    assert build_manifest(s, RULES).dense_dim is None
    assert len(manifest_digest(s, RULES)) == 16


class _ManifestFake:
    """Minimal completions-collection double: upsert capture + get-by-id."""

    def __init__(self):
        self.points: dict[str, dict[str, object]] = {}

    def collection_exists(self, name):
        return True

    def get_collection(self, name):
        from mainframe_rag.config import HASH_EMBED_DIM

        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(vectors={"dense": SimpleNamespace(size=HASH_EMBED_DIM)})
            )
        )

    def create_collection(self, name, **kwargs):
        return True

    def create_payload_index(self, *a, **k):
        return SimpleNamespace()

    def scroll(self, name, *, scroll_filter=None, limit=1, with_payload=None, offset=None):
        from tests.test_run_ingest import _filter_doc_id

        doc_id = _filter_doc_id(scroll_filter)
        pts = [
            SimpleNamespace(id=pid, payload=p)
            for pid, p in self.points.get(name, {}).items()
            if doc_id is None or (isinstance(p, dict) and p.get("doc_id") == doc_id)
        ]
        return pts[:limit], None

    def retrieve(self, name, ids, *, with_payload=True):
        store = self.points.get(name, {})
        return [SimpleNamespace(id=i, payload=store[i]) for i in ids if i in store]

    def upsert(self, name, *, points, wait=True):
        for p in points:
            self.points.setdefault(name, {})[str(p.id)] = p.payload
        return SimpleNamespace()

    def delete(self, name, *, points_selector, wait=True):
        from tests.test_run_ingest import _filter_doc_id

        doc_id = _filter_doc_id(points_selector)
        if doc_id is not None and name in self.points:
            self.points[name] = {
                pid: p for pid, p in self.points[name].items()
                if not (isinstance(p, dict) and p.get("doc_id") == doc_id)
            }
        return SimpleNamespace()


def test_write_read_roundtrip():
    from mainframe_rag.ingest.completion import completion_collection_name

    settings = _settings()
    name = completion_collection_name(settings)
    fake = _ManifestFake()
    digest = write_manifest(fake, name, settings, RULES)
    assert digest == "efb582c923ae147d"
    stored = read_manifest(fake, name)
    assert stored is not None
    assert stored == build_manifest(settings, RULES)
    # Idempotent overwrite: second write replaces, no duplicate.
    assert write_manifest(fake, name, settings, RULES) == digest
    assert len(fake.points[name]) == 1


def test_read_absent_or_corrupt_is_none():
    fake = _ManifestFake()
    assert read_manifest(fake, "missing__completions") is None
    name = "coll__completions"
    # Wrong record type (e.g. a stray point id collision) reads as legacy.
    fake.points[name] = {manifest_point_id(name): {"record_type": "nope"}}
    assert read_manifest(fake, name) is None
    # Non-dict / unparsable manifest reads as legacy, never raises.
    fake.points[name] = {manifest_point_id(name): {"record_type": "representation-manifest"}}
    assert read_manifest(fake, name) is None
    fake.points[name] = {
        manifest_point_id(name): {
            "record_type": "representation-manifest",
            "manifest": {"schema_version": "bogus"},
        }
    }
    assert read_manifest(fake, name) is None


def test_completion_record_carries_manifest_digest():
    from mainframe_rag.ingest.completion import read_completion, write_completion

    settings = _settings()
    fake = _ManifestFake()
    record = write_completion(
        fake,
        settings,
        doc_id="D",
        sha256="ab" * 32,
        rules_v=RULES,
        source_labels="||",
        source_rev="rev-D",
        expected_chunks=1,
        chunk_ids_digest="i" * 64,
        content_digest="c" * 64,
    )
    assert record.manifest_digest == "efb582c923ae147d"
    assert record.source_rev == "rev-D"
    stored = read_completion(
        fake, settings, "D", source_rev="rev-D", generation_id=record.generation_id
    )
    assert stored is not None
    assert stored.manifest_digest == "efb582c923ae147d"


def test_weights_revision_matches_pin_file():
    """The bm25_weights_revision default mirrors the `revision` line in
    bm25-weights.sha256 — bump together in a dedicated PR, never drive-by."""
    revision = None
    for line in (REPO_ROOT / "bm25-weights.sha256").read_text().splitlines():
        if line.startswith("# revision "):
            revision = line.split()[-1]
    assert revision, "pin file must carry a `# revision <hex>` line"
    assert Settings(_env_file=None).bm25_weights_revision == revision


def test_model_revision_env_override_recorded(monkeypatch):
    monkeypatch.setenv("EMBED_MODEL_REVISION", "opaque-rev-7")
    s = Settings(_env_file=None)
    assert s.embed_model_revision == "opaque-rev-7"
    assert build_manifest(s, RULES).embed_model_revision == "opaque-rev-7"
    assert manifest_digest(s, RULES) != manifest_digest(_settings(), RULES)


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


def test_run_writes_manifest_and_logs(tmp_path, synthetic_pdf, capsys, monkeypatch):
    """The claimed path: a real run commits the manifest point into the
    completions collection and logs the contract line — enforcement off."""
    from mainframe_rag.ingest import run_ingest

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    fake = _ManifestFake()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    progress = tmp_path / "inventory.jsonl"
    assert run_ingest.main(["--src", str(synthetic_pdf.parent), "--progress", str(progress),
                            "--workers", "1"]) == 0
    settings = Settings(_env_file=None)
    name = f"{settings.qdrant_collection}__completions"
    stored = read_manifest(fake, name)
    assert stored is not None
    assert stored == build_manifest(settings, extraction_rules_version())
    lines = [a for a in _stderr_actions(capsys) if a.get("action") == "representation"]
    assert len(lines) == 1
    assert lines[0]["collection"] == settings.qdrant_collection
    assert lines[0]["manifest_digest"] == manifest_digest(settings, extraction_rules_version())
    assert lines[0]["result"] == "committed"
    assert lines[0]["model_revision_attested"] is False
    # Steady state: identical rerun re-reads the manifest and stays
    # zero-write (already_current, no new upserts anywhere).
    n_upserts = sum(len(v) for v in fake.points.values())
    progress.unlink()
    assert run_ingest.main(["--src", str(synthetic_pdf.parent), "--progress", str(progress),
                            "--workers", "1"]) == 0
    assert sum(len(v) for v in fake.points.values()) == n_upserts
    lines2 = [a for a in _stderr_actions(capsys) if a.get("action") == "representation"]
    assert [l["result"] for l in lines2] == ["already_current"]


def test_dry_run_record_stamps_parent_manifest_view(tmp_path, synthetic_pdf):
    """Worker-stamped inventory digest equals the parent's manifest view —
    the agreement 362B enforcement relies on (two independent
    constructions, not one value compared to itself)."""
    from mainframe_rag.ingest import run_ingest

    progress = tmp_path / "inventory.jsonl"
    assert run_ingest.main(["--src", str(synthetic_pdf.parent), "--progress", str(progress),
                            "--workers", "1", "--dry-run"]) == 0
    records = [json.loads(l) for l in progress.read_text().splitlines() if l.strip()]
    assert len(records) == 1
    assert records[0]["manifest_digest"] == manifest_digest(
        Settings(_env_file=None), extraction_rules_version()
    )


def test_default_revision_settings_pinned():
    assert Settings(_env_file=None).embed_model_revision == ""
    assert Settings(_env_file=None).bm25_weights_revision == "22b8d2af71a76161e18dd432d2cee0eefa66e412"
