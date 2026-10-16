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


