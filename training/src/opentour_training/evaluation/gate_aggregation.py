"""Aggregate caller-qualified gate observations without granting release authority."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from opentour_training.evaluation.inference_plan import numeric_alpha, strict_count, strict_number

if TYPE_CHECKING:
    from opentour_training.evaluation.evaluation_statistics import MetricResult


class GateState(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class Decision(StrEnum):
    PASS = "pass"
    HOLD = "hold"
    REJECT = "reject"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ExecutionAdmissibility(StrEnum):
    ADMISSIBLE = "admissible"
    INADMISSIBLE = "inadmissible"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class GateRequirement:
    gate_id: str
    required: bool
    fatal_on_known_failure: bool


@dataclass(frozen=True, slots=True)
class GateObservation:
    gate_id: str
    state: GateState
    reason: str


@dataclass(frozen=True, slots=True)
class GateAggregation:
    execution_admissibility: ExecutionAdmissibility
    decision: Decision | None
    gates: tuple[GateObservation, ...]


def _ordered_requirements(requirements: Sequence[GateRequirement]) -> dict[str, GateRequirement]:
    by_id: dict[str, GateRequirement] = {}
    for requirement in requirements:
        if (
            type(requirement.gate_id) is not str
            or not requirement.gate_id
            or type(requirement.required) is not bool
            or type(requirement.fatal_on_known_failure) is not bool
            or requirement.gate_id in by_id
        ):
            raise ValueError("Invalid or duplicate gate requirement.")
        by_id[requirement.gate_id] = requirement
    if not any(gate.required for gate in by_id.values()):
        raise ValueError("Evaluation policy needs at least one required gate.")
    return dict(sorted(by_id.items()))


def _complete_observations(
    by_id: dict[str, GateRequirement], observations: Sequence[GateObservation]
) -> tuple[GateObservation, ...]:
    observed: dict[str, GateObservation] = {}
    for result in observations:
        if (
            type(result.gate_id) is not str
            or result.gate_id not in by_id
            or result.gate_id in observed
            or type(result.state) is not GateState
            or type(result.reason) is not str
            or not result.reason
        ):
            raise ValueError("Invalid, duplicate, or undeclared gate observation.")
        observed[result.gate_id] = result
    return tuple(
        observed[gate_id]
        if gate_id in observed
        else GateObservation(gate_id, GateState.UNKNOWN, "missing_observation")
        for gate_id in by_id
    )


def aggregate_gates(
    requirements: Sequence[GateRequirement],
    observations: Sequence[GateObservation],
    *,
    execution_admissibility: ExecutionAdmissibility,
) -> GateAggregation:
    """Apply the frozen requirement policy to observations supplied after evidence checks."""
    if type(execution_admissibility) is not ExecutionAdmissibility:
        raise ValueError("Invalid execution admissibility.")
    by_id = _ordered_requirements(requirements)
    gates = _complete_observations(by_id, observations)
    if execution_admissibility is not ExecutionAdmissibility.ADMISSIBLE:
        return GateAggregation(execution_admissibility, None, gates)

    required = tuple(result for result in gates if by_id[result.gate_id].required)
    failures = tuple(result for result in required if result.state is GateState.FAIL)
    if any(by_id[result.gate_id].fatal_on_known_failure for result in failures):
        decision = Decision.REJECT
    elif failures:
        decision = Decision.HOLD
    elif any(result.state is GateState.UNKNOWN for result in required):
        decision = Decision.INSUFFICIENT_EVIDENCE
    else:
        decision = Decision.PASS
    return GateAggregation(execution_admissibility, decision, gates)


class MetricOperator(StrEnum):
    LOWER_BOUND_GE = "lower_bound_ge"
    UPPER_BOUND_LE = "upper_bound_le"
    POINT_GE = "point_ge"
    POINT_LE = "point_le"


@dataclass(frozen=True, slots=True)
class MetricGatePolicy:
    """Caller-qualified frozen metric and coverage policy projection."""

    gate_id: str
    metric_ref: bytes
    operator: MetricOperator
    threshold: float
    unit: str
    minimum_independent_units: int
    alpha_allocation: float | None


def evaluate_metric_gate(
    policy: MetricGatePolicy,
    result: MetricResult | None,
    *,
    coverage: GateState,
) -> GateObservation:
    """Compare the declared statistic, preserving missing evidence as unknown.

    The caller qualifies the exact plan, method and coverage policy. Point
    operators are reserved by that policy for non-inferential constraints.
    """
    if type(coverage) is not GateState or type(policy.operator) is not MetricOperator:
        raise ValueError("Invalid metric gate operator or coverage state.")
    operator = policy.operator
    uses_interval = operator in (MetricOperator.LOWER_BOUND_GE, MetricOperator.UPPER_BOUND_LE)
    if uses_interval:
        expected_alpha = numeric_alpha(policy.alpha_allocation)
    else:
        if policy.alpha_allocation is not None:
            raise ValueError("Point gates do not carry an interval alpha allocation.")
        expected_alpha = None
    threshold = strict_number(policy.threshold)
    minimum = strict_count(policy.minimum_independent_units)
    if result is None:
        return GateObservation(policy.gate_id, GateState.UNKNOWN, "missing_metric")
    if result.metric_ref != policy.metric_ref or result.unit != policy.unit:
        raise ValueError("Metric reference or unit does not match the frozen gate policy.")
    if strict_count(result.independent_units) < minimum:
        return GateObservation(policy.gate_id, GateState.UNKNOWN, "insufficient_independent_units")
    if coverage is not GateState.PASS:
        return GateObservation(policy.gate_id, GateState.UNKNOWN, "coverage_not_established")
    if result.undefined_reason is not None:
        return GateObservation(policy.gate_id, GateState.UNKNOWN, "undefined_metric")

    if result.value is None:
        return GateObservation(policy.gate_id, GateState.UNKNOWN, "missing_statistic")
    point_estimate = strict_number(result.value)
    if expected_alpha is not None and result.interval is not None:
        if (
            result.interval.claim_alpha != expected_alpha
            or result.interval.confidence_level != float(1 - expected_alpha)
        ):
            raise ValueError("Interval confidence does not match the frozen gate allocation.")
    if operator is MetricOperator.LOWER_BOUND_GE:
        statistic = result.interval.lower if result.interval is not None else None
    elif operator is MetricOperator.UPPER_BOUND_LE:
        statistic = result.interval.upper if result.interval is not None else None
    else:
        statistic = point_estimate
    if statistic is None:
        return GateObservation(policy.gate_id, GateState.UNKNOWN, "missing_statistic")

    value = strict_number(statistic)
    passed = (
        value >= threshold
        if operator in (MetricOperator.LOWER_BOUND_GE, MetricOperator.POINT_GE)
        else value <= threshold
    )
    if passed:
        return GateObservation(policy.gate_id, GateState.PASS, "threshold_met")
    return GateObservation(policy.gate_id, GateState.FAIL, "threshold_not_met")


def evaluate_check_gate(gate_id: str, checks: Sequence[GateState]) -> GateObservation:
    """Apply an all-checks-pass requirement to caller-qualified checks."""
    if any(type(check) is not GateState for check in checks):
        raise ValueError("Invalid gate check state.")
    if GateState.FAIL in checks:
        return GateObservation(gate_id, GateState.FAIL, "check_failed")
    if not checks or GateState.UNKNOWN in checks:
        return GateObservation(gate_id, GateState.UNKNOWN, "check_unknown")
    return GateObservation(gate_id, GateState.PASS, "checks_passed")
