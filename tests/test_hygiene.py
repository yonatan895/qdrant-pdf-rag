import ast
import re
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


def test_no_litellm_anywhere():
    """LiteLLM was a phantom dependency: pinned in the lockfile and baked into
    the images while every src module used plain OpenAI-compatible httpx2. This
    repo does not install LiteLLM (AGENTS.md) — no imports (src, scripts, tests),
    no pyproject dependency or extra, no lock pin."""
    scanned = [
        *(ROOT / "src").rglob("*.py"),
        *(ROOT / "scripts").rglob("*.py"),
        *(ROOT / "tests").rglob("*.py"),
    ]
    bad_files = _litellm_import_files(scanned, ROOT)
    assert bad_files == [], f"litellm must not be imported: {bad_files}"

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


def test_litellm_import_guard_flags_synthetic_files(tmp_path):
    clean = tmp_path / "clean.py"
    clean.write_text("import httpx2\n", encoding="utf-8")
    mention = tmp_path / "mention.py"
    mention.write_text('BASE_URL = "http://litellm:4000/v1"\n', encoding="utf-8")
    plain = tmp_path / "plain.py"
    plain.write_text("import litellm\n", encoding="utf-8")
    from_import = tmp_path / "from_import.py"
    from_import.write_text("from litellm import Router\n", encoding="utf-8")
    nested = tmp_path / "nested.py"
    nested.write_text("def f():\n    import litellm.router\n", encoding="utf-8")

    flagged = _litellm_import_files(
        [clean, mention, plain, from_import, nested], tmp_path
    )

    assert flagged == ["plain.py", "from_import.py", "nested.py"]


def test_litellm_declaration_guard_flags_specifiers():
    assert _declares_litellm(["litellm"]) == ["litellm"]
    assert _declares_litellm(["LiteLLM[proxy]>=1.0", "litellm==1.98.0"]) == [
        "LiteLLM[proxy]>=1.0",
        "litellm==1.98.0",
    ]
    assert _declares_litellm(["openai", "httpx2", "litellm-proxy"]) == []
