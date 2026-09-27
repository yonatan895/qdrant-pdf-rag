#!/usr/bin/env python3
"""Compatibility entry for canonical evaluation and benchmark report rendering."""
from __future__ import annotations

import sys
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[1] / "src"
if str(_SOURCE) not in sys.path:
    sys.path.insert(0, str(_SOURCE))

from mainframe_rag.eval.reports import (  # noqa: F401 — compatibility exports
    BASE_HTML_STYLE,
    _diff_badge,
    _get,
    _html_esc,
    _load_json,
    _md_esc,
    compare_bench,
    compare_eval,
    main,
    render_bench,
    render_eval,
)

if __name__ == "__main__":
    sys.exit(main())
