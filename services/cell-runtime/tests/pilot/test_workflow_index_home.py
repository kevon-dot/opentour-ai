"""Generated label publication with actual private Slack, intake and release records."""

import json
from dataclasses import replace
from unittest.mock import Mock

from opentour_cell.pilot.intake_admission import AuthorizedSource, DurableIntake
from opentour_cell.pilot.intake_mcp import IntakePrincipal
from opentour_cell.pilot.source_intake import check_current_intake
from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake, BenchmarkScope
from opentour_cell.pilot.workflow_index import IndexScope, WorkflowLabel
from test_app_home import publish
from test_managed_slack import setup_workflow
from test_reviewer_workflow import restart, screen
from test_slack_adapter import ACTOR, OTHER
from test_status_questions import counts, inspect
from test_workflow_benchmarks import HISTORY


def test_private_queue_reopens_exact_managed_proposal_and_fences_prior_labels(
    tmp_path, monkeypatch
):
    h, plan, run, _ = setup_workflow(tmp_path, "rule", release=True)
    cfg = h.app.settings()
    epoch = h.store.engagement_id
    current = check_current_intake(cfg.directory, cfg.organization, epoch, plan["intake_revision"])
    workflow = current["workflow"]
    principal = IntakePrincipal(cfg.organization, "generated-intake", "intake")
    source = Mock()
    source.guard.side_effect = lambda actor: h.store.engagement_guard(read_only=True)
    source.historical.return_value = AuthorizedSource(
        workflow, epoch, "evaluation", "pair", "grant-v1"
    )
    BenchmarkIntake(DurableIntake(cfg.directory, source)).submit_intake(principal, HISTORY)
    label = WorkflowLabel(
        BenchmarkScope(cfg.organization, epoch, workflow), "owner", "v1", "Purchasing", ("Orders",)
    )
    labels = [label]
    member = True

    def scope(settings, actor, proposals):
        if not member:
            return None
        return IndexScope(
            actor,
            epoch,
            "membership-v1",
            settings.permission(actor.actor, "inspect")["expires_at"],
            tuple(labels),
            proposals,
        )

    h.app.workflow_index_scope = scope
    assert "Your workflow queue is empty." in json.dumps(publish(h, monkeypatch, ACTOR))
    _, revision = inspect(h)
    before = counts(h)
    old = publish(h, monkeypatch, ACTOR)
    rendered = json.dumps(old)
    assert "Needs you" in rendered and workflow in rendered and revision in rendered
    assert "Purchasing" in rendered and "Orders" in rendered and run in rendered
    assert "ot_app_queue_open" in rendered and "ot_app_pending" not in rendered
    assert "Purchasing" not in json.dumps(publish(h, monkeypatch, OTHER))

    def reopen(home):
        choice = next(b["accessory"] for b in home["blocks"] if b.get("accessory"))
        h.send(
            "interactive",
            {
                "api_app_id": cfg.app,
                "team": {"id": cfg.team},
                "user": {"id": ACTOR},
                "container": {"type": "view", "view_id": "VHOME"},
                "view": {**home, "id": "VHOME"},
                "type": "block_actions",
                "trigger_id": "private-open",
                "actions": [{"action_id": choice["action_id"], "value": choice["value"]}],
            },
        )
        return screen(h, list(h.slack.screens)[-1])

    restart(h)
    h.app.workflow_index_scope = scope
    labels[:] = [replace(label, label="Invoices", revision="v2")]
    assert revision not in json.dumps(reopen(old))
    renamed = publish(h, monkeypatch, ACTOR)
    assert revision in json.dumps(reopen(renamed))
    member = False
    denied = json.dumps(publish(h, monkeypatch, ACTOR))
    assert "Workflow queue access is unavailable" in denied and "Invoices" not in denied
    assert revision not in json.dumps(reopen(renamed))
    assert counts(h) == before


def test_queue_groups_and_opaque_pagination_render_only_available_actions():
    from opentour_cell.pilot.slack_views import app_home_view
    from opentour_cell.pilot.workflow_index import IndexPage, IndexRow

    first = IndexRow(
        "workflow-a",
        "Purchasing",
        (),
        "proposal-a",
        "v1",
        "pending",
        1700000000,
        1690000000,
        "needs_you",
        "open_review",
        "a" * 64,
    )
    second = replace(first, proposal_ref="proposal-b", group="waiting", action=None, selection=None)
    third = replace(
        first,
        workflow_ref="workflow-b",
        proposal_ref="proposal-c",
        state="approved",
        group="recent",
        action="open_receipt",
        selection="b" * 64,
    )
    page = IndexPage((first, second, third), "c" * 64)
    home = app_home_view("home-token", "Workspace", queue=page, queue_continuation=True)
    headers = [b["text"]["text"] for b in home["blocks"] if b["type"] == "header"]
    assert headers == ["Workspace", "Needs you", "Waiting", "Recent"]
    rows = [b for b in home["blocks"] if "Proposal " in b.get("text", {}).get("text", "")]
    assert "workflow-a" in rows[0]["text"]["text"] and "workflow-b" in rows[2]["text"]["text"]
    assert "proposal-b" in rows[1]["text"]["text"] and "accessory" not in rows[1]
    assert [b["accessory"]["value"] for b in rows if "accessory" in b] == [
        first.selection,
        third.selection,
    ]
    assert "<!date^1700000000^" in rows[0]["fields"][0]["text"]
    nav = next(
        b["elements"]
        for b in home["blocks"]
        if b.get("elements") and b["elements"][0]["action_id"] == "ot_app_queue_first"
    )
    assert [a["action_id"] for a in nav] == ["ot_app_queue_first", "ot_app_queue_next"]
    assert nav[1]["value"] == page.next_cursor


def test_authorized_workflow_without_a_visible_proposal_has_no_open_handle(tmp_path, monkeypatch):
    import pytest
    from opentour_cell.pilot.slack_adapter import Refused
    from opentour_cell.pilot.slack_views import app_home_view
    from opentour_cell.pilot.workflow_index import IndexPage, IndexRow
    from test_slack_adapter import Harness

    directory_row = IndexRow(
        "workflow-c",
        "Orders",
        (),
        None,
        None,
        "no_visible_proposal",
        None,
        100,
        "waiting",
        None,
        None,
    )
    page = IndexPage((directory_row,), None)
    home = app_home_view("home-token", "Workspace", queue=page)
    assert [b["text"]["text"] for b in home["blocks"] if b["type"] == "header"] == [
        "Workspace",
        "Waiting",
    ]
    row = next(
        b
        for b in home["blocks"]
        if "No proposals available to you." in b.get("text", {}).get("text", "")
    )
    assert "workflow-c" in row["text"]["text"] and "accessory" not in row and "fields" not in row
    assert "ot_app_queue_open" not in json.dumps(home)
    h = Harness(tmp_path)
    index = Mock()
    index.select.return_value = directory_row
    monkeypatch.setattr(h.app, "_workflow_index", lambda cfg, actor: index)
    with pytest.raises(Refused):
        h.app._open_workflow_proposal(h.app.settings(), ACTOR, "VGENERATED", "d" * 64)
    assert not h.slack.screens
