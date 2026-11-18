"""Pre-outcome policy checks for AC07 using simulated design documents."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from opentour_training.benchmarks.statistical_design import validate_statistical_design
from opentour_training.evaluation.inference_plan import (
    ContractValidator,
    InvalidKernelInput,
    build_contract_validator,
)

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def contracts() -> ContractValidator:
    base = ROOT / "packages/contracts"
    return build_contract_validator(
        (base / "learning/benchmarks/statistical-design.schema.json").read_bytes(),
        (base / "common/common-contract.schema.json").read_bytes(),
    )


def reference(name: str) -> dict[str, Any]:
    return {
        "artifact_id": name,
        "kind": "synthetic_fixture",
        "revision": "v1",
        "sha256": "a" * 64,
        "manifest_id": "simulated-manifest",
        "environment_id": "simulated-environment",
        "classification": "control_metadata",
        "purpose_grant_id": "simulated-grant",
    }


def gate(
    identifier: str, operator: str, threshold: int | float | None, alpha: float | None
) -> dict[str, Any]:
    return {
        "gate_id": identifier,
        "metric_ref": reference(identifier),
        "operator": operator,
        "threshold": threshold,
        "unit": "fraction",
        "alpha_allocation": alpha,
        "required": True,
        "fatal_on_known_failure": identifier == "risk",
        "minimum_independent_units": 1,
        "required_coverage_policy_ref": reference("policy"),
    }


@pytest.fixture
def policy() -> dict[str, Any]:
    return {
        "required_gates": [
            gate("gain", "lower_bound_ge", 0.05, 0.025),
            gate("retention", "lower_bound_ge", -0.01, 0.015),
            gate("risk", "upper_bound_le", 0.01, 0.01),
            gate("coverage", "all_checks_pass", None, None),
            dict(gate("latency", "point_le", 500, None), unit="milliseconds"),
        ],
        "fatal_gate_ids": ["risk"],
        "effect_requirement": {
            "metric_ref": reference("gain"),
            "minimum_gain": 0.05,
            "independent_unit": "case",
        },
        "critical_limits": [
            {
                "slice_id": "risk-slice",
                "metric_ref": reference("risk"),
                "max_error_rate": 0.01,
                "minimum_exposures": 1,
            }
        ],
        "retention_margin": {"metric_ref": reference("retention"), "max_degradation": 0.01},
        "coverage_requirement": {
            "population_ref": reference("population"),
            "min_gradeable_fraction": 0.90,
            "required_slice_ids": ["risk-slice"],
            "missingness_policy": "hold_if_required_unknown",
        },
    }


@pytest.fixture
def plan() -> dict[str, Any]:
    return {
        "independent_unit": "case",
        "cluster_method_ref": None,
        "paired_effect_method": "paired_binary_exact_marginals_v1",
        "confidence_level": 0.95,
        "effect_interval_sides": 2,
        "retention_interval_sides": 1,
        "critical_bound_method": "binomial_clopper_pearson_one_sided_v1",
        "multiplicity_policy_ref": reference("multiplicity"),
        "stopping_mode": "bonferroni_finite_looks",
        "look_plan": [
            {
                "look_id": "early",
                "cumulative_independent_units": 100,
                "claim_alpha": {"gain": 0.01},
            },
            {
                "look_id": "final",
                "cumulative_independent_units": 200,
                "claim_alpha": {"gain": 0.015, "retention": 0.015, "risk": 0.01},
            },
        ],
        "repetitions": 1,
        "nuisance_assumptions_ref": reference("assumptions"),
    }


def test_predeclared_claims_and_fatal_set(
    plan: dict[str, Any], policy: dict[str, Any], contracts: ContractValidator
) -> None:
    before = copy.deepcopy((plan, policy))
    assert validate_statistical_design(plan, policy, contracts) is None
    assert (plan, policy) == before


def test_registered_policy_validation_returns_independent_document(
    policy: dict[str, Any], contracts: ContractValidator
) -> None:
    validated = contracts.validate_gate_policy(policy)
    assert validated == policy
    validated["effect_requirement"]["metric_ref"]["artifact_id"] = "different-fixture"
    assert policy["effect_requirement"]["metric_ref"]["artifact_id"] == "gain"


def test_fixed_final_family_and_unused_optional_gate(
    plan: dict[str, Any], policy: dict[str, Any], contracts: ContractValidator
) -> None:
    plan.update(
        independent_unit="family",
        cluster_method_ref=reference("families"),
        paired_effect_method="bounded_independent_family_hoeffding_v1",
        stopping_mode="fixed_final",
        look_plan=plan["look_plan"][1:],
    )
    policy["effect_requirement"]["independent_unit"] = "family"
    policy["required_gates"].append(
        dict(
            gate("optional", "upper_bound_le", 0.03, 0.001),
            required=False,
            fatal_on_known_failure=True,
        )
    )
    validate_statistical_design(plan, policy, contracts)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("fatal_gate_ids",), []),
        (("fatal_gate_ids",), ["risk", "risk"]),
        (("fatal_gate_ids",), ["coverage", "risk"]),
        (("required_gates", 2, "required"), False),
        (("required_gates", 1, "gate_id"), "gain"),
        (("required_gates", 0, "gate_id"), "different-claim"),
        (("required_gates", 0, "alpha_allocation"), 0.024999999999999998),
        (("effect_requirement", "independent_unit"), "family"),
        (("required_gates", 2, "operator"), "point_le"),
        (("required_gates", 2, "operator"), "unregistered"),
        (("required_gates", 2, "alpha_allocation"), None),
        (("required_gates", 3, "threshold"), 0.9),
        (("required_gates", 2, "threshold"), float("nan")),
        (("required_gates", 0, "minimum_independent_units"), 1.0),
        (("critical_limits", 0, "minimum_exposures"), True),
        (("critical_limits", 0, "max_error_rate"), 1.0),
        (("retention_margin", "metric_ref", "foreign_payload"), "simulated-private-sentinel"),
        (("effect_requirement", "metric_ref", "sha256"), "malformed"),
        (("effect_requirement", "minimum_gain"), None),
        (("coverage_requirement",), {}),
    ],
)
def test_reject_incompatible_policy(
    plan: dict[str, Any],
    policy: dict[str, Any],
    contracts: ContractValidator,
    path: tuple[str | int, ...],
    value: object,
) -> None:
    target: Any = policy
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    before = copy.deepcopy((plan, policy))
    with pytest.raises(InvalidKernelInput, match="^Invalid numerical input$"):
        validate_statistical_design(plan, policy, contracts)
    # Compare the document's representation so the NaN adverse case also checks for mutation.
    assert repr((plan, policy)) == repr(before)


@pytest.mark.parametrize("identifier", ["risk", "coverage", "latency", "unknown"])
def test_missing_or_ineligible_claim(
    plan: dict[str, Any], policy: dict[str, Any], contracts: ContractValidator, identifier: str
) -> None:
    final = plan["look_plan"][-1]["claim_alpha"]
    del final["risk"]
    if identifier != "risk":
        final[identifier] = 0.01
    with pytest.raises(InvalidKernelInput):
        validate_statistical_design(plan, policy, contracts)


def test_familywise_budget_applies_even_when_each_claim_is_within_its_cap(
    plan: dict[str, Any], policy: dict[str, Any], contracts: ContractValidator
) -> None:
    final = plan["look_plan"][-1]["claim_alpha"]
    final["risk"] = 0.010000000000000002
    policy["required_gates"][2]["alpha_allocation"] = final["risk"]
    with pytest.raises(InvalidKernelInput):
        validate_statistical_design(plan, policy, contracts)


def test_optional_scheduled_gate_still_has_a_cap(
    plan: dict[str, Any], policy: dict[str, Any], contracts: ContractValidator
) -> None:
    optional = dict(gate("optional", "upper_bound_le", 0.03, 0.001), required=False)
    policy["required_gates"].append(optional)
    final = plan["look_plan"][-1]["claim_alpha"]
    final["risk"] = 0.005
    final["optional"] = 0.002
    with pytest.raises(InvalidKernelInput):
        validate_statistical_design(plan, policy, contracts)


def test_reject_open_plan_before_policy(
    plan: dict[str, Any], policy: dict[str, Any], contracts: ContractValidator
) -> None:
    del plan["retention_interval_sides"]
    with pytest.raises(InvalidKernelInput):
        validate_statistical_design(plan, policy, contracts)
