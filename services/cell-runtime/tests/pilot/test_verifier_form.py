"""Authenticated private Slack verifier and real current reducer on generated data."""

import json
import sqlite3

import pytest
from opentour_cell.pilot.intake_mcp import IntakeError
from test_app_home import click
from test_home_readiness import ReadyHome
from test_live_readiness import decision
from test_live_settings import form, open_live, sign
from test_reviewer_workflow import buttons, restart, screen
from test_slack_adapter import ACTOR, OTHER
from test_starter_setup import effects

pytest_plugins = ("test_live_settings",)


@pytest.fixture
def named(connected_settings, monkeypatch):
    ready = ReadyHome(*connected_settings, monkeypatch)
    h = ready.h
    truth = ready.grant["grants"][1]
    truth["verifier"] = OTHER
    ready.grant["actors"][OTHER] = ["verify"]
    ready.grant["slack_verifiers"] = {truth["resource"]: OTHER}
    sign(h, ready.grant)
    ready.ingest(decision())
    assert "ot_app_verify_outcome" not in buttons(ready.home(OTHER))
    for _ in range(2):
        view = open_live(h, monkeypatch)
        assert OTHER in json.dumps(screen(h, view))
        h.form(view, form(h, view, 1))
        restart(h)
    return ready


def open_form(ready):
    click(ready.h, ready.home(OTHER), "ot_app_verify_outcome", OTHER)
    view = list(ready.h.slack.screens)[-1]
    assert screen(ready.h, view)["callback_id"] == "ot_verify_outcome"
    return view


def submission(ready, view, which="D1", state="final"):
    modal = screen(ready.h, view)
    fields = {b["block_id"]: b["element"] for b in modal["blocks"] if b["type"] == "input"}
    opt = next(o for o in fields["target"]["options"] if o["text"]["text"].startswith(which + " ·"))
    fin = next(o for o in fields["finality"]["options"] if o["value"] == state)
    return {
        "target": {"target": {"type": "static_select", "selected_option": opt}},
        "finality": {"finality": {"type": "static_select", "selected_option": fin}},
        **{
            k: {k: {"type": "plain_text_input", "value": v}}
            for k, v in [
                ("action", "freight" if state == "final" else None),
                ("effective", "185"),
                ("basis", "custodian-evidence-19"),
            ]
        },
    }


def totals(ready):
    snapshot = ready.worker.source.live.snapshot(ready.worker.worker, ready.workflow)
    return snapshot.decisions, snapshot.verified, snapshot.pending, snapshot.disputed


def test_named_verifier_actual_callback_final_reopen_stale_reverify_and_asof(named):
    ready = named
    h = ready.h
    assert "ot_app_verify_outcome" not in buttons(ready.home(ACTOR))
    view = open_form(ready)
    before = totals(ready)
    assert before == (1, 0, 1, 0)
    # Closing or opening again before submitting cannot write an outcome.
    view = open_form(ready)
    data = submission(ready, view)
    body = h.form(view, data, OTHER)
    assert screen(h, view)["callback_id"] == "ot_verified_outcome" and totals(ready) == (1, 1, 0, 0)
    receipt = json.dumps(screen(h, view))
    assert "verification-" in receipt and "custodian-evidence-19" not in receipt
    old = ready.worker.source.live.snapshot(ready.worker.worker, ready.workflow)
    h.send("interactive", body)
    assert totals(ready) == (1, 1, 0, 0)
    view = open_form(ready)
    h.form(view, submission(ready, view, state="reopened"), OTHER)
    assert totals(ready) == (1, 0, 1, 0)
    h.send("interactive", body)
    assert totals(ready) == (1, 0, 1, 0)
    restart(h)
    view = open_form(ready)
    h.form(view, submission(ready, view), OTHER)
    assert totals(ready) == (1, 1, 0, 0)
    assert (
        ready.worker.source.live.snapshot(
            ready.worker.worker, ready.workflow, as_of_revision=old.sequence
        )
        == old
    )
    with sqlite3.connect(ready.worker.source.live.path) as db:
        assert db.execute("SELECT count(*) FROM live_form_receipts").fetchone() == (3,)
        assert db.execute(
            "SELECT count(*) FROM live_bindings WHERE kind='independent_outcome'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT json_extract(payload,'$.action_ref') FROM live_events "
            "WHERE json_extract(payload,'$.kind')='DecisionRecorded'"
        ).fetchone() == ("supplies",)
        assert db.execute(
            "SELECT DISTINCT json_extract(payload,'$.verifier') FROM live_events "
            "WHERE json_extract(payload,'$.kind')='OutcomeVerified'"
        ).fetchall() == [(OTHER,)]
    assert effects(h) == (0,) * 7


def test_two_decisions_share_case_exact_target_only_and_forged_worker_denied(named):
    ready = named
    h = ready.h
    ready.ingest(decision("D2", case_id="invoice-D1"))
    view = open_form(ready)
    values = submission(ready, view)
    values["target"]["target"]["selected_option"]["value"] = "invoice-D1"
    h.form(view, values, OTHER)
    assert totals(ready) == (2, 0, 2, 0)
    view = open_form(ready)
    h.form(view, submission(ready, view), OTHER)
    assert totals(ready) == (2, 1, 1, 0)
    with sqlite3.connect(ready.worker.source.live.path) as db:
        ref = db.execute(
            "SELECT ref FROM live_bindings WHERE kind='independent_outcome'"
        ).fetchone()[0]
    with pytest.raises(IntakeError):
        ready.worker.source.live.ingest_page(
            ready.worker.worker,
            ref,
            page_ref="spoof",
            source_revision="truth-v1",
            events=(),
            source_positions=(),
            event_epochs=(),
            checkpoint=21,
        )
    assert effects(h) == (0,) * 7


@pytest.mark.parametrize("fault", ["withdrawn", "foreign", "expired", "new_decision_revision"])
def test_verifier_denied_before_outcome_commit(named, fault):
    ready = named
    h = ready.h
    view = open_form(ready)
    values = submission(ready, view)
    actor = OTHER
    if fault == "withdrawn":
        ready.grant.pop("slack_verifiers")
        sign(h, ready.grant)
    elif fault == "foreign":
        actor = ACTOR
    elif fault == "expired":
        ready.grant["expires_at"] = 1
        sign(h, ready.grant)
    else:
        ready.ingest(decision(decision_revision=2, event_id="updated-commit"))
    h.form(view, values, actor)
    assert screen(h, view).get("callback_id") != "ot_verified_outcome"
    with sqlite3.connect(ready.worker.source.live.path) as db:
        assert db.execute(
            "SELECT count(*) FROM live_events "
            "WHERE json_extract(payload,'$.kind')='OutcomeVerified'"
        ).fetchone() == (0,)
    assert effects(h) == (0,) * 7


def test_withdrawal_during_exact_form_commit_rolls_back_event_and_receipt(named, monkeypatch):
    ready = named
    h = ready.h
    view = open_form(ready)
    values = submission(ready, view)
    cls = type(ready.worker.source.live)
    original = cls._record_event

    def revoke(live, *args):
        original(live, *args)
        ready.grant["state"] = "revoked"
        sign(h, ready.grant)

    monkeypatch.setattr(cls, "_record_event", revoke)
    h.form(view, values, OTHER)
    with sqlite3.connect(ready.worker.source.live.path) as db:
        assert db.execute("SELECT count(*) FROM live_events").fetchone() == (1,)
        assert (
            db.execute("SELECT 1 FROM sqlite_master WHERE name='live_form_receipts'").fetchone()
            is None
        )
    assert "verification-" not in json.dumps(screen(h, view))


def test_foreign_namespace_and_self_committed_decision_excluded(named):
    ready = named
    h = ready.h
    ready.ingest(decision("D2", actor_ref=OTHER))
    view = open_form(ready)
    assert "D2" not in json.dumps(screen(h, view))
    ready.grant["grants"][1]["deciding_namespace"] = "other-namespace"
    ready.grant["grants"][1]["revision"] = "other-scope"
    sign(h, ready.grant)
    view = open_live(h, ready.patch)
    h.form(view, form(h, view, 1))
    assert "ot_app_verify_outcome" not in buttons(ready.home(OTHER))


def test_withdrawal_during_layout_hides_every_exact_decision(named, monkeypatch):
    ready = named
    h = ready.h
    normal = h.app.views.verify_outcome

    def withdraw(*args, **kwargs):
        layout = normal(*args, **kwargs)
        ready.grant["state"] = "revoked"
        sign(h, ready.grant)
        return layout

    monkeypatch.setattr(h.app.views, "verify_outcome", withdraw)
    click(h, ready.home(OTHER), "ot_app_verify_outcome", OTHER)
    last = list(h.slack.screens)[-1]
    assert "D1" not in json.dumps(screen(h, last))
    assert effects(h) == (0,) * 7


def test_owner_nomination_is_visible_to_custodian_without_creating_verification_rights(
    connected_settings, monkeypatch
):
    ready = ReadyHome(*connected_settings, monkeypatch)
    h = ready.h
    ready.grant["actors"][OTHER] = ["workflow_owner"]
    sign(h, ready.grant)
    click(h, ready.home(OTHER), "ot_app_settings", OTHER)
    view = list(h.slack.screens)[-1]
    pin = buttons(screen(h, view))["ot_nominate_verifier"]
    before = ready.worker.source.live.path.read_bytes()
    h.action(view, "ot_nominate_verifier", pin, OTHER)
    assert screen(h, view)["callback_id"] == "ot_nomination"
    h.form(view, {"nominee": {"nominee": {"type": "plain_text_input", "value": OTHER}}}, OTHER)
    assert OTHER in json.dumps(screen(h, view))
    click(h, ready.home(ACTOR), "ot_app_settings")
    custodian = list(h.slack.screens)[-1]
    assert OTHER in json.dumps(screen(h, custodian))
    assert "ot_nominate_verifier" not in buttons(screen(h, custodian))
    assert "ot_app_verify_outcome" not in buttons(ready.home(OTHER))
    assert ready.worker.source.live.path.read_bytes() == before and effects(h) == (0,) * 7
