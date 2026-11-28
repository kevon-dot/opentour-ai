"""Generated private Home and slash journeys for result-scoped workplace questions."""

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from opentour_cell.pilot.intake_admission import AuthorizedSource, DurableIntake
from opentour_cell.pilot.intake_mcp import IntakePrincipal
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.source_intake import check_current_intake
from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake, MaterializedBenchmark
from test_app_home import click, publish
from test_managed_slack import setup_workflow
from test_reviewer_workflow import buttons, screen, supplied, text
from test_slack_adapter import ACTOR, Harness
from test_status_questions import counts, inspect
from test_workflow_benchmarks import HISTORY


@pytest.mark.parametrize("surface", ["home", "slash"])
@pytest.mark.parametrize("support", [True, False])
@pytest.mark.parametrize("managed", [True, False])
def test_workplace_questions_inspect_current_frozen_scope(
    tmp_path, monkeypatch, surface, support, managed
):
    if managed:
        h, plan, *_ = setup_workflow(tmp_path, "rule")
        intake = check_current_intake(
            h.path.parent, h.store.organization, h.store.engagement_id, plan["intake_revision"]
        )
        h.config["live_workflow_ref"] = intake["workflow"]
    else:
        h = Harness(tmp_path)
        h.config["users"][ACTOR]["datasets"] = [supplied(h)]
    h.save()
    _, revision = inspect(h)
    before = counts(h)
    question = (
        "How many independent workplace cases support this?"
        if support
        else "Did synthetic data help?"
    )
    if surface == "slash":
        view = h.launch(prompt=question)
    else:
        click(h, publish(h, monkeypatch, ACTOR), "ot_app_prompt", ACTOR)
        view = list(h.slack.screens)[-1]
        h.form(view, {"command": {"command": {"value": question}}})
    assert "Dataset " in text(h, view)
    assert buttons(screen(h, view))["ot_result_open"] == revision
    h.action(view, "ot_result_open", revision)
    rendered = text(h, view)
    assert screen(h, view)["callback_id"] == "ot_status_question" and revision in rendered
    if managed:
        assert (
            f"Workflow {intake['workflow']}" in rendered and "evaluates a simple rule" in rendered
        )
    else:
        assert "scored supplied outputs" in rendered
    if support:
        assert "Independent workplace support is unavailable" in rendered
        assert "frozen cohort contains" in rendered and "data custodian" in rendered
    else:
        assert "Augmentation benefit is unproven" in rendered
        assert "Incremental cost is unavailable" in rendered and "evaluation operator" in rendered
    h.action(view, "ot_evidence", buttons(screen(h, view))["ot_evidence"])
    assert counts(h) == before


def test_rebound_workflow_and_late_revocation_withhold_workplace_status(tmp_path, monkeypatch):
    h, plan, *_ = setup_workflow(tmp_path, "rule")
    workflow = check_current_intake(
        h.path.parent, h.store.organization, h.store.engagement_id, plan["intake_revision"]
    )["workflow"]
    h.config["live_workflow_ref"] = workflow
    h.save()
    _, revision = inspect(h)
    view = h.launch(prompt="Did synthetic data help?")
    assert buttons(screen(h, view))["ot_result_open"] == revision
    h.config["live_workflow_ref"] = "different-workflow"
    h.save()
    h.action(view, "ot_result_open", revision)
    assert "Augmentation benefit" not in text(h, view)
    view = h.launch(prompt="Did synthetic data help?")
    assert "ot_result_open" not in buttons(screen(h, view))
    h.config["live_workflow_ref"] = workflow
    h.save()
    view = h.launch(prompt="How many independent workplace cases support this?")
    original = h.app.views.status_question_modal

    def withdraw(*args, **kwargs):
        layout = original(*args, **kwargs)
        h.config["users"][ACTOR]["datasets"] = []
        h.save()
        return layout

    monkeypatch.setattr(h.app.views, "status_question_modal", withdraw)
    h.action(view, "ot_result_open", revision)
    assert workflow not in text(h, view) and "Independent workplace support" not in text(h, view)


def test_old_workplace_page_does_not_rebind_to_another_workflow(tmp_path, monkeypatch):
    from opentour_cell.pilot import slack_adapter
    from test_reviewer_workflow import generated_evaluation_dataset

    h, plan, *_ = setup_workflow(tmp_path, "rule")
    monkeypatch.setattr(slack_adapter, "RESULT_PAGE_SIZE", 1)
    intake = check_current_intake(
        h.path.parent, h.store.organization, h.store.engagement_id, plan["intake_revision"]
    )
    managed = intake["dataset_id"]
    data = generated_evaluation_dataset(h.store.organization)
    h.store.register(data)
    history = data["dataset_id"]
    h.config["users"][ACTOR]["datasets"] = [managed, history]
    h.save()
    for dataset in (managed, history):
        view = h.launch()
        for _ in range(3):
            choices = screen(h, view)["blocks"]
            option = next(
                (
                    r["accessory"]["value"]
                    for r in choices
                    if "accessory" in r and r["text"]["text"].startswith(dataset + " · ")
                ),
                None,
            )
            if option:
                break
            next_button = buttons(screen(h, view))["ot_result_next"]
            h.action(view, "ot_result_next", next_button)
        assert option is not None
        h.action(view, "ot_result_open", option)
        revision = buttons(screen(h, view))["ot_evidence"]
        h.action(view, "ot_evidence", revision)
    picker = h.launch(prompt="Did synthetic data help?")
    initial = screen(h, picker)
    assert "ot_result_next" in buttons(initial)
    before = counts(h)
    h.config["live_workflow_ref"] = intake["workflow"]
    h.save()
    h.action(picker, "ot_result_next", buttons(initial)["ot_result_next"])
    after = screen(h, picker)
    assert after == initial or "ot_result_open" not in buttons(after)
    assert counts(h) == before


@pytest.mark.parametrize("surface", ["home", "slash"])
@pytest.mark.parametrize(
    ("question", "expected", "next_actor"),
    [
        ("Graph baseline versus rule on v27", "requested version 27", "evaluation operator"),
        ("Graph baseline versus rule on latest", "Choose one benchmark version", "Ask again"),
        (
            "Which workflows had recurring invoice code errors last month?",
            "previous calendar month",
            "data custodian",
        ),
    ],
)
def test_typed_query_holds_are_private_and_read_only(
    tmp_path, monkeypatch, surface, question, expected, next_actor
):
    h = Harness(tmp_path)
    before = counts(h)
    if surface == "slash":
        view = h.launch(prompt=question)
    else:
        click(h, publish(h, monkeypatch, ACTOR), "ot_app_prompt", ACTOR)
        view = list(h.slack.screens)[-1]
        h.form(view, {"command": {"command": {"value": question}}})
    assert screen(h, view)["callback_id"] == "ot_query_hold"
    assert expected in text(h, view) and next_actor in text(h, view)
    assert not buttons(screen(h, view))
    assert counts(h) == before


@pytest.mark.parametrize("surface", ["home", "slash"])
def test_chart_selection_rechecks_frozen_receipt_and_named_grant(tmp_path, monkeypatch, surface):
    h, plan, run, outcome = setup_workflow(tmp_path, "model")
    assert outcome == "model_candidate"
    cfg = h.app.settings()
    target = h.app._managed(cfg, ACTOR, "managed:" + run)
    intake = check_current_intake(
        cfg.directory, cfg.organization, h.store.engagement_id, plan["intake_revision"]
    )
    principal = IntakePrincipal(cfg.organization, "generated-intake", "intake")
    authority = Mock()
    authority.guard.side_effect = lambda actor: h.store.engagement_guard(read_only=True)
    authority.historical.return_value = AuthorizedSource(
        intake["workflow"], h.store.engagement_id, "evaluation", HISTORY.pair_ref, "grant-v1"
    )
    benchmarks = BenchmarkIntake(DurableIntake(cfg.directory, authority))
    benchmarks.submit_intake(principal, HISTORY)
    second = replace(HISTORY, source_revision="trace-v2", idempotency_key="history-v2")
    admitted = benchmarks.submit_intake(principal, second)
    comparison_dir = cfg.directory / "managed-outputs" / run
    frozen_result = digest(json.loads((comparison_dir / "comparison.json").read_bytes()))
    frozen_plan = json.loads((comparison_dir / "plan.json").read_bytes())["revision"]
    assert frozen_result == target.audit["run_revision"]
    # The generated producer publishes the actual managed fixture's result through
    # the durable benchmark API. The Slack query performs the normal real join.
    producer = Mock()
    producer.materialize.return_value = MaterializedBenchmark(
        "scored",
        "protected-plan-" + frozen_plan,
        frozen_plan,
        second.source_revision,
        "protected-result-" + frozen_result,
        frozen_result,
    )
    assert benchmarks.materialize(principal, admitted.run_ref, producer)
    assert benchmarks.get_run(principal, admitted.run_ref).benchmark.version == 2
    before = counts(h)
    question = "Graph baseline versus rule on v2"

    def ask():
        if surface == "slash":
            return h.launch(prompt=question)
        click(h, publish(h, monkeypatch, ACTOR), "ot_app_prompt", ACTOR)
        view = list(h.slack.screens)[-1]
        h.form(view, {"command": {"command": {"value": question}}})
        return view

    view = ask()
    pin = buttons(screen(h, view))["ot_result_open"]
    h.action(view, "ot_result_open", pin)
    assert screen(h, view)["callback_id"] == "ot_arm_chart"
    rendered = text(h, view)
    assert "frozen benchmark v2" in rendered and "Incumbent" in rendered
    assert "Simple repair" in rendered and "model" not in rendered.casefold()
    for key in ("current", "repair"):
        arm = target.audit["comparisons"]["rule"][key]
        assert (
            f"{arm['incorrect_automatic']}/{arm['observed_error_rate']['denominator']}" in rendered
        )

    view = ask()
    pin = buttons(screen(h, view))["ot_result_open"]
    grant = next(cfg.directory.glob("slack-review-*.json"))
    backup = grant.with_suffix(".generated-backup")
    grant.rename(backup)
    h.action(view, "ot_result_open", pin)
    assert screen(h, view).get("callback_id") != "ot_arm_chart"
    backup.rename(grant)
    view = ask()
    pin = buttons(screen(h, view))["ot_result_open"]
    third = replace(HISTORY, source_revision="trace-v3", idempotency_key="history-v3")
    benchmarks.submit_intake(principal, third)
    h.action(view, "ot_result_open", pin)
    assert screen(h, view).get("callback_id") != "ot_arm_chart"
    assert counts(h) == before
