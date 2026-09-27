#!/usr/bin/env python3
"""Compatibility exports for the seeded evaluation statistics owner.

Retirement requires migrated callers and a maintainer compatibility decision.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[1] / "src"
if str(_SOURCE) not in sys.path:
    sys.path.insert(0, str(_SOURCE))

from mainframe_rag.eval.statistics import (
    DEFAULT_ALPHA,
    DEFAULT_RESAMPLES,
    DEFAULT_SEED,
    _bootstrap_stat,
    _mean,
    _percentile,
    ci95,
    ci95_paired,
    ci_excludes_zero,
)

__all__ = ['DEFAULT_ALPHA', 'DEFAULT_RESAMPLES', 'DEFAULT_SEED', '_bootstrap_stat', '_mean', '_percentile', 'ci95', 'ci95_paired', 'ci_excludes_zero']
