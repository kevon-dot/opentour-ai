"""Reopen the private handoff state using only the authorized worker read path."""

from opentour_cell.pilot import handoff_readback
from opentour_cell.pilot.local_handoff_permissions import handoff_outbox
from opentour_cell.pilot.recipient_delivery import LocalRecipientDelivery, dispatch_local_release
from opentour_cell.pilot.training import LocalTrainingStore
from test_local_handoff_permissions import KEY, signed_permission
from test_recipient_delivery import approved_local_offer
from test_repair_bundle import write


def test_selected_readback_recovers_the_same_handoff_without_sending(tmp_path, monkeypatch):
    generated, receiver, offered, selection = approved_local_offer(tmp_path)
    store = generated.store
    permit = generated.authority.permissions.current
    directory = store.path.parent
    write(directory / "handoff-permission-key.json", {"key": KEY.hex()})
    grant_path = signed_permission(directory, permit)
    released = {
        "actor": "inspector",
        "workflow_ref": permit.workflow_ref,
        "proposal_ref": permit.proposal_ref,
        "proposal_revision": permit.proposal_revision,
        "artifact_sha256": permit.artifact_sha256,
    }

    def unexpected_read(*args, **kwargs):
        raise AssertionError("a wrong actor or version reached the review receipt")

    with monkeypatch.context() as denied:
        denied.setattr(handoff_readback.release_gate, "inspect_recorded_release", unexpected_read)
        for changed in (
            {"actor": "dispatcher"},
            {"workflow_ref": "different-workflow"},
            {"proposal_ref": "different-proposal"},
            {"proposal_revision": "1" * 64},
            {"artifact_sha256": "2" * 64},
        ):
            assert handoff_readback.read_handoff(store, **(released | changed)) is None

    approved = handoff_readback.read_handoff(store, **released)
    assert approved is not None
    assert approved.selection == selection and approved.delivery is None
    assert approved.destination_ref == permit.destination_ref
    with store._db() as db:
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='release_outbox'").fetchone()
            is None
        )

    outbox = handoff_outbox(store)
    outbox.reserve(selection, "dispatcher", "exact-authorized-delivery")
    effects = []

    class DelayedAck(LocalRecipientDelivery):
        def _receive_claimed(self, *args, **kwargs):
            effects.append(super()._receive_claimed(*args, **kwargs))
            raise TimeoutError("generated readback lost after the recipient wrote")

    dispatch_local_release(
        outbox, selection, "dispatcher", DelayedAck(receiver.grant_file), offered
    )
    pending = handoff_readback.read_handoff(store, **released)
    assert pending is not None and pending.delivery.stage == "delivery_unconfirmed"
    fresh_store = LocalTrainingStore(store.path, store.organization, existing_only=True)
    assert handoff_readback.read_handoff(fresh_store, **released) == pending
    assert handoff_readback.read_handoff(fresh_store, **released) == pending
    assert len(effects) == 1
    independently_confirmed = handoff_outbox(fresh_store).reconcile(
        selection, "dispatcher", LocalRecipientDelivery(receiver.grant_file)
    )
    assert independently_confirmed.stage == "delivered"
    observed = handoff_readback.read_handoff(fresh_store, **released)
    assert observed is not None and observed.delivery == independently_confirmed
    assert observed.delivery.receiver_receipt_ref == effects[0]
    assert observed.delivery.last_checked_at is not None
    assert observed.delivery.received_at is not None
    assert observed.approved_at == approved.approved_at
    assert len(effects) == 1

    status = handoff_readback.ReleaseOutbox.status

    def withdraw_after_status(*args, **kwargs):
        result = status(*args, **kwargs)
        grant_path.unlink()
        return result

    monkeypatch.setattr(handoff_readback.ReleaseOutbox, "status", withdraw_after_status)
    assert handoff_readback.read_handoff(fresh_store, **released) is None
    assert handoff_readback.read_handoff(fresh_store, **released) is None
    assert len(effects) == 1
