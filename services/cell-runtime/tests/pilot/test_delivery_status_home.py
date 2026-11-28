"""Generated private Slack reviewer opens the current worker and receiver receipts."""

from datetime import UTC, datetime

from opentour_cell.pilot.local_handoff_permissions import handoff_outbox
from opentour_cell.pilot.recipient_delivery import LocalRecipientDelivery, dispatch_local_release
from opentour_cell.pilot.release_gate import inspect_recorded_release
from test_local_handoff_permissions import KEY, signed_permission
from test_repair_bundle import write
from test_reviewer_workflow import buttons, restart, screen, text
from test_status_questions import counts


def test_private_status_refresh_reads_only_current_independent_receiver(tmp_path, monkeypatch):
    import test_recipient_delivery as fixture

    captured = []
    stage = fixture.stage

    def capture(*args, **kwargs):
        result = stage(*args, **kwargs)
        captured.append(result)
        return result

    monkeypatch.setattr(fixture, "stage", capture)
    generated, receiver, offered, selection = fixture.approved_local_offer(tmp_path)
    h, _, _, _, audit = captured[0]
    permit = generated.authority.permissions.current
    actor = h.config["reviewer"]
    directory = h.store.path.parent
    write(directory / "handoff-permission-key.json", {"key": KEY.hex()})
    grant_path = signed_permission(
        directory,
        permit,
        actors={actor: ["inspect"], "dispatcher": ["reserve", "claim", "reconcile"]},
    )
    h.config["live_workflow_ref"] = permit.workflow_ref
    h.save()

    def stamp(timestamp):
        return datetime.fromtimestamp(timestamp, UTC).isoformat()

    decision = inspect_recorded_release(
        directory,
        h.store.organization,
        permit.proposal_ref,
        permit.proposal_revision,
        permit.artifact_sha256,
    )
    assert decision and decision["decision_seal"]
    approval = f"Approved {stamp(decision['recorded_at'])}."
    scope = (
        f"Workflow {permit.workflow_ref} · Proposal {permit.proposal_ref} "
        f"revision {permit.proposal_revision} · Artifact {permit.artifact_sha256}\n"
        f"Benchmark cohort revision {audit['cohort_revision']}\n"
        f"Destination {permit.destination_ref}"
    )
    no_check = "No independent check time is recorded."
    reconcile_action = "The authorized handoff operator can reconcile delivery."

    def open_status(*, home=False):
        if home:
            from test_app_home import click as home_click
            from test_app_home import publish

            home_click(h, publish(h, monkeypatch, actor), "ot_app_prompt", actor)
            view = list(h.slack.screens)[-1]
            h.form(view, {"command": {"command": {"value": "Did it ship?"}}}, actor)
        else:
            view = h.launch(actor=actor, prompt="Did it ship?")
        assert f"Workflow {permit.workflow_ref} · Dataset" in text(h, view)
        ref = buttons(screen(h, view))["ot_result_open"]
        h.action(view, "ot_result_open", ref, actor)
        return view

    def click(view, action):
        h.slack.screens[view]["hash"] = "generated-current-modal"
        h.action(view, action, buttons(screen(h, view))[action], actor)

    def sections(modal):
        return [b["text"]["text"] for b in modal["blocks"] if b["type"] == "section"]

    def assert_timeline(view, *rows, can_refresh):
        receipt = screen(h, view)
        assert receipt["callback_id"] == "ot_handoff_receipt"
        assert sections(receipt) == [scope, approval, *rows]
        assert set(buttons(receipt)) == (
            {"ot_home", "ot_delivery_status"} if can_refresh else {"ot_home"}
        )

    view = open_status(home=True)
    assert sections(screen(h, view))[1:] == [
        "Approval recorded. Delivery is unconfirmed.",
        f"{scope} · {approval} {no_check}",
        reconcile_action,
    ]
    click(view, "ot_release_receipt")
    assert_timeline(
        view, f"Delivery is unconfirmed.\n{no_check}", reconcile_action, can_refresh=True
    )
    click(view, "ot_delivery_status")
    assert_timeline(
        view, f"Delivery is unconfirmed.\n{no_check}", reconcile_action, can_refresh=True
    )
    committed = []

    class LoseAck(LocalRecipientDelivery):
        def _receive_claimed(self, *args, **kwargs):
            committed.append(super()._receive_claimed(*args, **kwargs))
            raise TimeoutError("generated missing acknowledgment")

    outbox = handoff_outbox(h.store)
    queued = outbox.reserve(selection, "dispatcher", "selected-delivery")
    assert queued.stage == "queued" and queued.last_checked_at is None
    click(view, "ot_delivery_status")
    assert_timeline(view, f"Awaiting delivery.\n{no_check}", reconcile_action, can_refresh=True)
    assert outbox.status(selection, actor) == queued
    dispatch_local_release(outbox, selection, "dispatcher", LoseAck(receiver.grant_file), offered)
    restart(h)
    pending = outbox.status(selection, actor)
    assert pending and pending.stage == "delivery_unconfirmed"
    checked = (
        no_check
        if pending.last_checked_at is None
        else f"Last checked {stamp(pending.last_checked_at)}."
    )
    before = counts(h)
    for home in (True, False):
        view = open_status(home=home)
        click(view, "ot_release_receipt")
        click(view, "ot_delivery_status")
        assert_timeline(
            view, f"Delivery is unconfirmed.\n{checked}", reconcile_action, can_refresh=True
        )
        assert outbox.status(selection, actor) == pending
    assert counts(h) == before and len(committed) == 1
    delivered = outbox.reconcile(selection, "dispatcher", receiver)
    assert delivered.last_checked_at is not None and delivered.received_at is not None
    assert delivered.receiver_receipt_ref == committed[0]
    click(view, "ot_delivery_status")
    assert_timeline(
        view,
        f"Delivered to the selected destination.\nLast checked {stamp(delivered.last_checked_at)}.",
        f"Receiver receipt {committed[0]} at {stamp(delivered.received_at)}.",
        "The recipient can independently replay the artifact.",
        can_refresh=True,
    )
    for _ in range(2):
        assert "Check replay status" in text(h, view)
        click(view, "ot_delivery_status")
        assert "Replayed by recipient" not in text(h, view)
    assert outbox.status(selection, actor) == delivered
    assert counts(h) == before and len(committed) == 1

    # A direct question opened on the same inspected result did not select a workflow.
    view = open_status()
    previous_pin = buttons(screen(h, view))["ot_release_receipt"]
    click(view, "ot_evidence")
    revision = buttons(screen(h, view))["ot_question"]
    h.action(view, "ot_question_delivery", revision, actor)
    # The public action allowlist already rejects this legacy callback. Direct
    # callers of the shared handler must also receive only approval evidence.
    assert "Access unavailable" in text(h, view)
    h.app._questions(
        h.app.settings(), actor, view, "managed:" + permit.proposal_ref, revision, "delivery"
    )
    assert "Handoff delivery is unconfirmed" in text(h, view)
    assert permit.destination_ref not in text(h, view) and committed[0] not in text(h, view)
    assert "ot_release_receipt" not in buttons(screen(h, view))
    h.slack.screens[view]["hash"] = "generated-unscoped-modal"
    h.action(view, "ot_release_receipt", previous_pin, actor)
    assert screen(h, view).get("callback_id") != "ot_handoff_receipt"

    view = open_status()
    original = h.app.views.handoff_modal

    def revoke(*args, **kwargs):
        modal = original(*args, **kwargs)
        grant_path.unlink()
        return modal

    monkeypatch.setattr(h.app.views, "handoff_modal", revoke)
    click(view, "ot_release_receipt")
    assert committed[0] not in text(h, view) and permit.destination_ref not in text(h, view)
    assert approval not in text(h, view) and scope not in sections(screen(h, view))
    assert screen(h, view).get("callback_id") != "ot_handoff_receipt"
    assert len(committed) == 1


def test_approval_opens_scoped_receipt_and_old_button_denies_changed_destination(tmp_path):
    import time

    from opentour_cell.pilot import approval_inbox, source_intake
    from opentour_cell.pilot.release_authority import HandoffPermission
    from test_approval_inbox import callback, opt_in, stage

    h, plan, run, pointer, audit = stage(tmp_path)
    directory = h.store.path.parent
    actor = h.config["reviewer"]
    intake = source_intake.check_current_intake(
        directory, h.store.organization, h.store.engagement_id, plan["intake_revision"]
    )
    permit = HandoffPermission(
        h.store.organization,
        h.store.engagement_id,
        intake["workflow"],
        run,
        audit["run_revision"],
        audit["candidate_digest"],
        plan["audience"],
        "generated-selected-destination",
        "generated-grant",
        int(time.time()) + 300,
    )
    write(directory / "handoff-permission-key.json", {"key": KEY.hex()})
    signed_permission(directory, permit, actors={actor: ["inspect"]})
    h.config["live_workflow_ref"] = permit.workflow_ref
    h.save()
    opt_in(h)
    assert approval_inbox.flush_approvals(h.app) == 1
    h.send("interactive", callback(h, pointer, actor=actor))
    view = list(h.slack.screens)[-1]
    approve = buttons(screen(h, view))["ot_release_approve"]
    h.action(view, "ot_release_approve", approve, actor)
    assert "Approved for handoff" in text(h, view)
    pin = buttons(screen(h, view))["ot_release_receipt"]
    h.slack.screens[view]["hash"] = "generated-approval-modal"
    h.action(view, "ot_release_receipt", pin, actor)
    assert permit.destination_ref in text(h, view)
    refresh = buttons(screen(h, view))["ot_delivery_status"]
    signed_permission(
        directory, permit, actors={actor: ["inspect"]}, destination_ref="other-recipient"
    )
    h.slack.screens[view]["hash"] = "generated-receipt-modal"
    h.action(view, "ot_delivery_status", refresh, actor)
    assert permit.destination_ref not in text(h, view) and "other-recipient" not in text(h, view)
    assert screen(h, view).get("callback_id") != "ot_handoff_receipt"


def test_timeline_renders_only_applicable_post_delivery_observations():
    """Render normalized observations for generated presentation coverage."""
    from dataclasses import replace

    from opentour_cell.pilot.handoff_readback import (
        HandoffReadback,
        ReplayReadback,
        RollbackReadback,
    )
    from opentour_cell.pilot.release_outbox import ReleaseSelection, ReleaseStatus
    from opentour_cell.pilot.slack_views import handoff_modal, handoff_status

    audit = dict(
        job_id="proposal",
        run_revision="revision",
        model_revision="winner",
        cohort_revision="cohort",
    )
    selection = ReleaseSelection("workflow", "proposal", "approval")
    delivered = ReleaseStatus("release", "destination", "delivered", 1, 3, "received", 2)
    replay = ReplayReadback("replay-proof", 4, True)
    rollback = RollbackReadback("rollback-proof", 5, "test-environment", "restored")
    receipt = HandoffReadback(selection, "destination", 1, delivered, replay, rollback)

    def rows(current):
        view = handoff_modal("context", "revision", current, audit)
        actions = {
            button["action_id"]
            for block in view["blocks"]
            if block["type"] == "actions"
            for button in block["elements"]
        }
        assert actions <= {"ot_home", "ot_delivery_status"}
        return "\n".join(
            block["text"]["text"] for block in view["blocks"] if block["type"] == "section"
        )

    evaluation = rows(receipt)
    assert (
        "Replayed by recipient at 1970-01-01T00:00:04+00:00. Replay receipt replay-proof."
        in evaluation
    )
    assert "Evaluation handoff complete." in evaluation
    assert "rollback-proof" not in evaluation and "test-environment" not in evaluation

    pending = replace(receipt, replay=replace(replay, evaluation_only=False), rollback=None)
    assert handoff_status(pending, audit).finding == "Activation unconfirmed."
    assert "Active since" not in rows(pending)
    restored = replace(pending, rollback=rollback)
    assert handoff_status(restored, audit).finding == "Rollback verified."
    assert "Rollback verified at 1970-01-01T00:00:05+00:00." in rows(restored)
    assert "Environment test-environment · Restored serving version restored." in rows(restored)

    unknown = replace(receipt, delivery=replace(delivered, stage="delivery_unconfirmed"))
    assert "Delivery is unconfirmed." in rows(unknown)
    assert "replay-proof" not in rows(unknown) and "rollback-proof" not in rows(unknown)
