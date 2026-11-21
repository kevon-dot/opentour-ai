"""Generated OPE-119 cases for admissibility and frozen gate precedence."""

from dataclasses import replace
from fractions import Fraction

import pytest
from opentour_training.evaluation.evaluation_statistics import ConfidenceInterval, MetricResult
from opentour_training.evaluation.gate_aggregation import (
    Decision,
    ExecutionAdmissibility,
    GateObservation,
    GateRequirement,
    GateState,
    MetricGatePolicy,
    MetricOperator,
    aggregate_gates,
    evaluate_check_gate,
    evaluate_metric_gate,
)

POLICY = (
    GateRequirement("retention", required=True, fatal_on_known_failure=False),
    GateRequirement("effect", required=True, fatal_on_known_failure=False),
    GateRequirement("risk", required=True, fatal_on_known_failure=True),
    GateRequirement("cost", required=False, fatal_on_known_failure=False),
)


def observations(**statuses: GateState) -> list[GateObservation]:
    return [
        GateObservation(gate_id, state, "generated_observation")
        for gate_id, state in statuses.items()
    ]


@pytest.mark.parametrize(
    ("effect", "retention", "risk", "expected"),
    [
        (GateState.PASS, GateState.FAIL, GateState.PASS, Decision.HOLD),
        (GateState.UNKNOWN, GateState.FAIL, GateState.FAIL, Decision.REJECT),
        (GateState.PASS, GateState.PASS, GateState.UNKNOWN, Decision.INSUFFICIENT_EVIDENCE),
        (GateState.PASS, GateState.PASS, GateState.PASS, Decision.PASS),
        (GateState.FAIL, GateState.UNKNOWN, GateState.UNKNOWN, Decision.HOLD),
    ],
)
def test_frozen_decision_precedence(
    effect: GateState, retention: GateState, risk: GateState, expected: Decision
) -> None:
    supplied = observations(effect=effect, retention=retention, risk=risk)
    result = aggregate_gates(
        POLICY, supplied, execution_admissibility=ExecutionAdmissibility.ADMISSIBLE
    )
    assert result.decision is expected
    assert tuple(item.gate_id for item in result.gates) == ("cost", "effect", "retention", "risk")
    assert result.gates[0].state is GateState.UNKNOWN
    assert (
        aggregate_gates(
            POLICY[::-1], supplied[::-1], execution_admissibility=ExecutionAdmissibility.ADMISSIBLE
        )
        == result
    )
    assert {item.gate_id for item in result.gates if item.state is GateState.FAIL} == {
        item.gate_id for item in supplied if item.state is GateState.FAIL
    }


def test_missing_required_gate_is_unknown_and_optional_failure_does_not_hold() -> None:
    partial = observations(effect=GateState.PASS, retention=GateState.PASS, cost=GateState.FAIL)
    result = aggregate_gates(
        POLICY, partial, execution_admissibility=ExecutionAdmissibility.ADMISSIBLE
    )
    assert result.decision is Decision.INSUFFICIENT_EVIDENCE
    assert result.gates[-1] == GateObservation("risk", GateState.UNKNOWN, "missing_observation")
    complete = [*partial, *observations(risk=GateState.PASS)]
    assert (
        aggregate_gates(
            POLICY, complete, execution_admissibility=ExecutionAdmissibility.ADMISSIBLE
        ).decision
        is Decision.PASS
    )


@pytest.mark.parametrize(
    "state", [ExecutionAdmissibility.INADMISSIBLE, ExecutionAdmissibility.UNKNOWN]
)
def test_execution_refusal_carries_no_statistical_decision(state: ExecutionAdmissibility) -> None:
    passed = observations(effect=GateState.PASS, retention=GateState.PASS, risk=GateState.PASS)
    result = aggregate_gates(POLICY, passed, execution_admissibility=state)
    assert result.execution_admissibility is state
    assert result.decision is None


@pytest.mark.parametrize(
    "malformed", ["empty_policy", "duplicate_policy", "duplicate_result", "extra"]
)
def test_invalid_policy_or_observations_fail_closed(malformed: str) -> None:
    policy: tuple[GateRequirement, ...] = POLICY
    supplied = observations(effect=GateState.PASS)
    if malformed == "empty_policy":
        policy = ()
    elif malformed == "duplicate_policy":
        policy = (*policy, policy[0])
    elif malformed == "duplicate_result":
        supplied.extend(supplied.copy())
    else:
        supplied.extend(observations(unknown_gate=GateState.PASS))
    with pytest.raises(ValueError):
        aggregate_gates(policy, supplied, execution_admissibility=ExecutionAdmissibility.ADMISSIBLE)


@pytest.fixture
def retention_metric() -> MetricResult:
    return MetricResult(
        b"generated-retention",
        -2,
        400,
        -0.005,
        "fraction",
        ConfidenceInterval(-0.03976509532, None, 0.95, "one", b"generated-method", Fraction(1, 20)),
        400,
        None,
        None,
    )


@pytest.fixture
def retention_policy() -> MetricGatePolicy:
    return MetricGatePolicy(
        "retention",
        b"generated-retention",
        MetricOperator.LOWER_BOUND_GE,
        -0.01,
        "fraction",
        300,
        0.05,
    )


def test_retention_floor_uses_the_bound_and_holds(
    retention_policy: MetricGatePolicy, retention_metric: MetricResult
) -> None:
    retention = evaluate_metric_gate(retention_policy, retention_metric, coverage=GateState.PASS)
    assert retention.state is GateState.FAIL
    supplied = [retention, *observations(effect=GateState.PASS, risk=GateState.PASS)]
    assert (
        aggregate_gates(
            POLICY,
            supplied,
            execution_admissibility=ExecutionAdmissibility.ADMISSIBLE,
        ).decision
        is Decision.HOLD
    )


def test_metric_gate_fails_closed_on_mixed_units_and_unallocated_confidence(
    retention_policy: MetricGatePolicy, retention_metric: MetricResult
) -> None:
    with pytest.raises(ValueError, match="reference or unit"):
        evaluate_metric_gate(
            retention_policy, replace(retention_metric, unit="percentage"), coverage=GateState.PASS
        )
    with pytest.raises(ValueError, match="confidence"):
        evaluate_metric_gate(
            replace(retention_policy, alpha_allocation=0.025),
            retention_metric,
            coverage=GateState.PASS,
        )


def test_metric_gate_requires_declared_coverage_and_independent_samples(
    retention_policy: MetricGatePolicy, retention_metric: MetricResult
) -> None:
    assert (
        evaluate_metric_gate(retention_policy, None, coverage=GateState.PASS).reason
        == "missing_metric"
    )
    assert (
        evaluate_metric_gate(retention_policy, retention_metric, coverage=GateState.UNKNOWN).reason
        == "coverage_not_established"
    )
    assert (
        evaluate_metric_gate(
            retention_policy,
            replace(retention_metric, independent_units=299),
            coverage=GateState.PASS,
        ).reason
        == "insufficient_independent_units"
    )
    risk = replace(retention_policy, operator=MetricOperator.UPPER_BOUND_LE)
    assert (
        evaluate_metric_gate(risk, retention_metric, coverage=GateState.PASS).reason
        == "missing_statistic"
    )


def test_known_check_failure_remains_known_among_unknown_checks() -> None:
    assert evaluate_check_gate("risk", [GateState.UNKNOWN, GateState.FAIL]).state is GateState.FAIL
    assert evaluate_check_gate("risk", []).state is GateState.UNKNOWN
    assert evaluate_check_gate("risk", [GateState.PASS]).state is GateState.PASS


def test_point_gate_accepts_measured_zero_and_threshold_equality(
    retention_policy: MetricGatePolicy, retention_metric: MetricResult
) -> None:
    policy = replace(
        retention_policy, operator=MetricOperator.POINT_LE, threshold=0.0, alpha_allocation=None
    )
    metric = replace(retention_metric, value=0.0, interval=None)
    assert evaluate_metric_gate(policy, metric, coverage=GateState.PASS).state is GateState.PASS
    with pytest.raises(ValueError, match="alpha allocation"):
        evaluate_metric_gate(
            replace(policy, alpha_allocation=0.05), metric, coverage=GateState.PASS
        )


def test_gate_cannot_pass_with_a_bound_but_no_measured_point(
    retention_policy: MetricGatePolicy, retention_metric: MetricResult
) -> None:
    policy = replace(retention_policy, threshold=-0.1)
    result = evaluate_metric_gate(
        policy, replace(retention_metric, value=None), coverage=GateState.PASS
    )
    assert result.state is GateState.UNKNOWN
    assert result.reason == "missing_statistic"


def test_claim_allocation_check_is_exact_even_when_confidence_rounds_the_same(
    retention_policy: MetricGatePolicy, retention_metric: MetricResult
) -> None:
    with pytest.raises(ValueError, match="frozen gate allocation"):
        evaluate_metric_gate(
            replace(retention_policy, alpha_allocation=0.05000000000000001),
            retention_metric,
            coverage=GateState.PASS,
        )
