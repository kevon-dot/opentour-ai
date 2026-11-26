"""Account for founder navigation without losing custody of protected or uncertain Slack views."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from opentour_cell.pilot import slack_views
from opentour_cell.pilot.custody import Closeout
from test_app_home import publish
from test_pilot_custody import START, record
from test_workflow_chooser import Founder, emitted, open_choices


def test_stock_founder_navigation_remains_audited_in_zero_export_closeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "founder"
    h = Founder(directory, local_recipient=False)
    try:
        home = publish(h, monkeypatch)
        view_id, chooser = open_choices(h, home)
        action = emitted(chooser, "We’re starting from scratch")
        h.action(view_id, action["action_id"], action["value"])
        h.assert_no_effects()
        with h.app.journal.db() as db:
            observed = sorted(tuple(row) for row in db.execute("SELECT kind,status FROM exposures"))
        assert observed == [
            ("app_home", "navigation_presented"),
            ("ot_workflow_chooser", "navigation_presented"),
            ("ot_workflow_starter_info", "navigation_presented"),
        ]
        c = Closeout(directory, h.config["organization"])
        report = c.withdraw(**START)
        delivery = report["delivery_inventory"]
        assert delivery["local_journal_records"] == 3
        assert delivery["slack_exposed"] == delivery["slack_possible"] == 0
        assert not any(item["kind"] == "destination" for item in report["items"])
        record(c, "local-handoff-journal", "receipt", "custody_transferred")
        record(c, "inventory-review", "inventory", "inventory_verified")
        record(c, "support-handoff", "support", "handoff_confirmed")
        assert (
            c.finish(custodian=START["custodian"], evidence="fixture-closeout")[
                "export_disposition"
            ]
            == "zero_exports"
        )
    finally:
        h.app.pool.shutdown()


def test_nonstock_views_and_unknown_delivery_remain_in_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "founder"
    h = Founder(directory, local_recipient=False)
    try:
        h.config["workspace_title"] = "Generated private workspace label"
        h.save()
        home = publish(h, monkeypatch)
        view_id, chooser = open_choices(h, home)
        action = emitted(chooser, "We’re starting from scratch")
        renderer = Mock(wraps=slack_views)

        def protected_instructions(context: str, route: str) -> dict:
            result = slack_views.workflow_instructions(context, route)
            result["blocks"].append(
                {"type": "section", "text": {"type": "plain_text", "text": "Private result"}}
            )
            return result

        renderer.workflow_instructions.side_effect = protected_instructions
        h.app.views = renderer
        h.action(view_id, action["action_id"], action["value"])
        assert "Private result" in json.dumps(h.slack.screens[view_id])
        for kind, state in (
            ("app_home", "presented"),  # Historic row with no content classification.
            ("ot_workflow_starter_info", "uncertain"),  # Lost acknowledgement.
            ("opaque-future-view", "navigation_presented"),  # Unknown classification.
        ):
            token = h.app.journal.admit_view(kind)
            h.app.journal.finish_view(token, state)
        c = Closeout(directory, h.config["organization"])
        report = c.withdraw(**START)
        delivery = report["delivery_inventory"]
        assert delivery["slack_exposed"] == 3  # Named Home, protected result and legacy Home.
        assert delivery["slack_possible"] == 2
        assert any(item["item"] == "slack-copies" for item in report["items"])
        assert "export_disposition" not in report
    finally:
        h.app.pool.shutdown()


def test_bound_recipient_keeps_existing_view_custody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "bound"
    h = Founder(directory, local_recipient=True)
    try:
        view_id, chooser = open_choices(h, publish(h, monkeypatch))
        action = emitted(chooser, "We’re starting from scratch")
        h.action(view_id, action["action_id"], action["value"])
        delivery = Closeout(directory, h.config["organization"]).withdraw(**START)[
            "delivery_inventory"
        ]
        assert delivery["slack_exposed"] == 3 and delivery["slack_possible"] == 0
    finally:
        h.app.pool.shutdown()


@pytest.mark.parametrize("stage", ["home", "chooser", "instructions", "ack"])
def test_binding_during_private_lookup_or_ack_keeps_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    directory = tmp_path / "founder"
    h = Founder(directory, local_recipient=False)
    try:

        def bind_during(method):
            def call(**kwargs):
                h.config["recipient"] = h.config["reviewer"]
                h.save()
                return method(**kwargs)

            return call

        def intercept(method):
            monkeypatch.setattr(h.slack, method, bind_during(getattr(h.slack, method)))

        if stage == "home":
            intercept("conversations_members")
        home = publish(h, monkeypatch)
        if stage != "home":
            if stage == "chooser":
                intercept("conversations_members")
            view_id, chooser = open_choices(h, home)
            if stage in {"instructions", "ack"}:
                intercept("views_update" if stage == "ack" else "conversations_members")
                action = emitted(chooser, "We’re starting from scratch")
                h.action(view_id, action["action_id"], action["value"])
        report = Closeout(directory, h.config["organization"]).withdraw(**START)
        assert report["delivery_inventory"]["slack_exposed"] == 1
        assert report["delivery_inventory"]["slack_possible"] == 0
    finally:
        h.app.pool.shutdown()
