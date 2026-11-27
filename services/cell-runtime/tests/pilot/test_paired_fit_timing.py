"""Use the native five-pair fixture and falsification cases."""

from dataclasses import replace
from fractions import Fraction

import pytest
from opentour_cell.pilot.paired_fit_timing import (
    FitInterval,
    FitScope,
    FitTimingPlan,
    RegisteredPair,
    compare_fit_intervals,
)

PLAN = FitTimingPlan(
    FitScope("model-a", "train-v1", "budget-v1", "cpu-a", "numeric-v1", "boot-a", "warm"),
    tuple(RegisteredPair(f"pair-{i}", "baseline" if i % 2 == 0 else "candidate") for i in range(5)),
    "reviewed-base",
    "tested-candidate",
    50,
    2_000,
    11_000,
    15_000,
)


def observations(candidate_times):
    intervals = []
    for index, (pair, duration) in enumerate(zip(PLAN.pairs, candidate_times, strict=True)):
        baseline = FitInterval(
            f"baseline-{index}",
            PLAN.scope,
            PLAN.baseline_recipe,
            pair.reference,
            "baseline",
            "fit",
            100 + index * 2000,
            200 + index * 2000,
            "completed",
        )
        if pair.first == "candidate":
            start = baseline.started_ms
            baseline = replace(
                baseline, started_ms=start + duration, finished_ms=start + duration + 100
            )
        else:
            start = baseline.finished_ms
        candidate = FitInterval(
            f"candidate-{index}",
            PLAN.scope,
            PLAN.candidate_recipe,
            pair.reference,
            "candidate",
            "fit",
            start,
            start + duration,
            "completed",
        )
        intervals.extend((baseline, candidate))
    # Receipt delivery order is independent of measured arm order.
    return list(reversed(intervals))


@pytest.mark.parametrize(
    ("candidates", "reduction", "total", "worst", "passes"),
    [
        ([75, 78, 80, 82, 84], Fraction(1, 5), Fraction(399, 500), Fraction(84, 100), True),
        ([75, 78, 81, 82, 84], Fraction(19, 100), Fraction(4, 5), Fraction(84, 100), False),
        ([80, 80, 80, 1000, 1000], Fraction(1, 5), Fraction(112, 25), Fraction(10), False),
    ],
)
def test_fit_median_total_and_tail_all_apply(candidates, reduction, total, worst, passes):
    result = compare_fit_intervals(PLAN, observations(candidates))
    assert result.registered_count == len(result.pairs) == 5
    assert result.hold_reason is None and result.timing_targets_met is passes
    assert (result.median_paired_reduction, result.total_ratio, result.worst_pair_ratio) == (
        reduction,
        total,
        worst,
    )
    assert [pair.candidate_ms for pair in result.pairs] == candidates


@pytest.mark.parametrize("state", ["timeout", "oom", "missing_examples", "cap_exceeded"])
def test_failed_pair_stays_in_denominator(state):
    rows = observations([75, 78, 80, 82, 84])
    rows[0] = replace(rows[0], state=state)
    result = compare_fit_intervals(PLAN, rows)
    assert result.registered_count == len(result.pairs) == 5
    assert result.pairs[-1].candidate_state == state
    assert result.hold_reason == "registered_fit_pair_failed"
    assert result.median_paired_reduction is None and not result.timing_targets_met


def test_missing_and_incomparable_receipts_produce_no_speed_claim():
    rows = observations([75, 78, 80, 82, 84])
    missing = compare_fit_intervals(PLAN, rows[:-1])
    assert missing.registered_count == 5 and len(missing.pairs) == 4
    assert missing.hold_reason == "registered_fit_pair_missing"
    reading = rows[0]
    for incompatible in (
        replace(reading, stage="replay"),
        replace(reading, scope=replace(PLAN.scope, hardware_revision="cpu-other")),
        replace(reading, scope=replace(PLAN.scope, cache="cold")),
        replace(reading, scope=replace(PLAN.scope, update_budget_revision="extra-steps")),
        replace(reading, receipt_ref=rows[-1].receipt_ref),
        replace(reading, started_ms=100, finished_ms=184),
    ):
        held = compare_fit_intervals(PLAN, [incompatible, *rows[1:]])
        assert held.hold_reason == "fit_receipts_do_not_match_plan"
        assert held.total_ratio is None and not held.timing_targets_met
