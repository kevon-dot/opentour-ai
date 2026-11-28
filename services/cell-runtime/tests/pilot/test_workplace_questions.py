"""Generated private Home and slash journeys for result-scoped workplace questions."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from opentour_cell.pilot.intake_admission import AuthorizedSource, DurableIntake
from opentour_cell.pilot.intake_mcp import IntakePrincipal
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.source_intake import check_current_intake
from opentour_cell.pilot.workflow_benchmarks import (
    BenchmarkIntake,
    BenchmarkScope,
    MaterializedBenchmark,
)
from test_app_home import click, publish
from test_managed_slack import setup_workflow
from test_reviewer_workflow import buttons, screen, supplied, text
from test_scoped_query_report import reporting_signers
from test_signed_records import kit as signer_kit  # noqa: F401
from test_signed_records import sign, stamp
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
        assert "comparable cases" in rendered and "newly wrong" in rendered
        assert "no matched real-only and augmented arms" in rendered
        assert "Incremental cost is unavailable" in rendered and "evaluation operator" in rendered
    h.action(view, "ot_evidence", buttons(screen(h, view))["ot_evidence"])
    assert counts(h) == before


@pytest.mark.parametrize("surface", ["home", "slash"])
def test_label_savings_separates_frozen_references_from_training_effort(
    tmp_path, monkeypatch, surface
):
    h, plan, run, _ = setup_workflow(tmp_path, "rule")
    intake = check_current_intake(
        h.path.parent, h.store.organization, h.store.engagement_id, plan["intake_revision"]
    )
    h.config["live_workflow_ref"] = intake["workflow"]
    h.save()
    _, revision = inspect(h)
    target = h.app._managed(h.app.settings(), ACTOR, "managed:" + run)
    question = "How many real labels did this save?"
    before = counts(h)

    def ask():
        if surface == "slash":
            return h.launch(prompt=question)
        click(h, publish(h, monkeypatch, ACTOR), "ot_app_prompt", ACTOR)
        view = list(h.slack.screens)[-1]
        h.form(view, {"command": {"command": {"value": question}}})
        return view

    view = ask()
    h.action(view, "ot_result_open", buttons(screen(h, view))["ot_result_open"])
    observed = text(h, view)
    assert "Label savings not measured." in observed and revision in observed
    assert f"Workflow {intake['workflow']}" in observed
    assert (
        f"{target.report['known_references']} known references across "
        f"{target.report['cohort_size']} cases" in observed
    )
    assert "matched label-budget comparison" in observed and "expert minutes" in observed
    assert buttons(screen(h, view))["ot_evidence"] == revision

    view = ask()
    pin = buttons(screen(h, view))["ot_result_open"]
    h.config["live_workflow_ref"] = "another-workflow"
    h.save()
    h.action(view, "ot_result_open", pin)
    assert "Label savings not measured" not in text(h, view)
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

        coverage = arm["coverage"]
        assert f"automatic coverage · {coverage['numerator']}/{coverage['denominator']}" in rendered
    pair = target.audit["comparisons"]["rule"]
    assert f"Eligible pairs · {pair['paired']['comparable']}/{pair['cohort_size']}" in rendered
    assert f"Excluded from the paired comparison · {pair['paired']['excluded']}" in rendered

    h.action(view, "ot_result_open", buttons(screen(h, view))["ot_result_open"])
    assert screen(h, view)["callback_id"] == "ot_chart_proof"
    calculation = text(h, view)
    assert frozen_result in calculation and target.audit["cohort_revision"] in calculation
    assert f"{pair['paired']['fixes']} fixed" in calculation
    assert f"{pair['paired']['regressions']} newly wrong" in calculation
    for key in ("current", "repair"):
        arm = pair[key]
        denominator = arm["observed_error_rate"]["denominator"]
        if denominator:
            rate = f"{100 * arm['incorrect_automatic'] / denominator:.2f}".rstrip("0").rstrip(".")
            exact = f"{arm['incorrect_automatic']}/{denominator} ({rate}%)"
            assert exact in rendered and exact in calculation
    h.action(view, "ot_result_open", buttons(screen(h, view))["ot_result_open"])
    assert screen(h, view)["callback_id"] == "ot_arm_chart"
    assert screen(h, view)["blocks"] == json.loads(rendered)["blocks"]

    view = ask()
    pin = buttons(screen(h, view))["ot_result_open"]
    h.action(view, "ot_result_open", pin)
    assert screen(h, view)["callback_id"] == "ot_arm_chart"
    grant = next(cfg.directory.glob("slack-review-*.json"))
    backup = grant.with_suffix(".generated-backup")
    grant.rename(backup)
    h.action(view, "ot_result_open", pin)
    assert screen(h, view).get("callback_id") != "ot_arm_chart"
    assert frozen_result not in text(h, view)
    backup.rename(grant)
    view = ask()
    pin = buttons(screen(h, view))["ot_result_open"]
    grant.rename(backup)
    h.action(view, "ot_result_open", pin)
    assert screen(h, view).get("callback_id") != "ot_arm_chart"
    backup.rename(grant)
    view = ask()
    pin = buttons(screen(h, view))["ot_result_open"]
    third = replace(HISTORY, source_revision="trace-v3", idempotency_key="history-v3")
    # Admission commits before the child benchmark publication. Even while that
    # publication is pending, the newer canonical admission fences the old chart.
    benchmarks.intake.submit_intake(principal, third)
    scope = BenchmarkScope(cfg.organization, h.store.engagement_id, intake["workflow"])
    assert benchmarks.benchmarks.inspect(scope, 2)["benchmark_state"] == "scored"
    h.action(view, "ot_result_open", pin)
    assert screen(h, view).get("callback_id") != "ot_arm_chart"
    assert screen(h, ask())["callback_id"] == "ot_query_hold"
    assert counts(h) == before


@pytest.mark.parametrize("surface", ["home", "slash"])
def test_scoped_monthly_answer_checks_live_membership_and_signed_receipt_at_render(
    tmp_path,
    monkeypatch,
    surface,
    signer_kit,  # noqa: F811
):
    from opentour_cell.pilot import scoped_query_report
    from opentour_cell.pilot.training import LocalTrainingStore
    from test_live_authority import SignedCase
    from test_reviewer_workflow import restart

    live = SignedCase(tmp_path / "authority")
    live.body["actors"][ACTOR] = ["inspect"]
    live.write()
    issuer, _ = reporting_signers(monkeypatch)

    def write(path, data):
        path.write_bytes(data)
        path.chmod(0o600)

    write(
        live.directory / "pilot-issuer-registry.json",
        signer_kit.registry(keys=[*signer_kit.issuers(), issuer.entry()]),
    )
    h = Harness(tmp_path / "workspace", organization="org-a")
    h.config["directory"] = str(live.directory)
    h.config["users"][ACTOR]["datasets"] = [live.body["dataset_ref"]]
    pin = dict(
        workflow_ref=live.workflow,
        publication_file="customer-month.json",
        custodian_key_id=issuer.key_id,
    )
    hidden = dict(
        pin, workflow_ref="unrelated-confidential", publication_file="unrelated-month.json"
    )
    timezone = "America/New_York"
    h.config["monthly_invoice_reports"] = dict(timezone=timezone, workflows=[hidden, pin])
    h.save()
    h.store = LocalTrainingStore(live.directory / "training.sqlite3", "org-a", existing_only=True)
    restart(h)
    month_end = datetime.now(UTC).astimezone(ZoneInfo(timezone)).date().replace(day=1)
    last = month_end - timedelta(days=1)
    start = last.replace(day=1)
    payload = {
        "profile_version": "1.0",
        "tenant": "org-a",
        "engagement_id": h.store.engagement_id,
        "workflow_ref": live.workflow,
        "metric": "recurring_invoice_code_errors",
        "timezone": timezone,
        "covered_from": start.isoformat(),
        "covered_until": month_end.isoformat(),
        "recurring_errors": 0,
        "verified_decisions": 0,
        "excluded_decisions": 0,
        "issued_at": stamp(5),
        "not_after": stamp(3600),
    }
    report_path = live.directory / pin["publication_file"]

    def publish_report(body):
        write(report_path, sign(body, scoped_query_report.REPORT_PURPOSE, issuer))

    publish_report(payload)
    calls = []
    original_read = scoped_query_report.open_signed_monthly_invoice_report

    def read(directory, publication_file, scope, *, timezone, at, custodian_key_id):
        assert directory == live.directory and publication_file == pin["publication_file"]
        assert (
            scope.workflow_ref == live.workflow and scope.engagement_epoch == h.store.engagement_id
        )
        assert timezone == payload["timezone"] and custodian_key_id == pin["custodian_key_id"]
        calls.append(publication_file)
        return original_read(
            directory,
            publication_file,
            scope,
            timezone=timezone,
            at=at,
            custodian_key_id=custodian_key_id,
        )

    monkeypatch.setattr(scoped_query_report, "open_signed_monthly_invoice_report", read)
    before = counts(h)

    def ask():
        prompt = "Which workflows had recurring invoice code errors last month?"
        if surface == "slash":
            return h.launch(prompt=prompt)
        click(h, publish(h, monkeypatch, ACTOR), "ot_app_prompt", ACTOR)
        v = list(h.slack.screens)[-1]
        h.form(v, {"command": {"command": {"value": prompt}}})
        return v

    v = ask()
    assert screen(h, v)["callback_id"] == "ot_monthly_invoice" and len(calls) == 2
    answer = text(h, v)
    assert f"{start} through {last} ({timezone})" in answer
    assert "0 recurring errors among 0 verified decisions" in answer and live.workflow in answer
    assert hidden["workflow_ref"] not in answer
    corrected = {
        **payload,
        "issued_at": stamp(10),
        "recurring_errors": 3,
        "verified_decisions": 42,
        "excluded_decisions": 5,
    }
    publish_report(corrected)
    assert "3 recurring errors among 42 verified decisions. Excluded decisions: 5." in text(
        h, ask()
    )
    publish_report(payload)
    assert screen(h, ask())["callback_id"] == "ot_query_hold"
    report_path.unlink()
    assert screen(h, ask())["callback_id"] == "ot_query_hold"
    publish_report(corrected)
    original = h.app.journal.admit_view

    def revoke(kind, **kw):
        if kind == "ot_monthly_invoice":
            del live.body["actors"][ACTOR]
            live.write()
        return original(kind, **kw)

    monkeypatch.setattr(h.app.journal, "admit_view", revoke)
    denied = ask()
    assert live.workflow not in text(h, denied) and "verified decisions" not in text(h, denied)
    assert hidden["workflow_ref"] not in text(h, denied) and counts(h) == before


def test_monthly_report_file_follows_worker_basename_bound():
    from opentour_cell.pilot.slack_adapter import MonthlyReportSettings

    row = dict(
        workflow_ref="workflow", publication_file="a" * 91 + ".json", custodian_key_id="key-one"
    )
    config = {"timezone": "UTC", "workflows": [row]}
    assert (
        MonthlyReportSettings.parse(config).workflows[0].publication_file == row["publication_file"]
    )
    for filename in ("a" * 92 + ".json", "../report.json", "report.txt"):
        row["publication_file"] = filename
        with pytest.raises(ValueError):
            MonthlyReportSettings.parse(config)
