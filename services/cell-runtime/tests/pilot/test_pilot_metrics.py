"""Denominators, owner-authorized economics, costs, missing information and small samples."""

import copy

import pytest
from opentour_cell.pilot.pilot_metrics import arm_metrics, economic_comparison, economics
from opentour_cell.pilot.readout import INPUT_VERSION, evaluate_manifest
from opentour_cell.pilot.simple_repair import RepairError


def auto(label):
    return {"status": "automatic", "label": label}


def observations(rows, *, usage="1", currency="USD", unit="cpu_second", basis="observed"):
    return {
        "basis": basis,
        "permission_ref": "generated-consent",
        "provenance_ref": "generated-meter",
        "hardware": "meter-a",
        "period_start": 1,
        "period_end": 2,
        "currency": currency,
        "unit": unit,
        "rate_per_unit": "0.20",
        "cases": {
            r["case_id"]: {"review_seconds": "6", "latency_ms": "5", "usage": usage} for r in rows
        },
    }


def test_metrics_include_review_material_errors_per_1000_and_wilson_uncertainty():
    rows = [
        {
            "case_id": "a",
            "group_id": "ga",
            "slices": ["material"],
            "reference": "x",
            "simple": auto("y"),
        },
        {"case_id": "b", "group_id": "gb", "slices": [], "reference": "x", "simple": auto("x")},
        {
            "case_id": "c",
            "group_id": "gc",
            "slices": [],
            "reference": "x",
            "simple": {"status": "review"},
        },
        {
            "case_id": "d",
            "group_id": "gd",
            "slices": [],
            "reference": None,
            "simple": {"status": "missing"},
        },
    ]
    cases = [
        {
            "case_id": r["case_id"],
            "reference": r["reference"],
            "current": r["simple"],
            "repair": r["simple"],
        }
        for r in rows
    ]
    arm = evaluate_manifest(
        {"schema_version": INPUT_VERSION, "error_ceiling_bps": 5000, "cases": cases}
    )["current"]
    terms = {
        "minimum_coverage_bps": 5000,
        "error_ceiling_bps": 5000,
        "material_slices": ["material"],
    }
    result = arm_metrics(arm, rows, "simple", terms)
    assert result["operating_point"] == {"coverage": "met", "error": "met", "combined": "met"}
    assert result["review_per_100"]["value"] == "25.000000"
    assert result["automatic_per_100"]["value"] == "50.000000"
    assert result["material_errors_per_1000"] == {
        "numerator": 1,
        "denominator": 4,
        "per": 1000,
        "value": "250.000000",
    }
    assert result["uncertainty"] == {
        "method": "wilson_95_independent_verified_cases",
        "verified": 2,
        "lower": 0.09453121,
        "upper": 0.90546879,
        "small_sample": True,
    }
    rows[1]["group_id"] = "ga"
    assert arm_metrics(arm, rows, "simple", terms)["uncertainty"]["method"] == "unavailable"
    empty = evaluate_manifest(
        {"schema_version": INPUT_VERSION, "error_ceiling_bps": 0, "cases": []}
    )
    missing = arm_metrics(empty["current"], [], "simple", terms)
    assert missing["operating_point"]["combined"] == "undetermined"
    assert missing["review_per_100"]["value"] is None


def test_real_decimal_rates_coverage_and_cost_caps_at_equality():
    rows = [{"case_id": "a"}, {"case_id": "b"}]
    m = observations(rows)
    m["cases"]["b"]["review_seconds"] = "0"
    terms = {"max_cost_per_1000": {"currency": "USD", "amount": "200"}, "max_latency_ms": "5"}
    adm = economics(rows, m, terms)
    assert (
        adm["review_minutes_per_100"] == "5.000000"
        and adm["review_minutes_per_1000"] == "50.000000"
    )
    assert adm["cost_per_1000"] == "200.000000" and adm["mean_latency_ms"] == "5.000000"
    assert adm["total_latency_ms_per_1000"] == "5000.000000"
    assert adm["latency_cap"] == adm["cost_cap"] == "met"
    low = economics(rows, observations(rows, usage="0.5"), terms)
    difference = economic_comparison(adm, low)
    assert (
        difference["basis"] == "observed" and difference["savings"]["cost_per_1000"] == "100.000000"
    )
    assert economic_comparison(low, adm)["observed_cost_increased"]
    incomplete = copy.deepcopy(m)
    del incomplete["cases"]["b"]["usage"]
    absent = economics(rows, incomplete, terms)
    assert absent["cost_per_1000"] is None and absent["cost_cap"] == "unavailable"
    assert absent["mean_latency_ms"] == "5.000000" and absent["observed_case_counts"]["usage"] == 1
    assert economics([], observations([]), {})["cost_per_1000"] is None


@pytest.mark.parametrize("change", [{"currency": "EUR"}, {"unit": "gpu_second"}, {"period_end": 3}])
def test_incompatible_units_period_or_currency_never_report_savings(change):
    rows = [{"case_id": "a"}]
    left = observations(rows)
    result = economic_comparison(economics(rows, left, {}), economics(rows, left | change, {}))
    assert result == {
        "basis": "unavailable",
        "reason": "currency_unit_or_period_mismatch",
        "savings": None,
    }


@pytest.mark.parametrize("basis", ["modeled", "generated"])
def test_generated_and_modeled_measurements_never_establish_savings(basis):
    rows = [{"case_id": "a"}]
    left = economics(rows, observations(rows, usage="5", basis=basis), {})
    right = economics(rows, observations(rows, usage="3", basis=basis), {})
    result = economic_comparison(left, right)
    assert result["savings"] is None
    assert result[basis + "_differences"]["cost_per_1000"] == "400.000000"
    assert economics(rows, None, {})["basis"] == "unavailable"


def test_scope_and_malformed_measurements_hold_without_fake_zero():
    rows = [{"case_id": "a"}]
    record = observations(rows)
    with pytest.raises(RepairError, match="outside_cohort"):
        economics(rows, {**record, "cases": {"unauthorized": {}}}, {})
    for value in (False, -1, "NaN", "1e5", 3.25):
        record["cases"]["a"]["latency_ms"] = value
        with pytest.raises(RepairError, match="invalid_decimal"):
            economics(rows, record, {})
    future = observations(rows) | {"period_end": 2**62}
    with pytest.raises(RepairError, match="window_in_future"):
        economics(rows, future, {})
    removal = observations(rows)
    del removal["provenance_ref"]
    with pytest.raises(RepairError, match="invalid_measurement_contract"):
        economics(rows, removal, {})
