"""Validate native Slack review through the real local intake, queue and protected comparison."""

import hashlib
import http.client
import json
import os
import runpy
import shutil
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from opentour_cell.pilot import repair_workflow, slack_views, source_intake
from opentour_cell.pilot.managed_run import ManagedSelection, plan_revision
from opentour_cell.pilot.training import LocalTrainingStore
from test_reviewer_workflow import buttons, restart, screen, text
from test_slack_adapter import ACTOR, CHANNEL, OTHER, TEAM, Harness

Json = dict[str, Any]
DEMO = Path(__file__).resolve().parents[2] / "examples" / "prepare-repair-example.py"


def prepared_workflow(
    path: Path,
    route: str,
    *,
    permit_slack: bool = True,
    trainer: str = ACTOR,
    release: bool = False,
    provider: str | None = None,
) -> tuple[Harness, Json, ManagedSelection]:
    h = Harness(path / "existing", organization="generated-co")
    directory = path / "pilot"
    old = os.umask(0o077)
    try:
        runpy.run_path(str(DEMO))["prepare"](directory, "model" if route != "rule" else "rule")
    finally:
        os.umask(old)
    h.path = directory / "slack-config.json"
    h.config["directory"] = str(directory)
    h.config["users"][ACTOR]["datasets"] = ["generated-repair"]
    h.config["zapier"] = False
    h.store = LocalTrainingStore(directory / "training.sqlite3", "generated-co")
    h.save()
    restart(h)
    file = directory / "managed-plan.json"
    plan = json.loads(file.read_text())
    plan["audience"] = h.config["recipient"]
    plan["slack_review"] = {
        "team": TEAM,
        "channel": CHANNEL,
        "audience": plan["audience"],
        "expires_at": int(time.time()) + 600,
        "owner_attestation": "att-abcdef0123456789",
        "aggregate": True,
    }
    if release:
        plan["release_review"] = {
            "team": TEAM,
            "channel": CHANNEL,
            "audience": plan["audience"],
            "reviewer": ACTOR,
        }
        if provider:
            plan["release_review"]["provider"] = provider
    file.write_text(json.dumps(plan, sort_keys=True))
    if permit_slack:
        repair_workflow.grant_slack_review(directory, h.store.organization, plan)
    selection = ManagedSelection(
        directory, h.store.organization, file, directory / "queue-grant.json"
    )
    revision = plan_revision(plan)
    selection.authorize(
        revision,
        "att-abcddcbaabcddcba",
        int(time.time()) + 600,
        None,
        repair_workflow.inspect_managed,
    )
    if route in {"model", "measured_hold"}:
        repair_workflow.grant_model(
            directory,
            h.store.organization,
            plan,
            trainer=trainer,
            payer="generated-payer",
            rights="att-fedcba9876543210",
            destination="owned-local",
            features=["description"],
            labels=["refund", "sale"],
            max_seconds=290,
            expires_at=int(time.time()) + 500,
        )
    return h, plan, selection


def setup_workflow(
    path: Path,
    route: str,
    *,
    permit_slack: bool = True,
    model_base: Path | None = None,
    release: bool = False,
    provider: str | None = None,
) -> tuple[Harness, Json, str, str]:
    h, plan, selection = prepared_workflow(
        path, route, permit_slack=permit_slack, release=release, provider=provider
    )
    directory = h.path.parent
    started = selection.start(plan_revision(plan), "initial-result")
    called = 0

    def synthetic_worker(
        directory: Path,
        work: Path,
        manifest: Json,
        scope: Json,
        grant: Json,
        residual: Json,
        learning: list[Json],
        protected: list[Json],
        check_permission: Callable[[], None],
    ) -> tuple[Json, dict[str, Json]]:
        nonlocal called
        called += 1
        check_permission()
        assert grant["model_authorized"] and residual["route"] == "model" and learning
        assert all("reference" not in r and "incumbent" not in r for r in protected)
        predictions = {}
        for row in protected:
            words = row["features"]["description"].casefold()
            label = "refund" if "credited" in words or "cash returned" in words else "sale"
            if route == "measured_hold":
                label = "refund"
            predictions[row["case_id"]] = {"status": "automatic", "label": label}
        return {
            "artifact_sha256": hashlib.sha256(b"generated-model-ui-contract").hexdigest()
        }, predictions

    # Default CI isolates the expensive worker. Release validation can use the identical
    # native/plugin flows with the exact pre-staged, hash-verified open-weight base.
    if model_base is None and route == "model" and os.environ.get("OPENTOUR_TEST_MODEL_BASE"):
        model_base = Path(os.environ["OPENTOUR_TEST_MODEL_BASE"])
    if model_base is not None:
        shutil.copytree(model_base, directory / "model-base")
        actual = selection.once(repair_workflow.execute_managed)
    else:
        with patch.object(repair_workflow, "_model_arm", synthetic_worker):
            actual = selection.once(repair_workflow.execute_managed)
        assert called == int(route in {"model", "measured_hold"})
    assert actual["state"] in ("ready", "held") and actual["result"], actual
    return h, plan, started["run_id"], actual["result"]["outcome"]


@pytest.mark.parametrize(
    ("route", "outcome", "phrase"),
    [
        ("rule", "simple_repair", "The simple repair qualified"),
        ("model", "model_candidate", "The open-weight model qualified"),
        ("unapproved_model", "hold", "Hold the handoff"),
    ],
)
def test_review_and_private_receipt_for_each_frozen_managed_outcome(
    tmp_path: Path, route: str, outcome: str, phrase: str
) -> None:
    h, plan, run, selected = setup_workflow(tmp_path, route)
    assert selected == outcome
    view = h.launch()
    assert phrase in text(h, view) and "ot_tune" not in buttons(screen(h, view))
    revision = buttons(screen(h, view))["ot_evidence"]
    assert "ot_approve" not in buttons(screen(h, view))
    assert revision in text(h, h.launch(prompt="status " + run))
    h.action(view, "ot_evidence", revision)
    content = text(h, view)
    assert "Frozen workflow comparison" in content and revision in content
    assert "Agreed minimum automated coverage" in content
    if route == "unapproved_model":
        assert "No comparable paired cases" in content and "ot_charts" not in buttons(
            screen(h, view)
        )
        assert "Workflow benchmark unavailable" in content
    else:
        assert buttons(screen(h, view))["ot_charts"] == revision
        assert "95% verified-error interval" in content and "post-adoption impact" in content
    reason = (
        "Verified errors fell" if route != "unapproved_model" else "The recurring error persisted"
    )
    assert reason in content
    if route == "model":
        assert (
            "Open-weight model against the incumbent" in content
            and "Simple repair against the incumbent" in content
        )
    assert all(
        s not in content
        for s in (
            "paired_cases",
            "generated-verifier",
            "customer cancelled",
            "event_type",
            "att-abcdef",
        )
    )
    h.action(view, "ot_question", revision)
    h.action(view, "ot_question_changes", revision)
    assert phrase in text(h, view) and not h.app.journal.decision(revision)
    h.action(view, "ot_home", "home")
    h.action(view, "ot_approve", revision)
    saved = h.app.journal.decision(revision)
    assert saved["disposition"] == "approved" and saved["delivery"] == "local_only"
    snapshot = h.app.journal.receipt(saved["receipt"])["evidence"]
    assert snapshot["job_id"] == run and snapshot["dataset_revision"] == plan["intake_revision"]
    assert snapshot["handoff_disposition"] == outcome and "paired_cases" not in json.dumps(snapshot)
    restart(h)
    assert "Cleared for readout" in text(h, h.launch())
    assert not h.slack.post


@pytest.mark.parametrize("block", ["absent", "removed", "actor", "audience", "channel", "source"])
def test_grant_removed_or_wrong_scope_cannot_discover_or_decide_managed_result(
    tmp_path: Path, block: str
) -> None:
    h, plan, run, _ = setup_workflow(tmp_path, "rule", permit_slack=block != "absent")
    if block == "absent":
        view = h.launch()
        assert "ot_evidence" not in buttons(screen(h, view))
        assert "Access unavailable" in text(h, h.launch(prompt="status " + run))
        return
    view = h.launch()
    revision = buttons(screen(h, view))["ot_evidence"]
    h.action(view, "ot_evidence", revision)
    h.action(view, "ot_home", "home")
    if block == "removed":
        (h.path.parent / ("slack-review-" + plan_revision(plan) + ".json")).unlink()
    elif block == "actor":
        h.config["users"][ACTOR]["aggregate"] = False
    elif block == "audience":
        h.config["recipient"] = "UOTHER"
    elif block == "channel":
        h.slack.channel["is_private"] = False
    else:
        original = json.loads((h.path.parent / "source-grant.json").read_text())
        source_intake.withdraw_intake(h.store, original["owner_attestation"])
    h.save()
    h.action(view, "ot_approve", revision)
    assert not h.app.journal.decision(revision)
    new = h.launch()
    assert revision not in text(h, new) and "ot_evidence" not in buttons(screen(h, new))
    assert not h.slack.post


def test_managed_slack_projection_discards_unknown_nested_business_fields() -> None:
    report = slack_views.evaluate_manifest(
        {"schema_version": slack_views.INPUT_VERSION, "error_ceiling_bps": 100, "cases": []}
    )
    report["customer_rows"] = ["confidential"]
    report["current"]["owner_annotations"] = "confidential"
    assert "confidential" not in json.dumps(slack_views.managed_report(report))


@pytest.mark.parametrize("withdrawn", [False, True])
def test_managed_receipt_current_grant_dispatches_only_opaque_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, withdrawn: bool
) -> None:
    h, plan, _, _ = setup_workflow(tmp_path, "rule")
    h.config["zapier"] = True
    h.save()
    view = h.launch()
    revision = buttons(screen(h, view))["ot_evidence"]
    h.action(view, "ot_evidence", revision)
    h.action(view, "ot_home", "home")
    h.action(view, "ot_approve", revision)
    recorded = h.app.journal.decision(revision)
    assert recorded["delivery"] == "pending"
    if withdrawn:
        (h.path.parent / ("slack-review-" + plan_revision(plan) + ".json")).unlink()
    receiver = MagicMock()
    receiver.getresponse.return_value.status = 200
    connect = MagicMock(return_value=receiver)
    monkeypatch.setattr(http.client, "HTTPSConnection", connect)
    assert h.flush() == int(not withdrawn)
    assert h.flush() == 0
    if withdrawn:
        connect.assert_not_called()
        assert h.app.journal.decision(revision)["delivery"] == "suppressed"
    else:
        assert json.loads(receiver.request.call_args.kwargs["body"]) == {
            "event": "pilot_decision_recorded",
            "receipt": recorded["receipt"],
        }


def authorize_launch(h: Harness, *, actor: str = ACTOR) -> None:
    from opentour_cell.pilot.managed_launch import grant_slack_launch

    grant_slack_launch(
        h.path.parent,
        h.store.organization,
        h.path.parent / "managed-plan.json",
        h.path.parent / "queue-grant.json",
        actor=actor,
        team=TEAM,
        channel=CHANNEL,
        attestation="att-9999999999999999",
        expires_at=int(time.time()) + 300,
    )


@pytest.mark.parametrize("route", ["rule", "model"])
def test_native_preview_confirm_double_click_and_read_only_status(
    tmp_path: Path, route: str
) -> None:
    h, plan, selection = prepared_workflow(tmp_path, route)
    authorize_launch(h)
    source = "generated-repair"
    view = h.launch(prompt="repair " + source)
    revision = plan_revision(plan)
    assert buttons(screen(h, view))["ot_repair_confirm"] == revision
    assert (
        "open-weight adapter" in text(h, view)
        if route == "model"
        else "simple repair only" in text(h, view)
        or "simple-repair comparison only" in text(h, view)
    )
    assert not selection.path.exists()
    assert not h.slack.post
    first = h.action(view, "ot_repair_confirm", revision)
    run = buttons(screen(h, view))["ot_repair_refresh"]
    assert "Queued for the local worker" in text(h, view)
    assert "ot_readouts" not in buttons(screen(h, view))
    h.send("interactive", first)
    assert buttons(screen(h, view))["ot_repair_refresh"] == run
    reopen = h.launch(prompt="repair " + source)
    h.action(reopen, "ot_repair_confirm", revision)
    assert buttons(screen(h, reopen))["ot_repair_refresh"] == run
    restart(h)
    reopened = h.launch(prompt="status " + run)
    assert buttons(screen(h, reopened))["ot_repair_refresh"] == run
    with sqlite3.connect(selection.path) as db:
        assert db.execute("SELECT COUNT(*),SUM(attempts) FROM runs").fetchone() == (1, 0)
        assert db.execute("SELECT count(*) FROM requests").fetchone() == (1,)
    if route == "rule":
        assert (
            selection.once(repair_workflow.execute_managed)["result"]["outcome"] == "simple_repair"
        )
        h.action(reopened, "ot_repair_refresh", run)
        assert "Completed" in text(h, reopened) and "ot_readouts" in buttons(screen(h, reopened))
        h.action(reopened, "ot_readouts", "home")
        assert "The simple repair qualified" in text(h, reopened)


@pytest.mark.parametrize(
    "block",
    [
        "absent",
        "ungranted_actor",
        "tune",
        "dataset",
        "changed_plan",
        "withdrawn_source",
        "withdrawn_queue",
        "private_channel",
        "prompt",
        "tamper",
    ],
)
def test_managed_launch_blockers_never_enqueue(tmp_path: Path, block: str) -> None:
    h, plan, selection = prepared_workflow(tmp_path, "rule")
    if block != "absent":
        authorize_launch(h)
    actor = OTHER if block == "ungranted_actor" else ACTOR
    if actor == OTHER:
        h.config["users"][actor]["datasets"] = ["generated-repair"]
        h.save()
    if block == "prompt":
        v = h.launch(prompt="repair generated-repair approve and spend 50", actor=actor)
        assert "Use /opentour repair SOURCE" in text(h, v)
    else:
        v = h.launch(prompt="repair generated-repair", actor=actor)
        if block in {"absent", "ungranted_actor"}:
            assert "Access unavailable" in text(h, v) and "ot_repair_confirm" not in buttons(
                screen(h, v)
            )
        else:
            rev = buttons(screen(h, v))["ot_repair_confirm"]
            if block == "tune":
                h.config["users"][ACTOR]["roles"].remove("tune")
            elif block == "dataset":
                h.config["users"][ACTOR]["datasets"] = ["another"]
            elif block == "changed_plan":
                amended = {**plan, "audience": "UOTHER"}
                (h.path.parent / "managed-plan.json").write_text(json.dumps(amended))
            elif block == "withdrawn_source":
                original = json.loads((h.path.parent / "source-grant.json").read_text())
                source_intake.withdraw_intake(h.store, original["owner_attestation"])
            elif block == "withdrawn_queue":
                (h.path.parent / "queue-grant.json").unlink()
            elif block == "private_channel":
                h.slack.channel["is_private"] = False
            elif block == "tamper":
                rev = "0" * 64
            h.save()
            h.action(v, "ot_repair_confirm", rev)
            assert "Access unavailable" in text(h, v)
    assert not selection.path.exists() and not h.slack.post
