"""Check the installed native Slack Home, scoped labels, command form and provider selection."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from opentour_cell.pilot import (
    approval_inbox,
    repair_workflow,
    slack_adapter,
    slack_views,
    zapier_approval,
)
from test_managed_slack import authorize_launch, prepared_workflow, setup_workflow
from test_reviewer_workflow import buttons, screen, text
from test_slack_adapter import ACTOR, APP, CHANNEL, HOOK, TEAM, Harness

Json = dict[str, Any]


def publish(
    h: Harness, monkeypatch: pytest.MonkeyPatch, actor: str = ACTOR, **changes: Any
) -> Json:
    sent = MagicMock(return_value={"ok": True})
    monkeypatch.setattr(h.slack, "views_publish", sent, raising=False)
    payload = {
        "api_app_id": APP,
        "team_id": TEAM,
        "type": "event_callback",
        "event": {"type": "app_home_opened", "user": actor, "tab": "home"},
    }
    payload.update(changes)
    h.send("events_api", payload)
    return sent.call_args.kwargs["view"] if sent.called else {}


def click(h: Harness, home: Json, action: str, actor: str = ACTOR) -> None:
    h.send(
        "interactive",
        {
            "api_app_id": APP,
            "team": {"id": TEAM},
            "user": {"id": actor},
            "container": {"type": "view", "view_id": "VHOME"},
            "view": {**home, "id": "VHOME"},
            "type": "block_actions",
            "trigger_id": "home-trigger",
            "actions": [{"action_id": action, "value": buttons(home).get(action, action)}],
        },
    )


def test_home_commands_use_private_views_and_live_actor_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h, plan, selection = prepared_workflow(tmp_path, "rule")
    h.config.update(workspace_title="Finance operations", workflow_label="Invoice classification")
    h.save()
    home = publish(h, monkeypatch)
    assert home["type"] == "home" and "Finance operations" in json.dumps(home)
    assert "Invoice classification" in json.dumps(home)
    assert "generated-repair" not in json.dumps(home) and plan["audience"] not in json.dumps(home)
    assert set(buttons(home)) == {"ot_app_review", "ot_app_data", "ot_app_prompt"}
    authorize_launch(h)
    home = publish(h, monkeypatch)
    assert list(buttons(home)) == ["ot_app_review", "ot_app_data", "ot_app_prompt", "ot_app_repair"]
    click(h, home, "ot_app_repair")
    view = list(h.slack.screens)[-1]
    assert screen(h, view)["callback_id"] == "ot_prompt_enter" and "initial_value" in text(h, view)
    h.form(view, {"command": {"command": {"value": "repair generated-repair"}}})
    assert buttons(screen(h, view))["ot_repair_confirm"]
    assert not selection.path.exists()
    click(h, home, "ot_app_prompt")
    prompt = list(h.slack.screens)[-1]
    h.form(prompt, {"command": {"command": {"value": "search description"}}})
    assert screen(h, prompt)["callback_id"] == "ot_dataset_search"
    slash = h.launch(prompt="search description")
    assert screen(h, prompt)["blocks"] == screen(h, slash)["blocks"]
    click(h, home, "ot_app_data")
    assert screen(h, list(h.slack.screens)[-1])["callback_id"] == "ot_dataset_search"
    click(h, home, "ot_app_review")
    assert "Finance operations" in text(h, list(h.slack.screens)[-1])
    assert not h.slack.post


@pytest.mark.parametrize(
    "blocked", ["actor", "public", "expired", "withdrawn", "team", "app", "stale_metadata"]
)
def test_home_denials_do_not_leave_an_actionable_cross_workspace_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked: str
) -> None:
    h = Harness(tmp_path)
    h.config.update(workspace_title="Private finance workflow")
    h.save()
    home = publish(h, monkeypatch)
    if blocked == "public":
        h.slack.channel["is_private"] = False
    elif blocked == "expired":
        h.config["users"][ACTOR]["expires_at"] = 0
    elif blocked == "withdrawn":
        h.store.withdraw()
    elif blocked == "actor":
        del h.config["users"][ACTOR]
    elif blocked in {"team", "app"}:
        field = "team_id" if blocked == "team" else "api_app_id"
        assert not publish(h, monkeypatch, **{field: "TOTHER" if blocked == "team" else "AOTHER"})
        return
    else:
        home["private_metadata"] = "home_forged"
        click(h, home, "ot_app_review")
        assert not h.slack.screens
        return
    h.save()
    denied = publish(h, monkeypatch)
    assert "access is unavailable" in json.dumps(denied) and not buttons(denied)
    assert "Private finance" not in json.dumps(denied)
    click(h, home, "ot_app_data")
    for modal in h.slack.screens.values():
        assert not buttons(modal) and "Private finance" not in json.dumps(modal)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("workspace_title", "a" * 49),
        ("workflow_label", "<@UOTHER>"),
        ("workspace_title", "secret\nlabel"),
        ("approval_provider", "http"),
    ],
)
def test_operator_labels_accept_only_plain_bounded_config(
    tmp_path: Path, field: str, value: str
) -> None:
    h = Harness(tmp_path)
    h.config[field] = value
    h.save()
    with pytest.raises(ValueError):
        slack_adapter.Settings.load(h.path)


@pytest.mark.parametrize("route", ["rule", "model"])
def test_configured_zapier_lane_stages_after_presentation_and_hides_native_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    h, plan, run, _ = setup_workflow(tmp_path, route, release=True, provider="zapier_slack")
    h.config.update(approval_requests=True, approval_provider="zapier_slack")
    h.save()
    audit = repair_workflow.managed_review_evidence(
        h.path.parent, h.store.organization, run, plan["audience"], TEAM, CHANNEL
    )["audit"]
    promise = approval_inbox.enqueue_ready(
        h.path.parent,
        h.store.organization,
        run,
        run_revision=audit["run_revision"],
        team=TEAM,
        channel=CHANNEL,
        audience=plan["audience"],
        reviewer=ACTOR,
    )
    assert promise["state"] == "pending_review"
    directory = h.path.parent
    options = {
        "schema_version": zapier_approval.POLICY,
        "enabled": True,
        "organization": h.store.organization,
        "engagement": h.store.engagement_id,
        "team": TEAM,
        "channel": CHANNEL,
        "reviewer": ACTOR,
        "audience": plan["audience"],
        "zap_id": "generated-zap",
        "zap_version": "generated-v1",
        "approver_mode": "verified_actor",
        "setup_attestation": "att-7777777777777777",
        "key_file": "generated-signer.json",
        "expires_at": int(time.time()) + 600,
        "hook_fingerprint": slack_adapter.hook_fingerprint(HOOK),
    }
    for filename, data in (
        ("zapier-approval.json", options),
        ("generated-signer.json", {"secret_hex": "3f" * 32}),
    ):
        path = directory / filename
        path.write_text(json.dumps(data))
        path.chmod(0o600)

    def staged() -> list[Path]:
        return list(directory.glob("zapier-approval-za-*-request.json"))

    view = h.launch()
    revision = buttons(screen(h, view))["ot_evidence"]
    assert not staged()
    h.action(view, "ot_evidence", revision)
    assert len(staged()) == 1 and "Slack Request Approval in Zapier" in text(h, view)
    issued = json.loads(staged()[0].read_text())["request"]
    assert (
        issued["run_revision"] == audit["run_revision"]
        and issued["artifact_sha"] == audit["candidate_digest"]
    )
    with h.app.journal.db() as db:
        saved = db.execute(
            "SELECT snapshot FROM presentations WHERE id=?", (issued["inspection_ref"],)
        ).fetchone()
    sealed = json.loads(saved[0])
    assert sealed["job_id"] == run and sealed["release_settings_file"] == str(h.path)
    assert str(h.path) not in text(h, view)
    assert "release_settings_file" not in json.dumps(issued)
    h.action(view, "ot_home", "home")
    h.action(view, "ot_evidence", revision)
    assert len(staged()) == 1
    assert "ot_release_approve" not in buttons(screen(h, view))
    h.action(view, "ot_release_approve", revision)
    assert "Access unavailable" in text(h, view)
    assert not (h.path.parent / "managed-outputs" / run / "release-decision.json").exists()
    connect = MagicMock()
    connect.return_value.getresponse.return_value.status = 200
    monkeypatch.setattr(http.client, "HTTPSConnection", connect)
    monkeypatch.delenv("OPENTOUR_ZAPIER_APPROVAL_HOOK_URL", raising=False)
    assert approval_inbox.delivery_cycle(h.app)["plugin_outbound"] == {"state": "hook_unavailable"}
    connect.assert_not_called()
    monkeypatch.setenv("OPENTOUR_ZAPIER_APPROVAL_HOOK_URL", HOOK)
    cycle = approval_inbox.delivery_cycle(h.app)
    assert cycle["plugin_outbound"] == {"accepted": 1, "held": 0}
    connect.assert_called_once()
    assert approval_inbox.delivery_cycle(h.app)["plugin_outbound"]["accepted"] == 0
    event = {
        "schema_version": zapier_approval.EVENT,
        "request_id": issued["request_id"],
        "nonce": issued["nonce"],
        "zap_id": options["zap_id"],
        "zap_version": options["zap_version"],
        "event_id": "generated-approval",
        "decided_at": int(time.time()),
        "status": "Approved",
        "actor_id": ACTOR,
    }
    signature = hmac.new(
        bytes.fromhex("3f" * 32),
        (
            zapier_approval.EVENT + "\n" + json.dumps(event, sort_keys=True, separators=(",", ":"))
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    incoming = directory / "zapier-decisions"
    incoming.mkdir(mode=0o700)
    return_file = incoming / (issued["request_id"] + ".approved.json")
    return_file.write_text(json.dumps({"event": event, "signature": signature}))
    return_file.chmod(0o600)
    monkeypatch.delenv("OPENTOUR_ZAPIER_APPROVAL_HOOK_URL")
    assert approval_inbox.delivery_cycle(h.app)["plugin_returns"] == {"processed": 1, "held": 0}
    assert approval_inbox.delivery_cycle(h.app)["plugin_returns"] == {"processed": 0, "held": 0}
    released = json.loads(
        (directory / "managed-outputs" / run / "release-decision.json").read_text()
    )
    assert released["decision"] == "approved" and released["provider"] == "zapier_slack"
    if route == "rule" or os.environ.get("OPENTOUR_TEST_MODEL_BASE"):
        pack = repair_workflow.prepare_handoff(directory, h.store.organization, plan, run)
        assert pack["disposition"] == route

    connect.assert_called_once()
    h.config["approval_provider"] = "slack"
    h.save()
    assert approval_inbox.delivery_cycle(h.app) == {"posted": 0}


def test_what_got_worse_uses_actual_denominator_and_empty_is_undetermined() -> None:
    base = {"schema_version": slack_views.INPUT_VERSION, "error_ceiling_bps": 500, "cases": []}
    empty = slack_views.evaluate_manifest(base)
    body = slack_views.evidence_modal(context="test", revision="rev_generated", report=empty)
    assert "Undetermined" in json.dumps(body) and "no comparable cases" in json.dumps(body)
    base["cases"] = [
        {
            "case_id": "r1",
            "reference": "a",
            "current": {"status": "automatic", "label": "a"},
            "repair": {"status": "automatic", "label": "b"},
        },
        {
            "case_id": "r2",
            "reference": "a",
            "current": {"status": "automatic", "label": "b"},
            "repair": {"status": "automatic", "label": "a"},
        },
        {
            "case_id": "r3",
            "reference": None,
            "current": {"status": "review"},
            "repair": {"status": "missing"},
        },
    ]
    report = slack_views.evaluate_manifest(base)
    body = slack_views.evidence_modal(context="test", revision="rev_generated", report=report)
    assert "1 of 2 comparable cases became wrong. 1 became correct. 1 fell outside" in json.dumps(
        body
    )


def test_pending_review_is_first_and_always_opens_the_current_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h, plan, run, _ = setup_workflow(tmp_path, "rule", release=True)
    home = publish(h, monkeypatch)
    choices = buttons(home)
    assert list(choices)[0] == "ot_app_pending"
    assert "generated-repair" not in json.dumps(home) and run not in json.dumps(home)
    click(h, home, "ot_app_pending")
    v = list(h.slack.screens)[-1]
    ref = buttons(screen(h, v))["ot_release_approve"]
    assert ref == choices["ot_app_pending"] and "What got worse?" in text(h, v)
    h.action(v, "ot_release_changes", ref)
    assert "ot_app_pending" not in buttons(publish(h, monkeypatch))
    screens = len(h.slack.screens)
    click(h, home, "ot_app_pending")
    assert len(h.slack.screens) == screens + 1
    assert "Access unavailable" in text(h, list(h.slack.screens)[-1])


def test_plugin_missing_connection_keeps_evidence_and_holds_handoff(tmp_path: Path) -> None:
    h, _, run, _ = setup_workflow(tmp_path, "rule", release=True, provider="zapier_slack")
    h.config["approval_provider"] = "zapier_slack"
    h.save()
    view = h.launch()
    h.action(view, "ot_evidence", buttons(screen(h, view))["ot_evidence"])
    assert "Zapier approval request is on hold" in text(h, view) and "What got worse?" in text(
        h, view
    )
    assert "ot_release_approve" not in buttons(screen(h, view))
    assert not (h.path.parent / "managed-outputs" / run / "release-decision.json").exists()
