#!/usr/bin/env python3
"""Real-corpus venue declaration: dev by default, RC only when declared.

The frozen holdout (``evals/holdout.jsonl``) and the real-manual corpora
are release-candidate instruments. Reading them during dev iteration
silently turns the holdout into a tuning set, so the default venue is
``dev`` and RC work must declare itself with ``VENUE=rc`` (set by the
``make eval-holdout`` recipe and by operators running the harness tiers).

One rule, one helper: every eval/harness entry point calls
``resolve_golden_paths`` / ``require_rc_for_collection`` instead of
hardcoding the holdout or re-deriving the rule (issue #268).

Pure module: no live imports, no I/O beyond ``os.environ`` — hermetic
tests drive every branch in ``tests/test_venue.py``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEV_GOLDEN_PATH = REPO / "evals" / "golden.jsonl"
HOLDOUT_PATH = REPO / "evals" / "holdout.jsonl"

# Collections that only exist in the real-corpus RC venue. Everything else
# (synthetic dev collections, scratch, gate-l1-*) is dev.
RC_ONLY_COLLECTIONS = frozenset({"real_manuals"})

VENUE_ENV = "VENUE"
DEV = "dev"
RC = "rc"


class VenueError(RuntimeError):
    """An RC-only instrument was requested without a declared RC venue."""


def resolve_venue(environ: Mapping[str, str] | None = None) -> str:
    """VENUE env: unset/blank -> dev; only dev/rc accepted (a typo fails
    closed rather than silently selecting the wrong instrument)."""
    env = os.environ if environ is None else environ
    raw = str(env.get(VENUE_ENV, "")).strip().lower()
    if not raw:
        return DEV
    if raw not in (DEV, RC):
        raise VenueError(f"{VENUE_ENV}={raw!r} is not a venue; use {DEV!r} or {RC!r}")
    return raw


def _is_holdout(path: Path | str) -> bool:
    try:
        return Path(path).resolve() == HOLDOUT_PATH
    except OSError:  # pragma: no cover — resolve() on a str never raises
        return False


def require_rc_for_golden(paths: Sequence[Path | str], venue: str | None = None) -> None:
    """Refuse the frozen holdout unless the venue is declared RC."""
    if (venue or resolve_venue()) == RC:
        return
    for path in paths:
        if _is_holdout(path):
            raise VenueError(
                f"frozen holdout {HOLDOUT_PATH.name} requires {VENUE_ENV}={RC}; "
                "dev runs tune against evals/golden.jsonl only"
            )


def require_rc_for_collection(collection: str, venue: str | None = None) -> None:
    """Refuse the real-corpus collection unless the venue is declared RC."""
    if (venue or resolve_venue()) == RC:
        return
    if collection in RC_ONLY_COLLECTIONS:
        raise VenueError(
            f"collection {collection!r} is the real-corpus RC venue; "
            f"set {VENUE_ENV}={RC} to evaluate it"
        )


def resolve_golden_paths(
    explicit: Sequence[Path | str] | None = None,
    venue: str | None = None,
) -> list[Path]:
    """Golden paths for one run: dev defaults to the golden set only; the
    frozen holdout joins the run only under ``VENUE=rc``. Explicit paths are
    honored but still guarded (an explicit holdout path in dev fails)."""
    venue = venue or resolve_venue()
    if explicit:
        paths = [Path(p) for p in explicit]
    else:
        paths = [DEV_GOLDEN_PATH]
        if venue == RC:
            paths.append(HOLDOUT_PATH)
    require_rc_for_golden(paths, venue)
    return paths
