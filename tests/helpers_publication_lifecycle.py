"""Independent bounded publication trace oracle; no product fingerprints as expectations."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

import fitz
import pytest

TEXTS = {
    "alpha": "The amber reactor uses the northern cooling channel during regular operation.",
    "beta": "The blue reactor uses the southern cooling channel during regular operation.",
}
REPLACEMENT = "The amber reactor now uses the western cooling channel during regular operation."


@dataclass
class PublicationModel:
    desired: dict[str, str] = field(default_factory=lambda: dict(TEXTS))
    published: dict[str, str] = field(default_factory=dict)

    def apply(self, operation):
        if operation == "change":
            self.desired["alpha"] = REPLACEMENT
        elif operation == "retire":
            del self.desired["beta"]
        else:
            assert operation == "repair"

    def commit(self):
        self.published = dict(self.desired)


def _write_source(corpus, name, text):
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_textbox(fitz.Rect(72, 100, 520, 250), text, fontsize=11)
        pdf.save(corpus / f"{name}.pdf")


def _records(client, collection):
    records = []
    offset = None
    while True:
        page, offset = client.scroll(collection, with_payload=True, limit=100, offset=offset)
        # retrieve uses real default projection semantics in both adapters.
        records.extend(client.retrieve(collection, [p.id for p in page], with_payload=True, with_vectors=True))
        if offset is None:
            break
    return {
        str(p.id): copy.deepcopy((p.payload, p.vector))
        for p in records
    }


def exercise_publication_trace(tmp_path, monkeypatch, client, operations, fault, alias):
    """Run public CLI operations; inject only the cutover interruption boundary."""
    from mainframe_rag.config import Settings
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.publish import publish_state_path
    from mainframe_rag.ingest.qdrant_io import swap_alias_to

    model = PublicationModel()
    corpus = tmp_path / "trace-corpus"
    corpus.mkdir()
    progress = tmp_path / "trace.jsonl"
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("INGEST_ALIAS_PUBLISH", "true")
    monkeypatch.setenv("QDRANT_COLLECTION", alias)
    monkeypatch.setenv("DENSE_DIM", "256")
    monkeypatch.setenv("EMBED_MODEL_REVISION", "")
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: client)
    # All operations go through the actual ingest CLI, planner, storage and verifier.
    def run(extra=()):
        return run_ingest.main([
            "--src", str(corpus), "--progress", str(progress), "--workers", "1", *extra,
        ])

    def ordinary():
        def refuse_write(*args, **kwargs):
            pytest.fail("steady ordinary run attempted a storage mutation")

        with monkeypatch.context() as patch:
            for method in ("create_collection", "recover_snapshot", "upsert", "delete", "update_collection_aliases"):
                patch.setattr(client, method, refuse_write)
            assert run() == 0
        assert not publish_state_path(progress, alias).exists()

    def target():
        return next(a.collection_name for a in client.get_aliases().aliases if a.alias_name == alias)

    retained = {}

    def observe(expected):
        physical = target()
        actual = _records(client, physical)
        by_doc = {}
        for payload, _ in actual.values():
            by_doc.setdefault(payload["doc_id"], []).append(payload["text"])
        assert by_doc == {name: [text] for name, text in expected.items()}
        controls = _records(client, physical + "__completions")
        markers = [payload for payload, _ in controls.values() if "expected_chunks" in payload]
        assert {p["doc_id"] for p in markers} == set(expected)
        assert all(p["expected_chunks"] == 1 and p["target_collection"] == physical for p in markers)
        for old, snapshots in retained.items():
            assert (_records(client, old), _records(client, old + "__completions")) == snapshots
        retained.setdefault(physical, (actual, controls))
        return physical

    for name, text in model.desired.items():
        _write_source(corpus, name, text)
    assert run() == 0
    model.commit()
    first = observe(model.published)

    for index, operation in enumerate(operations):
        old = target()
        model.apply(operation)
        if operation == "change":
            (corpus / "alpha.pdf").unlink()
            _write_source(corpus, "alpha", model.desired["alpha"])
        elif operation == "retire":
            (corpus / "beta.pdf").unlink()
        extra = ("--reingest",) if operation == "repair" else (
            ("--retire-doc", "beta") if operation == "retire" else ()
        )
        interrupted = None
        if index == 1:
            original = run_ingest.swap_alias_to

            def interrupt(*args, _swap=original, **kwargs):
                if fault == "after":
                    _swap(*args, **kwargs)
                raise RuntimeError("trace cutover interruption")

            with monkeypatch.context() as patch:
                patch.setattr(run_ingest, "swap_alias_to", interrupt)
                with pytest.raises(RuntimeError, match="trace cutover interruption"):
                    run(extra)
            if fault == "after":
                model.commit()
                interrupted = observe(model.published)
                assert interrupted != old
            else:
                assert observe(model.published) == old
        assert run(extra) == 0
        model.commit()
        current = observe(model.published)
        assert current != old
        if interrupted:
            assert current == interrupted
        # A completed operation must leave the next ordinary invocation usable.
        for _ in range(2):
            ordinary()
            assert observe(model.published) == current

    # A previously bound reader still reads the original exact physical pair.
    assert first in retained
    assert len(retained) == 4
    # Explicit operator rollback/roll-forward uses retained data, never rebuilds.
    last = target()
    settings = Settings(_env_file=None, qdrant_collection=alias)
    swap_alias_to(client, settings, first, last)
    assert observe(TEXTS) == first
    swap_alias_to(client, settings, last, first)
    assert observe(model.published) == last
    ordinary()
    assert observe(model.published) == last
