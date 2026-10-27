"""The upstream/gap extension surface contract (ADR-0009 §9, GH#1118).

The committed snapshot must equal the surface derived from the code. A removed
or renamed item, including one left behind as a refusing stub, needs a release
note whose migration its owner file provides; an added item needs only a
snapshot refresh. The counterexamples run the real derivation and refresh on a
copy of the input files.
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
PIPELINE = "scripts/airgap/pipeline.sh"
TASKS = "taskfiles/airgap.yml"
SKIP_LOAD_ARM = "        --skip-load)   SKIP_LOAD=1 ;;\n"
FLAG_STUB = ('        --no-load)   SKIP_LOAD=1 ;;\n'
             '        --skip-load) die "--skip-load was renamed: use --no-load" ;;\n')
TASK_STUB = ('  pipeline:\n'
             '    desc: Renamed to airgap:run\n'
             '    cmds:\n'
             '      - \'echo "airgap:pipeline was renamed: use airgap:run" >&2; exit 1\'\n'
             '\n'
             '  run:\n')


def test_snapshot_equals_the_surface_derived_from_the_code():
    assert surface.compare(ROOT) == []


def test_derivation_covers_what_the_gap_reads():
    """Independent anchors: names ADR-0009 and the operator docs use directly."""
    current = surface.derive(ROOT)
    expected = {
        "airgap_env_keys": {"INTERNAL_REGISTRY", "OPENWEBUI_OIDC_CLIENT_ID", "QDRANT_DATA_SIZE"},
        "retired_env_keys": {"REGISTRY_INTERNAL", "VLLM_BASE_URL"},
        "task_names": {"airgap:pipeline", "airgap:retire-legacy", "artifacts:chart-check", "qa:unit"},
        "chart_values": {"mainframe-rag:openwebui.oidc.clientId", "mainframe-rag:openwebui.oidc.caBundle",
                         "mainframe-rag:feedback.secretName", "mainframe-rag:ingest.retireDocs",
                         "qdrant:persistence.size", "qdrant:resources.limits.memory",
                         "qdrant:imagePullSecrets[0].name", "qdrant:imagePullSecrets"},
        "script_flags": {"pipeline.sh --dry-run", "pipeline.sh -h", "retire_legacy.sh --stage",
                         "import-script SHA", "import-script SNEAKERNET_TRUSTED_PUB"},
        "gap_paths": {"gap/patches.md", "gap/config/", ".gitlab/CODEOWNERS"},
        # What the §4 import script reads from the bundle.
        "bundle_layout": {"tarball qdrant-pdf-rag-{IMAGE_SHA}.tar.sha256", "member bootstrap.sh",
                          "member repo.bundle", "member MANIFEST.txt", "manifest sha"},
    }
    for category, items in expected.items():
        assert items <= set(current[category]), category
    assert current.stubs == {}


def test_names_match_as_whole_tokens():
    assert not surface.token("-h", 'echo "--help"')
    assert surface.token("-h", "--help|-h)")
    assert not surface.token("sbom.json", "old-sbom.json.bak")
    assert surface.token("airgap:run", 'echo "use airgap:run" >&2')


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


def removed(category: str, item: str) -> str:
    return surface.REMOVED.format(category=category, item=item)


def note(root: Path, line: str) -> None:
    with (root / surface.RELEASE_NOTES).open("a", encoding="utf-8") as notes:
        notes.write(f"\n## 2026-10-27 GH#1118 Fixture\n\n{line}\n")


def retire(root: Path, key: str, new: str) -> None:
    edit(root / surface.MODEL_CONFIG, "RETIRED_KEYS: tuple[tuple[str, str, str], ...] = (\n",
         f'RETIRED_KEYS: tuple[tuple[str, str, str], ...] = (\n    ("{key}", "0000000", "set {new} and remove it"),\n')


def assert_refused(root: Path, category: str, item: str) -> None:
    snapshot = (root / surface.SNAPSHOT).read_bytes()
    assert removed(category, item) in surface.compare(root)
    assert removed(category, item) in surface.write(root)
    assert (root / surface.SNAPSHOT).read_bytes() == snapshot


def assert_refreshes(root: Path, category: str, item: str) -> None:
    assert removed(category, item) not in surface.compare(root)
    assert surface.write(root) == []
    assert surface.compare(root) == []
    assert item in json.loads((root / surface.SNAPSHOT).read_text())["removed"][category]


def test_removing_a_key_without_a_migration_fails_and_the_refresh_refuses(tree):
    edit(tree / surface.EXAMPLE, example_line(tree, EXAMPLE_KEY), "")

    assert surface.compare(tree) == [removed("airgap_env_keys", EXAMPLE_KEY)]
    assert_refused(tree, "airgap_env_keys", EXAMPLE_KEY)


@pytest.mark.parametrize("half", ["release note only", "refusal only"])
def test_a_rename_needs_both_the_refusal_and_the_release_note(tree, half):
    line = example_line(tree, EXAMPLE_KEY)
    edit(tree / surface.EXAMPLE, line, line.replace(EXAMPLE_KEY, NEW_KEY))
    if half == "release note only":
        note(tree, f"- removed `airgap_env_keys:{EXAMPLE_KEY}`; replaced by `{NEW_KEY}`; "
                   "migration: retired-key in `scripts/airgap/model_config.py`")
    else:
        retire(tree, EXAMPLE_KEY, NEW_KEY)

    assert_refused(tree, "airgap_env_keys", EXAMPLE_KEY)


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
    assert_refreshes(tree, "airgap_env_keys", EXAMPLE_KEY)
    assert NEW_KEY in json.loads((tree / surface.SNAPSHOT).read_text())["current"]["airgap_env_keys"]

    # The migration stays checked after the refresh: dropping the refusal fails.
    edit(tree / surface.MODEL_CONFIG, f'    ("{EXAMPLE_KEY}", "0000000", "set {NEW_KEY} and remove it"),\n', "")
    assert surface.BROKEN.format(category="airgap_env_keys", item=EXAMPLE_KEY,
                                 reason=f"`{EXAMPLE_KEY}` is not in RETIRED_KEYS") in surface.compare(tree)


# B1: text alone never satisfies the migration. The evidence is the owner file,
# names match as tokens, and a refusal names the replacement.
def _drop_skip_load(root: Path) -> None:
    edit(root / PIPELINE, SKIP_LOAD_ARM, "")


def _drop_short_help(root: Path) -> None:
    edit(root / PIPELINE, "--help|-h)", "--help)")


def _drop_sbom(root: Path) -> None:
    edit(root / surface.PACK, "PACKING_RECORD.txt sbom.json THIRD-PARTY-NOTICES.txt sneakernet-signing.pub SHA256SUMS SHA256SUMS.sig",
         "PACKING_RECORD.txt THIRD-PARTY-NOTICES.txt sneakernet-signing.pub SHA256SUMS SHA256SUMS.sig")


def _rename_pipeline_task(root: Path) -> None:
    edit(root / TASKS, "  pipeline:\n", "  run:\n")


@pytest.mark.parametrize(("mutate", "category", "item", "line"), [
    pytest.param(_drop_skip_load, "script_flags", "pipeline.sh --skip-load",
                 "replaced by `--dry-run`; migration: refusal in `docs/release-notes.md`", id="self-cited note"),
    pytest.param(_drop_skip_load, "script_flags", "pipeline.sh --skip-load",
                 "replaced by `--dry-run`; migration: refusal in `scripts/airgap/pipeline.sh`",
                 id="flag mentioned in usage text, no refusing arm"),
    pytest.param(_drop_short_help, "script_flags", "pipeline.sh -h",
                 "replaced by `--help`; migration: refusal in `scripts/airgap/pipeline.sh`", id="-h inside --help"),
    pytest.param(_drop_sbom, "bundle_layout", "member sbom.json",
                 "replaced by `MANIFEST.txt`; migration: refusal in `scripts/airgap/pack.sh`",
                 id="member still mentioned in pack.sh"),
    pytest.param(_drop_sbom, "bundle_layout", "member sbom.json",
                 "replaced by `MANIFEST.txt`; migration: alias in `scripts/airgap/pack.sh`", id="alias kind"),
    pytest.param(_rename_pipeline_task, "task_names", "airgap:pipeline",
                 "replaced by `airgap:run`; migration: refusal in `docs/release-notes.md`", id="task self-cite"),
    pytest.param(_rename_pipeline_task, "task_names", "airgap:pipeline",
                 "replaced by `airgap:run`; migration: refusal in `taskfiles/airgap.yml`", id="task without stub"),
])
def test_a_release_note_alone_is_never_the_migration(tree, mutate, category, item, line):
    mutate(tree)
    note(tree, f"- removed `{category}:{item}`; {line}")

    assert_refused(tree, category, item)


# B3: a refusing stub under the old name is a retirement, not a current item.
# The `{ echo ... >&2; exit N; }` form the taskfiles use (dev.yml, artifacts.yml, local.yml).
FLAG_BRACE_STUB = FLAG_STUB.replace('die "--skip-load was renamed: use --no-load"',
                                    '{ echo "--skip-load was renamed: use --no-load" >&2; exit 2; }')
TASK_BRACE_STUB = TASK_STUB.replace("'echo \"airgap:pipeline was renamed: use airgap:run\" >&2; exit 1'",
                                    "'{ echo \"airgap:pipeline was renamed: use airgap:run\" >&2; exit 2; }'")


@pytest.mark.parametrize(("path", "old", "stub", "category", "item", "replacement"), [
    (PIPELINE, SKIP_LOAD_ARM, FLAG_STUB, "script_flags", "pipeline.sh --skip-load", "--no-load"),
    (TASKS, "  pipeline:\n", TASK_STUB, "task_names", "airgap:pipeline", "airgap:run"),
    (PIPELINE, SKIP_LOAD_ARM, FLAG_BRACE_STUB, "script_flags", "pipeline.sh --skip-load", "--no-load"),
    (TASKS, "  pipeline:\n", TASK_BRACE_STUB, "task_names", "airgap:pipeline", "airgap:run"),
], ids=["flag", "task", "brace-group flag", "brace-group task"])
def test_a_refusing_stub_needs_its_release_note(tree, path, old, stub, category, item, replacement):
    edit(tree / path, old, stub)

    assert removed(category, item) in surface.compare(tree)
    assert_refused(tree, category, item)

    note(tree, f"- removed `{category}:{item}`; replaced by `{replacement}`; migration: refusal in `{path}`")
    assert_refreshes(tree, category, item)


def test_a_stub_that_does_not_name_the_replacement_is_refused(tree):
    edit(tree / PIPELINE, SKIP_LOAD_ARM, FLAG_STUB.replace('use --no-load', 'see the docs'))
    note(tree, f"- removed `script_flags:pipeline.sh --skip-load`; replaced by `--no-load`; "
               f"migration: refusal in `{PIPELINE}`")

    assert_refused(tree, "script_flags", "pipeline.sh --skip-load")


# B2: deploy.sh values with list indices and the null form are part of the surface.
def test_renaming_an_indexed_qdrant_value_is_a_removal(tree):
    edit(tree / surface.DEPLOY, '"imagePullSecrets[0].name=$PULL_SECRET"', '"imagePullSecrets[0].ref=$PULL_SECRET"')

    assert_refused(tree, "chart_values", "qdrant:imagePullSecrets[0].name")


def test_a_values_refusal_is_a_fixed_line_in_the_owner_naming_both_paths(tree):
    edit(tree / surface.DEPLOY, '"imagePullSecrets[0].name=$PULL_SECRET"', '"imagePullSecrets[0].ref=$PULL_SECRET"')
    edit(tree / surface.DEPLOY, "_sizing=$(",
         '[ -z "${QDRANT_VALUES_OLD:-}" ] || die "imagePullSecrets[0].name was renamed: use imagePullSecrets[0].ref"\n'
         "_sizing=$(")
    note(tree, "- removed `chart_values:qdrant:imagePullSecrets[0].name`; replaced by `imagePullSecrets[0].ref`; "
               "migration: refusal in `scripts/airgap/deploy.sh`")

    assert_refreshes(tree, "chart_values", "qdrant:imagePullSecrets[0].name")


def test_brace_group_fixtures_use_the_taskfiles_own_idiom():
    assert "; exit 1; }'" in (ROOT / "taskfiles/dev.yml").read_text(encoding="utf-8")
    assert "{ echo" in FLAG_BRACE_STUB and "{ echo" in TASK_BRACE_STUB


def test_a_conditional_refusal_in_a_real_task_is_not_a_stub(tree):
    edit(tree / TASKS, "        sh scripts/airgap/pipeline.sh\n",
         '        if [ -n "${OLD_PIPELINE:-}" ]; then { echo "use airgap:run" >&2; exit 1; }; fi;\n'
         "        sh scripts/airgap/pipeline.sh\n")

    current = surface.derive(tree)
    assert "airgap:pipeline" in current["task_names"]
    assert current.stubs == {}
    assert surface.compare(tree) == []


def test_a_stub_naming_the_replacement_only_in_a_comment_is_refused(tree):
    edit(tree / PIPELINE, SKIP_LOAD_ARM, "        --no-load)   SKIP_LOAD=1 ;;\n"
         '        --skip-load) die "--skip-load was removed" # use --no-load\n            ;;\n')
    note(tree, f"- removed `script_flags:pipeline.sh --skip-load`; replaced by `--no-load`; "
               f"migration: refusal in `{PIPELINE}`")

    assert ("script_flags", "pipeline.sh --skip-load") in surface.derive(tree).stubs
    assert_refused(tree, "script_flags", "pipeline.sh --skip-load")


# B1: only an executable refusal counts; comments, docstrings and bare strings do not.
GATEWAY = '            "caConfigMapName": env("GATEWAY_CA_CONFIGMAP"),\n'
BUILD = "def build_values(deploy_only: bool = False) -> dict:\n"
FIRST = '    internal_registry = env("INTERNAL_REGISTRY")\n'
MESSAGE = "gateway.caConfigMapName was renamed to gateway.caConfigMap"
GUARD = '    if env("GATEWAY_CA_CONFIGMAP_NAME"):\n'


@pytest.mark.parametrize(("anchor", "evidence", "counts"), [
    pytest.param(BUILD, "    # TODO: raise if gateway.caConfigMapName is set (renamed to gateway.caConfigMap)\n",
                 False, id="TODO comment"),
    pytest.param(BUILD, f'    """raise: {MESSAGE}"""\n', False, id="docstring"),
    pytest.param(FIRST, f'    "raise: {MESSAGE}"\n', False, id="bare string"),
    pytest.param(FIRST, f'    _unused = 0  # die("{MESSAGE}")\n', False, id="trailing comment"),
    pytest.param(FIRST, f'{GUARD}        raise SystemExit("{MESSAGE}")\n', True, id="raise"),
    pytest.param(FIRST, f'{GUARD}        die("{MESSAGE}")\n', True, id="die call"),
])
def test_a_values_refusal_in_map_values_must_be_executable(tree, anchor, evidence, counts):
    edit(tree / surface.MAP_VALUES, GATEWAY, GATEWAY.replace('"caConfigMapName"', '"caConfigMap"'))
    edit(tree / surface.MAP_VALUES, anchor, anchor + evidence)
    note(tree, "- removed `chart_values:mainframe-rag:gateway.caConfigMapName`; replaced by `gateway.caConfigMap`; "
               "migration: refusal in `scripts/airgap/map_values.py`")

    if counts:
        assert_refreshes(tree, "chart_values", "mainframe-rag:gateway.caConfigMapName")
    else:
        assert_refused(tree, "chart_values", "mainframe-rag:gateway.caConfigMapName")


@pytest.mark.parametrize("evidence", [
    '# die "imagePullSecrets[0].name was renamed: use imagePullSecrets[0].ref"\n',
    'true # die "imagePullSecrets[0].name was renamed: use imagePullSecrets[0].ref"\n',
    '[ -z "${OLD:-}" ] || die "renamed" # imagePullSecrets[0].name, use imagePullSecrets[0].ref\n',
], ids=["comment line", "trailing comment", "names only in the comment"])
def test_a_shell_comment_is_not_a_refusal(tree, evidence):
    edit(tree / surface.DEPLOY, '"imagePullSecrets[0].name=$PULL_SECRET"', '"imagePullSecrets[0].ref=$PULL_SECRET"')
    edit(tree / surface.DEPLOY, "_sizing=$(", evidence + "_sizing=$(")
    note(tree, "- removed `chart_values:qdrant:imagePullSecrets[0].name`; replaced by `imagePullSecrets[0].ref`; "
               "migration: refusal in `scripts/airgap/deploy.sh`")

    assert_refused(tree, "chart_values", "qdrant:imagePullSecrets[0].name")
