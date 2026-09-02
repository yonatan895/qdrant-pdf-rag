"""Makefile hygiene: the EMBED_MODE export must stay target-scoped.

A global `export EMBED_MODE` reaches EVERY make recipe — including the
airgap scripts, whose fail-closed refusal of EMBED_MODE=hash then fires and
breaks the CI airgap-dryrun job (the CI dry-run must prove the refusal
fires, not cause it). This pins the scoping fix at the file level."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_makefile_has_no_global_embed_mode_export() -> None:
    lines = (REPO / "Makefile").read_text(encoding="utf-8").splitlines()
    global_exports = [ln for ln in lines if ln.strip().startswith("export EMBED_MODE")]
    assert global_exports == [], (
        "bare `export EMBED_MODE` leaks the hash default into every recipe, "
        "including the airgap scripts that refuse it fail-closed; scope the "
        "export to the eval-family targets instead"
    )


def test_makefile_scopes_embed_mode_export_to_eval_family() -> None:
    lines = (REPO / "Makefile").read_text(encoding="utf-8").splitlines()
    scoped = [ln for ln in lines if "export EMBED_MODE :=" in ln]
    assert scoped, "eval-family targets must export EMBED_MODE (target-scoped)"
    assert any(t in " ".join(scoped) for t in ("eval", "eval-baseline", "eval-holdout")), (
        "the target-scoped export must cover the eval family"
    )
