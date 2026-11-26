"""Independent checks of the private Slack projection from actual protected evaluator results."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from opentour_cell.pilot.slack_benchmark import (
    benchmark_summary,
    select_benchmark,
    validated_benchmark,
)
from opentour_cell.pilot.training import LocalTrainingStore
from test_comparison_plan import auto, enroll, isolated_score, rows, setup
from test_pilot_metrics import observations

Json = dict[str, Any]


def measured(tmp_path, *, basis="observed", other=None, missing=False, model=False):
    store = LocalTrainingStore(tmp_path / "training.sqlite3", "generated")
    protected = rows("protected")
    protected[0]["incumbent"] = {"status": "review"}
    data = enroll(store, protected=protected)
    first = observations(protected, basis=basis)
    second = observations(protected, basis=basis)
    for m in second["cases"].values():
        m.update(review_seconds="0", latency_ms="0", usage="0")
    if missing:
        del second["cases"][protected[-1]["case_id"]]["review_seconds"]
    second.update(other or {})
    meters = {"incumbent": first, "simple": second}
    model_outputs = {item["case_id"]: auto("a") for item in protected} if model else None
    if model:
        meters["model"] = second
    plan, rule = setup(
        data, measured=meters, model=model_outputs, mapping={"x": "b"} if model else None
    )
    return isolated_score(
        store, plan, data, rule=rule, measurements=meters, model_predictions=model_outputs
    )


def test_selected_frozen_rule_and_observed_work_are_presented_with_real_denominators(tmp_path):
    report = measured(tmp_path)
    value = select_benchmark(report)
    assert (
        value["candidate_arm"] == "simple"
        and value["candidate_revision"] == report["arms"]["simple"]["revision"]
    )
    rendered = benchmark_summary(value)
    assert "1/4 → 0/4 (25 → 0 per 100, 25 fewer per 100)" in rendered
    assert "Review minutes / 100  10 → 0. 10 lower." in rendered
    assert "Mean workflow latency (ms)  5 → 0." in rendered
    assert "Cost (USD) / 1,000  200 → 0." in rendered
    assert "Observed in the benchmark" in rendered and "n=3, small sample" in rendered
    assert "Realized post-adoption impact is unmeasured" in rendered
    assert all(
        s not in json.dumps(value)
        for s in (
            "generated-consent",
            "generated-meter",
            "protected-0",
            "family-",
            "independent_reference",
            "hardware",
        )
    )


@pytest.mark.parametrize(
    "basis,phrase", [("generated", "Generated example"), ("modeled", "Modeled estimate")]
)
def test_nonobserved_sources_never_claim_actual_customer_savings(tmp_path, basis, phrase):
    result = select_benchmark(measured(tmp_path, basis=basis, model=True))
    assert result["candidate_arm"] == "model"
    text = benchmark_summary(result)
    assert phrase in text and "Observed in the benchmark" not in text
    assert "saved" not in text.lower() and "Realized post-adoption impact is unmeasured" in text


@pytest.mark.parametrize(
    "changed",
    [{"currency": "EUR"}, {"unit": "other_unit"}, {"period_end": 3}, {"basis": "modeled"}],
)
def test_incomparable_meters_keep_routing_and_hide_differences(tmp_path, changed):
    text = benchmark_summary(select_benchmark(measured(tmp_path, other=changed)))
    assert "1/4 → 0/4" in text and "measurement bases, periods or units differ" in text
    assert "Cost (USD)" not in text and "Review minutes / 100" not in text


def test_partial_observations_preserve_the_valid_arm_and_complete_metrics(tmp_path):
    text = benchmark_summary(select_benchmark(measured(tmp_path, missing=True)))
    assert "Review minutes / 100  10 → unavailable. Recorded 4/4 → 3/4." in text
    assert "Cost (USD) / 1,000  200 → 0." in text


def test_no_measurement_and_empty_or_correlated_cases_do_not_manufacture_evidence(tmp_path):
    store = LocalTrainingStore(tmp_path / "private.sqlite3", "generated")
    present = rows("cohort")
    present[1]["group_id"] = present[0]["group_id"]
    data = enroll(store, protected=present)
    plan, rule = setup(data)
    result = select_benchmark(isolated_score(store, plan, data, rule=rule))
    text = benchmark_summary(result)
    assert "no comparable measurements" in text and "zero verified or correlated cases" in text
    empty = enroll(store, protected=[])
    plan, rule = setup(empty)
    zero = benchmark_summary(select_benchmark(isolated_score(store, plan, empty, rule=rule)))
    assert "0 eligible decisions" in zero and "zero verified or correlated cases" in zero


def test_strict_reprojection_refuses_stale_corrupt_bindings_and_drops_private_fields(tmp_path):
    result = select_benchmark(measured(tmp_path))
    args = dict(
        cohort_revision=result["cohort_revision"],
        population=4,
        candidate_arm="simple",
        candidate_revision=result["candidate_revision"],
    )
    edited = copy.deepcopy(result)
    edited["unknown"] = "private-case"
    edited["candidate"]["meter"]["raw"] = "private-case"
    assert validated_benchmark(edited, **args) == result
    for key, value in (
        ("cohort_revision", "a" * 64),
        ("population", 3),
        ("candidate_arm", "model"),
        ("candidate_revision", "b" * 64),
    ):
        with pytest.raises(ValueError, match="invalid_workflow_benchmark"):
            validated_benchmark(result, **(args | {key: value}))
    for field, change in (("verified", 99), ("review", -1), ("automatic", True)):
        bad = copy.deepcopy(result)
        bad["candidate"][field] = change
        with pytest.raises(ValueError, match="invalid_workflow_benchmark"):
            validated_benchmark(bad, **args)
    edited = copy.deepcopy(result)
    edited["candidate"]["meter"]["readings"]["cost_per_1000"]["value"] = "<@private>"
    with pytest.raises(ValueError, match="invalid_workflow_benchmark"):
        benchmark_summary(edited)


def test_reversed_workload_and_unchanged_work_are_visible(tmp_path):
    result = select_benchmark(measured(tmp_path))
    result["incumbent"], result["candidate"] = result["candidate"], result["incumbent"]
    assert "25 more per 100" in benchmark_summary(result)
    result["candidate"] = result["incumbent"]
    assert "unchanged" in benchmark_summary(result)


def test_only_a_genuine_unscored_hold_can_omit_the_benchmark(tmp_path):
    from opentour_cell.pilot.comparison_plan import development_hold

    store = LocalTrainingStore(tmp_path / "private.sqlite3", "generated")
    data = enroll(store)
    plan, _ = setup(data, mapping={"x": "b"})
    held = development_hold(plan)
    assert select_benchmark(held) == {}
    for change in (
        {"outcome": "model_candidate"},
        {"protected_rows_evaluated": 1},
        {"paired_cases": [{}]},
        {"comparisons": {"incumbent_vs_simple": {}}},
    ):
        with pytest.raises(ValueError, match="invalid_workflow_benchmark"):
            select_benchmark(held | change)
