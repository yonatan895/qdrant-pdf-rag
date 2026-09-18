"""Command-interface env scoping (issue #402 B5).

Migration map for the retired Make-syntax coupling in this file's history:
- Old `test_makefile_has_no_global_embed_mode_export` -> retained below as
  `test_shim_makefile_exports_nothing`: any `export` in the shim Makefile
  would leak into EVERY forwarded task environment, so the shim must not
  export at all.
- Old `test_makefile_scopes_embed_mode_export_to_eval_family` (which asserted
  the literal `export EMBED_MODE :=` source lines in the Makefile) -> retired
  as obsolete syntax coupling. The protected behavior — hash default scoped
  to eval consumers, explicit overrides preserved, empty preserved, never
  global — is proven at the real runner boundary in
  tests/test_taskfile_contracts.py (mode/venue defaults, CLI/env forms,
  empty handling, golden-flag derivation, no-mode leak checks on bench,
  verify and load). What remains here pins the new locus: per-task
  EMBED_MODE bridges in taskfiles/eval.yml, with the documented exceptions.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Eval tasks that intentionally carry no mode exports (same as Make).
MODELESS_EVAL_TASKS = {"verify-golden", "bench", "bench-baseline", "load"}


def _non_comment_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def test_shim_makefile_exports_nothing() -> None:
    lines = _non_comment_lines(REPO / "Makefile")
    exported = [ln for ln in lines if re.match(r"export\s+\w", ln.strip())]
    assert exported == [], (
        "the Make->Task shim must not export anything: an exported variable "
        f"would leak into every forwarded task environment: {exported}"
    )


def _eval_task_blocks() -> dict[str, str]:
    text = (REPO / "taskfiles/eval.yml").read_text(encoding="utf-8")
    blocks: dict[str, str] = {}
    current: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        header = re.match(r"  ([A-Za-z0-9_:.-]+):\s*$", line)
        if header:
            if current is not None:
                blocks[current] = "\n".join(body)
            current, body = header.group(1), []
        elif current is not None and not line.lstrip().startswith("#"):
            body.append(line)
    if current is not None:
        blocks[current] = "\n".join(body)
    return blocks


def test_eval_tasks_scope_embed_mode_per_task() -> None:
    blocks = _eval_task_blocks()
    assert len(blocks) >= 20, f"eval module unexpectedly small: {sorted(blocks)}"
    for name, body in blocks.items():
        if name in MODELESS_EVAL_TASKS:
            assert "EMBED_MODE" not in body, (
                f"eval:{name} must carry no mode exports (same as Make)"
            )
        else:
            assert "EMBED_MODE: '{{.EMBED_MODE}}'" in body, (
                f"eval:{name} must bridge EMBED_MODE under its exact script-read name"
            )
