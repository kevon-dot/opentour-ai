"""Aggregate caller-qualified gate observations without granting release authority."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


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
