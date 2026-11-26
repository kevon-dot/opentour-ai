"""An owner-declared or development-only rule never sees protected reference labels."""

import copy

import pytest
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.simple_repair import (
    RepairError,
    assess_development,
    declared_rule,
    fit_rule,
    predict_rule,
    validate_rule,
)


def auto(label):
    return {"status": "automatic", "label": label}


def examples(n=4):
    return [
        {
            "case_id": f"gen-{i}",
            "group_id": f"family-{i}",
            "features": {"bucket": "x"},
            "incumbent": auto("b"),
            "reference": "a",
        }
        for i in range(n)
    ]


def test_declared_rule_inference_abstention_and_corruption():
    art = declared_rule(
        feature="bucket", allowed_features=["bucket"], mapping={"x": "a"}, allowed_labels=["a"]
    )
    assert validate_rule(art) == art
    assert predict_rule(art, {"bucket": "x"}) == auto("a")
    assert predict_rule(art, {}) == {"status": "review"}
    previous = declared_rule(
        feature="bucket", allowed_features=["bucket"], mapping={}, fallback="incumbent"
    )
    assert predict_rule(previous, {}, auto("existing")) == auto("existing")
    assert predict_rule(previous, {}) == {"status": "missing"}
    corrupted = copy.deepcopy(art)
    corrupted["mapping"]["x"] = "b"
    with pytest.raises(RepairError, match="revision_mismatch"):
        predict_rule(corrupted, {"bucket": "x"})
    with pytest.raises(RepairError, match="unpermitted_output"):
        declared_rule(
            feature="bucket",
            allowed_features=["bucket"],
            mapping={"x": "unsafe"},
            allowed_labels=["a"],
        )


@pytest.mark.parametrize(
    "feature,allowed",
    [
        ("bucket", ["safe"]),
        ("reference", ["reference"]),
        ("event_id", ["event_id"]),
        ("bucket", ["bucket", "label"]),
    ],
)
def test_unavailable_or_outcome_only_field_cannot_be_selected(feature, allowed):
    with pytest.raises(RepairError, match="decision_time_feature_required"):
        fit_rule(examples(), feature=feature, allowed_features=allowed)


def test_group_out_qualification_and_equal_replay_zero_model_calls():
    items = examples()
    terms = {"minimum_coverage_bps": 10000, "error_ceiling_bps": 0}
    art = fit_rule(items, feature="bucket", allowed_features=["bucket"])
    assert fit_rule(items[::-1], feature="bucket", allowed_features=["bucket"]) == art
    regime = assess_development(
        items, art, terms=terms, model_authorized=True, compute_authorized=True
    )
    assert regime["route"] == "simple" and regime["fit_calls"] == 0
    assert regime["paired"]["fixes"] == 4 and regime["validation"] == "leave_one_group_out"
    assert (
        regime["revision"]
        == regime["residual_revision"]
        == digest({k: v for k, v in regime.items() if k not in ("revision", "residual_revision")})
    )
    few = examples(2)
    apparent = fit_rule(few, feature="bucket", allowed_features=["bucket"])
    assert predict_rule(apparent, {"bucket": "x"}) == auto("a")
    held = assess_development(few, apparent, terms=terms)
    assert held["route"] == "hold"  # One other independent vote is below min_support.
    with pytest.raises(RepairError, match="development_revision_mismatch"):
        assess_development(items[:-1], art, terms=terms)


def test_distinct_recurring_residual_and_named_rights_required():
    rows = examples()
    rule = declared_rule(feature="bucket", allowed_features=["bucket"], mapping={"x": "b"})
    terms = {"minimum_coverage_bps": 10000, "error_ceiling_bps": 0}
    denied = assess_development(rows, rule, terms=terms, model_authorized=True)
    assert denied["route"] == "hold"
    permitted = assess_development(
        rows, rule, terms=terms, model_authorized=True, compute_authorized=True
    )
    assert permitted["route"] == "model" and permitted["residual_groups"] == 4
    assert permitted["residual_class"] == ["b", "a"]
    distinct = examples(2)
    distinct[1]["reference"] = "unrelated"
    assert (
        assess_development(
            distinct, rule, terms=terms, model_authorized=True, compute_authorized=True
        )["route"]
        == "hold"
    )
    for r in rows:
        r["group_id"] = "same"
    assert (
        assess_development(rows, rule, terms=terms, model_authorized=True, compute_authorized=True)[
            "route"
        ]
        == "hold"
    )


def test_tie_missing_truth_duplicate_rows_and_ambiguous_mapping():
    terms = {"minimum_coverage_bps": 10000, "error_ceiling_bps": 0}
    rows = examples()
    for row in rows:
        row["incumbent"] = auto("a")
    declared = declared_rule(feature="bucket", allowed_features=["bucket"], mapping={"x": "a"})
    assert assess_development(rows, declared, terms=terms)["route"] == "no_change"
    rows[0]["reference"] = None
    assert assess_development(rows, declared, terms=terms)["route"] == "hold"
    with pytest.raises(RepairError, match="duplicate_case"):
        fit_rule([*rows, rows[0]], feature="bucket", allowed_features=["bucket"])
    split = examples(4)
    for row in split[:2]:
        row["reference"] = "b"
    ambiguous = fit_rule(split, feature="bucket", allowed_features=["bucket"])
    assert predict_rule(ambiguous, {"bucket": "x"}) == {"status": "review"}
