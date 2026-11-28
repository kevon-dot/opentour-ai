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
from test_reviewer_workflow import buttons, restart, screen
from test_slack_adapter import ACTOR, OTHER
from test_status_questions import counts, inspect
from test_workflow_benchmarks import HISTORY


def admit_workflow_label(h, plan):
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
    return label


def test_private_queue_reopens_exact_managed_proposal_and_fences_prior_labels(
    tmp_path, monkeypatch
):
    h, plan, run, _ = setup_workflow(tmp_path, "rule", release=True)
    cfg = h.app.settings()
    epoch = h.store.engagement_id
    label = admit_workflow_label(h, plan)
    workflow = label.scope.workflow_ref
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
    # The current named reviewer sees an assigned, completed proposal before inspecting it.
    assert "Needs you" in json.dumps(publish(h, monkeypatch, ACTOR))
    assert "Your workflow queue is empty." in json.dumps(publish(h, monkeypatch, OTHER))
    _, revision = inspect(h)
    before = counts(h)
    old = publish(h, monkeypatch, ACTOR)
    rendered = json.dumps(old)
    assert "Needs you" in rendered and workflow in rendered and revision in rendered
    assert "Purchasing" in rendered and "Orders" in rendered and run in rendered
    assert "ot_app_queue_open" in rendered and "ot_app_pending" not in rendered
    assert "Purchasing" not in json.dumps(publish(h, monkeypatch, OTHER))

    def reopen(home, actor=ACTOR):
        choice = next(b["accessory"] for b in home["blocks"] if b.get("accessory"))
        h.send(
            "interactive",
            {
                "api_app_id": cfg.app,
                "team": {"id": cfg.team},
                "user": {"id": actor},
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
    assert counts(h) == before
    h.config["users"][OTHER]["datasets"] = list(h.config["users"][ACTOR]["datasets"])
    h.save()
    # Current aggregate rights alone expose no proposal. The second user opens their own evidence.
    assert "Invoices" not in json.dumps(publish(h, monkeypatch, OTHER))
    other_view = h.launch(OTHER)
    h.action(other_view, "ot_evidence", buttons(screen(h, other_view))["ot_evidence"], OTHER)
    waiting = publish(h, monkeypatch, OTHER)
    assert "Waiting" in json.dumps(waiting) and "Review proposal" not in json.dumps(waiting)
    assert "ot_release_approve" not in json.dumps(reopen(waiting, OTHER))
    assert counts(h) == before

    # Only the existing private release action records the decision.
    evidence = next(
        v for v, data in reversed(h.slack.screens.items()) if "ot_release_changes" in buttons(data)
    )
    from opentour_cell.pilot import release_gate

    decision = h.path.parent / "managed-outputs" / run / "release-decision.json"
    record = release_gate._record

    def lose_membership_before_publish(path, payload, *, before_publish=None):
        nonlocal member
        assert before_publish is not None

        def revoke_then_check():
            nonlocal member
            member = False
            before_publish()

        return record(path, payload, before_publish=revoke_then_check)

    with monkeypatch.context() as scoped:
        scoped.setattr(release_gate, "_record", lose_membership_before_publish)
        h.action(evidence, "ot_release_changes", revision, ACTOR)
    assert not decision.exists() and counts(h) == before
    member = True
    reopen(publish(h, monkeypatch, ACTOR))
    evidence = list(h.slack.screens)[-1]
    h.action(evidence, "ot_release_changes", revision, ACTOR)
    saved = decision.read_bytes()
    assert json.loads(saved)["decision"] == "changes_requested"
    before = counts(h)
    recent = publish(h, monkeypatch, ACTOR)
    assert "Recent" in json.dumps(recent) and "changes requested" in json.dumps(recent)
    assert revision in json.dumps(reopen(recent))
    assert decision.read_bytes() == saved
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


def test_provider_request_opens_only_existing_private_status_and_rechecks_membership(
    tmp_path, monkeypatch
):
    from test_zapier_approval import make_case

    case = make_case(tmp_path)
    h = case.h
    label = admit_workflow_label(h, case.plan)
    granted = True

    def scope(cfg, principal, proposals):
        if not granted:
            return None
        return IndexScope(
            principal,
            h.store.engagement_id,
            "member",
            cfg.permission(principal.actor, "inspect")["expires_at"],
            (label,),
            proposals,
        )

    h.app.workflow_index_scope = scope
    assert "Your workflow queue is empty" in json.dumps(publish(h, monkeypatch, ACTOR))
    request = case.inspect()
    before = {p.name: p.read_bytes() for p in case.directory.glob("zapier-approval-za-*.json")}
    ready = publish(h, monkeypatch, ACTOR)
    assert "Waiting" in json.dumps(ready)
    choice = next(b["accessory"] for b in ready["blocks"] if "accessory" in b)
    h.send(
        "interactive",
        {
            "api_app_id": h.config["app"],
            "team": {"id": h.config["team"]},
            "user": {"id": ACTOR},
            "container": {"type": "view", "view_id": "VHOME"},
            "view": {**ready, "id": "VHOME"},
            "type": "block_actions",
            "trigger_id": "test-trigger",
            "actions": [{"action_id": choice["action_id"], "value": choice["value"]}],
        },
    )
    view = list(h.slack.screens)[-1]
    assert "request is staged" in json.dumps(screen(h, view))
    assert buttons(screen(h, view)) == {"ot_plugin_status": request["request_id"]}
    h.action(view, "ot_plugin_status", request["request_id"])
    assert "request is staged" in json.dumps(screen(h, view))
    granted = False
    h.action(view, "ot_plugin_status", request["request_id"])
    assert "request is staged" not in json.dumps(screen(h, view))
    assert before == {
        p.name: p.read_bytes() for p in case.directory.glob("zapier-approval-za-*.json")
    }
    assert not h.slack.post


def test_two_native_proposals_share_a_workflow_and_page_independently(tmp_path, monkeypatch):
    import time

    from opentour_cell.pilot import repair_workflow, slack_adapter
    from opentour_cell.pilot.managed_run import ManagedSelection, plan_revision
    from test_app_home import click
    from test_managed_slack import prepared_workflow

    h, plan, selection = prepared_workflow(tmp_path, "rule", release=True)
    label = admit_workflow_label(h, plan)
    cfg = h.app.settings()
    run_ids = []
    for idx in range(2):
        if idx:
            plan["slack_review"]["expires_at"] -= 90
            selection = ManagedSelection(
                cfg.directory,
                cfg.organization,
                cfg.directory / "second-review-plan.json",
                cfg.directory / "second-review-grant.json",
            )
            selection.manifest.write_text(json.dumps(plan))
            selection.manifest.chmod(0o600)
            repair_workflow.grant_slack_review(cfg.directory, cfg.organization, plan)
            selection.authorize(
                plan_revision(plan),
                "att-abcddcbaabcddcba",
                int(time.time()) + 500,
                None,
                repair_workflow.inspect_managed,
            )
        run_ids.append(selection.start(plan_revision(plan), f"open-{idx}")["run_id"])
        assert selection.once(repair_workflow.execute_managed)["state"] == "ready"

    def authority(cfg, actor, proposals):
        return IndexScope(
            actor,
            h.store.engagement_id,
            "v1",
            cfg.permission(actor.actor, "inspect")["expires_at"],
            (label,),
            proposals,
        )

    h.app.workflow_index_scope = authority
    monkeypatch.setattr(slack_adapter, "WORKFLOW_PAGE_SIZE", 1)
    first = publish(h, monkeypatch, ACTOR)
    assert run_ids[1] in json.dumps(first) and run_ids[0] not in json.dumps(first)
    click(h, first, "ot_app_queue_next", ACTOR)
    second = h.slack.views_publish.call_args.kwargs["view"]
    assert run_ids[0] in json.dumps(second) and run_ids[1] not in json.dumps(second)
    assert "ot_app_queue_next" not in json.dumps(second)
    handles = [
        next(b["accessory"]["value"] for b in home["blocks"] if "accessory" in b)
        for home in (first, second)
    ]
    assert handles[0] != handles[1]
    restart(h)
    h.app.workflow_index_scope = authority
    idx = h.app._workflow_index(cfg, ACTOR)
    for token, run in zip(handles, reversed(run_ids), strict=True):
        assert (
            idx.select(IntakePrincipal(cfg.organization, ACTOR, "slack"), token).proposal_ref == run
        )


def test_socket_mode_two_installations_reusing_source_ids_do_not_share_handles(
    tmp_path, monkeypatch
):
    import sys
    from types import SimpleNamespace

    from opentour_cell.pilot import slack_adapter, workflow_index_authority

    handles = []
    projections = []

    def exercise_install(name):
        h, plan, run, _ = setup_workflow(tmp_path / name, "rule", release=True, slot=name)
        cfg = h.app.settings()
        label = admit_workflow_label(h, plan)
        labels = [replace(label, label="Orders", aliases=())]
        h.config["workflow_index_publication_file"] = "approved-workflows.json"
        h.save()

        def read_scope(installed, actor, proposals):
            if installed.workflow_index_publication_file != "approved-workflows.json":
                return None
            return IndexScope(
                actor,
                h.store.engagement_id,
                "membership",
                installed.permission(actor.actor, "inspect")["expires_at"],
                tuple(labels),
                proposals,
            )

        monkeypatch.setattr(workflow_index_authority, "open_workflow_index_scope", read_scope)
        monkeypatch.setattr(
            h.slack,
            "auth_test",
            lambda: {"team_id": cfg.team, "bot_id": "generated-bot"},
            raising=False,
        )
        monkeypatch.setattr(
            h.slack,
            "views_publish",
            lambda **kwargs: projections.append(kwargs["view"]) or {"ok": True},
            raising=False,
        )
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-offline")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-offline")

        class Socket:
            def __init__(self, **kwargs):
                self.socket_mode_request_listeners = []
                self.acks = []

            def send_socket_mode_response(self, response):
                self.acks.append(response)

            def connect(self):
                def send(kind, payload):
                    count = len(self.acks)
                    self.socket_mode_request_listeners[0](
                        self, SimpleNamespace(type=kind, payload=payload, envelope_id="generated")
                    )
                    assert len(self.acks) == count + 1

                send(
                    "events_api",
                    {
                        "api_app_id": cfg.app,
                        "team_id": cfg.team,
                        "type": "event_callback",
                        "event": {"type": "app_home_opened", "user": cfg.reviewer, "tab": "home"},
                    },
                )
                home = projections[-1]
                assert run in json.dumps(home)
                choice = next(b["accessory"] for b in home["blocks"] if "accessory" in b)
                handles.append(choice["value"])
                if len(handles) == 2:
                    assert handles[0] != handles[1]
                    send(
                        "interactive",
                        {
                            "api_app_id": cfg.app,
                            "team": {"id": cfg.team},
                            "user": {"id": cfg.reviewer},
                            "container": {"type": "view", "view_id": "VHOME"},
                            "view": {**home, "id": "VHOME"},
                            "type": "block_actions",
                            "trigger_id": "test",
                            "actions": [{"action_id": choice["action_id"], "value": handles[0]}],
                        },
                    )
                    assert "ot_evidence_view" not in json.dumps(
                        screen(h, list(h.slack.screens)[-1])
                    )
                raise KeyboardInterrupt

            def close(self):
                pass

        monkeypatch.setitem(
            sys.modules, "slack_sdk", SimpleNamespace(WebClient=lambda **kwargs: h.slack)
        )
        monkeypatch.setitem(
            sys.modules, "slack_sdk.socket_mode", SimpleNamespace(SocketModeClient=Socket)
        )
        monkeypatch.setitem(
            sys.modules,
            "slack_sdk.socket_mode.response",
            SimpleNamespace(SocketModeResponse=SimpleNamespace),
        )
        assert slack_adapter.main([str(h.path)]) == 0
        assert not h.slack.post

    exercise_install("alpha")
    exercise_install("beta")
