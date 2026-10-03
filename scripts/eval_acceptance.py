#!/usr/bin/env python3
"""Entry for independent release-set validation and pre-registered scoring (#367)."""
from __future__ import annotations

import sys
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[1] / "src"
if str(_SOURCE) not in sys.path:
    sys.path.insert(0, str(_SOURCE))

from mainframe_rag.eval.acceptance import main

if __name__ == "__main__":
    raise SystemExit(main())
