import ast
import os
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".venv", "venv", ".git", "output", "wheelhouse", "bundles"}


def test_repo_contains_no_pdf_or_adobe_catalogs():
    bad = []
    for p in ROOT.rglob("*"):
        if any(part in SKIP for part in p.parts):
            continue
        if p.suffix.lower() in {".pdf", ".pdx", ".idx"}:
            bad.append(p.relative_to(ROOT).as_posix())
    assert bad == [], f"binaries must not be committed: {bad}"


def _litellm_import_files(paths: list[Path], root: Path) -> list[str]:
    """Root-relative paths of files importing litellm (strings/URLs do not count)."""
    bad = []
    for p in paths:
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            imported = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            )
            if any(name.split(".")[0] == "litellm" for name in imported):
                bad.append(str(p.relative_to(root)))
                break
    return bad


def _declares_litellm(deps: list[str]) -> list[str]:
    """Dep specifiers that name litellm, in the given order."""
    return [
        dep
        for dep in deps
        if re.split(r"[<>=!\[]", dep.strip())[0].strip().lower() == "litellm"
    ]


def test_litellm_is_only_imported_by_the_gateway_only_hooks():
    """LiteLLM was a phantom dependency: pinned in the lockfile and baked into
    the images while every src module used plain OpenAI-compatible httpx2.
    Only the gateway-only finish adapter, model-scope callback and trace-privacy
    exporter (#636) may import it; their directory is excluded from product
    images. No product dependency or lock pin."""
    scanned = [
        *(ROOT / "src").rglob("*.py"),
        *(ROOT / "scripts").rglob("*.py"),
        *(ROOT / "tests").rglob("*.py"),
    ]
    bad_files = _litellm_import_files(scanned, ROOT)
    assert sorted(bad_files) == ['scripts/gateway/scoped_passthrough.py',
                                 'scripts/gateway/strict_finish.py',
                                 'scripts/gateway/trace_privacy.py'], f"unexpected LiteLLM imports: {bad_files}"
    assert 'scripts/gateway' in (ROOT / '.dockerignore').read_text().splitlines()

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    declared = _declares_litellm(project["dependencies"])
    assert declared == [], f"litellm must not return to pyproject dependencies: {declared}"

    bad_extras = {
        name: found
        for name, deps in project.get("optional-dependencies", {}).items()
        if (found := _declares_litellm(deps))
    }
    assert bad_extras == {}, f"litellm must not return to pyproject extras: {bad_extras}"

    lock = (ROOT / "requirements.lock.txt").read_text(encoding="utf-8")
    pins = [
        line for line in lock.splitlines() if line.strip().lower().startswith("litellm")
    ]
    assert pins == [], f"litellm must not return to the lockfile: {pins}"




def _site_layer_paths_on_github(root: Path, env: dict[str, str]) -> list[str]:
    """ADR-0009 site-layer paths present in a GitHub Actions checkout.

    gap/ and .gitlab/ belong only to the air-gapped GitLab fork, which tracks
    them and never runs GitHub Actions; upstream must never track them.
    """
    if env.get("GITHUB_ACTIONS") != "true":
        return []
    return [name for name in ("gap", ".gitlab") if (root / name).exists()]


def test_github_never_tracks_the_gap_site_layer(tmp_path):
    assert _site_layer_paths_on_github(ROOT, dict(os.environ)) == []
    (tmp_path / "gap").mkdir()
    (tmp_path / ".gitlab").mkdir()
    assert _site_layer_paths_on_github(tmp_path, {"GITHUB_ACTIONS": "true"}) == ["gap", ".gitlab"]
    assert _site_layer_paths_on_github(tmp_path, {}) == []


def _exe_mode_violations(root: Path) -> list[str]:
    """Ruff EXE001/EXE002 from the git index: a '#!' file must be 100755 and a
    100755 file must start with '#!'. Scope is the CI ruff scope (python files
    under src/ and tests/ plus CI_LINT_SCRIPTS). The mode comes from the index,
    not the working tree, because ruff skips its EXE checks on WSL/Windows."""
    from scripts.local_verify import CI_LINT_SCRIPTS

    out = subprocess.run(
        ["git", "ls-files", "-s", "-z"], cwd=root, check=True, capture_output=True
    ).stdout.decode()
    bad = []
    for entry in filter(None, out.split("\0")):
        meta, path = entry.split("\t", 1)
        mode = meta.split()[0]
        if mode not in {"100644", "100755"} or not path.endswith(".py"):
            continue
        if not (path.startswith(("src/", "tests/")) or path in CI_LINT_SCRIPTS):
            continue
        if not (root / path).is_file():
            continue
        shebang = (root / path).read_bytes().startswith(b"#!")
        if shebang and mode != "100755":
            bad.append(f"{path}: shebang but mode {mode} (EXE001)")
        elif mode == "100755" and not shebang:
            bad.append(f"{path}: mode 100755 without shebang (EXE002)")
    return bad


def test_ci_lint_scope_shebang_and_exec_bit_agree_in_the_git_index():
    assert _exe_mode_violations(ROOT) == []


def test_exe_mode_check_flags_both_directions_in_a_fixture_repo(tmp_path):
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts" / "review_tooling.py").write_text("#!/usr/bin/env python3\nx = 1\n")
    (tmp_path / "tests" / "test_plain.py").write_text("x = 1\n")
    (tmp_path / "tests" / "test_ok.py").write_text("#!/usr/bin/env python3\n")
    (tmp_path / "scripts" / "outside_scope.py").write_text("#!/usr/bin/env python3\n")
    git("add", ".")
    # Index modes are set explicitly: the working tree's exec bits are irrelevant.
    git("update-index", "--chmod=-x", "scripts/review_tooling.py")
    git("update-index", "--chmod=+x", "tests/test_plain.py", "tests/test_ok.py")
    assert _exe_mode_violations(tmp_path) == [
        "scripts/review_tooling.py: shebang but mode 100644 (EXE001)",
        "tests/test_plain.py: mode 100755 without shebang (EXE002)",
    ]
