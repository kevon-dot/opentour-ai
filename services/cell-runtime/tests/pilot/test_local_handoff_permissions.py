"""Drive the signed worker permission through the real approval and recipient route."""

import hmac
import time

import pytest
from opentour_cell.pilot import repair_bundle
from opentour_cell.pilot.local_handoff_permissions import (
    PURPOSE,
    SCHEMA,
    LocalHandoffPermissions,
    handoff_outbox,
)
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.recipient_delivery import dispatch_local_release
from opentour_cell.pilot.release_outbox import ReleaseOutboxError
from test_recipient_delivery import approved_local_offer
from test_repair_bundle import write

KEY = b"GENERATED-DESTINATION-SIGNER-KEY"


def signed_permission(directory, permit, key=KEY, **changes):
    grant = {
        "schema_version": SCHEMA,
        "purpose": PURPOSE,
        "tenant": permit.tenant,
        "engagement": permit.engagement,
        "workflow_ref": permit.workflow_ref,
        "proposal_ref": permit.proposal_ref,
        "proposal_revision": permit.proposal_revision,
        "artifact_sha256": permit.artifact_sha256,
        "recipient": permit.recipient,
        "destination_ref": permit.destination_ref,
        "grant_ref": "generated-release-grant",
        "expires_at": permit.expires_at,
        "actors": {
            "dispatcher": ["reserve", "claim", "reconcile"],
            "inspector": ["inspect"],
        },
    }
    grant.update(changes)
    seal = hmac.digest(key, canonical(grant).encode(), "sha256").hex()
    scope = digest([permit.tenant, permit.workflow_ref])
    path = directory / f"handoff-permission-{scope}.json"
    write(path, {"grant": grant, "seal_sha256": seal})
    return path


def test_worker_readback_reopens_same_signed_permission(tmp_path):
    generated, receiver, offered, selection = approved_local_offer(tmp_path)
    worker = generated.store.path.parent
    permit = generated.authority.permissions.current
    outbox = handoff_outbox(generated.store)
    with pytest.raises(ReleaseOutboxError):
        outbox.reserve(selection, "dispatcher", "trust-not-configured")
    write(worker / "handoff-permission-key.json", {"key": KEY.hex()})
    signed_permission(worker, permit)
    assert outbox.status(selection, "inspector") is None
    with pytest.raises(ReleaseOutboxError):
        outbox.reserve(selection, "inspector", "cannot-send")
    with pytest.raises(ReleaseOutboxError):
        outbox.status(selection, "dispatcher")
    reserved = [outbox.reserve(selection, "dispatcher", f"same-{n}") for n in range(3)]
    assert reserved == [reserved[0]] * 3
    pending = dispatch_local_release(outbox, selection, "dispatcher", receiver, offered)
    reopened = handoff_outbox(generated.store)
    assert pending.stage == "delivery_unconfirmed"
    assert reopened.status(selection, "inspector") == pending
    assert reopened.status(selection, "inspector") == pending
    delivered = reopened.reconcile(selection, "dispatcher", receiver)
    assert delivered.stage == "delivered" and delivered.destination_ref == receiver.destination()
    assert reopened.status(selection, "inspector") == delivered
    assert len(list(receiver.grant_file.parent.glob("custody/delivery-*/artifact.zip"))) == 1


def test_mutated_expired_wrong_scope_and_rotated_permissions_are_denied(tmp_path):
    generated, receiver, _, selection = approved_local_offer(tmp_path)
    directory = generated.store.path.parent
    permit = generated.authority.permissions.current
    key_path = directory / "handoff-permission-key.json"
    write(key_path, {"key": KEY.hex()})
    permissions = LocalHandoffPermissions(directory, permit.tenant)
    outbox = handoff_outbox(generated.store)
    path = signed_permission(directory, permit, tenant="generated-neighbor")
    assert permissions.resolve(selection.workflow_ref, "dispatcher", "claim") is None
    signed_permission(directory, permit, expires_at=int(time.time()) - 1)
    assert permissions.resolve(selection.workflow_ref, "dispatcher", "claim") is None
    signed_permission(directory, permit, recipient="UFOREIGN")
    with pytest.raises(ReleaseOutboxError):
        outbox.reserve(selection, "dispatcher", "wrong-audience")
    signed_permission(directory, permit)
    old = path.read_bytes()
    path.write_bytes(old.replace(permit.destination_ref.encode(), b"other-destination"))
    assert permissions.resolve(selection.workflow_ref, "dispatcher", "claim") is None
    write(path, old)
    write(key_path, {"key": bytes(32).hex()})
    assert permissions.resolve(selection.workflow_ref, "dispatcher", "claim") is None
    with generated.store._db() as db:
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='release_outbox'").fetchone()
            is None
        )
    assert not list(receiver.grant_file.parent.glob("custody/delivery-*/artifact.zip"))


def test_revocation_during_staging_fences_restored_permission(tmp_path, monkeypatch):
    generated, receiver, offered, selection = approved_local_offer(tmp_path)
    worker = generated.store.path.parent
    permit = generated.authority.permissions.current
    write(worker / "handoff-permission-key.json", {"key": KEY.hex()})
    path = signed_permission(worker, permit)
    previous = path.read_bytes()
    outbox = handoff_outbox(generated.store)
    outbox.reserve(selection, "dispatcher", "before-withdrawal")
    revoke = digest(
        [permit.tenant, permit.workflow_ref, permit.engagement, "generated-release-grant"]
    )
    install = repair_bundle._install

    def revocation_at_receipt(staged, payload):
        install(staged, payload)
        if staged.name == "receipt.json":
            write(worker / f"handoff-permission-revoked-{revoke}.json", {})
            path.unlink()
            write(path, previous)

    monkeypatch.setattr(repair_bundle, "_install", revocation_at_receipt)
    with pytest.raises(ReleaseOutboxError):
        dispatch_local_release(outbox, selection, "dispatcher", receiver, offered)
    assert not list(receiver.grant_file.parent.glob("custody/delivery-*/artifact.zip"))
    with pytest.raises(ReleaseOutboxError):
        handoff_outbox(generated.store).status(selection, "inspector")
