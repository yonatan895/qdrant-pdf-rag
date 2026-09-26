#!/usr/bin/env python3
"""Real-corpus venue declaration: dev by default, RC only when declared.

Compatibility delegate (issue #508 C2): the canonical implementation lives
in :mod:`mainframe_rag.eval.datasets`. This module re-exports the same
classes/functions (not a copy) so existing ``from venue import ...`` and
``from scripts.venue import ...`` callers keep working during migration.

Retirement condition: all known callers import
``mainframe_rag.eval.datasets`` directly, the successor is documented and
qualified, and the maintainer approves removing this shim. Unknown external
usage is not deletion authority.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from mainframe_rag.eval.datasets import (  # noqa: E402
    DEV,
    DEV_GOLDEN_PATH,
    HOLDOUT_FILENAME,
    HOLDOUT_PATH,
    RC,
    RC_ONLY_COLLECTIONS,
    VENUE_ENV,
    VenueError,
    _is_holdout,
    require_rc_for_collection,
    require_rc_for_golden,
    resolve_golden_paths,
    resolve_venue,
)

__all__ = [
    "DEV",
    "DEV_GOLDEN_PATH",
    "HOLDOUT_FILENAME",
    "HOLDOUT_PATH",
    "RC",
    "RC_ONLY_COLLECTIONS",
    "VENUE_ENV",
    "VenueError",
    "require_rc_for_collection",
    "require_rc_for_golden",
    "resolve_golden_paths",
    "resolve_venue",
]
