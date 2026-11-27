"""Compare registered fit intervals on one matched measurement scope."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from statistics import median
from typing import Literal

from opentour_cell.pilot.intake_mcp import require_ref

Arm = Literal["baseline", "candidate"]
FitState = Literal["completed", "timeout", "oom", "missing_examples", "cap_exceeded"]
_STATES = {"completed", "timeout", "oom", "missing_examples", "cap_exceeded"}
_BPS = 10_000


@dataclass(frozen=True)
class FitScope:
    """Identity pinned before timing. Keep cold and warm runs in separate plans."""

    model_revision: str
    training_revision: str
    update_budget_revision: str
    hardware_revision: str
    runtime_revision: str
    clock_ref: str
    cache: Literal["cold", "warm"]


@dataclass(frozen=True)
class RegisteredPair:
    reference: str
    first: Arm


@dataclass(frozen=True)
class FitTimingPlan:
    scope: FitScope
    pairs: tuple[RegisteredPair, ...]
    baseline_recipe: str
    candidate_recipe: str
    registered_tick_ms: int
    minimum_median_reduction_bps: int
    maximum_total_ratio_bps: int
    maximum_worst_pair_ratio_bps: int


@dataclass(frozen=True)
class FitInterval:
    receipt_ref: str
    scope: FitScope
    recipe: str
    pair_ref: str
    arm: Arm
    stage: Literal["fit", "preparation", "replay", "verification", "end_to_end"]
    started_ms: int
    finished_ms: int
    state: FitState


@dataclass(frozen=True)
class PairTiming:
    pair_ref: str
    baseline_ms: int
    candidate_ms: int
    baseline_state: FitState
    candidate_state: FitState


@dataclass(frozen=True)
class FitTimingComparison:
    registered_count: int
    pairs: tuple[PairTiming, ...]
    hold_reason: str | None
    median_paired_reduction: Fraction | None = None
    total_ratio: Fraction | None = None
    worst_pair_ratio: Fraction | None = None
    timing_targets_met: bool = False


def compare_fit_intervals(
    plan: FitTimingPlan, intervals: Sequence[FitInterval]
) -> FitTimingComparison:
    """Calculate fit-only timing gates from independently verified interval receipts."""
    pair_refs = {pair.reference for pair in plan.pairs}
    if (
        not 5 <= len(pair_refs) == len(plan.pairs) <= 100
        or plan.baseline_recipe == plan.candidate_recipe
        or plan.scope.cache not in {"cold", "warm"}
        or type(plan.registered_tick_ms) is not int
        or plan.registered_tick_ms < 0
        or type(plan.minimum_median_reduction_bps) is not int
        or not 0 <= plan.minimum_median_reduction_bps <= _BPS
        or type(plan.maximum_total_ratio_bps) is not int
        or plan.maximum_total_ratio_bps <= 0
        or type(plan.maximum_worst_pair_ratio_bps) is not int
        or plan.maximum_worst_pair_ratio_bps <= 0
    ):
        raise ValueError("fit_timing_plan_invalid")
    for value in (
        plan.scope.model_revision,
        plan.scope.training_revision,
        plan.scope.update_budget_revision,
        plan.scope.hardware_revision,
        plan.scope.runtime_revision,
        plan.scope.clock_ref,
        plan.baseline_recipe,
        plan.candidate_recipe,
        *pair_refs,
    ):
        require_ref(value)
    first = plan.pairs[0].first
    second = "candidate" if first == "baseline" else "baseline"
    if first not in {"baseline", "candidate"} or any(
        pair.first != (first if index % 2 == 0 else second) for index, pair in enumerate(plan.pairs)
    ):
        raise ValueError("fit_timing_order_unregistered")

    expected = len(plan.pairs)
    by_arm: dict[tuple[str, Arm], FitInterval] = {}
    seen_refs: set[str] = set()
    recipes = {"baseline": plan.baseline_recipe, "candidate": plan.candidate_recipe}
    if len(intervals) > expected * 2:
        return FitTimingComparison(expected, (), "fit_receipts_do_not_match_plan")
    for interval in intervals:
        ref = require_ref(interval.receipt_ref)
        key = (interval.pair_ref, interval.arm)
        if (
            ref in seen_refs
            or key in by_arm
            or interval.pair_ref not in pair_refs
            or interval.scope != plan.scope
            or recipes.get(interval.arm) != interval.recipe
            or interval.stage != "fit"
            or interval.state not in _STATES
            or type(interval.started_ms) is not int
            or type(interval.finished_ms) is not int
            or not plan.registered_tick_ms < interval.started_ms < interval.finished_ms < 2**63
        ):
            return FitTimingComparison(expected, (), "fit_receipts_do_not_match_plan")
        by_arm[key] = interval
        seen_refs.add(ref)

    projected = []
    previous_finish = plan.registered_tick_ms
    for pair in plan.pairs:
        baseline = by_arm.get((pair.reference, "baseline"))
        candidate = by_arm.get((pair.reference, "candidate"))
        if baseline is None or candidate is None:
            continue
        earlier, later = (
            (baseline, candidate) if pair.first == "baseline" else (candidate, baseline)
        )
        if earlier.started_ms < previous_finish or earlier.finished_ms > later.started_ms:
            return FitTimingComparison(expected, (), "fit_receipts_do_not_match_plan")
        previous_finish = later.finished_ms
        projected.append(
            PairTiming(
                pair.reference,
                baseline.finished_ms - baseline.started_ms,
                candidate.finished_ms - candidate.started_ms,
                baseline.state,
                candidate.state,
            )
        )
    pairs = tuple(projected)
    if len(pairs) != expected:
        return FitTimingComparison(expected, pairs, "registered_fit_pair_missing")
    if any(
        (pair.baseline_state, pair.candidate_state) != ("completed", "completed") for pair in pairs
    ):
        return FitTimingComparison(expected, pairs, "registered_fit_pair_failed")

    ratios = [Fraction(pair.candidate_ms, pair.baseline_ms) for pair in pairs]
    reduction = 1 - median(ratios)
    total = Fraction(
        sum(pair.candidate_ms for pair in pairs), sum(pair.baseline_ms for pair in pairs)
    )
    worst = max(ratios)
    return FitTimingComparison(
        expected,
        pairs,
        None,
        reduction,
        total,
        worst,
        reduction * _BPS >= plan.minimum_median_reduction_bps
        and total * _BPS <= plan.maximum_total_ratio_bps
        and worst * _BPS <= plan.maximum_worst_pair_ratio_bps,
    )
