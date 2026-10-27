"""The upstream/gap extension surface contract (ADR-0009 §9, GH#1118).

The committed snapshot must equal the surface derived from the code. A removed
or renamed item needs a working migration and a release note; an added item
needs only a snapshot refresh. The counterexamples run the real derivation and
refresh on a copy of the input files.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from scripts import extension_surface as surface

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_KEY = "PULL_SECRET"
NEW_KEY = "PULL_SECRET_NAME"


def test_snapshot_equals_the_surface_derived_from_the_code():
    assert surface.compare(ROOT) == []


def test_derivation_covers_what_the_gap_reads():
    """Independent anchors: names ADR-0009 and the operator docs use directly."""
    current = surface.derive(ROOT)
    expected = {
        "airgap_env_keys": {"INTERNAL_REGISTRY", "OPENWEBUI_OIDC_CLIENT_ID", "QDRANT_DATA_SIZE"},
        "retired_env_keys": {"REGISTRY_INTERNAL", "VLLM_BASE_URL"},
        "task_names": {"airgap:pipeline", "airgap:retire-legacy", "qa:unit"},
        "chart_values": {"mainframe-rag:openwebui.oidc.clientId", "mainframe-rag:openwebui.oidc.caBundle",
                         "mainframe-rag:feedback.secretName", "mainframe-rag:ingest.retireDocs",
                         "qdrant:persistence.size", "qdrant:resources.limits.memory"},
        "script_flags": {"pipeline.sh --dry-run", "retire_legacy.sh --stage", "import-script SHA",
                         "import-script SNEAKERNET_TRUSTED_PUB"},
        "gap_paths": {"gap/patches.md", "gap/config/", ".gitlab/CODEOWNERS"},
        # What the §4 import script reads from the bundle.
        "bundle_layout": {"tarball qdrant-pdf-rag-{IMAGE_SHA}.tar.sha256", "member bootstrap.sh",
                          "member repo.bundle", "member MANIFEST.txt", "manifest sha"},
    }
    for category, items in expected.items():
        assert items <= set(current[category]), category


@pytest.fixture
def tree(tmp_path):
    for relative in surface.INPUTS:
        source, target = ROOT / relative, tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, target)
    return tmp_path


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new), encoding="utf-8")


def example_line(root: Path, key: str) -> str:
    [line] = [line for line in (root / surface.EXAMPLE).read_text().splitlines()
              if line.lstrip("# \t").startswith(key + "=")]
    return line + "\n"


def removed_message(category: str, item: str) -> str:
    return surface.REMOVED.format(category=category, item=item)


def note(root: Path, line: str) -> None:
    with (root / surface.RELEASE_NOTES).open("a", encoding="utf-8") as notes:
        notes.write(f"\n## 2026-10-27 GH#1118 Fixture\n\n{line}\n")


def retire(root: Path, key: str, new: str) -> None:
    edit(root / surface.MODEL_CONFIG, "RETIRED_KEYS: tuple[tuple[str, str, str], ...] = (\n",
         f'RETIRED_KEYS: tuple[tuple[str, str, str], ...] = (\n    ("{key}", "0000000", "set {new} and remove it"),\n')


def test_removing_a_key_without_a_migration_fails_and_the_refresh_refuses(tree):
    snapshot = (tree / surface.SNAPSHOT).read_bytes()
    edit(tree / surface.EXAMPLE, example_line(tree, EXAMPLE_KEY), "")

    assert surface.compare(tree) == [removed_message("airgap_env_keys", EXAMPLE_KEY)]
    assert surface.write(tree) == [removed_message("airgap_env_keys", EXAMPLE_KEY)]
    assert (tree / surface.SNAPSHOT).read_bytes() == snapshot


@pytest.mark.parametrize("half", ["release note only", "refusal only"])
def test_a_rename_needs_both_the_refusal_and_the_release_note(tree, half):
    line = example_line(tree, EXAMPLE_KEY)
    edit(tree / surface.EXAMPLE, line, line.replace(EXAMPLE_KEY, NEW_KEY))
    if half == "release note only":
        note(tree, f"- removed `airgap_env_keys:{EXAMPLE_KEY}`; replaced by `{NEW_KEY}`; "
                   "migration: retired-key in `scripts/airgap/model_config.py`")
    else:
        retire(tree, EXAMPLE_KEY, NEW_KEY)

    assert removed_message("airgap_env_keys", EXAMPLE_KEY) in surface.compare(tree)
    assert removed_message("airgap_env_keys", EXAMPLE_KEY) in surface.write(tree)


def test_adding_a_key_fails_until_the_snapshot_is_refreshed(tree):
    with (tree / surface.EXAMPLE).open("a", encoding="utf-8") as example:
        example.write("# SURFACE_FIXTURE_KEY=\n")

    assert surface.compare(tree) == [surface.ADDED.format(category="airgap_env_keys", item="SURFACE_FIXTURE_KEY")]
    assert surface.write(tree) == []
    assert surface.compare(tree) == []
    assert "SURFACE_FIXTURE_KEY" in json.loads((tree / surface.SNAPSHOT).read_text())["current"]["airgap_env_keys"]


def test_a_rename_with_a_retired_key_refusal_and_a_release_note_passes(tree):
    line = example_line(tree, EXAMPLE_KEY)
    edit(tree / surface.EXAMPLE, line, line.replace(EXAMPLE_KEY, NEW_KEY))
    retire(tree, EXAMPLE_KEY, NEW_KEY)
    note(tree, f"- removed `airgap_env_keys:{EXAMPLE_KEY}`; replaced by `{NEW_KEY}`; "
               "migration: retired-key in `scripts/airgap/model_config.py`")

    assert sorted(surface.compare(tree)) == sorted([
        surface.ADDED.format(category="airgap_env_keys", item=NEW_KEY),
        surface.ADDED.format(category="retired_env_keys", item=EXAMPLE_KEY),
        surface.UNREFRESHED.format(category="airgap_env_keys", item=EXAMPLE_KEY),
    ])
    assert surface.write(tree) == []
    assert surface.compare(tree) == []
    written = json.loads((tree / surface.SNAPSHOT).read_text())
    assert written["removed"]["airgap_env_keys"] == [EXAMPLE_KEY]
    assert NEW_KEY in written["current"]["airgap_env_keys"]

    # The migration stays checked after the refresh: dropping the refusal fails.
    edit(tree / surface.MODEL_CONFIG, f'    ("{EXAMPLE_KEY}", "0000000", "set {NEW_KEY} and remove it"),\n', "")
    assert surface.BROKEN.format(category="airgap_env_keys", item=EXAMPLE_KEY,
                                 reason=f"`{EXAMPLE_KEY}` is not in RETIRED_KEYS") in surface.compare(tree)


@pytest.mark.parametrize(("kind", "evidence", "reason"), [
    ("refusal", "scripts/airgap/pipeline.sh", "`scripts/airgap/pipeline.sh` does not name `--no-load`"),
    ("alias", "scripts/airgap/absent.sh", "`scripts/airgap/absent.sh` is not a file in the repository"),
    ("retired-key", "scripts/airgap/pipeline.sh",
     "a retired-key refusal is an airgap_env_keys entry in `scripts/airgap/model_config.py`"),
])
def test_a_release_note_must_name_a_migration_the_code_provides(tree, kind, evidence, reason):
    note(tree, f"- removed `script_flags:pipeline.sh --skip-load`; replaced by `--no-load`; "
               f"migration: {kind} in `{evidence}`")
    [match] = surface.release_notes(tree).values()
    assert surface.migration_problem(tree, match) == reason
