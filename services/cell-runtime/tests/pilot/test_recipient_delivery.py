"""Deliver the actually approved signed package into separate generated custody."""

import hashlib
import time
from dataclasses import replace

import pytest
from opentour_cell.pilot import approval_inbox, release_gate, repair_workflow, source_intake
from opentour_cell.pilot import repair_bundle as bundle
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.recipient_delivery import LocalRecipientDelivery, dispatch_local_release
from opentour_cell.pilot.release_authority import (
    HandoffPermission,
    RecordedReleaseAuthority,
    approval_ref,
)
from opentour_cell.pilot.release_outbox import ReleaseOutbox, ReleaseOutboxError, ReleaseSelection
from opentour_cell.pilot.training import LocalTrainingStore
from test_approval_inbox import callback, opt_in, stage
from test_recorded_release_authority import DestinationPermissions
from test_repair_bundle import cli, read, transfer, write
from test_reviewer_workflow import buttons, screen
from test_slack_adapter import ACTOR


def approved_local_offer(tmp_path):
    h, plan, run, pointer, audit = stage(tmp_path / "worker")
    owner = h.path.parent
    opt_in(h)
    assert approval_inbox.flush_approvals(h.app) == 1
    h.send("interactive", callback(h, pointer))
    view = list(h.slack.screens)[-1]
    h.action(view, "ot_release_approve", buttons(screen(h, view))["ot_release_approve"], ACTOR)
    receipt = release_gate.authorize_release(
        owner, h.store.organization, run, audit["run_revision"], audit["candidate_digest"]
    )
    prepared = repair_workflow.prepare_handoff(owner, h.store.organization, plan, run)
    operator = owner / "local-delivery-operator.json"
    write(
        operator,
        {
            **{
                key: prepared[key]
                for key in (
                    *bundle.BOUND,
                    "dataset_id",
                    "store_engagement",
                    "intake_revision",
                    "spec_sha256",
                    "local_assets",
                )
            },
            "role": "operator",
            "actor": plan["handoff"]["exporter"],
            "schema_version": bundle.GRANT,
            "expires_at": int(time.time()) + 600,
            "state": "active",
            "store": str(h.store.path),
        },
    )
    recipient = tmp_path / "recipient"
    recipient.mkdir(mode=0o700)
    grant_file, offered, _ = transfer(owner, recipient, prepared, operator, plan["audience"])
    receiver = LocalRecipientDelivery(grant_file)
    intake = source_intake.check_current_intake(
        owner, h.store.organization, h.store.engagement_id, plan["intake_revision"]
    )
    permission = HandoffPermission(
        h.store.organization,
        h.store.engagement_id,
        intake["workflow"],
        run,
        audit["run_revision"],
        audit["candidate_digest"],
        plan["audience"],
        receiver.destination(),
        "owner-bound-recipient",
        int(time.time()) + 180,
    )
    outbox = ReleaseOutbox(
        h.store,
        RecordedReleaseAuthority(owner, h.store.organization, DestinationPermissions(permission)),
    )
    return (
        outbox,
        receiver,
        offered,
        ReleaseSelection(intake["workflow"], run, approval_ref(receipt)),
    )


def test_lost_ack_reconciles_exact_signed_artifact_without_a_second_install(tmp_path):
    outbox, receiver, offered, selected = approved_local_offer(tmp_path)
    reserved = [outbox.reserve(selected, "dispatcher", f"equivalent-{n}") for n in range(3)]
    assert reserved == [reserved[0]] * 3
    with outbox.store._db() as db:
        release = outbox.authority.resolve(db, selected, "dispatcher", "claim")
        assert db.execute("SELECT count(*) FROM release_outbox").fetchone()[0] == 1
    assert release is not None
    assert receiver.lookup(replace(release, tenant="other-tenant"), "neighbor").state == "unknown"
    with pytest.raises(bundle.BundleError, match="recipient_release_scope_mismatch"):
        receiver.receive(replace(release, artifact_sha256="4" * 64), offered)
    receiver_root = receiver.grant_file.parent / "custody"
    assert list(receiver_root.glob("delivery-*")) == []
    committed = []

    class LoseReply(LocalRecipientDelivery):
        def receive(self, *args, **kwargs):
            committed.append(super().receive(*args, **kwargs))
            raise TimeoutError("generated transport lost the committed readback")

    delivery = LoseReply(receiver.grant_file)
    pending = dispatch_local_release(outbox, selected, "dispatcher", delivery, offered)
    assert pending.stage == "delivery_unconfirmed"
    assert len(committed) == 1
    fresh_receiver = LocalRecipientDelivery(receiver.grant_file)
    fresh_outbox = ReleaseOutbox(
        LocalTrainingStore(outbox.store.path, outbox.store.organization, existing_only=True),
        outbox.authority,
    )
    for _ in range(2):
        assert fresh_outbox.status(selected, "inspector") == pending
    assert (
        dispatch_local_release(fresh_outbox, selected, "dispatcher", delivery, offered) == pending
    )
    assert len(committed) == 1
    got = committed[0]
    delivered = fresh_outbox.reconcile(selected, "dispatcher", fresh_receiver)
    assert delivered.stage == "delivered" and delivered.receiver_receipt_ref == got
    assert fresh_receiver.receive(release, offered) == got
    (installed,) = receiver_root.glob("delivery-*/artifact.zip")
    assert (
        hashlib.sha256(installed.read_bytes()).digest()
        == hashlib.sha256(offered.read_bytes()).digest()
    )
    replay = cli(receiver.grant_file, "replay", "--archive", installed)
    assert (
        replay["fallback_rehearsed"]
        and replay["package_sha256"] == hashlib.sha256(offered.read_bytes()).hexdigest()
    )
    receipt_path = installed.with_name("receipt.json")
    genuine = read(receipt_path)
    changed = replace(release, artifact_sha256="5" * 64)
    rebound = {key: value for key, value in genuine.items() if key != "receipt_ref"}
    rebound.update(release_ref=changed.reference, artifact_sha256=changed.artifact_sha256)
    write(receipt_path, rebound | {"receipt_ref": "received-" + digest(rebound)[:48]})
    assert fresh_receiver.lookup(changed, "rebound-receipt").state == "unknown"
    write(receipt_path, genuine)
    installed.write_bytes(b"GENERATED_MUTATED_OFFER")
    assert fresh_receiver.lookup(release, "tampered-readback").state == "unknown"
    cli(receiver.grant_file, "withdraw-recipient")
    assert fresh_receiver.lookup(release, "withdrawn").state == "unknown"
    with pytest.raises(bundle.BundleError, match="recipient_custody_withdrawn"):
        fresh_receiver.receive(release, offered)


def test_permission_withdrawn_after_staging_prevents_recipient_commit(tmp_path, monkeypatch):
    outbox, receiver, offered, selected = approved_local_offer(tmp_path)
    outbox.reserve(selected, "dispatcher", "before-withdrawal")
    install = bundle._install

    def withdraw_after_staging(path, payload):
        install(path, payload)
        if path.name == "receipt.json":
            outbox.authority.permissions.current = None

    monkeypatch.setattr(bundle, "_install", withdraw_after_staging)
    with pytest.raises(ReleaseOutboxError):
        dispatch_local_release(outbox, selected, "dispatcher", receiver, offered)
    assert list((receiver.grant_file.parent / "custody").glob("delivery-*")) == []
