"""OPE-560 installed Socket Mode projection with generated worker and Slack receipts."""

import sys
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from opentour_cell.pilot import slack_adapter, slack_views
from opentour_cell.pilot.slack_adapter import Refused
from opentour_cell.pilot.slack_learning import LearningPlan, LearningStatus
from test_app_home import click
from test_reviewer_workflow import buttons, restart, screen
from test_slack_adapter import ACTOR, APP, CHANNEL, OTHER, TEAM

pytest_plugins = ("test_home_readiness",)


class Worker:
    def __init__(self):
        self.receipt = LearningStatus(
            "run-generated",
            "running",
            False,
            "receipt-1",
            3,
            12,
            None,
            15,
            can_pause=True,
            can_stop=True,
        )
        self.requests = []

    def plan(self, scope):
        return LearningPlan(
            scope,
            "plan-generated",
            "route-private",
            "bench-private",
            "mandate-private",
            1,
            0,
            "off",
            False,
            False,
            self.receipt.run_ref,
        )

    def status(self, scope, run_ref):
        assert self.plan(scope).current_run == run_ref
        return self.receipt

    def control(self, scope, run_ref, action, revision, key):
        assert scope.actor == OTHER and run_ref == self.receipt.run_ref
        assert revision == self.receipt.revision and action in self.receipt.controls()
        self.requests.append((action, key))
        self.receipt = replace(
            self.receipt,
            revision=f"request-{len(self.requests)}",
            state={"pause": "pausing", "resume": "resuming", "stop": "stopping"}[action],
            can_pause=False,
            can_resume=False,
            can_stop=False,
        )
        return self.receipt


@pytest.fixture
def installed(ready, monkeypatch):
    h = ready.h
    h.config.update(channel_aggregates=True, learning_thread_ts="100.001")
    h.save()
    worker = Worker()
    h.app.learning_service = worker
    calls = []

    def deliver(operation, payload):
        calls.append((operation, payload))
        ts = "101.101"
        if operation == "update":
            assert payload["ts"] == ts
        return {"ok": True, "channel": payload["channel"], "ts": ts}

    monkeypatch.setattr(h.slack, "chat_postMessage", lambda **kw: deliver("post", kw))
    monkeypatch.setattr(h.slack, "chat_update", lambda **kw: deliver("update", kw), raising=False)
    return h, worker, calls


def press_payload(payload, action):
    return {
        "type": "block_actions",
        "api_app_id": APP,
        "team": {"id": TEAM},
        "user": {"id": OTHER},
        "channel": {"id": CHANNEL},
        "container": {"type": "message", "channel_id": CHANNEL, "message_ts": "101.101"},
        "message": {"ts": "101.101", "thread_ts": "100.001"},
        "actions": [{"action_id": action, "value": buttons(payload)[action]}],
    }


def content(payload):
    return "\n".join(block["text"]["text"] for block in payload["blocks"] if "text" in block)


def test_live_card_observations_controls_and_restart(installed):
    h, worker, calls = installed
    h.app.sync_learning_card()
    first = calls[-1][1]
    assert first["thread_ts"] == "100.001"
    assert "3 of 12 steps" in content(first) and "15 seconds" in content(first)
    assert "Confirmed checkpoint · Awaiting confirmation" in content(first)
    assert "mandate-private" not in str(first) and "route-private" not in str(first)
    assert set(buttons(first)) == {"ot_learning_pause", "ot_learning_stop"}
    pause = press_payload(first, "ot_learning_pause")
    h.send("interactive", pause)
    h.send("interactive", pause)
    assert [action for action, _ in worker.requests] == ["pause"]
    h.app.sync_learning_card()
    assert "Pause requested" in content(calls[-1][1]) and not buttons(calls[-1][1])
    worker.receipt = replace(
        worker.receipt,
        state="paused",
        revision="confirmed-2",
        completed_steps=4,
        checkpoint_step=4,
        active_seconds=22,
        can_resume=True,
        can_stop=True,
    )
    restart(h)
    h.app.learning_service = worker
    h.app.sync_learning_card()
    paused = calls[-1][1]
    assert "Confirmed checkpoint · Step 4" in content(paused)
    assert set(buttons(paused)) == {"ot_learning_resume", "ot_learning_stop"}
    h.app.sync_learning_card()
    assert [op for op, _ in calls] == ["post", "update", "update"]
    h.send("interactive", press_payload(paused, "ot_learning_resume"))
    h.app.sync_learning_card()
    assert "Resume requested" in content(calls[-1][1])
    worker.receipt = replace(
        worker.receipt,
        state="running",
        revision="confirmed-3",
        completed_steps=7,
        active_seconds=48,
        can_stop=True,
    )
    h.app.sync_learning_card()
    assert "7 of 12 steps" in content(calls[-1][1]) and "48 seconds" in content(calls[-1][1])
    h.send("interactive", press_payload(calls[-1][1], "ot_learning_stop"))
    h.app.sync_learning_card()
    assert "Stop requested" in content(calls[-1][1])
    worker.receipt = replace(worker.receipt, state="stopped", revision="confirmed-4")
    h.app.sync_learning_card()
    assert "Watching continues" in content(calls[-1][1]) and not buttons(calls[-1][1])
    assert [action for action, _ in worker.requests] == ["pause", "resume", "stop"]
    assert len({key for _, key in worker.requests}) == 3


def test_private_card_rejects_stale_forged_and_revoked_controls(installed):
    h, worker, calls = installed
    h.app.sync_learning_card()
    press = press_payload(calls[-1][1], "ot_learning_stop")
    for field, key, value in (
        ("user", "id", ACTOR),
        ("team", "id", "TFOREIGN"),
        ("channel", "id", "CFOREIGN"),
        ("container", "message_ts", "999.999"),
        ("message", "thread_ts", "999.999"),
        ("container", "channel_id", "CFOREIGN"),
    ):
        foreign = deepcopy(press)
        foreign[field][key] = value
        h.send("interactive", foreign)
    h.slack.channel["is_ext_shared"] = True
    h.send("interactive", press)
    h.slack.channel.pop("is_ext_shared")
    h.slack.members.remove(OTHER)
    h.send("interactive", press)
    h.slack.members.append(OTHER)
    h.config["users"][OTHER]["roles"].remove("review")
    h.save()
    h.send("interactive", press)
    h.config["users"][OTHER]["roles"].append("review")
    h.config["channel_aggregates"] = False
    h.save()
    h.send("interactive", press)
    h.app.sync_learning_card()
    h.config["channel_aggregates"] = True
    h.save()
    worker.receipt = replace(worker.receipt, revision="new")
    h.send("interactive", press)
    worker.receipt = replace(worker.receipt, revision="receipt-1", stale=True)
    h.send("interactive", press)
    h.app.sync_learning_card()
    assert [op for op, _ in calls] == ["post", "update"]
    assert "Progress is stale" in content(calls[-1][1]) and not buttons(calls[-1][1])
    assert worker.requests == []


def test_completed_card_does_not_publish_a_release(installed):
    h, worker, calls = installed
    h.config.pop("learning_thread_ts")
    h.save()
    worker.receipt = replace(worker.receipt, state="completed", completed_steps=12)
    h.app.sync_learning_card()
    assert "thread_ts" not in calls[-1][1]
    assert "Awaiting independent scoring" in content(calls[-1][1]) and not buttons(calls[-1][1])


def test_uncertain_post_is_not_repeated_after_restart(installed, monkeypatch):
    h, worker, calls = installed

    def uncertain(**payload):
        calls.append(("unknown", payload))
        raise TimeoutError()

    monkeypatch.setattr(h.slack, "chat_postMessage", uncertain)
    with pytest.raises(TimeoutError):
        h.app.sync_learning_card()
    restart(h)
    h.app.learning_service = worker
    h.app.sync_learning_card()
    assert len(calls) == 1
    h.send("interactive", press_payload(calls[0][1], "ot_learning_pause"))
    assert not worker.requests


def test_revoke_during_card_render_prevents_delivery(installed, monkeypatch):
    h, _, calls = installed
    layout = slack_views.learning_progress_blocks

    def revoke(*args, **kwargs):
        blocks = layout(*args, **kwargs)
        h.config["users"][OTHER]["aggregate"] = False
        h.save()
        return blocks

    monkeypatch.setattr(slack_views, "learning_progress_blocks", revoke)
    with pytest.raises(Refused):
        h.app.sync_learning_card()
    assert not calls


def test_home_modal_controls_refresh_after_observation(installed, ready):
    h, worker, _ = installed
    click(h, ready.home(OTHER), "ot_app_prompt", OTHER)
    view = list(h.slack.screens)[-1]
    h.form(view, {"command": {"command": {"value": "Why didn't it train?"}}}, OTHER)

    def press(action):
        h.action(view, action, buttons(screen(h, view))[action], OTHER)

    for action in ("ot_current_training", "ot_learning_preview", "ot_learning_status"):
        press(action)
    worker.receipt = replace(worker.receipt, completed_steps=5, revision="latest")
    press("ot_learning_status")
    assert "5 of 12 steps" in content(screen(h, view))
    press("ot_learning_pause")
    assert "Pause requested" in content(screen(h, view))
    assert len(worker.requests) == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"completed_steps": -1},
        {"checkpoint_step": 4},
        {"active_seconds": "private-path"},
        {"completed_steps": 13},
        {"total_steps": True},
        {"can_resume": "yes"},
    ],
)
def test_only_normalized_measured_fields_can_render(fields):
    with pytest.raises(ValueError):
        slack_views.learning_progress_blocks(replace(Worker().receipt, **fields), "pin")


def test_definite_rejection_retries_but_uncertain_update_reuses_timestamp(installed, monkeypatch):
    h, worker, calls = installed
    with monkeypatch.context() as patch:
        patch.setattr(
            h.slack, "chat_postMessage", lambda **kw: {"ok": False, "error": "not_in_channel"}
        )
        h.app.sync_learning_card()
    h.app.sync_learning_card()
    worker.receipt = replace(worker.receipt, completed_steps=6)
    with monkeypatch.context() as patch:

        def uncertain(**payload):
            calls.append(("uncertain", payload))
            raise TimeoutError()

        patch.setattr(h.slack, "chat_update", uncertain)
        with pytest.raises(TimeoutError):
            h.app.sync_learning_card()
    restart(h)
    h.app.learning_service = worker
    h.app.sync_learning_card()
    assert [operation for operation, _ in calls] == ["post", "uncertain", "update"]
    assert calls[1][1] == calls[2][1] and calls[2][1]["ts"] == "101.101"


def test_installed_socket_startup_opens_bound_service_and_publishes(installed, monkeypatch):
    h, worker, calls = installed
    opened = []

    def open_service(directory, organization, workspace, channel):
        opened.append((directory, organization, workspace, channel))
        return worker

    monkeypatch.setitem(
        sys.modules,
        "opentour_cell.pilot.eve_learning_service",
        SimpleNamespace(open_eve_learning_service=open_service),
    )
    monkeypatch.setattr(
        h.slack, "auth_test", lambda: {"team_id": TEAM, "bot_id": "BGENERATED"}, raising=False
    )
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-generated-offline")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-generated-offline")
    monkeypatch.setitem(sys.modules, "slack_sdk", SimpleNamespace(WebClient=lambda **kw: h.slack))
    socket = SimpleNamespace(
        socket_mode_request_listeners=[], connect=lambda: None, close=lambda: None
    )
    monkeypatch.setitem(
        sys.modules, "slack_sdk.socket_mode", SimpleNamespace(SocketModeClient=lambda **kw: socket)
    )
    monkeypatch.setitem(
        sys.modules, "slack_sdk.socket_mode.response", SimpleNamespace(SocketModeResponse=object)
    )
    sync = slack_adapter.Workbench.sync_learning_card

    def one_cycle(installed_app):
        sync(installed_app)
        raise KeyboardInterrupt

    monkeypatch.setattr(slack_adapter.Workbench, "sync_learning_card", one_cycle)
    assert slack_adapter.main([str(h.path)]) == 0
    assert opened == [(h.path.parent, h.store.organization, TEAM, CHANNEL)]
    assert len(socket.socket_mode_request_listeners) == 1
    assert len(calls) == 1 and "3 of 12 steps" in content(calls[0][1])


def test_contending_publishers_and_root_message_with_thread_replies(installed, monkeypatch):
    h, worker, calls = installed
    h.config.pop("learning_thread_ts")
    h.save()
    peer = slack_adapter.Workbench(
        h.app.config, h.slack, h.store, slack_views, learning_service=worker
    )
    post = h.slack.chat_postMessage

    def racing_post(**payload):
        peer.sync_learning_card()
        return post(**payload)

    monkeypatch.setattr(h.slack, "chat_postMessage", racing_post)
    try:
        h.app.sync_learning_card()
        peer.sync_learning_card()
    finally:
        peer.pool.shutdown()
    assert len(calls) == 1
    body = press_payload(calls[0][1], "ot_learning_pause")
    body["message"]["thread_ts"] = body["message"]["ts"]
    h.send("interactive", body)
    assert [action for action, _ in worker.requests] == ["pause"]
