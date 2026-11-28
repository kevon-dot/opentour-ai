"""Exact-bound arithmetic for the audit. Reuses the registered statistics kernel."""

from __future__ import annotations

import math
from collections.abc import Sequence

from opentour_training.evaluation.statistics_kernel import binomial_upper_bound


def zero_error_upper_bound(n: int, alpha: float = 0.05) -> float:
    """One-sided exact bound for zero errors in n independent units: 1 - alpha**(1/n)."""
    if n < 1:
        raise ValueError("n must be positive")
    return float(1.0 - alpha ** (1.0 / n))


def upper_error(errors: int, n: int, alpha: float) -> float | None:
    """One-sided Clopper-Pearson upper bound; None when nothing was verified (n == 0)."""
    return binomial_upper_bound(errors, n, alpha)


def families_needed_for_ceiling(ceiling: float, alpha: float) -> int:
    """Smallest n whose zero-error exact upper bound is at or below the ceiling."""
    n = max(1, math.ceil(math.log(alpha) / math.log(1.0 - ceiling)) - 2)
    while zero_error_upper_bound(n, alpha) > ceiling:
        n += 1
    return n


def design_weighted_error(
    errors: Sequence[bool], probabilities: Sequence[float | None]
) -> float | None:
    """Horvitz-Thompson ratio estimate; unavailable unless every inclusion probability is known."""
    if not errors or len(errors) != len(probabilities):
        return None
    weights: list[float] = []
    for probability in probabilities:
        if probability is None or not 0.0 < probability <= 1.0:
            return None
        weights.append(1.0 / probability)
    return sum(w for w, e in zip(weights, errors, strict=True) if e) / sum(weights)
