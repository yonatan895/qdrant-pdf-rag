#!/usr/bin/env python3
"""Seeded percentile-bootstrap confidence intervals for the harness gate.

Pure stdlib (no numpy — the repo does not carry it): resampling with
random.Random(seed) keeps CIs reproducible run-to-run, which the promotion
gate depends on. Two shapes:

    ci95(values)          -> CI of a single metric's mean
    ci95_paired(pairs)    -> CI of the mean of paired differences
                             (candidate - baseline, joined by entry id)

Why paired deltas: retrieval is deterministic against the pinned snapshot
(same index state + same queries -> same hits), so per-entry differences
candidate - baseline measure the change itself, not run-to-run noise. The
bootstrap resamples entries, not queries, so correlated per-entry shifts
(e.g. one class improving wholesale) move the CI honestly.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

DEFAULT_RESAMPLES = 2000
DEFAULT_SEED = 0
DEFAULT_ALPHA = 0.05


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated percentile of pre-sorted values."""
    if not sorted_values:
        raise ValueError("percentile of empty sequence")
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q / 100.0 * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def _bootstrap_stat(
    values: Sequence[float], stat, resamples: int, seed: int, alpha: float
) -> tuple[float, float]:
    if not values:
        raise ValueError("bootstrap of empty sequence")
    rng = random.Random(seed)
    stats: list[float] = []
    n = len(values)
    for _ in range(resamples):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        stats.append(stat(sample))
    stats.sort()
    lo = _percentile(stats, 100.0 * (alpha / 2.0))
    hi = _percentile(stats, 100.0 * (1.0 - alpha / 2.0))
    return lo, hi


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def ci95(
    values: Sequence[float],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
    alpha: float = DEFAULT_ALPHA,
) -> tuple[float, float] | None:
    """95% percentile-bootstrap CI of the mean. None for empty input (the
    caller renders 'n/a' — an empty class must not fabricate a CI)."""
    if not values:
        return None
    return _bootstrap_stat(list(values), _mean, resamples, seed, alpha)


def ci95_paired(
    pairs: Sequence[tuple[float, float]],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
    alpha: float = DEFAULT_ALPHA,
) -> tuple[float, float] | None:
    """95% CI of mean(candidate - baseline) over paired per-entry values.
    None for empty input."""
    if not pairs:
        return None
    deltas = [c - b for c, b in pairs]
    return _bootstrap_stat(deltas, _mean, resamples, seed, alpha)


def ci_excludes_zero(ci: tuple[float, float], *, improvement: bool) -> bool:
    """True when the CI clears zero on the improvement side.

    improvement=True means larger is better: the CI's LOWER bound must be
    > 0. improvement=False (lower is better): the UPPER bound must be < 0.
    A CI straddling zero is 'within noise' — the gate does not merge on it."""
    lo, hi = ci
    return lo > 0.0 if improvement else hi < 0.0
