"""Generated OPE-119 cases for admissibility and frozen gate precedence."""

import pytest
from opentour_training.evaluation.gate_aggregation import (
    Decision,
    ExecutionAdmissibility,
    GateObservation,
    GateRequirement,
    GateState,
    aggregate_gates,
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
