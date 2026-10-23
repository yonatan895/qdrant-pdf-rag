#!/usr/bin/env python3
"""Entry for the release-set authoring kit (#367); see docs/eval.md section 11."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SOURCE = _ROOT / "src"
if str(_SOURCE) not in sys.path:
    sys.path.insert(0, str(_SOURCE))

from mainframe_rag.eval.release_kit import main

if __name__ == "__main__":
    raise SystemExit(main(workspace_root=_ROOT))
