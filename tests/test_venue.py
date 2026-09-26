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
    import scripts.eval_answers as ea

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


def test_venue_delegate_is_canonical():
    """Issue #508 C2: scripts/venue.py re-exports the package owner."""
    import scripts.venue as venue_shim

    from mainframe_rag.eval import datasets as canonical

    for name in (
        "VenueError",
        "resolve_venue",
        "require_rc_for_golden",
        "require_rc_for_collection",
        "resolve_golden_paths",
    ):
        assert getattr(venue_shim, name) is getattr(canonical, name), name


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
