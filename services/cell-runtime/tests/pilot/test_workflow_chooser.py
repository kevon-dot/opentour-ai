"""Follow both private setup routes from the generated one-policy founder fixture."""

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from opentour_cell.pilot import slack_adapter, slack_views
from opentour_cell.pilot.training import LocalTrainingStore
from test_app_home import click, publish
from test_reviewer_workflow import buttons
from test_slack_adapter import ACTOR, APP, CHANNEL, OTHER, TEAM, Harness, Slack

Json = dict[str, Any]


class Founder(Harness):
    """The two founder settings fixtures from the customer-entry probe on PR #87."""

    def __init__(self, root: Path, local_recipient: bool) -> None:
        root.mkdir(mode=0o700)
        (root / "invoice-policy.md").write_text(
            "Carrier charges on invoices with a purchase order are freight."
        )
        self.path = root / "settings.json"
        self.config = {
            "organization": "founder",
            "team": TEAM,
            "app": APP,
            "channel": CHANNEL,
            "reviewer": ACTOR,
            "directory": str(root),
            "users": {
                ACTOR: {
                    "roles": ["inspect", "review"],
                    "datasets": [],
                    "preview": True,
                    "aggregate": True,
                    "expires_at": 4102444800,
                }
            },
        }
        if local_recipient:
            self.config["recipient"] = ACTOR
        self.save()
        self.store = LocalTrainingStore(root / "training.sqlite3", "founder")
        self.slack = Slack()
        self.work = MagicMock()
        self.app = slack_adapter.Workbench(
            lambda: slack_adapter.Settings.load(self.path),
            self.slack,
            self.store,
            slack_views,
            submit=self.work,
        )

    def assert_no_effects(self) -> None:
        self.work.assert_not_called()
        assert not self.slack.post
        with sqlite3.connect(self.store.path) as db:
            for table in ("datasets", "jobs", "fit_checkpoints", "artifacts"):
                assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        with self.app.journal.db() as db:
            for table in ("jobs", "proposals", "decisions", "approval_posts", "authorizations"):
                assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert not list(self.path.parent.glob("managed-*"))
        assert not list(self.path.parent.glob("zapier-*"))


@pytest.fixture(params=[False, True], ids=["no-recipient", "local-evaluation-owner"])
def founder(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Founder]:
    h = Founder(tmp_path / "customer", request.param)
    try:
        yield h
    finally:
        h.app.pool.shutdown()


def emitted(view: Json, label: str) -> Json:
    matches = [
        item
        for block in view["blocks"]
        for item in block.get("elements", [])
        if item.get("type") == "button" and item["text"]["text"] == label
    ]
    assert len(matches) == 1, label
    return matches[0]


def open_choices(h: Founder, home: Json) -> tuple[str, Json]:
    click(h, home, emitted(home, "Add workflow")["action_id"])
    view_id = list(h.slack.screens)[-1]
    view = h.slack.screens[view_id]
    assert view["type"] == "modal" and view["callback_id"] == "ot_workflow_chooser"
    assert len(buttons(view)) == 2
    return view_id, view


def test_one_policy_fresh_home_offers_workflow_creation(
    founder: Founder, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = founder
    assert h.store.catalog() == []
    home = publish(h, monkeypatch)
    h.slack.views_publish.assert_called_once_with(user_id=ACTOR, view=home)
    for label, expected in (
        ("We’re starting from scratch", ("permitted policy", "revision", "workflow owner")),
        (
            "Connect existing decisions",
            (
                "original decision feed",
                "independent correction source",
                "authorized data custodian",
                "submit_intake",
            ),
        ),
    ):
        view_id, chooser = open_choices(h, home)
        h.assert_no_effects()
        action = emitted(chooser, label)
        h.action(view_id, action["action_id"], action["value"])
        destination = h.slack.screens[view_id]
        assert destination["type"] == "modal" and not buttons(destination)
        assert all(word in json.dumps(destination) for word in expected)
        assert destination["private_metadata"] != chooser["private_metadata"]
        h.assert_no_effects()


@pytest.mark.parametrize("stage", ["home", "starter", "historical"])
@pytest.mark.parametrize("loss", ["membership", "role", "unknown", "expired", "withdrawn"])
def test_old_controls_recheck_current_access(
    founder: Founder, monkeypatch: pytest.MonkeyPatch, stage: str, loss: str
) -> None:
    h = founder
    home = publish(h, monkeypatch)
    if stage != "home":
        view_id, chooser = open_choices(h, home)
        label = (
            "We’re starting from scratch" if stage == "starter" else "Connect existing decisions"
        )
        choice = emitted(chooser, label)
    if loss == "membership":
        h.slack.members.remove(ACTOR)
    elif loss == "role":
        h.config["users"][ACTOR]["roles"].remove("review")
    elif loss == "expired":
        h.config["users"][ACTOR]["expires_at"] = 0
    elif loss == "unknown":
        del h.config["users"][ACTOR]
    else:
        h.store.withdraw()
    h.save()
    if stage == "home":
        click(h, home, emitted(home, "Add workflow")["action_id"])
    else:
        h.action(view_id, choice["action_id"], choice["value"])
    if loss == "withdrawn":
        if stage == "home":
            assert not h.slack.screens
        else:
            assert h.slack.screens[view_id] == chooser
    else:
        denial = list(h.slack.screens.values())[-1]
        assert "Ask the pilot operator" in json.dumps(denial) and not buttons(denial)
    fresh = publish(h, monkeypatch)
    assert "ot_app_add_workflow" not in buttons(fresh)
    if loss != "role":
        assert "check your current access" in json.dumps(fresh) and not buttons(fresh)
    h.assert_no_effects()


def test_other_workspace_member_cannot_reuse_founder_controls(
    founder: Founder, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = founder
    h.config["users"][OTHER] = h.config["users"][ACTOR].copy()
    h.save()
    home = publish(h, monkeypatch)
    assert "ot_app_add_workflow" not in buttons(publish(h, monkeypatch, actor=OTHER))
    click(h, home, emitted(home, "Add workflow")["action_id"], actor=OTHER)
    assert not h.slack.screens
    view_id, chooser = open_choices(h, home)
    action = emitted(chooser, "Connect existing decisions")
    h.action(view_id, action["action_id"], action["value"], actor=OTHER)
    assert h.slack.screens[view_id] == chooser
    h.assert_no_effects()
