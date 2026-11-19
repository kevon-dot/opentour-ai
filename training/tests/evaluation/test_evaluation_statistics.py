"""Generated OPE-118 cases for the typed numerical result assembly."""

import json
from pathlib import Path

import pytest
from opentour_training.evaluation.evaluation_statistics import (
    MetricDescriptor,
    assemble_critical_risk_metric,
    assemble_paired_metric,
)
from opentour_training.evaluation.inference_plan import (
    ContractValidator,
    InvalidKernelInput,
    build_contract_validator,
    validate_inference_plan,
)
from test_statistics_kernel import denoms, plan_data

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "packages/contracts"


def artifact_ref(artifact_id: str) -> bytes:
    reference = {**plan_data(n=2)["nuisance_assumptions_ref"], "artifact_id": artifact_id}
    return json.dumps(reference, sort_keys=True, separators=(",", ":")).encode()


DESCRIPTOR = MetricDescriptor(
    artifact_ref("generated-metric"), "fraction", artifact_ref("generated-method")
)


@pytest.fixture(scope="module")
def contract_validator() -> ContractValidator:
    return build_contract_validator(
        (CONTRACTS / "learning/benchmarks/statistical-design.schema.json").read_bytes(),
        (CONTRACTS / "common/common-contract.schema.json").read_bytes(),
    )


def test_paired_effect_matches_registered_numeric_method(
    contract_validator: ContractValidator,
) -> None:
    plan = validate_inference_plan(plan_data(n=200), contract_validator)
    result = assemble_paired_metric(
        DESCRIPTOR,
        plan,
        denoms(200),
        "final",
        "claim",
        metric_role="effect",
        pair_cells={"b": 40, "c": 10, "both_correct": 130, "both_wrong": 20},
    )
    assert (result.numerator, result.denominator, result.value) == (30, 200, 0.15)
    assert result.undefined_reason is None
    assert result.metric_ref == DESCRIPTOR.metric_ref
    assert result.independent_units == 200 and result.cluster_count is None
    assert result.interval is not None
    assert result.interval.lower == pytest.approx(0.04398798836)
    assert result.interval.upper == pytest.approx(0.24962234988)
    assert result.interval.confidence_level == pytest.approx(0.95)
    assert result.interval.sidedness == "two"
    assert result.interval.method_ref == DESCRIPTOR.method_ref


def test_reported_confidence_uses_allocated_claim_alpha(
    contract_validator: ContractValidator,
) -> None:
    data = plan_data(n=3)
    data["look_plan"][0]["claim_alpha"]["claim"] = 0.025
    plan = validate_inference_plan(data, contract_validator)
    result = assemble_paired_metric(
        DESCRIPTOR,
        plan,
        denoms(3),
        "final",
        "claim",
        metric_role="effect",
        pair_cells={"b": 0, "c": 0, "both_correct": 3, "both_wrong": 0},
    )
    assert result.interval is not None
    assert result.interval.confidence_level == pytest.approx(0.975)


def test_retention_respects_frozen_one_sided_plan(
    contract_validator: ContractValidator,
) -> None:
    plan = validate_inference_plan(plan_data(n=400), contract_validator)
    result = assemble_paired_metric(
        DESCRIPTOR,
        plan,
        denoms(400),
        "final",
        "claim",
        metric_role="retention",
        pair_cells={"b": 10, "c": 12, "both_correct": 355, "both_wrong": 23},
    )
    assert result.value == -0.005
    assert result.interval is not None
    assert result.interval.sidedness == "one"
    assert result.interval.lower == pytest.approx(-0.03976509532)
    assert result.interval.upper is None


@pytest.mark.parametrize(
    ("units", "reason", "value"),
    [
        (0, "no_gradeable_units", None),
        (1, "singleton", None),
        (2, "incomplete_planned_look", None),
        (3, None, 0.0),
    ],
)
def test_measured_zero_missing_and_incomplete_are_distinct(
    contract_validator: ContractValidator,
    units: int,
    reason: str | None,
    value: float | None,
) -> None:
    plan = validate_inference_plan(plan_data(n=3), contract_validator)
    counts = denoms(units)
    counts["unknown"] = 3 - units
    counts["attempted"] = 3
    result = assemble_paired_metric(
        DESCRIPTOR,
        plan,
        counts,
        "final",
        "claim",
        metric_role="effect",
        pair_cells={"b": 0, "c": 0, "both_correct": units, "both_wrong": 0},
    )
    assert (result.value, result.undefined_reason, result.independent_units) == (
        value,
        reason,
        units,
    )
    assert (result.interval is None) == (reason is not None)
    assert result.denominator == (None if reason else units)


def test_family_analysis_preserves_cluster_count(
    contract_validator: ContractValidator,
) -> None:
    plan = validate_inference_plan(plan_data(n=3, family=True), contract_validator)
    counts = denoms(12)
    counts["independent_units"] = 3
    result = assemble_paired_metric(
        DESCRIPTOR,
        plan,
        counts,
        "final",
        "claim",
        metric_role="effect",
        family_effects=[("family-a", 0.5), ("family-b", -0.5), ("family-c", 0.0)],
    )
    assert (result.value, result.independent_units, result.cluster_count) == (0.0, 3, 3)
    assert result.denominator == 3
    assert result.numerator is None


def test_malformed_kernel_input_is_not_reported_as_missing(
    contract_validator: ContractValidator,
) -> None:
    plan = validate_inference_plan(plan_data(n=3), contract_validator)
    with pytest.raises(InvalidKernelInput):
        assemble_paired_metric(
            DESCRIPTOR,
            plan,
            denoms(3),
            "final",
            "claim",
            metric_role="effect",
            pair_cells={"b": 0, "c": 0, "both_correct": 2, "both_wrong": 0},
        )


@pytest.mark.parametrize(("n", "upper"), [(240, 0.0124046), (300, 0.0099361)])
def test_zero_errors_still_has_a_nonzero_upper_risk_bound(n: int, upper: float) -> None:
    result = assemble_critical_risk_metric(
        DESCRIPTOR,
        errors=0,
        independent_exposures=n,
        alpha=0.05,
    )
    assert (result.numerator, result.denominator, result.value) == (0, n, 0.0)
    assert result.interval is not None
    assert result.interval.lower is None and result.interval.sidedness == "one"
    assert result.interval.upper == pytest.approx(upper, abs=1e-7)


def test_no_eligible_critical_exposures_have_no_measured_rate() -> None:
    result = assemble_critical_risk_metric(
        DESCRIPTOR,
        errors=0,
        independent_exposures=0,
        alpha=0.05,
    )
    assert result.value is None and result.interval is None and result.denominator is None
    assert result.undefined_reason == "no_gradeable_units"
    with pytest.raises(InvalidKernelInput):
        assemble_critical_risk_metric(DESCRIPTOR, errors=True, independent_exposures=0, alpha=0.05)
