"""Render worker-persisted readiness through actual Home events and signed private grants."""

import json
import sqlite3
from dataclasses import replace

import pytest
from opentour_cell.pilot.intake_local import open_intake
from opentour_cell.pilot.intake_mcp import parse_intake
from opentour_cell.pilot.readiness_selection import SCHEMA, open_readiness
from opentour_cell.pilot.status_questions import StatusTopic
from test_app_home import click, publish
from test_intake_mcp import STARTER, _sealed_starter, _write_private
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


def test_unattributed_frozen_result_keeps_its_ordinary_answer(ready):
    h = ready.h
    ready.worker.refresh()
    _, revision = inspect(h)
    result = answer(h, "Why didn't it train?", revision)
    assert (
        "evaluation-only" in result
        and ready.workflow not in result
        and "No live decisions yet" not in result
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


def test_two_admitted_workflows_sharing_dataset_revision_do_not_share_readout_readiness(ready):
    h = ready.h
    root = ready.directory
    selection = {**json.loads((root / "selected.json").read_bytes()), "grant_ref": "second-grant"}
    charter = json.loads((root / "starter-intake-grant-policy-scope.json").read_bytes())["grant"]
    changes = {
        "creation_ref": "second-creation",
        "setup_policy_revision": charter["setup_policy_revision"],
    }
    _write_private(root / "second-selection.json", selection)
    _write_private(
        root / "starter-intake-grant-second-grant.json", _sealed_starter(selection, **changes)
    )
    actor, intake = open_intake(root / "second-selection.json")
    receipt = intake.submit_intake(
        actor, parse_intake({**STARTER, **changes, "idempotency_key": "second-admission"})
    )
    second = intake.get_run(actor, receipt.run_ref).workflow_ref
    assert second != ready.workflow
    # Both workflow grants name the exact same tenant, epoch, dataset and revision.
    sign(h, {**ready.grant, "workflow_ref": second})
    ready.worker.refresh()
    stale_view = h.launch(prompt="Why didn't it train?")
    old_token = buttons(screen(h, stale_view))["ot_current_training"]
    h.config["live_workflow_ref"] = second
    h.save()
    h.action(stale_view, "ot_current_training", old_token)
    assert "ot_current_training_status" != screen(h, stale_view).get("callback_id")
    new_view = h.launch(prompt="Why didn't it train?")
    new_token = buttons(screen(h, new_view))["ot_current_training"]
    assert old_token != new_token
    h.action(new_view, "ot_current_training", new_token)
    second_answer = json.dumps(screen(h, new_view))
    assert second in second_answer and ready.workflow not in second_answer
    assert "Current training readiness is unavailable" in second_answer
    h.config["live_workflow_ref"] = ready.workflow
    h.save()
    cfg = h.app.settings()
    target = h.app._readout(cfg, ACTOR, "eval:" + ready.grant["dataset_ref"])
    assert target is not None and target.dataset_revision == ready.grant["dataset_revision"]
    for frozen_workflow, should_match in ((second, False), (ready.workflow, True)):
        # Generated output of a producer that explicitly pins frozen workflow identity.
        pinned = replace(target, audit={**target.audit, "workflow_ref": frozen_workflow})
        value = h.app._status_answer(cfg, ACTOR, pinned, StatusTopic.TRAINING)
        assert "evaluation-only" in value.finding
        assert (ready.workflow in value.basis) == should_match
    # The actual frozen evaluation producer currently emits no workflow relation.
    _, revision = inspect(h)
    normal = answer(h, "Why didn't it train?", revision)
    assert ready.workflow not in normal and second not in normal


def test_home_live_evidence_distinguishes_unmatched_ingestion_and_read_coverage(ready):
    h = ready.h
    first = json.dumps(ready.home(), ensure_ascii=False)
    assert "Unmatched outcomes · 0" in first and "Last event ingested · unavailable" in first
    assert "No source-read receipt" in first
    view = open_live(h, ready.patch)
    h.form(view, form(h, view, 1))
    ready.ingest(outcome(verifier="independent-verifier"), truth=True)
    unmatched = json.dumps(ready.home(), ensure_ascii=False)
    assert "Unmatched outcomes · 1" in unmatched
    assert "Last event ingested · unavailable" not in unmatched
    assert "No source-read receipt" in unmatched
    live = ready.worker.source.live
    with sqlite3.connect(live.path) as db:
        ref = db.execute("SELECT ref FROM live_bindings WHERE kind='decision'").fetchone()[0]

    def poll(name, complete):
        return live.ingest_page(
            ready.worker.worker,
            ref,
            page_ref=name,
            source_revision="source-v1",
            events=(),
            source_positions=(),
            event_epochs=(),
            checkpoint=150,
            read_metadata={
                "interval_start": 10,
                "interval_end": 25,
                "coverage_through": 25 if complete else None,
                "complete": complete,
                "next_cursor": None if complete else "next",
            },
        )

    first_page = poll("one", False)
    partial = json.dumps(ready.home(), ensure_ascii=False)
    assert (
        first_page in partial
        and "0 rows, partial" in partial
        and "Coverage through unavailable" in partial
    )
    second = poll("two", True)
    completed = json.dumps(ready.home(), ensure_ascii=False)
    assert second in completed and "0 rows, complete" in completed
    assert "Coverage through 1970-01-01 00:00:25 UTC" in completed
    assert "Unmatched outcomes · 1" in completed
    ready.ingest(decision())
    assert "Unmatched outcomes · 0" in json.dumps(ready.home(), ensure_ascii=False)
    h.config["users"][ACTOR]["aggregate"] = False
    h.save()
    hidden = json.dumps(ready.home(), ensure_ascii=False)
    assert "Unmatched outcomes" not in hidden and second not in hidden


def test_revoked_live_scope_at_layout_withholds_source_summary(ready, monkeypatch):
    original = ready.h.app.views.app_home_view

    def revoke(*args, **kwargs):
        layout = original(*args, **kwargs)
        if kwargs.get("live_summary"):
            ready.grant["state"] = "revoked"
            sign(ready.h, ready.grant)
        return layout

    monkeypatch.setattr(ready.h.app.views, "app_home_view", revoke)
    home = json.dumps(ready.home(), ensure_ascii=False)
    assert "access is unavailable" in home and "Unmatched outcomes" not in home
    assert effects(ready.h) == (0,) * 7


@pytest.mark.parametrize("surface", ["slash", "home"])
def test_current_workflow_question_reaches_persisted_answer_and_back(ready, surface):
    h = ready.h
    question = "Is training worth it yet?" if surface == "slash" else "Why didn't it train?"
    if surface == "slash":
        view = h.launch(prompt=question)
    else:
        click(h, ready.home(), "ot_app_prompt")
        view = list(h.slack.screens)[-1]
        h.form(view, {"command": {"command": {"value": question}}})

    def choose():
        action = buttons(screen(h, view))["ot_current_training"]
        h.action(view, "ot_current_training", action)
        assert screen(h, view)["callback_id"] == "ot_current_training_status"
        return json.dumps(screen(h, view), ensure_ascii=False)

    empty = choose()
    assert "Current training readiness is unavailable" in empty and "0 verified" not in empty
    assert not ready.worker.readiness.path.exists()
    ready.ingest(decision())
    receipt = ready.worker.refresh()
    h.action(view, "ot_training_scopes", "scopes")
    pending = choose()
    assert "1 decision awaiting verification" in pending and receipt["readiness_ref"] in pending
    assert f"Current workflow · {ready.workflow}" in pending
    ready.bind()
    ready.ingest(outcome(verifier="independent-verifier"), truth=True)
    receipt = ready.worker.refresh()
    stable = {
        p: p.read_bytes()
        for p in (
            ready.worker.readiness.path,
            ready.worker.source.live.path,
            ready.directory / "workflow-benchmarks.sqlite3",
        )
    }
    h.action(view, "ot_training_scopes", "scopes")
    held = choose()
    assert "Too early to train" in held and "1 independent family" in held
    assert "0 residual groups" in held and receipt["readiness_ref"] in held
    h.action(view, "ot_readiness_proof", buttons(screen(h, view))["ot_readiness_proof"])
    proof = json.dumps(screen(h, view))
    assert screen(h, view)["callback_id"] == "ot_current_training_proof"
    assert receipt["assessment_ref"] in proof and receipt["evidence_revision"] in proof
    assert all(p.read_bytes() == body for p, body in stable.items()) and effects(h) == (0,) * 7


def test_changed_receipt_or_actor_cannot_select_stale_training_scope(ready):
    h = ready.h
    first = ready.worker.refresh()
    view = h.launch(prompt="Why didn't it train?")
    pin = buttons(screen(h, view))["ot_current_training"]
    ready.ingest(decision())
    current = ready.worker.refresh()
    h.action(view, "ot_current_training", pin)
    result = json.dumps(screen(h, view))
    assert current["readiness_ref"] not in result and first["readiness_ref"] not in result
    view = h.launch(prompt="Why didn't it train?")
    pin = buttons(screen(h, view))["ot_current_training"]
    h.action(view, "ot_current_training", pin, OTHER)
    assert screen(h, view)["callback_id"] != "ot_current_training_status"
    assert effects(h) == (0,) * 7


def test_current_training_question_shows_justified_residual_and_scoped_mandate(ready):
    import time
    from pathlib import Path

    from opentour_cell.pilot.learning_mandate import LearningMandates

    h = ready.h
    owner = "workflow-owner"
    specification = json.loads(
        (
            Path(__file__).resolve().parents[2] / "examples/pilot-learning-mandate.template.json"
        ).read_text()
    )
    specification.update(
        mandate_id="fixture-current-training",
        owner_id=owner,
        workflow=ready.workflow,
        delegate=ready.worker.worker.actor,
        trainer=ready.worker.worker.actor,
        agreement_revision="e" * 64,
        expires_at=int(time.time()) + 900,
    )
    mandates = LearningMandates(ready.directory, h.store.organization, h.store.engagement_id)
    enrolled = mandates.enroll(specification, actor=owner)
    ready.policy.update(
        owner_ref=owner,
        mandate_ref=enrolled["mandate_id"],
        mandate_revision=enrolled["mandate_revision"],
        agreement_revision="e" * 64,
        target_ref="registered-target",
        rule_mapping={"carrier": "supplies"},
    )
    _write_private(ready.directory / "repair-policy.json", ready.policy)
    ready.ingest(decision())
    ready.worker.refresh()
    ready.bind()
    for i in range(1, 4):
        if i != 1:
            ready.ingest(decision(f"D{i}", f"supplier-{i}"))
        ready.ingest(outcome(f"D{i}", verifier="independent-verifier"), truth=True)
    receipt = ready.worker.refresh()
    assert (
        receipt["reason_code"] == "untouched_holdout_required"
        and receipt["residual_group_count"] == 3
    )
    view = h.launch(prompt="Why didn't it train?")
    h.action(view, "ot_current_training", buttons(screen(h, view))["ot_current_training"])
    result = json.dumps(screen(h, view))
    assert (
        "3 independent families" in result
        and "3 residual groups" in result
        and "independent holdout" in result
    )
    h.action(view, "ot_readiness_proof", buttons(screen(h, view))["ot_readiness_proof"])
    assert enrolled["mandate_revision"] in json.dumps(screen(h, view))
    assert (
        mandates.inspect(enrolled["mandate_id"], actor=ready.worker.worker.actor)["reserved_fits"]
        == 0
    )
    assert effects(h) == (0,) * 7


def test_current_training_proof_revocation_at_disclosure_hides_receipt(ready, monkeypatch):
    receipt = ready.worker.refresh()
    h = ready.h
    view = h.launch(prompt="Why didn't it train?")
    h.action(view, "ot_current_training", buttons(screen(h, view))["ot_current_training"])
    pin = buttons(screen(h, view))["ot_readiness_proof"]
    original = h.app.views.current_training_question

    def revoke(*args, **kwargs):
        modal = original(*args, **kwargs)
        if kwargs["proof"]:
            ready.grant["state"] = "revoked"
            sign(h, ready.grant)
        return modal

    monkeypatch.setattr(h.app.views, "current_training_question", revoke)
    h.action(view, "ot_readiness_proof", pin)
    assert receipt["readiness_ref"] not in json.dumps(screen(h, view))
    assert effects(h) == (0,) * 7
