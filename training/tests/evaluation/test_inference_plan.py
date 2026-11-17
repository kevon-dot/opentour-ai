"""Synthetic contract fixtures; these assertions do not establish customer authority."""

from __future__ import annotations

import copy
import dataclasses
import json
import math
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from opentour_training.evaluation.inference_plan import (
    MAX_JSON_INTEGER,
    ContractValidator,
    InvalidKernelInput,
    UnsupportedNumericProfile,
    build_contract_validator,
    qualify_numeric_profile,
    validate_denominators,
    validate_inference_plan,
)
from opentour_training.evaluation.statistics_kernel import compute_interval

ROOT = Path(__file__).resolve().parents[3]


def reference() -> dict[str, Any]:
    return {
        "artifact_id": "generated-artifact",
        "kind": "synthetic_fixture",
        "revision": "v1",
        "sha256": "a" * 64,
        "manifest_id": "generated-manifest",
        "environment_id": "generated-environment",
        "classification": "control_metadata",
        "purpose_grant_id": "generated-grant",
    }


def raw_plan(*, family: bool = False, n: int = 200) -> dict[str, Any]:
    return {
        "independent_unit": "family" if family else "case",
        "cluster_method_ref": reference() if family else None,
        "paired_effect_method": (
            "bounded_independent_family_hoeffding_v1"
            if family
            else "paired_binary_exact_marginals_v1"
        ),
        "confidence_level": 0.95,
        "effect_interval_sides": 2,
        "retention_interval_sides": 1,
        "critical_bound_method": "binomial_clopper_pearson_one_sided_v1",
        "multiplicity_policy_ref": reference(),
        "stopping_mode": "fixed_final",
        "look_plan": [
            {
                "look_id": "final",
                "cumulative_independent_units": n,
                "claim_alpha": {"generated-claim": 0.05},
            }
        ],
        "repetitions": 1,
        "nuisance_assumptions_ref": reference(),
    }


def counts(n: int, *, unknown: int = 0) -> dict[str, int]:
    return {
        "attempted": n + unknown,
        "paired_gradeable": n,
        "unknown": unknown,
        "excluded": 0,
        "runtime_failed": 0,
        "independent_units": n,
    }


@pytest.fixture(scope="module")
def contracts() -> ContractValidator:
    return build_contract_validator(
        (
            ROOT / "packages/contracts/learning/benchmarks/statistical-design.schema.json"
        ).read_bytes(),
        (ROOT / "packages/contracts/common/common-contract.schema.json").read_bytes(),
    )


@pytest.mark.parametrize("family", [False, True])
def test_valid_case_and_family_plan(contracts: ContractValidator, family: bool) -> None:
    source = raw_plan(family=family)
    before = copy.deepcopy(source)
    parsed = validate_inference_plan(source, contracts)
    selected = qualify_numeric_profile(parsed, "final", "generated-claim", metric_role="effect")
    assert selected.alpha == Fraction("0.05")
    assert selected.sides == 2
    assert source == before


@pytest.mark.parametrize("field", list(raw_plan()))
def test_missing_plan_field(contracts: ContractValidator, field: str) -> None:
    plan = raw_plan()
    del plan[field]
    with pytest.raises(InvalidKernelInput):
        validate_inference_plan(plan, contracts)


@pytest.mark.parametrize(
    "field,value",
    [
        ("extra", 1),
        ("paired_effect_method", "paired_normal_reference_v1"),
        ("critical_bound_method", "normal"),
        ("independent_unit", "row"),
        ("effect_interval_sides", 3),
        ("effect_interval_sides", True),
        ("retention_interval_sides", 1.0),
        ("repetitions", True),
        ("repetitions", 1.0),
        ("repetitions", 2),
        ("repetitions", MAX_JSON_INTEGER + 1),
        ("confidence_level", math.nan),
        ("confidence_level", math.inf),
        ("confidence_level", -math.inf),
        ("confidence_level", 0),
        ("confidence_level", 1),
        ("confidence_level", True),
        ("multiplicity_policy_ref", None),
        ("nuisance_assumptions_ref", "not-an-artifact"),
        ("cluster_method_ref", reference()),
        ("look_plan", []),
    ],
)
def test_invalid_plan_field(contracts: ContractValidator, field: str, value: Any) -> None:
    plan = raw_plan()
    plan[field] = value
    with pytest.raises(InvalidKernelInput):
        validate_inference_plan(plan, contracts)


@pytest.mark.parametrize("change", ["missing", "extra", "hash", "classification", "timestamp"])
def test_artifact_reference_closed(contracts: ContractValidator, change: str) -> None:
    plan = raw_plan()
    ref = plan["multiplicity_policy_ref"]
    if change == "missing":
        del ref["environment_id"]
    elif change == "extra":
        ref["uri"] = "invalid"
    elif change == "hash":
        ref["sha256"] = "a" * 63
    elif change == "classification":
        ref["classification"] = "unknown"
    else:
        ref["expires_at"] = "2026-02-31T00:00:00Z"
    with pytest.raises(InvalidKernelInput):
        validate_inference_plan(plan, contracts)


@pytest.mark.parametrize("kind", ["same_uri", "remote_ref", "wrong_uri", "common", "malformed"])
def test_no_remote_schema_resolution(kind: str) -> None:
    original = (
        ROOT / "packages/contracts/learning/benchmarks/statistical-design.schema.json"
    ).read_bytes()
    common = (ROOT / "packages/contracts/common/common-contract.schema.json").read_bytes()
    document = json.loads(original)
    if kind == "same_uri":
        document["$ref"] = "#/$defs/GatePolicy"
    elif kind == "remote_ref":
        document["$ref"] = "https://invalid.example/untrusted"
    elif kind == "wrong_uri":
        document["$id"] = "urn:invalid"
    elif kind == "common":
        common += b" "
    data = original if kind == "common" else json.dumps(document).encode()
    if kind == "malformed":
        data = b"{"
    with pytest.raises(InvalidKernelInput):
        build_contract_validator(data, common)


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate",
        "reverse",
        "repeat_count",
        "empty_look",
        "empty_gate",
        "long_gate",
        "zero",
        "fraction",
        "boolean",
    ],
)
def test_invalid_look_semantics(contracts: ContractValidator, fault: str) -> None:
    plan = raw_plan()
    plan["stopping_mode"] = "bonferroni_finite_looks"
    plan["look_plan"] = [
        {"look_id": "first", "cumulative_independent_units": 100, "claim_alpha": {"g": 0.02}},
        {"look_id": "second", "cumulative_independent_units": 200, "claim_alpha": {"g": 0.03}},
    ]
    item = plan["look_plan"][1]
    if fault == "duplicate":
        item["look_id"] = "first"
    elif fault == "empty_look":
        item["look_id"] = ""
    elif fault in ("empty_gate", "long_gate"):
        item["claim_alpha"] = {"" if fault == "empty_gate" else "x" * 257: 0.03}
    else:
        item["cumulative_independent_units"] = {
            "reverse": 99,
            "repeat_count": 100,
            "zero": 0,
            "fraction": 200.5,
            "boolean": True,
        }[fault]
    with pytest.raises(InvalidKernelInput):
        validate_inference_plan(plan, contracts)


def test_fixed_and_finite_look_plans(contracts: ContractValidator) -> None:
    plan = raw_plan()
    plan["stopping_mode"] = "bonferroni_finite_looks"
    plan["look_plan"] = [
        {"look_id": "first", "cumulative_independent_units": 100, "claim_alpha": {"g": 0.02}},
        {"look_id": "second", "cumulative_independent_units": 200, "claim_alpha": {"g": 0.03}},
    ]
    parsed = validate_inference_plan(plan, contracts)
    assert qualify_numeric_profile(
        parsed, "second", "g", metric_role="retention"
    ).alpha == Fraction("0.03")
    plan["stopping_mode"] = "fixed_final"
    with pytest.raises(InvalidKernelInput):
        validate_inference_plan(plan, contracts)


@pytest.mark.parametrize("excess", [0.05000000000000001, 0.06])
def test_alpha_budget_no_epsilon_bypass(contracts: ContractValidator, excess: float) -> None:
    plan = raw_plan()
    plan["look_plan"][0]["claim_alpha"] = {"g": excess}
    with pytest.raises(InvalidKernelInput):
        validate_inference_plan(plan, contracts)


def test_exact_same_budget_from_equivalent_json_numbers(contracts: ContractValidator) -> None:
    plan = raw_plan()
    plan["look_plan"][0]["claim_alpha"] = json.loads('{"effect": 2e-2, "retention": 0.0300}')
    first = validate_inference_plan(plan, contracts)
    plan["look_plan"][0]["claim_alpha"] = {"effect": 0.02, "retention": 0.03}
    second = validate_inference_plan(plan, contracts)
    assert first == second


@pytest.mark.parametrize(
    "fault",
    ["case-family-method", "family-null-cluster", "repeated-case", "empty-claims", "invalid-alpha"],
)
def test_schema_cross_field_constraints(contracts: ContractValidator, fault: str) -> None:
    plan = raw_plan(family=fault == "family-null-cluster")
    if fault == "case-family-method":
        plan["paired_effect_method"] = "bounded_independent_family_hoeffding_v1"
    elif fault == "family-null-cluster":
        plan["cluster_method_ref"] = None
    elif fault == "repeated-case":
        plan["repetitions"] = 3
    elif fault == "empty-claims":
        plan["look_plan"][0]["claim_alpha"] = {}
    else:
        plan["look_plan"][0]["claim_alpha"] = {"claim": math.nan}
    with pytest.raises(InvalidKernelInput):
        validate_inference_plan(plan, contracts)


@pytest.mark.parametrize("family", [False, True])
@pytest.mark.parametrize("role,expected", [("effect", 2), ("retention", 1)])
def test_metric_role_selects_frozen_sides(
    contracts: ContractValidator, family: bool, role: Any, expected: int
) -> None:
    plan = raw_plan(family=family)
    opposite = "retention" if role == "effect" else "effect"
    plan["look_plan"][0]["claim_alpha"] = {opposite: 0.05}
    parsed = validate_inference_plan(plan, contracts)
    assert qualify_numeric_profile(parsed, "final", opposite, metric_role=role).sides == expected


@pytest.mark.parametrize("role", [None, "unknown", {}, True])
def test_invalid_metric_role_and_override(contracts: ContractValidator, role: Any) -> None:
    parsed = validate_inference_plan(raw_plan(), contracts)
    with pytest.raises(InvalidKernelInput):
        qualify_numeric_profile(parsed, "final", "generated-claim", metric_role=role)


def test_missing_role_and_side_override(contracts: ContractValidator) -> None:
    parsed = validate_inference_plan(raw_plan(), contracts)
    with pytest.raises(TypeError):
        qualify_numeric_profile(parsed, "final", "generated-claim")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        qualify_numeric_profile(parsed, "final", "generated-claim", metric_role="effect", sides=1)  # type: ignore[call-arg]


def test_replaced_validated_plan_is_not_revalidated_by_assertion(
    contracts: ContractValidator,
) -> None:
    parsed = validate_inference_plan(raw_plan(), contracts)
    changed = dataclasses.replace(parsed, effect_interval_sides=1)
    with pytest.raises(InvalidKernelInput):
        qualify_numeric_profile(changed, "final", "generated-claim", metric_role="effect")


@pytest.mark.parametrize(
    "looks,claims,supported", [(64, 128, True), (65, 1, False), (1, 129, False)]
)
def test_plan_size_profile_boundary(
    contracts: ContractValidator, looks: int, claims: int, supported: bool
) -> None:
    plan = raw_plan()
    plan["stopping_mode"] = "bonferroni_finite_looks"
    plan["look_plan"] = [
        {
            "look_id": str(index),
            "cumulative_independent_units": index + 1,
            "claim_alpha": {str(gate): 1e-6 for gate in range(claims)},
        }
        for index in range(looks)
    ]
    parsed = validate_inference_plan(plan, contracts)
    if supported:
        assert qualify_numeric_profile(parsed, "0", "0", metric_role="effect").alpha == Fraction(
            "1e-6"
        )
    else:
        with pytest.raises(UnsupportedNumericProfile):
            qualify_numeric_profile(parsed, "0", "0", metric_role="effect")
    with pytest.raises(InvalidKernelInput):
        qualify_numeric_profile(parsed, "absent", "0", metric_role="effect")
    with pytest.raises(InvalidKernelInput):
        qualify_numeric_profile(parsed, "0", "absent", metric_role="effect")


@pytest.mark.parametrize("count,supported", [(1_000_000, True), (1_000_001, False)])
def test_count_profile_boundary(contracts: ContractValidator, count: int, supported: bool) -> None:
    parsed = validate_inference_plan(raw_plan(n=count), contracts)
    if supported:
        assert qualify_numeric_profile(parsed, "final", "generated-claim", metric_role="effect")
    else:
        with pytest.raises(UnsupportedNumericProfile):
            qualify_numeric_profile(parsed, "final", "generated-claim", metric_role="effect")


def test_denominator_reconciliation() -> None:
    value = counts(200, unknown=20)
    value["runtime_failed"] = 10
    assert (
        validate_denominators(value, {"b": 40, "c": 10, "both_correct": 130, "both_wrong": 20})
        == value
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("attempted", 199),
        ("runtime_failed", 201),
        ("unknown", True),
        ("excluded", -1),
        ("paired_gradeable", 200.0),
        ("extra", 0),
    ],
)
def test_invalid_denominators(field: str, value: Any) -> None:
    data: dict[str, Any] = counts(200)
    data[field] = value
    with pytest.raises(InvalidKernelInput):
        validate_denominators(data)


@pytest.mark.parametrize("field", list(counts(0)))
def test_missing_denominator_field(field: str) -> None:
    data = counts(200)
    del data[field]
    with pytest.raises(InvalidKernelInput):
        validate_denominators(data)


@pytest.mark.parametrize(
    "cells",
    [
        {"b": 0, "c": 0, "both_correct": 199, "both_wrong": 0},
        {"b": True, "c": 0, "both_correct": 199, "both_wrong": 0},
        {"b": -1, "c": 0, "both_correct": 201, "both_wrong": 0},
        {"b": 1.0, "c": 0, "both_correct": 199, "both_wrong": 0},
        {"b": 0, "c": 0, "both_correct": 200},
    ],
)
def test_invalid_cell_reconciliation(cells: object) -> None:
    with pytest.raises(InvalidKernelInput):
        validate_denominators(counts(200), cells)


@pytest.mark.parametrize(
    "n,reason", [(0, "no_gradeable_units"), (1, "singleton"), (199, "incomplete_planned_look")]
)
def test_empty_unknown_singleton(contracts: ContractValidator, n: int, reason: str) -> None:
    parsed = validate_inference_plan(raw_plan(), contracts)
    result = compute_interval(
        parsed,
        counts(n, unknown=200 - n),
        "final",
        "generated-claim",
        metric_role="effect",
        pair_cells={"b": 0, "c": 0, "both_correct": n, "both_wrong": 0},
    )
    assert result.undefined_reason == reason
    assert result.value is result.lower is result.upper is None


@pytest.mark.parametrize("supplied", ["missing", "extra", "mixed", "wrong_units", "surplus"])
def test_missing_surplus_mixed_aggregates(contracts: ContractValidator, supplied: str) -> None:
    parsed = validate_inference_plan(raw_plan(n=2), contracts)
    cell: Any = {"b": 1, "c": 0, "both_correct": 1, "both_wrong": 0}
    den = counts(2)
    kwargs: dict[str, Any] = {"pair_cells": cell}
    if supplied == "missing":
        kwargs.clear()
    elif supplied == "extra":
        cell["extra"] = 1
    elif supplied == "mixed":
        kwargs["family_effects"] = []
    elif supplied == "wrong_units":
        den["independent_units"] = 1
    else:
        den = counts(3)
        cell["both_correct"] = 2
    with pytest.raises(InvalidKernelInput):
        compute_interval(parsed, den, "final", "generated-claim", metric_role="effect", **kwargs)


def test_surplus_observations_stay_invalid_above_profile(contracts: ContractValidator) -> None:
    parsed = validate_inference_plan(raw_plan(n=1_000_001), contracts)
    with pytest.raises(InvalidKernelInput):
        compute_interval(
            parsed,
            counts(1_000_002),
            "final",
            "generated-claim",
            metric_role="effect",
            pair_cells={"b": 0, "c": 0, "both_correct": 1_000_002, "both_wrong": 0},
        )


def test_invalid_plan_input_carries_no_private_data(caplog: pytest.LogCaptureFixture) -> None:
    marker = "generated-sensitive-value"
    inference = (
        ROOT / "packages/contracts/learning/benchmarks/statistical-design.schema.json"
    ).read_bytes()
    common = (ROOT / "packages/contracts/common/common-contract.schema.json").read_bytes()
    validator = build_contract_validator(inference, common)
    with pytest.raises(InvalidKernelInput) as error:
        validate_inference_plan({**raw_plan(), "extra": marker}, validator)
    assert marker not in str(error.value)
    assert marker not in caplog.text
