"""Pre-outcome checks on the registered InferencePlan and GatePolicy documents."""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from opentour_training.evaluation.inference_plan import (
    INFERENCE_URI,
    ContractValidator,
    InvalidKernelInput,
    _json_copy,
    strict_count,
    validate_inference_plan,
)

_INTERVAL_OPERATORS = {"lower_bound_ge", "upper_bound_le"}


def _gate_requirements(policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    gates: dict[str, dict[str, Any]] = {}
    required_fatal = set()
    for gate in policy["required_gates"]:
        gate_id = gate["gate_id"]
        if gate_id in gates:
            raise InvalidKernelInput()
        gates[gate_id] = gate
        strict_count(gate["minimum_independent_units"])
        if gate["required"] and gate["fatal_on_known_failure"]:
            required_fatal.add(gate_id)
    fatal_ids = policy["fatal_gate_ids"]
    if len(fatal_ids) != len(required_fatal) or set(fatal_ids) != required_fatal:
        raise InvalidKernelInput()
    for limit in policy["critical_limits"]:
        strict_count(limit["minimum_exposures"])
    return gates


def validate_statistical_design(
    inference_plan: object, gate_policy: object, contracts: ContractValidator
) -> None:
    """Validate the proposed design before reading outcomes; no lock or receipt is issued.

    The caller supplies the two pre-outcome documents and the pinned local registry.
    Current authority, population eligibility and exposure reservations are checked at lock.
    """
    plan = validate_inference_plan(inference_plan, contracts)
    policy = _json_copy(gate_policy)
    # Reuse the kernel's digest-checked registry and its closed common references.
    validator = contracts._validator.evolve(schema={"$ref": INFERENCE_URI + "#/$defs/GatePolicy"})
    if not validator.is_valid(policy):
        raise InvalidKernelInput()
    if policy["effect_requirement"]["independent_unit"] != plan.independent_unit:
        raise InvalidKernelInput()

    gates = _gate_requirements(policy)
    allocated: dict[str, Fraction] = {}
    for look in plan.look_plan:
        for gate_id, alpha in look.claim_alpha:
            gate = gates.get(gate_id)
            if gate is None or gate["operator"] not in _INTERVAL_OPERATORS:
                raise InvalidKernelInput()
            allocated[gate_id] = allocated.get(gate_id, Fraction(0)) + alpha
    for gate_id, gate in gates.items():
        if gate["operator"] not in _INTERVAL_OPERATORS:
            continue
        if gate["required"] and gate_id not in allocated:
            raise InvalidKernelInput()
        if allocated.get(gate_id, 0) > Fraction(str(gate["alpha_allocation"])):
            raise InvalidKernelInput()
