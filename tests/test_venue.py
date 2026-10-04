"""Unit tests for the real-corpus venue rule (issue #268).

The contract: dev is the default; the frozen holdout and the real-corpus
collection fail closed unless `VENUE=rc` is declared. Pure helpers, no
stack, no environment mutation outside monkeypatch.
"""

from pathlib import Path

import pytest

from mainframe_rag.eval.datasets import (
    DEV,
    DEV_GOLDEN_PATH,
    HOLDOUT_PATH,
    RC,
    VenueError,
    require_rc_for_collection,
    require_rc_for_golden,
    resolve_golden_paths,
    resolve_venue,
)


def test_venue_unset_is_dev(monkeypatch):
    monkeypatch.delenv("VENUE", raising=False)
    assert resolve_venue() == DEV


def test_venue_blank_is_dev():
    assert resolve_venue({}) == DEV
    assert resolve_venue({"VENUE": "   "}) == DEV


def test_venue_rc_case_and_whitespace_folded():
    assert resolve_venue({"VENUE": "rc"}) == RC
    assert resolve_venue({"VENUE": "RC"}) == RC
    assert resolve_venue({"VENUE": " rc "}) == RC


def test_venue_typo_fails_closed():
    with pytest.raises(VenueError):
        resolve_venue({"VENUE": "release"})


def test_default_golden_is_dev_only():
    assert resolve_golden_paths(None, venue=DEV) == [DEV_GOLDEN_PATH]


def test_rc_default_adds_frozen_holdout():
    assert resolve_golden_paths(None, venue=RC) == [DEV_GOLDEN_PATH, HOLDOUT_PATH]


def test_explicit_holdout_refused_in_dev():
    with pytest.raises(VenueError):
        resolve_golden_paths([HOLDOUT_PATH], venue=DEV)


def test_explicit_holdout_allowed_in_rc():
    assert resolve_golden_paths([HOLDOUT_PATH], venue=RC) == [HOLDOUT_PATH]


def test_explicit_dev_golden_allowed_without_rc(tmp_path: Path):
    mine = tmp_path / "golden.jsonl"
    assert resolve_golden_paths([mine], venue=DEV) == [mine]


def test_explicit_paths_never_append_holdout():
    # An explicit golden selection is authoritative: rc does not add entries.
    paths = resolve_golden_paths([DEV_GOLDEN_PATH], venue=RC)
    assert paths == [DEV_GOLDEN_PATH]


def test_require_rc_for_golden_ignores_dev_paths():
    require_rc_for_golden([DEV_GOLDEN_PATH], venue=DEV)


def test_real_corpus_collection_refused_in_dev():
    with pytest.raises(VenueError):
        require_rc_for_collection("real_manuals", venue=DEV)


def test_real_corpus_collection_allowed_in_rc():
    require_rc_for_collection("real_manuals", venue=RC)


def test_dev_collection_allowed_without_rc():
    require_rc_for_collection("mainframe_manuals", venue=DEV)
    require_rc_for_collection("paraphrase-manuals", venue=DEV)


# ------------------------------------------------------- entry-point wiring
def test_eval_answers_refuses_holdout_without_rc(monkeypatch, capfd):
    import mainframe_rag.eval.answers as ea
    from mainframe_rag.config import Settings

    monkeypatch.delenv("VENUE", raising=False)
    monkeypatch.setattr(
        ea, "load_settings",
        lambda: Settings(embed_mode="hash", qdrant_collection="test-corpus", _env_file=None),
    )
    assert ea.main(["--golden", "evals/holdout.jsonl"]) == 2
    assert "frozen holdout" in capfd.readouterr().err


def test_harness_l2_refuses_real_corpus_without_rc(monkeypatch, capfd):
    from scripts.harness_l2 import main as l2_main

    from mainframe_rag import config
    from mainframe_rag.config import Settings

    monkeypatch.delenv("VENUE", raising=False)
    monkeypatch.setattr(
        config, "load_settings",
        lambda: Settings(embed_mode="hash", qdrant_collection="real_manuals", _env_file=None),
    )
    assert l2_main([]) == 2
    assert "real-corpus RC venue" in capfd.readouterr().err


def test_gate_l1_refuses_holdout_without_rc(monkeypatch, capfd):
    from scripts.gate_l1 import run_gate

    monkeypatch.delenv("VENUE", raising=False)
    rc, md = run_gate(golden_path=HOLDOUT_PATH)
    assert rc == 2
    assert "frozen holdout" in capfd.readouterr().err
    assert "**ERROR:**" in md


def test_replay_sweep_refuses_holdout_without_rc(monkeypatch, capfd, tmp_path):
    from scripts.replay_sweep import main as sweep_main

    monkeypatch.delenv("VENUE", raising=False)
    pools = tmp_path / "pools.jsonl"
    pools.write_text("")
    assert sweep_main(["--pools", str(pools), "--golden", str(HOLDOUT_PATH)]) == 2
    assert "frozen holdout" in capfd.readouterr().err


def test_holdout_copy_elsewhere_still_requires_rc(tmp_path):
    """Filename identity crosses installs: a holdout copy outside evals/ is
    still the frozen holdout in dev."""
    elsewhere = tmp_path / "elsewhere" / "holdout.jsonl"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("{}\n")
    with pytest.raises(VenueError, match="frozen holdout"):
        require_rc_for_golden([elsewhere], venue=DEV)
    require_rc_for_golden([elsewhere], venue=RC)


def test_holdout_symlink_alias_resolves_consistently(tmp_path):
    """A symlink alias for holdout content resolves to the holdout name."""
    target = tmp_path / "holdout.jsonl"
    target.write_text("{}\n")
    alias = tmp_path / "golden-alias.jsonl"
    try:
        alias.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(VenueError, match="frozen holdout"):
        require_rc_for_golden([alias], venue=DEV)


def _synthetic_holdout(tmp_path, data=b'{"query":"Synthetic?","expected_doc_ids":["synthetic"]}\n'):
    import hashlib

    root = tmp_path / "data with spaces אב"
    root.mkdir()
    path = root / "holdout.jsonl"
    path.write_bytes(data)
    pin = path.with_name(path.name + ".sha256")
    pin.write_text(hashlib.sha256(data).hexdigest() + "  evals/holdout.jsonl\n")
    return path, pin


@pytest.mark.parametrize("fault", ["missing", "empty", "digest", "target", "multiple", "invalid_utf8", "missing_data"])
def test_holdout_pin_fails_closed(tmp_path, monkeypatch, fault):
    from mainframe_rag.eval.datasets import DatasetPinError, load_golden

    monkeypatch.setenv("VENUE", "rc")
    path, pin = _synthetic_holdout(tmp_path)
    if fault == "missing":
        pin.unlink()
    elif fault == "empty":
        pin.write_text("")
    elif fault == "digest":
        path.write_bytes(b"invalid JSON must not reach the parser")
    elif fault == "target":
        pin.write_text(pin.read_text().replace("holdout.jsonl", "other.jsonl"))
    elif fault == "multiple":
        pin.write_text(pin.read_text() * 2)
    elif fault == "invalid_utf8":
        pin.write_bytes(b"\xff")
    else:
        path.unlink()
    with pytest.raises(DatasetPinError):
        load_golden(path)


def test_holdout_venue_refuses_before_read(tmp_path, monkeypatch):
    from mainframe_rag.eval.datasets import read_golden_text

    monkeypatch.setenv("VENUE", "dev")
    path, _ = _synthetic_holdout(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("dataset/pin read before RC authorization")
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    with pytest.raises(VenueError):
        read_golden_text(path)


@pytest.mark.parametrize("alias", [False, True])
def test_holdout_verified_bytes_are_the_parsed_bytes_and_next_read_revalidates(tmp_path, monkeypatch, alias):
    from mainframe_rag.eval.datasets import DatasetPinError, load_golden

    monkeypatch.setenv("VENUE", "rc")
    target, _ = _synthetic_holdout(tmp_path)
    path = tmp_path / "ordinary alias.jsonl" if alias else target
    if alias:
        path.symlink_to(target)
    read_bytes = Path.read_bytes
    reads = []
    original = target.read_bytes()
    def replace_after_read(candidate):
        result = read_bytes(candidate)
        if candidate == target:
            reads.append(candidate)
            candidate.write_bytes(b"not the approved bytes")
        return result
    monkeypatch.setattr(Path, "read_bytes", replace_after_read)
    assert [entry.query for entry in load_golden(path)] == ["Synthetic?"]
    assert reads == [target]
    with pytest.raises(DatasetPinError, match="sha256 mismatch"):
        load_golden(path)
    target.write_bytes(original)
    assert [entry.expected_doc_ids for entry in load_golden(path)] == [["synthetic"]]


@pytest.mark.parametrize("module_name,extra", [
    ("scripts.eval_retrieval", ["--no-check"]),
    ("mainframe_rag.eval.answers", []),
    ("mainframe_rag.eval.chat", []),
    ("scripts.harness", []),
    ("scripts.harness_l2", []),
    ("scripts.harness_l4", []),
    ("scripts.gate_l1", []),
    ("scripts.capture_pool", ["--out", "must-not-create.jsonl"]),
    ("scripts.replay_sweep", ["--pools", "must-not-read.jsonl"]),
])
def test_all_holdout_entry_points_refuse_bad_pin_before_effects(tmp_path, monkeypatch, capfd, module_name, extra):
    import importlib

    from mainframe_rag import config
    from mainframe_rag.config import Settings

    module = importlib.import_module(module_name)
    path, pin = _synthetic_holdout(tmp_path)
    pin.write_text("0" * 64 + "  holdout.jsonl\n")
    monkeypatch.setenv("VENUE", "rc")
    monkeypatch.chdir(tmp_path)
    settings = Settings(embed_mode="vllm", qdrant_collection="synthetic", _env_file=None)
    monkeypatch.setattr(config, "load_settings", lambda: settings)
    if hasattr(module, "load_settings"):
        monkeypatch.setattr(module, "load_settings", lambda: settings)
    def forbidden(*args, **kwargs):
        pytest.fail("external work before pin validation")
    import qdrant_client

    from mainframe_rag.ingest import embed
    monkeypatch.setattr(qdrant_client, "QdrantClient", forbidden)
    monkeypatch.setattr(embed, "build_embedder", forbidden)
    for name in ("evaluate", "run_l2", "run_query", "start_simulator"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, forbidden)
    assert module.main(["--golden", str(path), *extra]) == 2
    assert "sha256 mismatch" in capfd.readouterr().err
    assert not (tmp_path / "must-not-create.jsonl").exists()
