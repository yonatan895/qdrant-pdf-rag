"""Dry-run ingest end-to-end on the synthetic fixture: parse, chunk, inventory.

No Qdrant, no embeddings (air-gap Job does that; CI uses --dry-run).
"""

import json
from pathlib import Path
from typing import ClassVar

import pytest

from mainframe_rag.ingest.ibm_pdf import sha256_file
from mainframe_rag.ingest.run_ingest import main
from tests.helpers_builder import staging_builder  # noqa: F401 — fixture

# Real runs publish (in-place ingest retired, #360); these tests cover the
# builder against a fixed collection through tests/helpers_builder.py.
pytestmark = pytest.mark.usefixtures("staging_builder")


def test_dry_run_ingest(tmp_path, synthetic_pdf, capsys):
    progress = tmp_path / "inventory.jsonl"
    args = ["--src", str(synthetic_pdf.parent), "--progress", str(progress),
            "--workers", "1", "--dry-run"]
    assert main(args) == 0
    records = [json.loads(l) for l in progress.read_text().splitlines() if l.strip()]
    assert len(records) == 1
    rec = records[0]
    assert rec["doc_id"] == "SA22-0000-00"
    assert rec["pages"] == 8
    assert rec["chunks"] > 0
    assert rec["status"] == "dry"
    # Second run: same sha256 -> skipped (still an ok outcome), no new records.
    # (One capsys read at the end: the StreamHandler binds stderr at creation.)
    assert main(args) == 0
    assert len(progress.read_text().splitlines()) == 1
    done = [l for l in _stderr_json(capsys) if l.get("action") == "done"]
    assert len(done) == 2
    for summary in done:
        assert summary["files_ok"] == 1
        assert summary["files_failed"] == 0
        assert summary["chunks_upserted"] == 0  # dry run: parsed, not upserted
        assert summary["elapsed_ms"] >= 0


def test_worker_qdrant_client_is_built_once_from_ingest_settings(monkeypatch):
    """Each spawned ingest worker opens one Qdrant client with the writer URL,
    key and ingest timeout, then reuses it for every document it processes."""
    import qdrant_client

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest import run_ingest

    built = []
    monkeypatch.setattr(qdrant_client, "QdrantClient", lambda **kw: built.append(kw) or object())
    monkeypatch.setattr(run_ingest, "_worker_qdrant", None)
    settings = Settings(_env_file=None, qdrant_url="http://writer:6333", qdrant_api_key="writer-key",
                        qdrant_ingest_timeout_s=42)
    first = run_ingest._get_qdrant(settings)
    assert run_ingest._get_qdrant(settings) is first
    assert built == [{"url": "http://writer:6333", "api_key": "writer-key", "timeout": 42}]


def test_resolve_workers_is_a_cap(tmp_path):
    from mainframe_rag.config import Settings
    from mainframe_rag.ingest.run_ingest import resolve_workers

    settings = Settings(_env_file=None)
    assert resolve_workers(None, settings) >= 1
    assert resolve_workers(0, settings) == 1
    assert resolve_workers(-5, settings) == 1
    assert resolve_workers(10_000, settings) <= 2 * (__import__("os").cpu_count() or 2)


def test_corrupt_pdf_writes_typed_error_record(tmp_path, synthetic_pdf, capsys):
    """One bad PDF must not kill the run; the error record carries the
    exception type for triage and the summary counts the failure."""
    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"this is not a pdf at all")
    progress = tmp_path / "inventory.jsonl"
    rc = main(["--src", str(tmp_path), "--progress", str(progress),
               "--workers", "1", "--dry-run"])
    assert rc == 1
    records = [json.loads(l) for l in progress.read_text().splitlines() if l.strip()]
    err = [r for r in records if r["status"] == "error"]
    assert err and err[0]["path"] == str(bad)
    assert err[0]["error_type"]
    done = [l for l in _stderr_json(capsys) if l.get("action") == "done"]
    assert len(done) == 1
    assert done[0]["files_failed"] == 1


def _crash_corpus(tmp_path, monkeypatch, crash_stems):
    """Four synthetic manuals parsed by real spawn workers whose parse
    exits the process for `crash_stems` (#841)."""
    from scripts.make_synthetic_pdf import build

    from mainframe_rag.ingest import run_ingest
    from tests.helpers_ingest_crash_worker import CRASH_PARSE_STEMS_ENV, crashing_parse_one

    corpus = tmp_path / "corpus"
    stems = [f"SA22-70{n}0-00" for n in range(4)]
    for n, stem in enumerate(stems):
        build(corpus / f"{stem}.pdf", doc_id=stem, title=f"Synthetic Reference {n}",
              message_id=f"IEA{n}00I")
    monkeypatch.setattr(run_ingest, "_parse_one", crashing_parse_one)
    monkeypatch.setenv(CRASH_PARSE_STEMS_ENV, ",".join(crash_stems))
    return corpus, {stem: str(corpus / f"{stem}.pdf") for stem in stems}


def test_crashed_parse_worker_is_quarantined_and_the_run_continues(tmp_path, monkeypatch, capsys):
    """#841: a worker that dies breaks the whole pool and fails every
    in-flight parse. The run rebuilds the pool, re-parses each pending file
    exactly once, records only the file that kills a worker on its own, and
    still parses the files queued after it."""
    corpus, paths = _crash_corpus(tmp_path, monkeypatch, ["SA22-7000-00"])
    progress = tmp_path / "inventory.jsonl"
    rc = main(["--src", str(corpus), "--progress", str(progress), "--workers", "1", "--dry-run"])
    assert rc == 1
    records = [json.loads(l) for l in progress.read_text().splitlines() if l.strip()]
    by_path = {r["path"]: r for r in records}
    assert len(records) == len(by_path) == 4, records  # each file recorded exactly once
    crashed = by_path[paths["SA22-7000-00"]]
    assert (crashed["status"], crashed["error_type"]) == ("error", "BrokenProcessPool")
    assert [by_path[paths[s]]["status"] for s in ("SA22-7010-00", "SA22-7020-00", "SA22-7030-00")] == [
        "dry", "dry", "dry"]
    done = [l for l in _stderr_json(capsys) if l.get("action") == "done"]
    assert len(done) == 1 and done[0]["files_failed"] == 1


def test_crashed_parse_worker_blocks_publication_and_the_next_run_publishes(
    tmp_path, monkeypatch
):
    """#841: a real run keeps the existing failed-document gate (the crashing
    file writes no points or completion, and the migration stays pending),
    and once the cause is gone the forced re-run ingests every file."""
    from mainframe_rag.ingest import run_ingest
    from tests.helpers_ingest_crash_worker import CRASH_PARSE_STEMS_ENV

    corpus, paths = _crash_corpus(tmp_path, monkeypatch, ["SA22-7010-00"])
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    progress = tmp_path / "inventory.jsonl"
    assert main(_migration_args(corpus, progress)) == 1
    records = [json.loads(l) for l in progress.read_text().splitlines() if l.strip()]
    assert {r["path"]: r["status"] for r in records} == {
        paths["SA22-7000-00"]: "upserted",
        paths["SA22-7010-00"]: "error",
        paths["SA22-7020-00"]: "upserted",
        paths["SA22-7030-00"]: "upserted",
    }
    stored = {(p.payload or {}).get("doc_id") for pts in fake._points.values() for p in pts}
    assert "SA22-7010-00" not in stored
    assert {"SA22-7000-00", "SA22-7020-00", "SA22-7030-00"} <= stored

    monkeypatch.delenv(CRASH_PARSE_STEMS_ENV)
    assert main(_migration_args(corpus, progress, "--reingest")) == 0
    stored = {(p.payload or {}).get("doc_id") for pts in fake._points.values() for p in pts}
    assert "SA22-7010-00" in stored


@pytest.mark.parametrize(
    ("workers", "recorded"),
    [
        (1, 2),  # window 2 -> the floor of 3 crashes in a row
        (2, 3),  # window 4 -> 4 crashes in a row
    ],
)
def test_parse_workers_that_keep_dying_abort_the_run_unpublished(
    tmp_path, monkeypatch, workers, recorded
):
    """#841: when fresh single-worker pools keep dying, the failure is not
    bad files: the run stops after max(3, window) crashes in a row instead
    of spawning a pool per document, and nothing is recorded as parsed. The
    crash that reaches the bound raises before it is recorded."""
    corpus, _ = _crash_corpus(
        tmp_path, monkeypatch, ["SA22-7000-00", "SA22-7010-00", "SA22-7020-00", "SA22-7030-00"])
    progress = tmp_path / "inventory.jsonl"
    with pytest.raises(RuntimeError, match="parse workers keep dying"):
        main(["--src", str(corpus), "--progress", str(progress), "--workers", str(workers),
              "--dry-run"])
    records = [json.loads(l) for l in progress.read_text().splitlines() if l.strip()]
    assert len(records) == recorded, records
    assert all(r["status"] == "error" for r in records)


def test_crashes_separated_by_returned_parses_never_reach_the_bound(tmp_path, monkeypatch):
    """#841: the bound counts crashes in a row. Three bad files with good
    files parsed between them are three error records, not an abort."""
    from scripts.make_synthetic_pdf import build

    from mainframe_rag.ingest import run_ingest
    from tests.helpers_ingest_crash_worker import CRASH_PARSE_STEMS_ENV, crashing_parse_one

    corpus = tmp_path / "corpus"
    stems = [f"SA22-70{n}0-00" for n in range(7)]
    for n, stem in enumerate(stems):
        build(corpus / f"{stem}.pdf", doc_id=stem, title=f"Synthetic Reference {n}",
              message_id=f"IEA{n}00I")
    bad = stems[1::2]  # three crashers, each with a good neighbour in its window
    assert len(bad) == run_ingest._MIN_PARSE_CRASH_LIMIT  # the limit at --workers 1
    monkeypatch.setattr(run_ingest, "_parse_one", crashing_parse_one)
    monkeypatch.setenv(CRASH_PARSE_STEMS_ENV, ",".join(bad))
    progress = tmp_path / "inventory.jsonl"
    assert main(["--src", str(corpus), "--progress", str(progress), "--workers", "1",
                 "--dry-run"]) == 1
    records = [json.loads(l) for l in progress.read_text().splitlines() if l.strip()]
    assert sorted((r["path"].rsplit("/", 1)[1][:-4], r["status"]) for r in records) == sorted(
        (s, "error" if s in bad else "dry") for s in stems)


class _CrashSemanticsPool:
    """In-process ProcessPoolExecutor stand-in with its crash semantics
    (#841): one worker thread runs submissions in order; a listed stem
    "kills the worker", failing every pending future with BrokenProcessPool,
    and the pool then refuses submits. A resubmitted task (a suspect) waits
    before running, so other work can finish while it runs alone.
    `submissions` records, per submit: (stem, is_resubmission, in-flight
    [(stem, is_resubmission)]) for that pool."""

    crash_stems: ClassVar[frozenset[str]] = frozenset()
    submissions: ClassVar[list[tuple[str, bool, list[tuple[str, bool]]]]] = []
    seen: ClassVar[set[str]] = set()

    def __init__(self, max_workers=None, mp_context=None):
        import queue
        import threading

        self.lock = threading.Lock()
        self.queue: queue.Queue = queue.Queue()
        self.in_flight: list = []
        self.broken = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, fn, task):
        from concurrent.futures import Future
        from concurrent.futures.process import BrokenProcessPool
        from pathlib import Path

        stem = Path(task.path).stem
        with self.lock:
            if self.broken:
                raise BrokenProcessPool("pool is broken")
            resubmission = stem in self.seen
            self.seen.add(stem)
            self.in_flight = [entry for entry in self.in_flight if not entry[0].done()]
            self.submissions.append((stem, resubmission, [(s, r) for _, s, r in self.in_flight]))
            future: Future = Future()
            self.in_flight.append((future, stem, resubmission))
        self.queue.put((future, fn, task, stem, resubmission))
        return future

    def _run(self):
        import time
        from concurrent.futures.process import BrokenProcessPool

        while (item := self.queue.get()) is not None:
            future, fn, task, stem, resubmission = item
            if future.done():
                continue
            if resubmission:
                time.sleep(0.5)
            if stem in self.crash_stems:
                with self.lock:
                    self.broken = True
                    for pending, _, _ in self.in_flight:
                        if not pending.done():
                            pending.set_exception(BrokenProcessPool("worker died"))
                return
            try:
                future.set_result(fn(task))
            except Exception as exc:  # noqa: BLE001 — surfaced through the future
                future.set_exception(exc)

    def shutdown(self, wait=True):
        self.queue.put(None)
        if wait:
            self.thread.join()


def test_no_file_starts_while_a_suspect_parses_alone(tmp_path, monkeypatch):
    """#841: attribution rests on a suspect being the only parse in its pool.
    An upsert that finishes while the suspect runs must not start new files
    beside it: if one of those crashed, the innocent suspect would be
    recorded as the culprit."""
    import time

    from mainframe_rag.ingest import run_ingest

    real_parse = run_ingest._parse_one
    corpus, paths = _crash_corpus(tmp_path, monkeypatch, [])
    monkeypatch.setattr(run_ingest, "_parse_one", real_parse)  # in-process; the pool crashes
    monkeypatch.setattr(_CrashSemanticsPool, "crash_stems", frozenset({"SA22-7010-00"}))
    monkeypatch.setattr(_CrashSemanticsPool, "submissions", [])
    monkeypatch.setattr(_CrashSemanticsPool, "seen", set())
    monkeypatch.setattr(run_ingest, "ProcessPoolExecutor", _CrashSemanticsPool)
    real_upsert = run_ingest._upsert_one

    def slow_upsert(*args, **kwargs):
        time.sleep(0.1)  # still in flight when the suspect starts, done before it ends
        return real_upsert(*args, **kwargs)

    monkeypatch.setattr(run_ingest, "_upsert_one", slow_upsert)
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    progress = tmp_path / "inventory.jsonl"
    assert main(_migration_args(corpus, progress)) == 1

    submissions = _CrashSemanticsPool.submissions
    assert ("SA22-7010-00", True) in {(stem, resub) for stem, resub, _ in submissions}
    beside_a_suspect = [
        (stem, in_flight) for stem, resub, in_flight in submissions
        if in_flight and (resub or any(r for _, r in in_flight))
    ]
    assert beside_a_suspect == [], beside_a_suspect
    records = [json.loads(l) for l in progress.read_text().splitlines() if l.strip()]
    assert {r["path"]: r["status"] for r in records} == {
        paths["SA22-7000-00"]: "upserted",
        paths["SA22-7010-00"]: "error",
        paths["SA22-7020-00"]: "upserted",
        paths["SA22-7030-00"]: "upserted",
    }


def _stderr_json(capsys) -> list[dict]:
    out = capsys.readouterr().err
    lines = []
    for line in out.splitlines():
        if line.strip():
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                lines.append(parsed)
    return lines


def _filter_match_value(filter_obj, key: str) -> str | None:
    """Extract one MatchValue from a Filter/FilterSelector shape (one rule
    for every key — doc_id and source_rev selectors share it)."""
    filt = getattr(filter_obj, "filter", filter_obj)
    must = getattr(filt, "must", None) or []
    for cond in must:
        if getattr(cond, "key", None) == key:
            match = getattr(cond, "match", None)
            value = getattr(match, "value", None)
            if value is not None:
                return str(value)
    return None


def _filter_doc_id(filter_obj) -> str | None:
    """Extract a doc_id equality value from a Filter/FilterSelector shape."""
    return _filter_match_value(filter_obj, "doc_id")


class _FakeQdrant:
    """QdrantPoints double for the parent-side run() path (review round on
    PR D: the non-dry upsert branch had no coverage).

    Stores points per collection so the verified-completion contract
    (issue #359) exercises the claimed path: skips require a written
    completion plus verifiable points, never a single sampled marker."""

    def __init__(self, stored_sha: str | None = None, stored_rules_v: str | None = None,
                 sample_rules_v: str | None = None):
        from mainframe_rag.ingest.rules_version import extraction_rules_version

        self.stored_sha = stored_sha
        # A stored collection defaults to the CURRENT rules so existing
        # skip tests model a matching collection; tests pass an explicit
        # value (including "" for a legacy pre-versioning collection) to
        # exercise the extraction-rules gate (issue #124). sample_rules_v
        # models a MIXED collection (startup sample reads current, but an
        # individual doc carries an older/unstamped rules_v) — the state
        # the live re-stamp run exposed.
        self.stored_rules_v = (
            extraction_rules_version() if stored_rules_v is None else stored_rules_v
        )
        self.sample_rules_v = (
            self.stored_rules_v if sample_rules_v is None else sample_rules_v
        )
        self.upserts: list[int] = []
        self.upsert_calls: list[tuple[str, int]] = []
        self.deletes = 0
        self._points: dict[str, list] = {}
        self.created_collections: list[str] = []

    def get_aliases(self):
        from types import SimpleNamespace

        return SimpleNamespace(aliases=[])

    def collection_exists(self, collection_name):
        return True

    def get_collection(self, collection_name):
        from types import SimpleNamespace

        from mainframe_rag.config import HASH_EMBED_DIM

        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(vectors={"dense": SimpleNamespace(size=HASH_EMBED_DIM)})
            )
        )

    def create_collection(self, collection_name, **kwargs):
        self.created_collections.append(collection_name)
        return True

    def create_payload_index(self, *a, **k):
        from types import SimpleNamespace

        return SimpleNamespace()

    def update_collection(self, collection_name, *, optimizer_config=None):
        return True

    def scroll(self, collection_name, *, scroll_filter=None, limit=1, with_payload=None, offset=None):
        from types import SimpleNamespace

        if with_payload == ["rules_v"] and _filter_doc_id(scroll_filter) is None:
            # Startup rules gate sample (issue #124): empty collection vs
            # versioned points. Doc-level reads below serve stored points.
            if self.stored_sha is None and not self._points.get(collection_name):
                return [], None
            if self._points.get(collection_name):
                return [self._points[collection_name][0]], None
            return [SimpleNamespace(payload={"rules_v": self.sample_rules_v})], None
        # Doc-level reads serve stored points only — the seeded sampling
        # marker above never leaks into a doc_id-filtered read (issue #361:
        # revision listing and marker reads must observe, never assume).
        doc_id = _filter_doc_id(scroll_filter)
        rev = _filter_match_value(scroll_filter, "source_rev")
        stored = self._points.get(collection_name, [])
        if doc_id is not None:
            stored = [p for p in stored if (p.payload or {}).get("doc_id") == doc_id]
        if rev is not None:
            stored = [p for p in stored if (p.payload or {}).get("source_rev") == rev]
        return stored[:limit], None

    def upsert(self, collection_name, *, points, wait=True):
        self.upserts.append(len(points))
        self.upsert_calls.append((collection_name, len(points)))
        # Production upsert overwrites same-id points (manifest recommit,
        # marker rewrite); the double must too, or reads see stale firsts.
        stored = self._points.setdefault(collection_name, [])
        ids = {str(p.id) for p in points}
        stored[:] = [p for p in stored if str(p.id) not in ids]
        stored.extend(points)
        from types import SimpleNamespace

        return SimpleNamespace()

    def retrieve(self, collection_name, ids, *, with_payload=True, with_vectors=False):
        from types import SimpleNamespace

        wanted = {str(i) for i in ids}
        return [
            SimpleNamespace(id=p.id, payload=p.payload)
            for p in self._points.get(collection_name, [])
            if str(p.id) in wanted
        ]

    def delete(self, collection_name, *, points_selector, wait=True):
        from types import SimpleNamespace

        self.deletes += 1
        ids = getattr(points_selector, "points", None)
        if ids is not None:
            # PointIdsList (precise completion invalidation, issue #361).
            # Seeded sampling markers carry no id and never match.
            wanted = {str(i) for i in ids}
            self._points[collection_name] = [
                p for p in self._points.get(collection_name, [])
                if str(getattr(p, "id", None)) not in wanted
            ]
            return SimpleNamespace()
        doc_id = _filter_doc_id(points_selector)
        rev = _filter_match_value(points_selector, "source_rev")
        if doc_id is None and rev is None:
            self._points[collection_name] = []
        else:
            self._points[collection_name] = [
                p for p in self._points.get(collection_name, [])
                if not (
                    (doc_id is None or (p.payload or {}).get("doc_id") == doc_id)
                    and (rev is None or (p.payload or {}).get("source_rev") == rev)
                )
            ]
        return SimpleNamespace()


def test_upsert_path_counters_with_fake_qdrant(tmp_path, synthetic_pdf, capsys, monkeypatch):
    """Non-dry run with a fake port: files_ok counts the upsert and
    chunks_upserted matches the points actually sent. A second run with a
    fresh inventory and warm Qdrant parses again, verifies the completion and
    skips: files_ok counts it, nothing is upserted or deleted. (A bare
    sampled point without a completion must never skip, issue #359, pinned in
    test_ingest_completion.py.)"""
    from mainframe_rag.ingest import run_ingest

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    progress = tmp_path / "inventory.jsonl"
    rc = main(["--src", str(synthetic_pdf.parent), "--progress", str(progress), "--workers", "1"])
    assert rc == 0
    from mainframe_rag.config import Settings as _Settings

    main_collection = _Settings(_env_file=None).qdrant_collection
    main_sent = sum(n for collection, n in fake.upsert_calls if collection == main_collection)
    assert main_sent > 0
    fake.upserts.clear()
    deletes_after_first = fake.deletes
    progress.unlink()
    assert main(["--src", str(synthetic_pdf.parent), "--progress", str(progress), "--workers", "1"]) == 0
    done = [l for l in _stderr_json(capsys) if l.get("action") == "done"]
    assert done[0]["files_ok"] == 1
    assert done[0]["chunks_upserted"] == main_sent
    assert done[-1]["files_ok"] == 1
    assert done[-1]["chunks_upserted"] == 0
    assert fake.upserts == [] and fake.deletes == deletes_after_first


def test_builder_refuses_a_real_run_without_a_staging_build(
    tmp_path, synthetic_pdf, capsys, monkeypatch
):
    """In-place ingest was retired (#360): the builder writes only a
    publication staging generation. Called directly for a real run without
    one, it refuses before any lock or storage write, so no caller can write
    into the serving collection; a dry run (which writes nothing) still
    plans without one."""
    import pytest
    from opentelemetry import trace

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest import run_ingest

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    settings = Settings(_env_file=None)
    tracer = trace.NoOpTracerProvider().get_tracer("test")
    root = trace.INVALID_SPAN
    progress = tmp_path / "real.jsonl"
    with pytest.raises(RuntimeError, match="writes only a publication staging generation"):
        run_ingest._run_impl(
            synthetic_pdf.parent, progress, 1, None, False, settings, tracer, root
        )
    assert fake._points == {} and not progress.exists()
    assert not (tmp_path / "real.jsonl.lock").exists()
    dry = tmp_path / "dry.jsonl"
    assert run_ingest._run_impl(
        synthetic_pdf.parent, dry, 1, None, True, settings, tracer, root
    ) == 0
    assert fake._points == {}
    assert not [l for l in _stderr_json(capsys) if l.get("action") == "in_place_deprecated"]


def test_embed_failfast_upserts_nothing(tmp_path, synthetic_pdf, monkeypatch):
    """Embedding happens in the parse workers now, so an embed failure fails
    the whole task — the upsert stage never sees the doc. Exercised through
    the real runtime path: EMBED_MODE=vllm without EMBED_BASE_URL fails fast
    in the worker on first use (no monkeypatching of spawn workers)."""
    from mainframe_rag.ingest import run_ingest

    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    monkeypatch.delenv("EMBED_BASE_URL", raising=False)
    monkeypatch.delenv("EMBED_MODEL", raising=False)
    monkeypatch.setenv("EMBED_MODE", "vllm")
    monkeypatch.setenv("EMBED_MODEL_REVISION", "test-rev")
    monkeypatch.setenv("DENSE_DIM", "256")  # collection setup passes; the
    # worker's embed call then fails fast on the missing EMBED_BASE_URL
    progress = tmp_path / "inventory.jsonl"
    rc = main(["--src", str(synthetic_pdf.parent), "--progress", str(progress), "--workers", "1"])
    assert rc == 1
    # The run-level representation manifest still commits (contract, not
    # doc data); what must never happen is a doc-point upsert.
    from mainframe_rag.config import Settings as _Settings

    main_collection = _Settings(_env_file=None).qdrant_collection
    assert all(c != main_collection for c, _ in fake.upsert_calls), \
        "a doc whose embed failed must never be upserted"
    records = [json.loads(l) for l in progress.read_text().splitlines() if l.strip()]
    assert records and all(r["status"] == "error" for r in records)
    assert all(r["error_type"] == "RuntimeError" for r in records)


def test_doc_locks_are_per_doc_id():
    """Colliding doc_ids (shared form numbers) must serialize their
    check-delete-upsert sequence; distinct doc_ids must not contend."""
    from mainframe_rag.ingest.run_ingest import _DocLocks

    locks = _DocLocks()
    held = locks.get("SC14-7315-70")
    assert held is locks.get("SC14-7315-70")
    assert locks.get("SC23-6845-22") is not held
    with held:
        again = locks.get("SC14-7315-70")
        assert again.acquire(blocking=False) is False  # same key excluded
        other = locks.get("SC23-6845-22")
        assert other.acquire(blocking=False) is True  # distinct key free
        other.release()


def test_worker_embed_batches_by_batch_size(synthetic_pdf, monkeypatch):
    """_parse_one must slice chunks by settings.batch_size rather than
    passing all chunks to embed_batch in a single call."""
    from mainframe_rag.config import Settings
    from mainframe_rag.ingest import run_ingest

    embed_calls: list[int] = []
    original_embed_batch = run_ingest.embed_batch

    def mock_embed_batch(chunks, product, version, title, embedder, contexts=None):
        embed_calls.append(len(chunks))
        return original_embed_batch(chunks, product, version, title, embedder, contexts)

    # batch_size min validator is 16; use Settings instance with batch_size=16 or mock chunks
    test_settings = Settings(batch_size=16, embed_mode="hash")
    # To verify slicing, override batch_size attribute to 2 for this test
    object.__setattr__(test_settings, "batch_size", 2)
    monkeypatch.setattr(run_ingest, "_load_worker_settings", lambda: test_settings)
    monkeypatch.setattr(run_ingest, "embed_batch", mock_embed_batch)

    task = (str(synthetic_pdf), None, None, None, str(synthetic_pdf.parent), sha256_file(synthetic_pdf), True, None)
    _rec, _parsed, chunks, vectors, contexts = run_ingest._parse_one(task)
    assert len(chunks) > 2
    assert contexts == {}
    assert len(chunks) > 2
    assert all(c <= 2 for c in embed_calls), f"every embed call must be <= batch_size=2: {embed_calls}"
    assert sum(embed_calls) == len(chunks)
    assert len(vectors) == len(chunks)


def test_bulk_load_disables_and_restores_indexing(tmp_path, synthetic_pdf, monkeypatch):
    """INGEST_BULK_LOAD raises the indexing threshold before the first upsert
    and restores the server default after the last one (HNSW builds must not
    compete with a bulk load). Default off."""
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.qdrant_io import (
        BULK_INDEXING_THRESHOLD_KB,
        DEFAULT_INDEXING_THRESHOLD_KB,
    )

    fake = _FakeQdrant()
    fake.events: list[tuple[str, object]] = []
    original_upsert = fake.upsert

    def upsert(collection_name, *, points, wait=True):
        fake.events.append(("upsert", len(points)))
        return original_upsert(collection_name, points=points, wait=wait)

    def update_collection(collection_name, *, optimizer_config):
        fake.events.append(("indexing_threshold", optimizer_config.indexing_threshold))
        return True

    fake.upsert = upsert
    fake.update_collection = update_collection
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    monkeypatch.setenv("INGEST_BULK_LOAD", "true")
    progress = tmp_path / "inventory.jsonl"
    rc = main(["--src", str(synthetic_pdf.parent), "--progress", str(progress), "--workers", "1"])
    assert rc == 0
    thresholds = [v for kind, v in fake.events if kind == "indexing_threshold"]
    assert thresholds[0] == BULK_INDEXING_THRESHOLD_KB, "indexing disabled before the load"
    assert thresholds[-1] == DEFAULT_INDEXING_THRESHOLD_KB, "restored after the load"
    upsert_idx = [i for i, (kind, _) in enumerate(fake.events) if kind == "upsert"]
    assert upsert_idx, "the run must have upserted points"
    assert upsert_idx[0] > 0 and upsert_idx[-1] < len(fake.events) - 1, (
        "restore happens strictly after all upserts"
    )


def test_parse_one_error_isolation_and_picklability(tmp_path):
    """Ensure worker errors are converted to plain data and round-trip through
    multiprocessing pickling across process boundaries without crashing."""
    import pickle

    from mainframe_rag.ingest.run_ingest import _parse_one

    bad_pdf = tmp_path / "corrupt_worker.pdf"
    bad_pdf.write_bytes(b"not a valid pdf file")

    record, parsed, chunks, vectors, contexts = _parse_one(
        (str(bad_pdf), "IBM", "z/OS", "3.2", str(tmp_path), "test_sha", True, None)
    )

    assert record.status == "error"
    assert record.error_type is not None
    assert record.error is not None
    assert record.path == str(bad_pdf)
    assert chunks == []
    assert vectors == []
    assert contexts == {}

    # Round-trip through pickle to verify IPC safety in ProcessPoolExecutor
    serialized = pickle.dumps((record, parsed, chunks, vectors, contexts))
    unpacked_rec, _unpacked_parsed, unpacked_chunks, unpacked_vectors, unpacked_contexts = pickle.loads(serialized)

    assert unpacked_rec.status == "error"
    assert unpacked_rec.error_type == record.error_type
    assert unpacked_rec.error == record.error
    assert unpacked_chunks == []
    assert unpacked_vectors == []
    assert unpacked_contexts == {}


# --------------------------------------- representation migration (391 F2) ---
def _migration_args(corpus, progress, *extra):
    return ["--src", str(corpus), "--progress", str(progress), "--workers", "1", *extra]


def _marker_generations(fake, collection):
    comp = f"{collection}__completions"
    return {
        (p.payload or {}).get("generation_id")
        for p in fake._points.get(comp, [])
        if (p.payload or {}).get("doc_id")
    }


def _second_doc(corpus):
    from scripts.make_synthetic_pdf import build

    build(
        corpus / "SA22-7777-01.pdf",
        doc_id="SA22-7777-01",
        title="Synthetic Initialization and Tuning Reference",
        message_id="IEB700I",
    )


def test_revision_change_blocks_skip_and_force_reembeds(tmp_path, synthetic_pdf, monkeypatch):
    """Issue #391 F2 (revision-only change): the versioned fingerprint embeds
    the operator revision, so A markers are ineligible under B — the run
    refuses without --reingest and re-embeds every document with it. The
    evidence is the marker generation id, never unchanged text or point ids."""
    import pytest

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.completion import doc_generation_id
    from mainframe_rag.ingest.ibm_pdf import sha256_file
    from mainframe_rag.ingest.representation import STATE_COMMITTED, read_manifest_record
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-A")
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    progress = tmp_path / "inventory.jsonl"
    args = _migration_args(synthetic_pdf.parent, progress)
    assert main(args) == 0
    a_model = Settings(_env_file=None, embed_model_revision="rev-A")
    b_model = Settings(_env_file=None, embed_model_revision="rev-B")
    sha = sha256_file(synthetic_pdf)
    rules_v = extraction_rules_version()
    a_gen = doc_generation_id(a_model, sha, rules_v, "||")
    b_gen = doc_generation_id(b_model, sha, rules_v, "||")
    assert a_gen != b_gen, "revision-only change must alter the generation identity"
    assert _marker_generations(fake, a_model.qdrant_collection) == {a_gen}

    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-B")
    with pytest.raises(RuntimeError, match="representation drift on embed_model_revision"):
        main(list(args))
    assert _marker_generations(fake, a_model.qdrant_collection) == {a_gen}, \
        "refused run deletes or rewrites nothing"

    assert main([*args, "--reingest"]) == 0
    assert _marker_generations(fake, a_model.qdrant_collection) == {b_gen}
    record = read_manifest_record(fake, f"{a_model.qdrant_collection}__completions")
    assert record.state == STATE_COMMITTED
    assert record.manifest.embed_model_revision == "rev-B"


def test_interrupted_migration_stays_pending_until_forced_resume(
    tmp_path, synthetic_pdf, monkeypatch
):
    """A crash between the pending declaration and the first document rewrite
    must not look compatible: a normal rerun refuses (A vectors must not skip
    as B) and --reingest resumes to a committed B contract."""
    import pytest

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.representation import (
        STATE_COMMITTED,
        STATE_PENDING,
        begin_manifest,
        read_manifest_record,
    )
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-A")
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    progress = tmp_path / "inventory.jsonl"
    args = _migration_args(synthetic_pdf.parent, progress)
    assert main(args) == 0
    model_a = Settings(_env_file=None, embed_model_revision="rev-A")
    markers_a = _marker_generations(fake, model_a.qdrant_collection)

    model_b = Settings(_env_file=None, embed_model_revision="rev-B")
    comp = f"{model_b.qdrant_collection}__completions"
    begin_manifest(fake, comp, model_b, extraction_rules_version())
    assert read_manifest_record(fake, comp).state == STATE_PENDING

    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-B")
    with pytest.raises(RuntimeError, match="unfinished representation migration"):
        main(list(args))
    assert _marker_generations(fake, model_a.qdrant_collection) == markers_a

    assert main([*args, "--reingest"]) == 0
    record = read_manifest_record(fake, comp)
    assert record.state == STATE_COMMITTED
    assert record.manifest.embed_model_revision == "rev-B"


def test_partial_migration_failure_leaves_pending_then_resume_commits(
    tmp_path, synthetic_pdf, monkeypatch
):
    """A failure after partial document work must never certify the new
    contract: it stays pending (skips and serving blocked) and the forced
    rerun completes it once the corpus is whole."""
    import shutil

    import pytest

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.representation import (
        STATE_COMMITTED,
        STATE_PENDING,
        read_manifest_record,
    )

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    shutil.copy(synthetic_pdf, corpus / synthetic_pdf.name)
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-A")
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    progress = tmp_path / "inventory.jsonl"
    args = _migration_args(corpus, progress)
    assert main(args) == 0
    collection = Settings(_env_file=None).qdrant_collection
    comp = f"{collection}__completions"

    bad = corpus / "broken.pdf"
    bad.write_bytes(b"not a pdf")
    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-B")
    assert main([*args, "--reingest"]) == 1
    record = read_manifest_record(fake, comp)
    assert record.state == STATE_PENDING, "failures must not certify the contract"
    with pytest.raises(RuntimeError, match="unfinished representation migration"):
        main(list(args))

    bad.unlink()
    assert main([*args, "--reingest"]) == 0
    record = read_manifest_record(fake, comp)
    assert record.state == STATE_COMMITTED
    assert record.manifest.embed_model_revision == "rev-B"


def test_stale_generation_marker_blocks_commit(tmp_path, synthetic_pdf, monkeypatch):
    """Commit-time scope proof (issue #391 F2): a source walk that no longer
    covers every stored doc cannot commit the new contract while the old
    representation's markers (and vectors) remain searchable."""
    import shutil

    import pytest

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.representation import (
        STATE_PENDING,
        read_manifest_record,
    )

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    shutil.copy(synthetic_pdf, corpus / synthetic_pdf.name)
    _second_doc(corpus)
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-A")
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    progress = tmp_path / "inventory.jsonl"
    args = _migration_args(corpus, progress)
    assert main(args) == 0
    collection = Settings(_env_file=None).qdrant_collection
    comp = f"{collection}__completions"
    assert len(_marker_generations(fake, collection)) == 2

    second = corpus / "SA22-7777-01.pdf"
    second.unlink()
    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-B")
    with pytest.raises(RuntimeError, match="representation migration incomplete"):
        main([*args, "--reingest"])
    assert read_manifest_record(fake, comp).state == STATE_PENDING

    shutil.copy(synthetic_pdf, corpus / synthetic_pdf.name)  # no-op keeper
    _second_doc(corpus)
    assert main([*args, "--reingest"]) == 0
    assert len(_marker_generations(fake, collection)) == 2
    assert read_manifest_record(fake, comp).state == "committed"


def test_limit_is_refused_on_every_real_run(tmp_path, synthetic_pdf, monkeypatch):
    """A real run is a publication of the whole corpus (#360), so --limit is
    refused before any write. This replaces the in-place subset bootstrap
    and its limited-migration refusal (issue #391 F2): a partial walk can no
    longer reach a collection at all."""
    import shutil

    import pytest

    from mainframe_rag.ingest import run_ingest

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    shutil.copy(synthetic_pdf, corpus / synthetic_pdf.name)
    _second_doc(corpus)
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    progress = tmp_path / "inventory.jsonl"
    with pytest.raises(RuntimeError, match="refuses --limit on a real run"):
        main(_migration_args(corpus, progress, "--limit", "1"))
    assert fake._points == {}, "the refusal precedes every write"
    assert not progress.exists()


def test_unmarked_searchable_point_blocks_migration_commit(tmp_path, synthetic_pdf, monkeypatch):
    """Issue #391 current packet (counterexample 1): the commit-time residue
    scan reads completion markers only, so an unmarked old-rules point was
    certified as the wanted representation. The scope proof must verify the
    actual searchable membership; unknown data blocks the commit and stays."""
    import pytest

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.representation import STATE_PENDING, read_manifest_record

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    collection = Settings(_env_file=None).qdrant_collection
    from types import SimpleNamespace

    fake._points[collection] = [
        SimpleNamespace(
            id="legacy-unmarked",
            payload={"doc_id": "SA99-0000-00", "rules_v": "pre-rp2", "source_rev": "rev-old"},
        )
    ]
    # A versioned collection under contract rev-A: the rev-B run is an
    # ordinary re-embed migration, not unversioned data.
    from mainframe_rag.ingest.representation import STATE_COMMITTED, write_manifest
    from mainframe_rag.ingest.rules_version import extraction_rules_version

    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-A")
    write_manifest(fake, f"{collection}__completions", Settings(_env_file=None),
                   extraction_rules_version(), state=STATE_COMMITTED)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    # Publication needs a non-empty corpus; the walked doc is not the
    # unmarked point, which stays unknown data.
    import shutil

    shutil.copy(synthetic_pdf, corpus / synthetic_pdf.name)
    progress = tmp_path / "inventory.jsonl"
    monkeypatch.setenv("EMBED_MODEL_REVISION", "rev-B")
    with pytest.raises(RuntimeError, match="searchable point"):
        main(_migration_args(corpus, progress, "--reingest"))
    comp = f"{collection}__completions"
    record = read_manifest_record(fake, comp)
    assert record is not None and record.state == STATE_PENDING, (
        "an incomplete scope proof must leave the contract pending"
    )
    assert any(
        str(getattr(p, "id", "")) == "legacy-unmarked"
        for p in fake._points[collection]
    ), "unknown data is preserved: refusal is never a deletion instruction"


def test_upsert_failure_log_carries_error_type_not_exception_text(
    tmp_path, synthetic_pdf, capsys, monkeypatch
):
    """AGENTS.md: logs never carry exception text. A failing upsert logs the
    class name only; the sentinel in the exception message must not reach
    stderr (the inventory record keeps its own bounded error field)."""
    from mainframe_rag.ingest import run_ingest

    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: _FakeQdrant())

    def boom(*args, **kwargs):
        raise ConnectionError("SECRET-XYZ upstream body")

    monkeypatch.setattr(run_ingest, "_upsert_one", boom)
    progress = tmp_path / "inventory.jsonl"
    assert main(["--src", str(synthetic_pdf.parent), "--progress", str(progress), "--workers", "1"]) != 0
    err = capsys.readouterr().err
    assert "SECRET-XYZ" not in err
    events = [json.loads(l) for l in err.splitlines() if l.startswith("{")]
    errors = [e for e in events if e.get("action") == "error"]
    assert errors and errors[0]["error_type"] == "ConnectionError"


# ---------------------------------------------------------- bounds (issue #374)


def _bounded_task(pdf):
    return (str(pdf), None, None, None, str(pdf.parent), sha256_file(pdf), True, None)


def _spy_embed(monkeypatch):
    from mainframe_rag.ingest import run_ingest

    calls: list[int] = []
    real = run_ingest.embed_batch

    def spy(chunks, *a, **k):
        calls.append(len(chunks))
        return real(chunks, *a, **k)

    monkeypatch.setattr(run_ingest, "embed_batch", spy)
    return calls


@pytest.mark.parametrize(
    ("override", "dimension"),
    [
        ({"ingest_max_pdf_bytes": 100}, "pdf_bytes"),
        ({"ingest_max_doc_pages": 3}, "pages"),
        ({"ingest_max_doc_chunks": 2}, "chunks"),
        ({"embed_max_input_chars": 40}, None),
    ],
)
def test_document_bounds_refuse_before_embedding_with_an_error_record(
    synthetic_pdf, monkeypatch, override, dimension
):
    """Issue #374: an over-bound document is an explicit typed error record
    (counts only), embeds nothing and returns no chunks/vectors to upsert.
    Defaults stay unbounded (the ordinary ingest tests prove it)."""
    from mainframe_rag.config import Settings
    from mainframe_rag.ingest import run_ingest

    monkeypatch.setattr(
        run_ingest, "_load_worker_settings", lambda: Settings(embed_mode="hash", _env_file=None, **override)
    )
    embed_calls = _spy_embed(monkeypatch)
    record, _parsed, chunks, vectors, contexts = run_ingest._parse_one(_bounded_task(synthetic_pdf))
    assert record.status == "error"
    assert record.error_type == ("EmbedInputTooLarge" if dimension is None else "DocumentTooLarge")
    if dimension:
        assert dimension in record.error
    assert (chunks, vectors, contexts) == ([], [], {})
    assert embed_calls == [], "no embed call for a refused document"


def test_oversize_document_is_never_upserted_or_completed_and_next_run_succeeds(
    tmp_path, synthetic_pdf, monkeypatch
):
    """End to end through run(): the refused document writes no points and no
    completion, the run reports failure, and once the bound is lifted the
    the forced re-run ingests the same file."""
    from mainframe_rag.ingest import run_ingest

    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    monkeypatch.setenv("INGEST_MAX_DOC_CHUNKS", "1")
    progress = tmp_path / "inventory.jsonl"
    args = ["--src", str(synthetic_pdf.parent), "--progress", str(progress), "--workers", "1"]
    assert main(args) == 1
    # Only the representation-manifest point (1 point) may be written; the
    # refused document leaves no chunk points and no completion marker.
    assert fake.upsert_calls == [(fake.upsert_calls[0][0], 1)], fake.upsert_calls
    manifest_collection = fake.upsert_calls[0][0]
    assert manifest_collection.endswith("_completions"), manifest_collection
    records = [json.loads(l) for l in progress.read_text().splitlines() if l.strip()]
    assert [r["status"] for r in records] == ["error"]
    assert records[0]["error_type"] == "DocumentTooLarge"

    # The failed run left the collection's representation contract pending
    # (existing behaviour for any failed document): the documented recovery
    # is a forced re-run once the cause is lifted.
    monkeypatch.delenv("INGEST_MAX_DOC_CHUNKS")
    assert main([*args, "--reingest"]) == 0
    assert sum(fake.upserts) > 0


@pytest.mark.parametrize(
    ("parts", "cli"),
    [
        (("IBM", "zOS|x", "2.5"), {}),
        (("IBM", "zOS", "2|5"), {}),
        (("IBM", "zOS", "2.5"), {"vendor": "a|b"}),
        (("IBM", "zOS", "2.5"), {"product": "p|q"}),
        (("IBM", "zOS", "2.5"), {"version": "1|2"}),
    ],
)
def test_pipe_in_a_label_is_refused_with_a_fixed_error_on_the_worker_path(
    plain_pdf, tmp_path, monkeypatch, parts, cli
):
    """Issue #878: `|` delimits source_rev_key, so a label containing it (path
    layout or CLI flag) is refused before any chunk, embed or key exists. The
    error record is fixed text: no label, path or manual text."""
    import shutil

    from mainframe_rag.config import Settings
    from mainframe_rag.ingest import run_ingest

    pdf = tmp_path.joinpath(*parts, "guide.pdf")
    pdf.parent.mkdir(parents=True)
    shutil.copy(plain_pdf, pdf)
    monkeypatch.setattr(
        run_ingest, "_load_worker_settings", lambda: Settings(embed_mode="hash", _env_file=None)
    )
    embed_calls = _spy_embed(monkeypatch)
    task = (str(pdf), cli.get("vendor"), cli.get("product"), cli.get("version"), str(tmp_path), sha256_file(pdf), True, None)
    record, _parsed, chunks, vectors, contexts = run_ingest._parse_one(task)
    assert record.status == "error"
    assert record.error_type == "LabelDelimiterRefused"
    assert record.error == (
        "label_delimiter: vendor, product or version label contains the reserved "
        "character '|'; document refused"
    )
    assert (chunks, vectors, contexts) == ([], [], {})
    assert embed_calls == []


def test_pipe_label_document_is_refused_run_continues_and_valid_labels_keep_their_key(
    tmp_path, synthetic_pdf, plain_pdf, monkeypatch
):
    """Issue #878 end to end through main(): the refused document is an error
    record, the valid document in the same run is ingested with the same
    source_rev key as before (independently spelled), and once the label is
    fixed the forced re-run ingests it."""
    import shutil

    from mainframe_rag.ingest import run_ingest
    from mainframe_rag.ingest.ibm_pdf import sha256_file

    root = tmp_path / "corpus"
    good = root / "IBM" / "zOS" / "2.5" / "good.pdf"
    bad = root / "IBM" / "zOS|x" / "2.5" / "bad.pdf"
    for dest, src in ((good, plain_pdf), (bad, synthetic_pdf)):
        dest.parent.mkdir(parents=True)
        shutil.copy(src, dest)
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    progress = tmp_path / "inventory.jsonl"
    args = ["--src", str(root), "--progress", str(progress), "--workers", "1"]
    assert main(args) == 1
    records = {Path(r["path"]).name: r for r in map(json.loads, progress.read_text().splitlines()) if r}
    assert records["bad.pdf"]["status"] == "error"
    assert records["bad.pdf"]["error_type"] == "LabelDelimiterRefused"
    assert "zOS|x" not in records["bad.pdf"]["error"] and "bad.pdf" not in records["bad.pdf"]["error"]
    assert records["good.pdf"]["status"] == "upserted"
    assert records["good.pdf"]["source_rev"] == f"ibm|zos|2.5|{sha256_file(good)}"

    shutil.move(str(bad.parent.parent), str(root / "IBM" / "zOS2"))
    assert main([*args, "--reingest"]) == 0
    last = {Path(r["path"]).name: r for r in map(json.loads, progress.read_text().splitlines()) if r}
    assert last["bad.pdf"]["status"] == "upserted"
    assert last["bad.pdf"]["source_rev"].startswith("ibm|zos2|2.5|")


@pytest.mark.parametrize(
    ("case", "fake_kwargs", "env", "extra", "match"),
    [
        ("rules_mismatch", {"stored_sha": "c" * 64, "stored_rules_v": "0123456789abcdef"}, {}, (),
         "extraction-rules mismatch"),
        # A pre-versioning collection cannot prove which rules extracted its payloads.
        ("legacy_unversioned", {"stored_sha": "c" * 64, "stored_rules_v": ""}, {}, (),
         "predates extraction-rules"),
        # A force flag may bypass rejection of old data, never the
        # requirement to identify the new representation (issue #391 F2).
        ("unattested_revision", {}, {"EMBED_MODE": "vllm", "DENSE_DIM": "256"}, ("--reingest",),
         "EMBED_MODEL_REVISION"),
        ("contextual_on_hash", {}, {"CONTEXTUAL_EMBED_ENABLED": "true"}, (), "requires embed_mode=vllm"),
        # Mode dispatch (issue #583 S5): refused before publication takes its
        # lock, records a build or prepares staging.
        ("contextual_on_hash_publish", {},
         {"CONTEXTUAL_EMBED_ENABLED": "true", "INGEST_ALIAS_PUBLISH": "true"}, (),
         "requires embed_mode=vllm"),
        ("retire_dry_run", {}, {}, ("--retire-doc", "SA22-7000-00", "--dry-run"), "requires a real run"),
    ],
)
def test_failed_preflight_starts_no_workers_and_writes_nothing(
    tmp_path, synthetic_pdf, monkeypatch, case, fake_kwargs, env, extra, match
):
    """Issue #583 acceptance: every preflight refusal happens before the
    parse pool exists and leaves storage and the inventory untouched."""
    import copy

    from mainframe_rag.ingest import run_ingest

    fake = _FakeQdrant(**fake_kwargs)
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    monkeypatch.delenv("EMBED_MODEL_REVISION", raising=False)
    monkeypatch.delenv("CONTEXTUAL_EMBED_ENABLED", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    pools: list[object] = []

    def no_pool(*a, **k):
        pools.append(k)
        raise AssertionError("parse worker pool started after a failed preflight")

    monkeypatch.setattr(run_ingest, "ProcessPoolExecutor", no_pool)
    before = copy.deepcopy(
        (fake._points, fake.upserts, fake.upsert_calls, fake.deletes, fake.created_collections)
    )
    progress = tmp_path / "inventory.jsonl"
    with pytest.raises(RuntimeError, match=match):
        main(_migration_args(synthetic_pdf.parent, progress, *extra))
    assert pools == []
    after = (fake._points, fake.upserts, fake.upsert_calls, fake.deletes, fake.created_collections)
    assert after == before, f"{case}: storage mutated before the refusal"
    assert not progress.exists() or progress.read_text() == ""
    if case in ("contextual_on_hash_publish", "retire_dry_run"):
        # Dispatch refusals precede every lock, so no lock/state file exists.
        assert sorted(p.name for p in tmp_path.iterdir()) == [], f"{case}: lock or state written"


def test_combined_in_flight_window_bounds_parse_and_upsert_work(tmp_path, monkeypatch):
    """Issue #583 S4: parse futures plus upsert futures awaiting the parent
    never exceed max(2, workers * 2), so slow upserts cannot make the parent
    hold unbounded parsed documents and vectors. Both pools run in-process
    here; a future counts as in flight from submit until the parent reads
    its result."""
    import threading
    from concurrent.futures import ThreadPoolExecutor as RealThreads

    from scripts.make_synthetic_pdf import build

    from mainframe_rag.ingest import run_ingest

    corpus = tmp_path / "corpus"
    for n in range(4):
        build(corpus / f"SA22-70{n}0-00.pdf", doc_id=f"SA22-70{n}0-00",
              title=f"Synthetic Reference {n}", message_id=f"IEA{n}00I")
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.delenv("DENSE_DIM", raising=False)
    fake = _FakeQdrant()
    monkeypatch.setattr(run_ingest, "_get_qdrant", lambda settings: fake)
    lock = threading.Lock()
    state = {"in_flight": 0, "peak": 0, "submitted": 0}

    class Recording(RealThreads):
        def __init__(self, max_workers=None, mp_context=None, **_):
            super().__init__(max_workers=max_workers)

        def submit(self, fn, /, *args, **kwargs):
            future = super().submit(fn, *args, **kwargs)
            with lock:
                state["in_flight"] += 1
                state["submitted"] += 1
                state["peak"] = max(state["peak"], state["in_flight"])
            real_result = future.result
            consumed = []

            def result(timeout=None):
                if not consumed:
                    consumed.append(True)
                    with lock:
                        state["in_flight"] -= 1
                return real_result(timeout)

            future.result = result
            return future

    monkeypatch.setattr(run_ingest, "ProcessPoolExecutor", Recording)
    monkeypatch.setattr(run_ingest, "ThreadPoolExecutor", Recording)
    progress = tmp_path / "inventory.jsonl"
    assert main(_migration_args(corpus, progress)) == 0
    assert state["submitted"] == 8  # 4 parses + 4 upserts
    assert state["peak"] <= 2, state  # workers=1 -> window = max(2, 1 * 2)
    assert state["in_flight"] == 0


def test_synthetic_corp_manuals_ingest_dry_run(tmp_path, capsys):
    """The #890 demo corpus is original, parses generically (filename-stem
    identity, no IBM signals) and every manual fits its pages."""
    from scripts.make_synthetic_pdf import CORP_MANUALS, build_corp_manuals

    corpus = tmp_path / "corp"
    paths = build_corp_manuals(corpus)
    assert [p.stem for p in paths] == sorted(CORP_MANUALS)
    assert main(["--src", str(corpus), "--dry-run", "--progress", str(tmp_path / "inv.jsonl")]) == 0
    assert '"pdfs":3' in capsys.readouterr().err
