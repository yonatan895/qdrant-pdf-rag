"""BM25 cache preparation and the actual offline consumer boundary."""
from __future__ import annotations

import hashlib
import json
import shutil
import socket
from pathlib import Path

import pytest
from scripts import fetch_bm25_weights as bm25

# FastEmbed 0.9.1 validates config.json plus the 17 language files before it
# accepts a cached Qdrant/bm25 snapshot. Keep one source of truth for every
# synthetic cache in this module. tamil.txt is intentionally absent: 0.9.1
# omits it from additional_files.
_BM25_LANGUAGES = (
    "arabic", "danish", "dutch", "english", "finnish", "french", "german",
    "greek", "hungarian", "italian", "norwegian", "portuguese", "romanian",
    "russian", "spanish", "swedish", "turkish",
)
_BM25_FILES = ("config.json", *(f"{language}.txt" for language in _BM25_LANGUAGES))
_REVISION = "1" * 40


def _snapshot(cache: Path) -> Path:
    return cache / bm25.MODEL_DIR / "snapshots" / _REVISION


def _bm25_snapshot_files(stopwords: bytes = b"the\nand\n") -> dict[str, bytes]:
    """Synthetic snapshot contents accepted by FastEmbed's BM25 loader."""
    files = {name: b"" for name in _BM25_FILES}
    files["config.json"] = b"{}"
    files["english.txt"] = stopwords
    return files


def _write_bm25_manifest(path: Path, files: dict[str, bytes]) -> Path:
    entries = sorted(
        (hashlib.sha256(content).hexdigest(), name) for name, content in files.items()
    )
    path.write_text("".join(f"{digest}  {name}\n" for digest, name in entries), encoding="utf-8")
    return path


def _write_bm25_cache(cache: Path, revision: str, files: dict[str, bytes]) -> None:
    """Write the pinned downloader's cache layout, including trees/<revision>.json."""
    from huggingface_hub._local_folder import _create_cachedir_tag
    from huggingface_hub._tree_cache import TreeCacheEntry, write_tree_cache

    model = cache / bm25.MODEL_DIR
    snapshot = model / "snapshots" / revision
    snapshot.mkdir(parents=True)
    _create_cachedir_tag(cache)
    (model / "blobs").mkdir()
    (model / "refs").mkdir()
    (model / "refs/main").write_text(revision, encoding="utf-8")
    metadata = {}
    tree = {}
    for name, content in files.items():
        blob = hashlib.sha256(content).hexdigest()
        (model / "blobs" / blob).write_bytes(content)
        (snapshot / name).symlink_to(f"../../blobs/{blob}")
        metadata[f"snapshots/{revision}/{name}"] = {"size": len(content), "blob_id": blob}
        tree[name] = TreeCacheEntry(size=len(content), blob_id=blob)
    (model / "files_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    write_tree_cache(str(model), revision, tree)


@pytest.fixture
def preparation(tmp_path, monkeypatch):
    source = tmp_path / "source"
    files = _bm25_snapshot_files()
    _write_bm25_cache(source, _REVISION, files)
    manifest = _write_bm25_manifest(tmp_path / "weights.sha256", files)
    calls = []

    def acquire(model, stage):
        calls.append(model)
        shutil.copytree(source, stage, dirs_exist_ok=True, symlinks=True)

    monkeypatch.setattr(bm25, "_acquire", acquire)
    return tmp_path / "cache", manifest, calls


def _bytes(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_preparation_verifies_bytes_and_refreshes_identity_without_download(preparation):
    cache, manifest, calls = preparation
    bm25.prepare(cache, manifest)
    original = _bytes(cache)
    assert calls == ["Qdrant/bm25"]
    bm25.prepare(cache, manifest)
    assert _bytes(cache) == original
    marker = cache / ".task-complete"
    marker.unlink()
    bm25.prepare(cache, manifest)
    assert _bytes(cache) == original
    marker.write_bytes(b"\xff")
    bm25.prepare(cache, manifest)
    assert _bytes(cache) == original
    manifest.write_text("# changed pin commentary\n" + manifest.read_text())
    bm25.prepare(cache, manifest)
    assert calls == ["Qdrant/bm25"]
    assert json.loads(marker.read_text())["manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    bm25.verify_manifest(cache, manifest)


@pytest.mark.parametrize("damage", ["corrupt", "reference", "reference_encoding"])
def test_invalid_cache_requires_explicit_repair(preparation, damage):
    cache, manifest, calls = preparation
    bm25.prepare(cache, manifest)
    weight = _snapshot(cache) / "english.txt"
    if damage == "corrupt":
        weight.write_bytes(b"corrupted")
    elif damage == "reference":
        (cache / bm25.MODEL_DIR / "refs/main").write_text("wrong")
    else:
        (cache / bm25.MODEL_DIR / "refs/main").write_bytes(b"\xff")
    before = _bytes(cache)
    with pytest.raises(SystemExit):
        bm25.verify_manifest(cache, manifest)
    assert calls == ["Qdrant/bm25"] and _bytes(cache) == before
    bm25.prepare(cache, manifest)
    assert calls == ["Qdrant/bm25", "Qdrant/bm25"]
    assert weight.read_bytes() == b"the\nand\n"
    bm25.prepare(cache, manifest)
    assert len(calls) == 2


@pytest.mark.parametrize("metadata", ["{corrupt", "[]",
                                     json.dumps({f"snapshots/{_REVISION}/english.txt": 1}),
                                     '{"../foreign": {"size": 7, "blob_id": "a"}}'])
def test_unloadable_metadata_fails_verification_and_explicit_preparation_repairs(preparation, metadata):
    cache, manifest, calls = preparation
    bm25.prepare(cache, manifest)
    sidecar = cache / bm25.MODEL_DIR / "files_metadata.json"
    sidecar.write_text(metadata)
    before = _bytes(cache)
    with pytest.raises(SystemExit, match="invalid BM25 cache metadata"):
        bm25.verify_manifest(cache, manifest)
    assert _bytes(cache) == before and len(calls) == 1
    bm25.prepare(cache, manifest)
    bm25.verify_manifest(cache, manifest)
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["download", "checksum"])
def test_staged_failure_preserves_prior_cache_then_recovers(preparation, monkeypatch, failure):
    cache, manifest, _calls = preparation
    bm25.prepare(cache, manifest)
    (_snapshot(cache) / "english.txt").unlink()
    before = _bytes(cache)
    acquire = bm25._acquire

    def fail(model, stage):
        if failure == "download":
            raise OSError("synthetic acquisition failure")
        acquire(model, stage)
        (_snapshot(stage) / "english.txt").write_bytes(b"wrong bytes")

    with monkeypatch.context() as m:
        m.setattr(bm25, "_acquire", fail)
        with pytest.raises((OSError, SystemExit)):
            bm25.prepare(cache, manifest)
    assert _bytes(cache) == before
    assert not list(cache.parent.glob(".cache.prepare-*"))
    bm25.prepare(cache, manifest)
    bm25.verify_manifest(cache, manifest)
    assert (cache / ".task-complete").is_file()


def test_failed_pin_replacement_preserves_usable_previous_cache(preparation, monkeypatch, tmp_path):
    cache, manifest, _calls = preparation
    bm25.prepare(cache, manifest)
    previous_pin = tmp_path / "previous.sha256"
    previous_pin.write_bytes(manifest.read_bytes())
    before = _bytes(cache)
    next_bytes = b"original replacement stopwords\n"
    next_files = _bm25_snapshot_files(next_bytes)
    _write_bm25_manifest(manifest, next_files)

    def unavailable(model, stage):
        raise OSError("acquisition unavailable")

    with monkeypatch.context() as m:
        m.setattr(bm25, "_acquire", unavailable)
        with pytest.raises(OSError):
            bm25.prepare(cache, manifest)
    assert _bytes(cache) == before
    bm25.verify_manifest(cache, previous_pin)
    # The same original acquisition fixture now represents the approved new pin.
    source = tmp_path / "source"
    shutil.rmtree(source)
    _write_bm25_cache(source, _REVISION, next_files)
    bm25.prepare(cache, manifest)
    assert (_snapshot(cache) / "english.txt").read_bytes() == next_bytes
    bm25.verify_manifest(cache, manifest)


def test_verify_only_cli_never_imports_or_acquires(preparation, monkeypatch):
    import builtins
    import sys

    cache, manifest, calls = preparation
    bm25.prepare(cache, manifest)
    original_import = builtins.__import__

    def no_model_import(name, *args, **kwargs):
        assert not name.startswith("fastembed"), "verification imported inference dependency"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_model_import)
    monkeypatch.setattr(sys, "argv", ["fetch_bm25_weights.py", "--verify-only", "--out", str(cache),
                                      "--verify-manifest", str(manifest)])
    before = _bytes(cache)
    bm25.main()
    assert _bytes(cache) == before and len(calls) == 1
    (_snapshot(cache) / "english.txt").unlink()
    with pytest.raises(SystemExit):
        bm25.main()
    assert len(calls) == 1


def test_interrupted_publication_withholds_receipt_and_next_prepare_repairs(preparation, monkeypatch):
    cache, manifest, calls = preparation
    bm25.prepare(cache, manifest)
    (cache / bm25.MODEL_DIR / "refs/main").write_text("wrong")
    replace = Path.replace

    def fail_model_publication(path, destination):
        if path.name == bm25.MODEL_DIR:
            raise OSError("synthetic publication interruption")
        return replace(path, destination)

    with monkeypatch.context() as m:
        m.setattr(Path, "replace", fail_model_publication)
        with pytest.raises(OSError, match="interruption"):
            bm25.prepare(cache, manifest)
    assert not (cache / ".task-complete").exists()
    with pytest.raises(SystemExit):
        bm25.verify_manifest(cache, manifest)
    bm25.prepare(cache, manifest)
    bm25.verify_manifest(cache, manifest)
    assert (cache / ".task-complete").is_file()
    bm25.prepare(cache, manifest)
    assert len(calls) == 3


def test_unsupported_model_refuses_before_effects(preparation):
    cache, manifest, calls = preparation
    with pytest.raises(ValueError, match="only Qdrant/bm25"):
        bm25.prepare(cache, manifest, "Other/model")
    assert not cache.exists() and not calls


@pytest.mark.parametrize("foreign", ["notes.txt", "models--Other--model/file",
                                    f"models--Qdrant--bm25/snapshots/{_REVISION}/foreign.txt",
                                    ".locks/models--Qdrant--bm25/notes.txt"])
def test_preparation_refuses_unowned_members_without_mutation(preparation, foreign):
    cache, manifest, calls = preparation
    bm25.prepare(cache, manifest)
    other = cache / foreign
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_bytes(b"preserve me")
    before = _bytes(cache)
    with pytest.raises(ValueError, match="unowned"):
        bm25.prepare(cache, manifest)
    assert _bytes(cache) == before and len(calls) == 1


@pytest.mark.parametrize("location", ["parent", "model", "file"])
def test_preparation_refuses_symlink_escapes(preparation, tmp_path, location):
    cache, manifest, calls = preparation
    bm25.prepare(cache, manifest)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "english.txt").write_bytes(b"private file")
    if location == "parent":
        link = tmp_path / "link"
        link.symlink_to(tmp_path, target_is_directory=True)
        selected = link / "cache"
    else:
        selected = cache
        target = {"model": cache / bm25.MODEL_DIR,
                  "file": _snapshot(cache) / "english.txt"}[location]
        if target.is_dir():
            shutil.rmtree(target)
            target.symlink_to(outside, target_is_directory=True)
        else:
            target.unlink()
            target.symlink_to(outside / "english.txt")
    with pytest.raises(ValueError, match="symlink"):
        bm25.prepare(selected, manifest)
    assert (outside / "english.txt").read_bytes() == b"private file"
    assert len(calls) == 1


def test_cooperative_preparers_serialize_and_second_reuses(preparation, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    cache, manifest, calls = preparation
    entered, second_lock, release = Event(), Event(), Event()
    acquire, flock = bm25._acquire, bm25.fcntl.flock

    def blocked(model, stage):
        entered.set()
        assert release.wait(5)
        acquire(model, stage)

    def observed(fd, operation):
        if entered.is_set():
            second_lock.set()
        return flock(fd, operation)

    monkeypatch.setattr(bm25, "_acquire", blocked)
    monkeypatch.setattr(bm25.fcntl, "flock", observed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(bm25.prepare, cache, manifest)
        try:
            assert entered.wait(5)
            second = pool.submit(bm25.prepare, cache, manifest)
            assert second_lock.wait(5)
            assert not second.done()
        finally:
            release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert calls == ["Qdrant/bm25"]
    bm25.verify_manifest(cache, manifest)


def test_task_reaches_real_preparation_with_literal_inputs():
    import sys

    from tests.test_taskfile_contracts import TaskContractsTests

    case = TaskContractsTests()
    try:
        case.setUp()
        case.make_artifact_fixtures()
        python = case.root / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        literal = "cache space אב;$(touch SENTINEL)"
        cache = case.root / literal / "bm25-weights"
        snapshot = cache / bm25.MODEL_DIR / "snapshots/original"
        snapshot.mkdir(parents=True)
        (snapshot / "weights.bin").write_bytes(b"synthetic-weights-content\n")
        refs = cache / bm25.MODEL_DIR / "refs"
        refs.mkdir()
        (refs / "main").write_text("original")
        run = case.run_task("artifacts:bm25", f"BUNDLE_DIR={literal}", "BM25_MODEL=Qdrant/bm25",
                            extra_env={"BUNDLE_DIR": "ambient", "BM25_MODEL": "Other/model"})
        assert run.returncode == 0, run.stdout
        assert json.loads((cache / ".task-complete").read_text())["model"] == "Qdrant/bm25"
        assert not (case.root / "ambient").exists()
        assert not (case.root / "SENTINEL").exists()
        before = _bytes(cache)
        run = case.run_task("artifacts:bm25", f"BUNDLE_DIR={literal}", "BM25_MODEL=Other/model")
        assert run.returncode != 0 and "only Qdrant/bm25" in run.stdout
        assert _bytes(cache) == before
        assert case.run_task("artifacts:bm25", f"BUNDLE_DIR={literal}").returncode == 0
    finally:
        case.doCleanups()


def test_verified_snapshot_remains_loadable_after_cache_relocation(tmp_path, monkeypatch):
    """Original stopwords exercise HF refs/blob links without downloading weights."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("HF_HUB_DISABLE_TELEMETRY", "1")

    def refuse_network(*args, **kwargs):
        raise AssertionError("offline BM25 cache loading attempted network access")

    monkeypatch.setattr(socket.socket, "connect", refuse_network)
    from fastembed import SparseTextEmbedding

    cache = tmp_path / "staged cache"
    stopwords = b"the\nand\n"
    files = _bm25_snapshot_files(stopwords)
    _write_bm25_cache(cache, _REVISION, files)
    model = cache / bm25.MODEL_DIR
    tree_metadata = model / "trees" / f"{_REVISION}.json"
    assert tree_metadata.is_file()
    tree_bytes = tree_metadata.read_bytes()
    manifest = _write_bm25_manifest(tmp_path / "original.sha256", files)
    bm25.verify_manifest(cache, manifest)

    destination = tmp_path / "published cache"

    def acquire(model, stage):
        assert model == "Qdrant/bm25"
        shutil.copytree(cache, stage, dirs_exist_ok=True, symlinks=True)

    monkeypatch.setattr(bm25, "_acquire", acquire)
    bm25.prepare(destination, manifest)
    shutil.rmtree(cache)
    bm25.verify_manifest(destination, manifest)
    selected = _snapshot(destination)
    assert {path.name for path in selected.iterdir()} == set(files)
    assert all((selected / name).is_symlink() for name in files)
    assert (selected / "config.json").read_bytes() == b"{}"
    assert (selected / "english.txt").is_symlink()
    assert (selected / "english.txt").read_bytes() == stopwords
    assert (destination / "CACHEDIR.TAG").read_bytes().startswith(b"Signature: 8a477f597d28d172789f06886806bc55\n")
    assert (destination / bm25.MODEL_DIR / "trees" / f"{_REVISION}.json").read_bytes() == tree_bytes
    embedder = SparseTextEmbedding(
        model_name="Qdrant/bm25", cache_dir=str(destination), local_files_only=True,
    )
    assert embedder.model._model_dir == selected
    assert embedder.model.stopwords == {"the", "and"}
    vectors = list(embedder.embed(["the and", "original retrieval example"]))
    assert len(vectors) == 2
    assert len(vectors[0].indices) == 0
    assert len(vectors[1].indices) > 0
