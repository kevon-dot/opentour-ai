"""Generated private Slack reviewer opens the current worker and receiver receipts."""

from opentour_cell.pilot.local_handoff_permissions import handoff_outbox
from opentour_cell.pilot.recipient_delivery import LocalRecipientDelivery, dispatch_local_release
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
        captured.append(result[0])
        return result

    monkeypatch.setattr(fixture, "stage", capture)
    generated, receiver, offered, selection = fixture.approved_local_offer(tmp_path)
    h = captured[0]
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
        h.action(view, action, buttons(screen(h, view))[action], actor)

    view = open_status(home=True)
    assert "Approval recorded. Delivery is unconfirmed." in text(h, view)
    click(view, "ot_release_receipt")
    assert permit.destination_ref in text(h, view)
    click(view, "ot_delivery_status")
    assert "No independent check time" in text(h, view)
    committed = []

    class LoseAck(LocalRecipientDelivery):
        def _receive_claimed(self, *args, **kwargs):
            committed.append(super()._receive_claimed(*args, **kwargs))
            raise TimeoutError("generated missing acknowledgment")

    outbox = handoff_outbox(h.store)
    outbox.reserve(selection, "dispatcher", "selected-delivery")
    dispatch_local_release(outbox, selection, "dispatcher", LoseAck(receiver.grant_file), offered)
    restart(h)
    before = counts(h)
    for _ in range(2):
        view = open_status()
        click(view, "ot_release_receipt")
        click(view, "ot_delivery_status")
        assert "Delivery is unconfirmed" in text(h, view)
    assert counts(h) == before and len(committed) == 1
    outbox.reconcile(selection, "dispatcher", receiver)
    click(view, "ot_delivery_status")
    assert "Delivered to the selected destination" in text(h, view)
    assert committed[0] in text(h, view)
    assert "ot_delivery_status" not in buttons(screen(h, view))
    assert counts(h) == before and len(committed) == 1

    view = open_status()
    original = h.app.views.handoff_modal

    def revoke(*args, **kwargs):
        modal = original(*args, **kwargs)
        grant_path.unlink()
        return modal

    monkeypatch.setattr(h.app.views, "handoff_modal", revoke)
    click(view, "ot_release_receipt")
    assert committed[0] not in text(h, view) and permit.destination_ref not in text(h, view)
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
    h.action(view, "ot_release_receipt", pin, actor)
    assert permit.destination_ref in text(h, view)
    refresh = buttons(screen(h, view))["ot_delivery_status"]
    signed_permission(
        directory, permit, actors={actor: ["inspect"]}, destination_ref="other-recipient"
    )
    h.action(view, "ot_delivery_status", refresh, actor)
    assert permit.destination_ref not in text(h, view) and "other-recipient" not in text(h, view)
    assert screen(h, view).get("callback_id") != "ot_handoff_receipt"
