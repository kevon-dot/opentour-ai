"""Generated private Slack settings interactions against the signed live authority."""

import hmac
import json
import sqlite3
import time
from dataclasses import asdict

import pytest
from opentour_cell.pilot.intake_admission import DurableIntake
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal, parse_intake
from opentour_cell.pilot.live_authority import (
    SCHEMA,
    TEMPORARY_ENFORCEMENT_OPT_OUT,
    LocalLiveIntake,
    PrivateLiveAuthority,
)
from opentour_cell.pilot.live_evidence import _canonical
from test_app_home import click, publish
from test_intake_mcp import _write_private
from test_live_evidence import DECISION, OUTCOME
from test_reviewer_workflow import buttons, restart, screen, text
from test_slack_adapter import ACTOR, OTHER, Harness
from test_starter_setup import effects, open_starter

pytest_plugins = ("test_starter_setup",)

KEY = bytes.fromhex("74" * 32)


def sign(h, body):
    _write_private(
        h.path.parent / f"live-authority-{body['workflow_ref']}.json",
        {"grant": body, "seal_sha256": hmac.digest(KEY, _canonical(body).encode(), "sha256").hex()},
    )


@pytest.fixture
def connected_settings(founder: Harness, monkeypatch):
    h = founder
    view = open_starter(h, monkeypatch)
    h.action(view, "ot_starter_save", buttons(screen(h, view))["ot_starter_save"])
    draft = json.loads(next(h.path.parent.glob("source-intake-starter-draft-*.json")).read_bytes())
    body = {
        "schema_version": SCHEMA,
        "tenant": h.store.organization,
        "workflow_ref": draft["workflow_ref"],
        "engagement_epoch": h.store.engagement_id,
        "dataset_ref": "dataset-a",
        "dataset_revision": "dataset-v1",
        "custodian": ACTOR,
        "expires_at": int(time.time()) + 3600,
        "state": "current",
        "actors": {ACTOR: ["custodian", "inspect"], OTHER: ["inspect"]},
        "grants": [asdict(DECISION), asdict(OUTCOME)],
        # Fixture predates the enforcing default; it does not enroll an engagement ledger.
        "engagement_ledger_required": TEMPORARY_ENFORCEMENT_OPT_OUT,
    }
    _write_private(h.path.parent / "live-authority-key.json", {"key": KEY.hex()})
    sign(h, body)
    return h, body


def open_live(h, monkeypatch, actor=ACTOR):
    home = publish(h, monkeypatch, actor)
    assert "ot_app_settings" in buttons(home)
    click(h, home, "ot_app_settings", actor)
    view = list(h.slack.screens)[-1]
    assert screen(h, view)["callback_id"] == "ot_workflow_settings"
    h.action(view, "ot_connect_live", buttons(screen(h, view))["ot_connect_live"], actor)
    return view


def form(h, view, index=0):
    block = next(b for b in screen(h, view)["blocks"] if b.get("block_id") == "live_resource")
    option = block["element"]["options"][index]
    return {
        "live_resource": {"live_resource": {"type": "static_select", "selected_option": option}}
    }


def test_save_same_binding_twice_and_reopen(connected_settings, monkeypatch):
    h, body = connected_settings
    intake = h.path.parent / "intake.sqlite3"
    before = intake.read_bytes()
    draft = next(h.path.parent.glob("source-intake-starter-draft-*.json"))
    original = draft.read_bytes()
    view = open_live(h, monkeypatch)
    live_db = h.path.parent / "live-evidence.sqlite3"
    assert not live_db.exists()
    assert (
        body["workflow_ref"] in text(h, view)
        and screen(h, view)["submit"]["text"] == "Save binding"
    )
    for n in (0, 0, 1):
        h.form(view, form(h, view, n))
        assert "Saved binding" in text(h, view)
    with sqlite3.connect(live_db) as db:
        refs = dict(db.execute("SELECT kind,ref FROM live_bindings"))
        assert db.execute("SELECT count(*) FROM live_bindings").fetchone() == (2,)
        assert db.execute("SELECT count(*) FROM live_events").fetchone() == (0,)
    assert set(refs) == {"decision", "independent_outcome"}
    restart(h)
    reopened = open_live(h, monkeypatch)
    assert all(ref in text(h, reopened) for ref in refs.values())
    assert intake.read_bytes() == before and draft.read_bytes() == original
    assert effects(h) == (0,) * 7


@pytest.mark.parametrize(
    "problem",
    [
        "grant_changed",
        "foreign_actor",
        "withdrawn",
        "injected_authority",
        "wrong_workflow",
        "dataset_rebound",
    ],
)
def test_stale_or_invalid_form_cannot_initialize_store(connected_settings, monkeypatch, problem):
    h, body = connected_settings
    view = open_live(h, monkeypatch)
    values = form(h, view)
    if problem == "grant_changed":
        body["grants"][0]["revision"] = "new-revision"
    elif problem == "dataset_rebound":
        body.update(dataset_ref="other-dataset", dataset_revision="other-version")
    elif problem == "withdrawn":
        body["state"] = "revoked"
    elif problem == "foreign_actor":
        body["actors"][ACTOR] = ["inspect"]
        body["custodian"] = OTHER
        body["actors"][OTHER] = ["custodian"]
    elif problem == "wrong_workflow":
        h.config["live_workflow_ref"] = "workflow-" + "0" * 32
        h.save()
    else:
        values["role"] = {"role": {"value": "custodian"}}
    sign(h, body)
    h.form(view, values)
    assert "Saved binding" not in text(h, view)
    assert not (h.path.parent / "live-evidence.sqlite3").exists()
    assert effects(h) == (0,) * 7


def test_separate_custodian_and_view_only_owner(connected_settings, monkeypatch):
    h, body = connected_settings
    body.update(custodian=OTHER, actors={OTHER: ["custodian"], ACTOR: ["inspect"]})
    sign(h, body)
    h.config["live_workflow_ref"] = body["workflow_ref"]
    h.save()
    owner = open_live(h, monkeypatch)
    assert "Ask the data custodian" in text(h, owner)
    assert "submit" not in screen(h, owner) and "source-v1" not in text(h, owner)
    custodian = open_live(h, monkeypatch, OTHER)
    h.form(custodian, form(h, custodian), actor=OTHER)
    assert "Saved binding" in text(h, custodian)
    assert "Committed decisions" in text(h, open_live(h, monkeypatch))


def test_revoked_during_layout_hides_choices(connected_settings, monkeypatch):
    h, body = connected_settings
    original = h.app.views.live_connections

    def revoke(*args):
        layout = original(*args)
        body["state"] = "revoked"
        sign(h, body)
        return layout

    monkeypatch.setattr(h.app.views, "live_connections", revoke)
    view = open_live(h, monkeypatch)
    assert screen(h, view).get("callback_id") != "ot_live_binding"
    assert "Authorized feed" not in text(h, view) and "live_resource" not in text(h, view)
    assert not (h.path.parent / "live-evidence.sqlite3").exists()


def test_ui_binding_passes_durable_intake_then_successor_and_revocation(
    connected_settings, monkeypatch
):
    h, body = connected_settings
    body["actors"]["reader"] = ["intake"]
    sign(h, body)
    view = open_live(h, monkeypatch)
    h.form(view, form(h, view))
    workflow = body["workflow_ref"]
    principal = IntakePrincipal(h.store.organization, "reader", "intake")
    authority = PrivateLiveAuthority(h.path.parent, principal.tenant, workflow)
    backend = DurableIntake(h.path.parent, LocalLiveIntake(authority, "reader"))
    old = backend.discover_sources(principal, limit=5, cursor=None).sources[0]
    assert old.feed_binding_ref in text(h, view)
    request = parse_intake(
        {
            "mode": "live",
            "source_revision": old.source_revision,
            "feed_binding_ref": old.feed_binding_ref,
            "idempotency_key": "live-settings",
        }
    )
    admitted = backend.submit_intake(principal, request)
    assert backend.get_run(principal, admitted.run_ref).workflow_ref == workflow
    assert backend.get_run(principal, admitted.run_ref).reason_code == "live_reader_unavailable"
    body["grants"][0]["revision"] = "successor"
    sign(h, body)
    view = open_live(h, monkeypatch)
    h.form(view, form(h, view))
    held = backend.submit_intake(principal, request)
    assert held.reason_code == "binding_superseded" and held.successor_ref in text(h, view)
    body["state"] = "revoked"
    sign(h, body)
    with pytest.raises(IntakeError):
        backend.submit_intake(principal, request)
    assert effects(h) == (0,) * 7


def test_other_actor_cannot_reuse_custodian_form(connected_settings, monkeypatch):
    h, body = connected_settings
    view = open_live(h, monkeypatch)
    h.form(view, form(h, view), actor=OTHER)
    assert not (h.path.parent / "live-evidence.sqlite3").exists()
    body["actors"].pop(OTHER)
    sign(h, body)
    h.config["live_workflow_ref"] = body["workflow_ref"]
    h.save()
    home = publish(h, monkeypatch, OTHER)
    assert "ot_app_settings" not in buttons(home)
    assert body["workflow_ref"] not in json.dumps(home)


def test_invalid_choice_and_wrong_callback_do_not_create_binding(connected_settings, monkeypatch):
    h, _ = connected_settings
    view = open_live(h, monkeypatch)
    values = form(h, view)
    values["live_resource"]["live_resource"]["selected_option"]["value"] = "resource-claim"
    h.form(view, values)
    assert not (h.path.parent / "live-evidence.sqlite3").exists()
    view = open_live(h, monkeypatch)
    values = form(h, view)
    body = h.payload(view)
    body["view"].update(callback_id="ot_tune_start", state={"values": values})
    h.send("interactive", {**body, "type": "view_submission"})
    assert not (h.path.parent / "live-evidence.sqlite3").exists() and effects(h) == (0,) * 7


def test_historical_workflow_settings_connect_twice_and_reject_neighbor(tmp_path, monkeypatch):
    from opentour_cell.pilot.training import LocalTrainingStore
    from test_live_authority import SignedCase

    case = SignedCase(tmp_path / "history", historical=True)
    assert case.workflow == "purchase-routing"
    h = Harness(tmp_path / "transport", organization="org-a")
    h.path = case.directory / "config.json"
    h.config.update(directory=str(case.directory), live_workflow_ref=case.workflow)
    h.store = LocalTrainingStore(case.directory / "training.sqlite3", "org-a", existing_only=True)
    case.body["actors"][ACTOR] = ["custodian", "inspect"]
    case.body["custodian"] = ACTOR
    case.write()
    h.save()
    restart(h)
    ledger = case.directory / "workflow-benchmarks.sqlite3"
    before = ledger.read_bytes()
    for _ in range(2):
        view = open_live(h, monkeypatch)
        h.form(view, form(h, view))
        assert "feed-" in text(h, view)
        restart(h)
    with sqlite3.connect(case.live.path) as db:
        assert db.execute("SELECT count(*) FROM live_bindings").fetchone()[0] == 1
    assert ledger.read_bytes() == before
    h.config["live_workflow_ref"] = "other-routable-workflow"
    h.save()
    assert "ot_app_settings" not in buttons(publish(h, monkeypatch))
