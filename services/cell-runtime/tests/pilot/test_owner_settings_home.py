"""Private App Home readback of custodian-signed generated owner settings."""

import time
from dataclasses import asdict, replace
from unittest.mock import Mock

from opentour_cell.pilot.intake_admission import AuthorizedSource, DurableIntake
from opentour_cell.pilot.intake_mcp import IntakePrincipal
from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake
from test_app_home import click, publish
from test_autonomous_cycle import read, save
from test_intake_mcp import _write_private
from test_live_authority import enroll_ledger
from test_live_evidence import DECISION, OUTCOME
from test_live_settings import KEY, SCHEMA, sign
from test_owner_settings import Setup
from test_reviewer_workflow import restart, screen, text
from test_slack_adapter import ACTOR, OTHER, Harness
from test_status_questions import counts
from test_workflow_benchmarks import HISTORY


def test_private_settings_rechecks_role_workflow_and_authority(tmp_path, monkeypatch):
    exercise_private_settings(tmp_path, monkeypatch)


def exercise_private_settings(tmp_path, monkeypatch, *, connect=None):
    setup = Setup(tmp_path)
    customer = setup.customer
    authority = read(setup.authority)
    authority["operator"] = OTHER
    save(setup.authority, authority)
    setup.policy["mandate"]["delegate"] = OTHER
    receipt = setup.apply()
    workflow = setup.policy["mandate"]["workflow"]
    other_workflow = "different-workflow"
    principal = IntakePrincipal(customer.org, customer.delegate, "intake")
    source_authority = Mock()
    source_authority.guard.side_effect = lambda actor: setup.service.store.engagement_guard(
        read_only=True
    )
    intake = BenchmarkIntake(DurableIntake(customer.owner, source_authority))
    for selected in (workflow, other_workflow):
        source_authority.historical.return_value = AuthorizedSource(
            selected, customer.eng, "evaluation", selected, "grant-v1"
        )
        intake.submit_intake(
            principal, replace(HISTORY, pair_ref=selected, idempotency_key=selected)
        )
    h = Harness(tmp_path / "slack", organization=customer.org, seed_generated_dataset=False)
    h.path = customer.owner / "slack-config.json"
    h.store = setup.service.store
    h.config.update(
        directory=str(customer.owner),
        live_workflow_ref=workflow,
        owner_settings_authority_file=setup.authority.name,
    )
    h.save()
    restart(h)
    if connect:
        connect(h)
    grants = (DECISION, OUTCOME)
    enroll_ledger(customer.owner, customer.org, customer.eng, grants)
    _write_private(customer.owner / "live-authority-key.json", {"key": KEY.hex()})
    live = {
        "schema_version": SCHEMA,
        "tenant": customer.org,
        "workflow_ref": workflow,
        "engagement_epoch": customer.eng,
        "dataset_ref": "dataset-a",
        "dataset_revision": "dataset-v1",
        "custodian": OTHER,
        "expires_at": int(time.time()) + 600,
        "state": "current",
        "actors": {ACTOR: ["inspect"], OTHER: ["inspect", "custodian"]},
        "grants": [asdict(grant) for grant in grants],
        "engagement_ledger_required": True,
    }
    sign(h, live)

    def open_settings(actor):
        click(h, publish(h, monkeypatch, actor), "ot_app_settings", actor)
        view = list(h.slack.screens)[-1]
        layout = screen(h, view)
        assert layout["callback_id"] == "ot_workflow_settings"
        return text(h, view)

    before = counts(h), setup.protocol()
    reviewer = open_settings(ACTOR)
    assert receipt["revision"] in reviewer and "Training policy · active" in reviewer
    assert "Spending limit" not in reviewer
    operator = open_settings(OTHER)
    assert receipt["revision"] in operator and "Spending limit · 0 USD cents" in operator
    assert "Recorded spending · unavailable" in operator

    h.config["live_workflow_ref"] = other_workflow
    h.save()
    sign(h, {**live, "workflow_ref": other_workflow})
    assert "Training policy" not in open_settings(OTHER)
    h.config["live_workflow_ref"] = workflow
    h.save()
    original = h.app.views.workflow_settings

    def revoke(*args, **kwargs):
        layout = original(*args, **kwargs)
        save(setup.authority, {**authority, "revoked": True})
        return layout

    monkeypatch.setattr(h.app.views, "workflow_settings", revoke)
    click(h, publish(h, monkeypatch, OTHER), "ot_app_settings", OTHER)
    assert receipt["revision"] not in text(h, list(h.slack.screens)[-1])
    assert "Training policy" not in open_settings(OTHER)
    assert (counts(h), setup.protocol()) == before
