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


def test_no_litellm_anywhere():
    """LiteLLM was a phantom dependency: pinned in the lockfile and baked into
    the images while every src module used plain OpenAI-compatible httpx. This
    repo does not install LiteLLM (AGENTS.md) — no imports, no pyproject
    dependency, no lock pin."""
    import ast
    import re
    import tomllib

    scanned = [*(ROOT / "src").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]
    bad_files = []
    for p in scanned:
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            imported = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            )
            if any(name.split(".")[0] == "litellm" for name in imported):
                bad_files.append(str(p.relative_to(ROOT)))
    assert bad_files == [], f"litellm must not be imported: {bad_files}"

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = [
        dep
        for dep in pyproject["project"]["dependencies"]
        if re.split(r"[<>=!\[]", dep.strip())[0].strip().lower() == "litellm"
    ]
    assert declared == [], f"litellm must not return to pyproject dependencies: {declared}"

    lock = (ROOT / "requirements.lock.txt").read_text(encoding="utf-8")
    pins = [
        line for line in lock.splitlines() if line.strip().lower().startswith("litellm")
    ]
    assert pins == [], f"litellm must not return to the lockfile: {pins}"
