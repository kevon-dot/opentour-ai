"""Render worker-persisted readiness through actual Home events and signed private grants."""

import json
import sqlite3

import pytest
from opentour_cell.pilot.readiness_selection import SCHEMA, open_readiness
from test_app_home import click, publish
from test_intake_mcp import _write_private
from test_live_readiness import Case, Contexts, decision, outcome
from test_live_settings import form, open_live, sign
from test_reviewer_workflow import buttons, restart, screen, supplied
from test_slack_adapter import ACTOR, OTHER
from test_starter_setup import effects
from test_status_questions import answer, inspect

pytest_plugins = ("test_live_settings",)


class ReadyHome:
    def __init__(self, h, grant, monkeypatch):
        self.h, self.grant, self.patch = h, grant, monkeypatch
        self.workflow = grant["workflow_ref"]
        self.directory = h.path.parent
        self.selection = {
            "schema_version": SCHEMA,
            "workflow_ref": self.workflow,
            "worker_ref": "readiness-worker",
            "policy_file": "repair-policy.json",
        }
        _write_private(self.directory / "readiness-selection.json", self.selection)
        grant["actors"]["readiness-worker"] = ["inspect", "intake"]
        dataset = supplied(h, qualified=True)
        grant.update(
            dataset_ref=dataset,
            dataset_revision=h.store.dataset_summary(dataset)["dataset_revision"],
        )
        sign(h, grant)
        h.config["live_workflow_ref"] = self.workflow
        for user in (ACTOR, OTHER):
            h.config["users"][user].update(datasets=[dataset], aggregate=True)
        h.save()
        sample = Case(self.directory / "generated-policy-template")
        self.policy = dict(
            sample.policy,
            organization=h.store.organization,
            workflow_ref=self.workflow,
            engagement=h.store.engagement_id,
            dataset_ref=dataset,
            dataset_revision=grant["dataset_revision"],
        )
        _write_private(self.directory / "repair-policy.json", self.policy)
        self.contexts = Contexts()
        self.worker = open_readiness(
            self.directory, h.store.organization, self.workflow, contexts=self.contexts
        )
        view = open_live(h, monkeypatch)
        h.form(view, form(h, view))
        self.position = 100

    def ingest(self, *events, truth=False):
        self.position += len(events)
        with sqlite3.connect(self.directory / "live-evidence.sqlite3") as db:
            ref = db.execute(
                "SELECT ref FROM live_bindings WHERE kind=? AND successor IS NULL",
                ("independent_outcome" if truth else "decision",),
            ).fetchone()[0]
        positions = tuple(range(self.position - len(events) + 1, self.position + 1))
        return self.worker.source.live.ingest_page(
            self.worker.worker,
            ref,
            page_ref=f"page-{self.position}",
            source_revision="truth-v1" if truth else "source-v1",
            events=events,
            source_positions=positions,
            event_epochs=(self.h.store.engagement_id,) * len(events),
            checkpoint=self.position,
        )

    def home(self, actor=ACTOR):
        return publish(self.h, self.patch, actor)

    def bind(self):
        home = self.home()
        click(self.h, home, "ot_app_bind_verifier")
        view = list(self.h.slack.screens)[-1]
        block = next(
            b for b in screen(self.h, view)["blocks"] if b.get("block_id") == "live_resource"
        )
        options = block["element"]["options"]
        assert len(options) == 1 and "Independent outcomes" in options[0]["text"]["text"]
        self.h.form(view, form(self.h, view))


@pytest.fixture
def ready(connected_settings, monkeypatch):
    return ReadyHome(*connected_settings, monkeypatch)


def test_home_first_verified_correction_and_reopen(ready):
    h = ready.h
    assert "No live decisions yet" not in json.dumps(ready.home())
    assert not ready.worker.readiness.path.exists()
    empty = ready.worker.refresh()
    home = json.dumps(ready.home())
    assert (
        empty["decision_count"] == 0
        and "No live decisions yet" in home
        and "error rate" not in home
    )
    ready.ingest(decision(), decision(), decision())
    assert "No live decisions yet" not in json.dumps(ready.home())
    assert ready.worker.refresh()["reason_code"] == "no_verifier_bound"
    pending = ready.home()
    assert "1 decision awaiting verification" in json.dumps(
        pending
    ) and "No verifier bound" in json.dumps(pending)
    assert "ot_app_bind_verifier" in buttons(pending)
    reader = ready.home(OTHER)
    assert "Ask the data custodian to bind a verifier" in json.dumps(reader)
    assert "ot_app_bind_verifier" not in buttons(reader)
    ready.bind()
    assert ready.worker.refresh()["reason_code"] == "awaiting_outcome"
    home = json.dumps(ready.home())
    assert "Awaiting outcome" in home and "independent-verifier" in home
    ready.ingest(outcome(verifier="independent-verifier"), truth=True)
    first = ready.worker.refresh()
    assert first["residual_group_count"] == 0
    home = json.dumps(ready.home())
    assert (
        "Too early to train" in home
        and "1 verified root." in home
        and "0 residual groups after simple repair" in home
        and "threshold 3" in home
    )
    assert "Augmentation is not enabled" in home
    assert len(ready.contexts.inputs) == 101 and ready.contexts.reads == ["input-1"]
    restart(h)
    assert "Too early to train" in json.dumps(ready.home())
    assert ready.worker.refresh()["readiness_ref"] == first["readiness_ref"]
    ready.ingest(
        outcome(
            verifier="independent-verifier",
            outcome_revision=2,
            event_revision=2,
            finality="reopened",
            verified_action_ref=None,
        ),
        truth=True,
    )
    assert "Too early to train" not in json.dumps(ready.home())
    refreshed = ready.worker.refresh()
    assert (refreshed["verified_root_count"], refreshed["pending_count"]) == (0, 1)
    assert "Awaiting outcome" in json.dumps(ready.home())
    assert effects(h) == (0,) * 7


def test_missing_or_foreign_worker_and_slack_scope(ready):
    ready.worker.refresh()
    h = ready.h
    original = ready.worker.readiness.path.read_bytes()
    for actor in ("unknown-worker", ACTOR):
        _write_private(
            ready.directory / "readiness-selection.json", {**ready.selection, "worker_ref": actor}
        )
        assert "No live decisions yet" not in json.dumps(ready.home())
    _write_private(ready.directory / "readiness-selection.json", ready.selection)
    h.config["users"][ACTOR]["aggregate"] = False
    h.save()
    assert "No live decisions yet" not in json.dumps(ready.home())
    assert ready.worker.readiness.path.read_bytes() == original and effects(h) == (0,) * 7


def test_evidence_reopened_during_layout_never_publishes_stale_home(ready, monkeypatch):
    ready.worker.refresh()
    original = ready.h.app.views.app_home_view

    def move(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get("readiness"):
            ready.ingest(decision())
        return result

    monkeypatch.setattr(ready.h.app.views, "app_home_view", move)
    home = ready.home()
    assert "No live decisions yet" not in json.dumps(home)
    assert "access is unavailable" in json.dumps(home)


def test_natural_training_answer_links_only_matching_readout(ready):
    h = ready.h
    ready.worker.refresh()
    _, revision = inspect(h)
    result = answer(h, "Why didn't it train?", revision)
    assert (
        "evaluation-only" in result
        and ready.workflow in result
        and "No live decisions yet" in result
    )
    other = supplied(h, qualified=True, suffix="-other")
    h.config["users"][ACTOR]["datasets"] = [other]
    h.save()
    _, other_revision = inspect(h)
    other_result = answer(h, "Why didn't it train?", other_revision)
    assert "evaluation-only" in other_result and ready.workflow not in other_result
    assert effects(h) == (0,) * 7


def test_threshold_reached_requests_missing_mandate_and_never_fits(ready):
    ready.policy["rule_mapping"] = {"carrier": "supplies"}
    _write_private(ready.directory / "repair-policy.json", ready.policy)
    ready.ingest(decision())
    ready.worker.refresh()
    ready.bind()
    for index in range(1, 4):
        if index != 1:
            ready.ingest(decision(f"D{index}", f"supplier-{index}"))
        ready.ingest(outcome(f"D{index}", verifier="independent-verifier"), truth=True)
        current = ready.worker.refresh()
        assert current["residual_group_count"] == index and current["verdict"] == "held"
        home = json.dumps(ready.home())
        unit = "group" if index == 1 else "groups"
        assert f"{index} residual {unit} after simple repair" in home
        assert ("Training needs a current mandate" if index == 3 else "Too early to train") in home
    with sqlite3.connect(ready.directory / "learning-mandates.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone() == (0,)
    assert effects(ready.h) == (0,) * 7


def test_bind_action_expires_with_readiness_and_cannot_select_decisions(ready):
    ready.ingest(decision())
    ready.worker.refresh()
    click(ready.h, ready.home(), "ot_app_bind_verifier")
    view = list(ready.h.slack.screens)[-1]
    values = form(ready.h, view)
    ready.ingest(decision("D2", "supplier-2"))
    ready.h.form(view, values)
    with sqlite3.connect(ready.directory / "live-evidence.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM live_bindings").fetchone() == (1,)
    ready.worker.refresh()
    click(ready.h, ready.home(), "ot_app_bind_verifier")
    view = list(ready.h.slack.screens)[-1]
    full = open_live(ready.h, ready.patch)
    ready.h.form(view, form(ready.h, full, 0))
    with sqlite3.connect(ready.directory / "live-evidence.sqlite3") as db:
        assert db.execute(
            "SELECT count(*) FROM live_bindings WHERE kind='independent_outcome'"
        ).fetchone() == (0,)


def test_unavailable_policy_and_no_outcome_resource_have_no_bind_action(ready):
    ready.grant["grants"] = ready.grant["grants"][:1]
    sign(ready.h, ready.grant)
    ready.ingest(decision())
    ready.worker.refresh()
    home = ready.home()
    assert "Ask the data custodian" in json.dumps(home) and "ot_app_bind_verifier" not in buttons(
        home
    )
    _write_private(
        ready.directory / "readiness-selection.json",
        {**ready.selection, "policy_file": "../wrong.json"},
    )
    assert "decision awaiting verification" not in json.dumps(ready.home())
