"""Counts and denominators on the decision graphs must match the protected sample."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from opentour_cell.pilot.slack_charts import decision_charts
from test_slack_views import readout

VERSION = "rev_" + "a" * 26
COHORT = "b" * 64
RULE = readout([("a", v, v) for v in ("b", "b", "a", "a")])
MODEL = readout(
    [("a", c, m) for c, m in zip(("b", "b", "a", "a"), ("a", "a", "b", "a"), strict=True)]
)


def metrics(rule, model=None, *, result="simple_repair"):
    items = {"rule": rule}
    if model is not None:
        items["model"] = model
    return {
        "comparisons": items,
        "comparison_cohorts": dict.fromkeys(items, COHORT),
        "run_revision": "d" * 64,
        "cohort_revision": COHORT,
        "handoff_disposition": result,
        "private_case": "never-disclose-source",
    }


def charts(msg):
    return [block for block in msg["blocks"] if block["type"] == "data_visualization"]


def counts(block):
    return [[p["value"] for p in row["data"]] for row in block["chart"]["series"]]


@pytest.mark.parametrize("winner", ["simple_repair", "model_candidate"])
def test_selected_winner_is_named_even_when_both_alternatives_ran(winner):
    selected = MODEL if winner == "model_candidate" else RULE
    message = decision_charts(selected, metrics(RULE, MODEL, result=winner), VERSION)
    first, second = charts(message)
    assert counts(first) == [[4, 4, 4], [2, 2, 1]]
    if winner == "model_candidate":
        assert "tuned model" in second["title"] and counts(second) == [[2, 1, 1, 0]]
    else:
        assert "simple repair" in second["title"] and counts(second) == [[0, 0, 2, 2]]
    assert "4/4 eligible" in message["text"] and "1/4 verified" in message["text"]
    assert message["text"] == message["blocks"][0]["text"]["text"]
    assert "never-disclose" not in json.dumps(message)
    for b in (first, second):
        cats = b["chart"]["axis_config"]["categories"]
        assert len(b["title"]) <= 50 and all(len(c) <= 20 for c in cats)
        assert all([p["label"] for p in row["data"]] == cats for row in b["chart"]["series"])


def test_missing_verification_does_not_become_zero_error_and_held_is_not_a_winner():
    unknown = readout([(None, "a", "a"), (None, "a", "a")])
    msg = decision_charts(unknown, metrics(unknown), VERSION)
    assert len(charts(msg)) == 1 and counts(charts(msg)[0]) == [[2, 2]]
    assert (
        "verified error rate unavailable" in msg["text"]
        and "No comparable paired cases" in msg["text"]
    )
    held = decision_charts(MODEL, metrics(RULE, MODEL, result="hold"), VERSION)
    assert len(charts(held)) == 1 and "No repair was selected" in held["text"]


def test_small_and_different_automated_subsets_remain_visible():
    value = readout([("a", "a", "review"), ("b", "review", "b"), (None, "a", "b")])
    msg = decision_charts(value, metrics(value), VERSION)
    assert "Different cases were automated" in msg["text"]
    assert "2/3 eligible" in msg["text"] and "0/1 verified" in msg["text"]
    assert "unknown answer: 1" in msg["text"] and "3 excluded" in msg["text"]
    empty = readout([])
    result = decision_charts(empty, metrics(empty, result="hold"), VERSION)
    assert not charts(result) and "coverage unavailable" in result["text"]
    assert "No comparable paired cases" in result["text"]


@pytest.mark.parametrize("bad", ["cohort", "binding", "winner", "shape", "mismatch", "foreign"])
def test_wrong_comparator_missing_scope_or_inconsistent_aggregate_refused(bad):
    audit = metrics(deepcopy(RULE), deepcopy(MODEL), result="model_candidate")
    report, revision = MODEL, VERSION
    if bad == "cohort":
        audit["comparison_cohorts"]["rule"] = "c" * 64
    elif bad == "binding":
        del audit["comparison_cohorts"]
    elif bad == "winner":
        report = RULE
    elif bad == "shape":
        audit["comparisons"]["model"]["paired"]["fixes"] = 90
    elif bad == "mismatch":
        audit["comparisons"]["rule"] = readout([("a", "a", "a")])
    else:
        revision = "<@someone>"
    with pytest.raises(ValueError):
        decision_charts(report, audit, revision)
