"""Registered pure CPU arithmetic. Numerical results confer no product authority."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Context, Decimal, localcontext
from fractions import Fraction
from typing import Literal, cast

from scipy import special  # type: ignore[import-untyped]

from opentour_training.evaluation.inference_plan import (
    MAX_UNITS,
    InferencePlan,
    InvalidKernelInput,
    Method,
    MetricRole,
    NumericalComputationError,
    UnsupportedNumericProfile,
    _check_profile,
    _select_claim,
    numeric_alpha,
    strict_count,
    strict_id,
    strict_number,
    validate_denominators,
    validate_pair_cells,
)

UndefinedReason = Literal["no_gradeable_units", "singleton", "incomplete_planned_look"]


@dataclass(frozen=True)
class IntervalEstimate:
    """Internal numerical value, not MetricResult, a receipt, or a product decision."""

    method: Method
    value: float | None
    lower: float | None
    upper: float | None
    undefined_reason: UndefinedReason | None = None


def _undefined(method: Method, reason: UndefinedReason) -> IntervalEstimate:
    return IntervalEstimate(method, None, None, None, reason)


def _binomial_inputs(k: object, n: object, alpha: object) -> tuple[int, int, Fraction]:
    errors, count = strict_count(k), strict_count(n)
    if errors > count:
        raise InvalidKernelInput()
    selected_alpha = numeric_alpha(alpha)
    if count > MAX_UNITS:
        raise UnsupportedNumericProfile()
    return errors, count, selected_alpha


def _edge_bound(n: int, alpha: Fraction, *, upper: bool) -> float:
    # At the profile's minimum distance from an endpoint (about 1e-18), this
    # precision keeps ample digits for 1 - exp and outward binary64 conversion.
    with localcontext(Context(prec=80)):
        exact = Decimal(alpha.numerator) / Decimal(alpha.denominator)
        root = (exact.ln() / n).exp()
        bound = 1 - root if upper else root
    return math.nextafter(float(bound), 1.0 if upper else 0.0)


def _binomial_tail(n: int, a: int, x: float, *, increasing: bool) -> float:
    """Independently verify a beta root using the binomial tail starting at its mode."""
    if x in (0.0, 1.0):
        return float(((n if x == 1.0 else 0) >= a) == increasing)
    with localcontext(Context(prec=80)):
        p = Decimal.from_float(x)
        q = 1 - p
        mode = int((n + 1) * p)
        total = Decimal(1)
        included = Decimal(int((mode >= a) == increasing))
        cutoff = Decimal("1e-40")
        for direction, odds in ((-1, q / p), (1, p / q)):
            i = mode
            weight = Decimal(1)
            terminal = 0 if direction == -1 else n
            while i != terminal:
                ratio = odds * (i if direction == -1 else n - i)
                weight *= ratio / (n - i + 1 if direction == -1 else i + 1)
                i += direction
                # Each direction is nonincreasing from the mode. At most
                # n+1 skipped weights below cutoff gives <1.1e-34 missing mass.
                if weight < cutoff:
                    break
                total += weight
                if (i >= a) == increasing:
                    included += weight
        return float(included / total)


def _checked_bound(k: int, n: int, alpha: Fraction, *, upper: bool) -> float:
    if upper and k == n:
        return 1.0
    if not upper and k == 0:
        return 0.0
    try:
        if (upper and k == 0) or (not upper and k == n):
            return _edge_bound(n, alpha, upper=upper)

        # Invert and check the smaller tail directly; never subtract a rounded
        # probability from one. Complementation here is exact rational arithmetic.
        use_complement = alpha > Fraction(1, 2)
        target = float(1 - alpha if use_complement else alpha)
        increasing_tail = upper == use_complement
        inverse = special.betaincinv if increasing_tail else special.betainccinv
        a, b = (k + 1, n - k) if upper else (k, n - k + 1)
        root = float(inverse(a, b, target))
        if not math.isfinite(root) or not 0 <= root <= 1:
            raise NumericalComputationError()

        def residual(bound: float) -> float:
            probability = _binomial_tail(n, a, bound, increasing=increasing_tail)
            return probability - target if use_complement else target - probability

        # A positive residual is outward. Require a margin over numerical noise
        # and verify closeness, or locate an outward neighbour of the transition.
        direction = 1.0 if upper else 0.0
        guard = 8 * math.ulp(target)
        tolerance = 1e-7 * target
        previous_steps = 0
        for exponent in range(21):
            steps = 2**exponent
            value = math.nextafter(root, direction, steps=steps)
            difference = residual(value)
            if difference >= guard:
                if difference <= tolerance:
                    return value
                while steps - previous_steps > 1:
                    mid = (steps + previous_steps) // 2
                    candidate = math.nextafter(root, direction, steps=mid)
                    if residual(candidate) >= guard:
                        steps, value = mid, candidate
                    else:
                        previous_steps = mid
                inward = math.nextafter(value, 0.0 if upper else 1.0)
                if residual(inward) < guard:
                    return value
                raise NumericalComputationError()
            if value == direction:
                break
            previous_steps = steps
    except Exception:
        raise NumericalComputationError() from None
    raise NumericalComputationError()


def binomial_upper_bound(k: int, n: int, alpha: float) -> float | None:
    """Upper one-sided critical-proportion bound; n=0 has no measured estimate."""
    errors, count, tail = _binomial_inputs(k, n, alpha)
    return None if count == 0 else _checked_bound(errors, count, tail, upper=True)


def binomial_lower_bound(k: int, n: int, alpha: float) -> float | None:
    """Separate lower one-sided bound; never combine with upper as a joint CI."""
    errors, count, tail = _binomial_inputs(k, n, alpha)
    return None if count == 0 else _checked_bound(errors, count, tail, upper=False)


def _sides(value: object) -> Literal[1, 2]:
    if type(value) is not int or value not in (1, 2):
        raise InvalidKernelInput()
    return cast(Literal[1, 2], value)


def _outward(value: float, *, lower: bool) -> float:
    if not math.isfinite(value):
        raise NumericalComputationError()
    return max(-1.0, min(1.0, math.nextafter(value, -math.inf if lower else math.inf)))


def paired_binary_interval(
    b: int,
    c: int,
    both_correct: int,
    both_wrong: int,
    alpha: float,
    sides: Literal[1, 2],
) -> IntervalEstimate:
    cells = validate_pair_cells(
        {"b": b, "c": c, "both_correct": both_correct, "both_wrong": both_wrong}
    )
    side_count = _sides(sides)
    return _paired_interval(cells, numeric_alpha(alpha), side_count)


def _paired_interval(
    cells: tuple[int, int, int, int], alpha: Fraction, sides: Literal[1, 2]
) -> IntervalEstimate:
    b, c, _, _ = cells
    n = sum(cells)
    method: Method = "paired_binary_exact_marginals_v1"
    if n > MAX_UNITS:
        raise UnsupportedNumericProfile()
    if n < 2:
        return _undefined(method, "no_gradeable_units" if n == 0 else "singleton")
    tail = alpha / (2 if sides == 1 else 4)
    lower = _checked_bound(b, n, tail, upper=False) - _checked_bound(c, n, tail, upper=True)
    upper = None
    if sides == 2:
        upper = _outward(
            _checked_bound(b, n, tail, upper=True) - _checked_bound(c, n, tail, upper=False),
            lower=False,
        )
    return IntervalEstimate(method, (b - c) / n, _outward(lower, lower=True), upper)


def _family_sequence(family_effects: object) -> list[object] | tuple[object, ...]:
    if type(family_effects) not in (tuple, list):
        raise InvalidKernelInput()
    return cast(list[object] | tuple[object, ...], family_effects)


def _family_values(supplied: list[object] | tuple[object, ...]) -> list[float]:
    if len(supplied) > MAX_UNITS:
        # No sampling or truncation; unexamined content is not claimed schema-valid.
        raise UnsupportedNumericProfile()
    seen: set[str] = set()
    values: list[float] = []
    for item in tuple(supplied):
        if type(item) not in (tuple, list) or len(cast(list[object], item)) != 2:
            raise InvalidKernelInput()
        identifier, raw = cast(tuple[object, object], item)
        name = strict_id(identifier)
        effect = strict_number(raw)
        if name in seen or not -1 <= effect <= 1:
            raise InvalidKernelInput()
        seen.add(name)
        values.append(effect)
    return values


def independent_family_interval(
    family_effects: object, alpha: float, sides: Literal[1, 2]
) -> IntervalEstimate:
    side_count = _sides(sides)
    return _family_interval(
        _family_values(_family_sequence(family_effects)), numeric_alpha(alpha), side_count
    )


def _family_interval(
    values: list[float], alpha: Fraction, sides: Literal[1, 2]
) -> IntervalEstimate:
    method: Method = "bounded_independent_family_hoeffding_v1"
    count = len(values)
    if count < 2:
        return _undefined(method, "no_gradeable_units" if count == 0 else "singleton")
    try:
        total = math.fsum(values)
        with localcontext(Context(prec=80)):
            ratio = Decimal(sides * alpha.denominator) / Decimal(alpha.numerator)
            radius = (2 * ratio.ln() / count).sqrt()
        radius_hi = math.nextafter(float(radius), math.inf)
        # Bound the rounded sum and division before subtraction. Widening only
        # the result cannot repair loss at a nearly cancelling mean and radius.
        mean_lo = math.nextafter(math.nextafter(total, -math.inf) / count, -math.inf)
        mean_hi = math.nextafter(math.nextafter(total, math.inf) / count, math.inf)
        return IntervalEstimate(
            method,
            total / count,
            _outward(mean_lo - radius_hi, lower=True),
            None if sides == 1 else _outward(mean_hi + radius_hi, lower=False),
        )
    except Exception:
        raise NumericalComputationError() from None


def compute_interval(
    plan: InferencePlan,
    denominators: object,
    selected_look_id: str,
    selected_gate_id: str,
    *,
    metric_role: MetricRole,
    pair_cells: object = None,
    family_effects: object = None,
) -> IntervalEstimate:
    selected = _select_claim(plan, selected_look_id, selected_gate_id, metric_role=metric_role)
    method = selected.method
    supplied: list[object] | tuple[object, ...] | None = None
    cells: tuple[int, int, int, int] | None = None
    if method == "paired_binary_exact_marginals_v1":
        if pair_cells is None or family_effects is not None:
            raise InvalidKernelInput()
        counts = validate_denominators(denominators)
        cells = validate_pair_cells(pair_cells)
        units = sum(cells)
        if units != counts["paired_gradeable"]:
            raise InvalidKernelInput()
    else:
        if family_effects is None or pair_cells is not None:
            raise InvalidKernelInput()
        counts = validate_denominators(denominators)
        supplied = _family_sequence(family_effects)
        units = len(supplied)
        if units > counts["paired_gradeable"]:
            raise InvalidKernelInput()
    if counts["independent_units"] != units or units > selected.independent_units:
        raise InvalidKernelInput()
    families = None if supplied is None else _family_values(supplied)
    if families is not None and len(families) != units:
        raise InvalidKernelInput()
    _check_profile(plan, selected)
    if units < 2:
        return _undefined(method, "no_gradeable_units" if units == 0 else "singleton")
    if units < selected.independent_units:
        return _undefined(method, "incomplete_planned_look")
    if cells is not None:
        return _paired_interval(cells, selected.alpha, selected.sides)
    assert families is not None
    return _family_interval(families, selected.alpha, selected.sides)
