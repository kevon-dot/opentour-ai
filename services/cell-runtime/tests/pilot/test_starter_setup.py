"""OPE-536 generated founder interactions against the real local starter authority and intake."""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import starter_setup, workplace_context
from opentour_cell.pilot.intake_local import STARTER_SELECTION_VERSION
from opentour_cell.pilot.learning_mandate import LearningMandates
from opentour_cell.pilot.operating_point import digest
from test_app_home import click, publish
from test_intake_mcp import STARTER_FIXTURE_KEY, _sealed_starter, _write_private
from test_reviewer_workflow import buttons, restart, screen, text
from test_slack_adapter import ACTOR, OTHER, Harness


@pytest.fixture
def founder(tmp_path: Path) -> Harness:
    h = Harness(tmp_path)
    directory = h.path.parent
    h.config.pop("recipient")
    h.config["reviewer"] = OTHER
    h.config["users"][ACTOR].update(roles=["inspect"], datasets=[], aggregate=False, preview=False)
    h.config["starter_setup"] = {
        "slack_owner": ACTOR,
        "owner_ref": "workflow-owner",
        "selection_file": "selected.json",
        "charter_file": "charter.json",
    }
    h.save()
    select = {
        "schema_version": STARTER_SELECTION_VERSION,
        "tenant": h.store.organization,
        "actor": "intake-worker",
        "engagement_epoch": h.store.engagement_id,
        "owner_id": "workflow-owner",
        "grant_ref": "policy-scope",
    }
    charter = {
        "schema_version": "opentour.pilot.starter-charter.v1",
        "intended_decision": "Route a generated invoice",
        "local_purpose": "Local workflow draft",
        "verification_plan": "Record an independent correction from the reviewer",
        "responsible_person": "verification-owner",
        "next_check": "2099-01-01",
        "verifier": None,
    }
    LearningMandates(directory, h.store.organization, h.store.engagement_id, owner="workflow-owner")
    _write_private(directory / "starter-intake-key.json", {"key": STARTER_FIXTURE_KEY})
    _write_private(directory / "selected.json", select)
    _write_private(directory / "charter.json", charter)
    _write_private(
        directory / "starter-intake-grant-policy-scope.json",
        _sealed_starter(select, setup_policy_revision=digest(charter)),
    )
    return h


def open_starter(h: Harness, monkeypatch: pytest.MonkeyPatch) -> str:
    home = publish(h, monkeypatch)
    assert "ot_app_add_workflow" in buttons(home)
    click(h, home, "ot_app_add_workflow")
    view = list(h.slack.screens)[-1]
    choice = buttons(screen(h, view))["ot_workflow_starter"]
    h.action(view, "ot_workflow_starter", choice)
    return view


def effects(h: Harness) -> tuple[int, ...]:
    with h.app.journal.db() as db:
        effects = [
            db.execute("SELECT count(*) FROM " + t).fetchone()[0]
            for t in ("proposals", "decisions", "approval_posts")
        ]
    with h.store._db() as db:
        effects.append(db.execute("SELECT count(*) FROM jobs").fetchone()[0])
    return (
        *effects,
        len(h.slack.post),
        len(list(h.path.parent.glob("workplace-snapshots/*"))),
        len(list(h.path.parent.glob("managed-outputs/*/release-decision.json"))),
    )


def test_save_resume_and_crash_after_admission(
    founder: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = founder
    reads = []

    def unexpected_source_read(*args: Any, **kwargs: Any) -> None:
        reads.append(True)
        raise AssertionError("Starter setup invoked a workplace reader.")

    for name in ("discover_workplace", "acquire_workplace"):
        monkeypatch.setattr(workplace_context, name, unexpected_source_read)
    view = open_starter(h, monkeypatch)
    assert "verifier pending" in text(h, view) and "policy-custodian" in text(h, view)
    assert not (h.path.parent / "intake.sqlite3").exists()
    save = buttons(screen(h, view))["ot_starter_save"]
    with monkeypatch.context() as m:

        def interrupted(*args: Any) -> None:
            raise OSError("simulated power loss before draft receipt")

        m.setattr(starter_setup, "_record", interrupted)
        h.action(view, "ot_starter_save", save)
    assert not list(h.path.parent.glob("source-intake-starter-draft-*.json"))
    intake = h.path.parent / "intake.sqlite3"
    with sqlite3.connect(intake) as db:
        admitted = db.execute("SELECT workflow_ref,run_ref FROM intake_runs").fetchall()
    assert len(admitted) == 1
    restart(h)
    view = open_starter(h, monkeypatch)
    action = h.action(view, "ot_starter_save", buttons(screen(h, view))["ot_starter_save"])
    assert screen(h, view)["callback_id"] == "ot_starter_saved"
    document = text(h, view)
    assert all(ref in document for ref in admitted[0])
    h.send("interactive", action)
    restart(h)
    resumed = open_starter(h, monkeypatch)
    assert screen(h, resumed)["callback_id"] == "ot_starter_saved"
    assert all(ref in text(h, resumed) for ref in admitted[0])
    receipts = list(h.path.parent.glob("source-intake-starter-draft-*.json"))
    assert len(receipts) == 1
    draft = json.loads(receipts[0].read_bytes())
    assert all(draft[key] in text(h, resumed) for key in ("draft_ref", "settings_revision"))
    with sqlite3.connect(intake) as db:
        assert db.execute("SELECT count(*) FROM intake_workflows").fetchone()[0] == 1
        assert db.execute("SELECT workflow_ref,run_ref FROM intake_runs").fetchall() == admitted
        assert db.execute("SELECT count(*) FROM intake_keys").fetchone()[0] == 2
    assert reads == [] and effects(h) == (0,) * 7


@pytest.mark.parametrize("problem", ["withdrawn", "foreign_owner", "foreign_policy", "fit_input"])
def test_stale_and_unentitled_starter_submission(
    founder: Harness, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    h = founder
    view = open_starter(h, monkeypatch)
    save = buttons(screen(h, view))["ot_starter_save"]
    if problem == "withdrawn":
        (h.path.parent / "starter-intake-withdrawn-policy-scope.json").touch(mode=0o600)
    elif problem == "foreign_owner":
        h.config["starter_setup"]["slack_owner"] = OTHER
        h.save()
    elif problem == "foreign_policy":
        charter_path = h.path.parent / "charter.json"
        charter = json.loads(charter_path.read_bytes())
        _write_private(charter_path, {**charter, "local_purpose": "foreign-purpose-sentinel"})
    if problem == "fit_input":
        body = h.payload(view)
        body.update(type="block_actions", actions=[{"action_id": "ot_starter_save", "value": save}])
        body["view"]["state"] = {"values": {"fit_budget": {"fit_budget": {"value": "100"}}}}
        h.send("interactive", body)
    else:
        h.action(view, "ot_starter_save", save)
    assert screen(h, view).get("callback_id") != "ot_starter_saved"
    assert "foreign-purpose-sentinel" not in text(h, view)
    assert not (h.path.parent / "intake.sqlite3").exists()
    assert not list(h.path.parent.glob("source-intake-starter-draft-*.json"))
    assert effects(h) == (0,) * 7


def test_revoked_charter_is_never_published_at_disclosure_admission(
    founder: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = founder
    original = h.app.views.starter_charter
    rendered = []

    def revoke_after_layout(*args: Any) -> dict[str, Any]:
        layout = original(*args)
        rendered.append(layout)
        (h.path.parent / "starter-intake-withdrawn-policy-scope.json").touch(mode=0o600)
        return layout

    monkeypatch.setattr(h.app.views, "starter_charter", revoke_after_layout)
    view = open_starter(h, monkeypatch)
    assert len(rendered) == 1 and "verification-owner" in json.dumps(rendered[0])
    assert "verification-owner" not in text(h, view)
    assert "ot_starter_save" not in buttons(screen(h, view))
    assert not (h.path.parent / "intake.sqlite3").exists()


def test_saved_charter_preserves_custodian_scope_and_holds_after_withdrawal(
    founder: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = founder
    view = open_starter(h, monkeypatch)
    h.action(view, "ot_starter_save", buttons(screen(h, view))["ot_starter_save"])
    doc = json.loads(next(h.path.parent.glob("source-intake-starter-draft-*.json")).read_bytes())
    assert doc["mandate"]["purpose"] == "starter_policy_draft"
    assert doc["mandate"]["owner_ref"] == "workflow-owner"
    assert doc["mandate"]["custodian_ref"] == "policy-custodian"
    assert doc["mandate"]["charter"]["verifier"] is None
    assert doc["mandate"]["charter"]["responsible_person"] == "verification-owner"
    (h.path.parent / "starter-intake-withdrawn-policy-scope.json").touch(mode=0o600)
    restart(h)
    held = open_starter(h, monkeypatch)
    assert doc["workflow_ref"] not in text(h, held) and doc["draft_ref"] not in text(h, held)
    assert effects(h) == (0,) * 7


def test_saved_draft_requires_matching_durable_intake_after_restore(
    founder: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = founder
    view = open_starter(h, monkeypatch)
    h.action(view, "ot_starter_save", buttons(screen(h, view))["ot_starter_save"])
    draft = json.loads(next(h.path.parent.glob("source-intake-starter-draft-*.json")).read_bytes())
    intake = h.path.parent / "intake.sqlite3"
    intake.rename(h.path.parent / "held-intake.sqlite3")
    restart(h)
    after_restore = open_starter(h, monkeypatch)
    assert draft["workflow_ref"] not in text(h, after_restore)
    assert not intake.exists() and effects(h) == (0,) * 7


def test_two_saved_views_reuse_the_same_settings_and_draft(
    founder: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    h = founder
    views = [open_starter(h, monkeypatch), open_starter(h, monkeypatch)]
    saves = [buttons(screen(h, v))["ot_starter_save"] for v in views]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(h.action, v, "ot_starter_save", s)
            for v, s in zip(views, saves, strict=True)
        ]
        for future in futures:
            future.result(timeout=15)
    assert all(screen(h, v)["callback_id"] == "ot_starter_saved" for v in views)
    assert len(list(h.path.parent.glob("source-intake-starter-draft-*.json"))) == 1
    with sqlite3.connect(h.path.parent / "intake.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM intake_workflows").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM intake_runs").fetchone()[0] == 1
    assert effects(h) == (0,) * 7


@pytest.mark.parametrize(
    "change", ["extra_training", "missing_plan", "unsigned", "unregistered_owner"]
)
def test_starter_preview_requires_current_complete_approved_charter(
    founder: Harness, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    h = founder
    directory = h.path.parent
    charter = json.loads((directory / "charter.json").read_bytes())
    selection = json.loads((directory / "selected.json").read_bytes())
    if change == "extra_training":
        charter["max_fits"] = 2
    elif change == "missing_plan":
        charter["verification_plan"] = ""
    elif change == "unregistered_owner":
        selection["owner_id"] = "unapproved-owner"
        h.config["starter_setup"]["owner_ref"] = "unapproved-owner"
        h.save()
    seal = _sealed_starter(selection, setup_policy_revision=digest(charter))
    if change == "unsigned":
        seal["seal_sha256"] = "0" * 64
    _write_private(directory / "charter.json", charter)
    _write_private(directory / "selected.json", selection)
    _write_private(directory / "starter-intake-grant-policy-scope.json", seal)
    denied = open_starter(h, monkeypatch)
    assert "verification-owner" not in text(h, denied)
    assert "ot_starter_save" not in buttons(screen(h, denied))
    assert not (directory / "intake.sqlite3").exists()
    assert effects(h) == (0,) * 7
