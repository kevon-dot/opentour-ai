"""Commit a signed handoff under separate recipient custody and read back its digest."""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

from opentour_cell.pilot import repair_bundle as bundle
from opentour_cell.pilot.intake_mcp import require_ref
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.readout import parse_manifest
from opentour_cell.pilot.release_outbox import (
    ApprovedRelease,
    ReceiverObservation,
    ReleaseOutbox,
    ReleaseOutboxError,
    ReleaseSelection,
    ReleaseStatus,
)

SCHEMA = "opentour.pilot.recipient-delivery.v1"


def _receiver_ref(entitlement: bundle.Json, owner_key_sha256: str) -> str:
    return (
        "recipient-"
        + digest(
            [entitlement[k] for k in ("organization", "engagement", "audience")]
            + [owner_key_sha256]
        )[:48]
    )


class LocalRecipientDelivery:
    """Use the recipient's currently enrolled trust anchor and signed package entitlement.

    This boundary accepts an approved release from the authenticated sender
    transport. Recipient enrollment separately binds the destination and the
    exact package bytes. A lookup never installs, replays or activates a package.
    """

    def __init__(self, grant_file: Path, clock: Callable[[], int] = lambda: int(time.time())):
        bundle._private_directory(grant_file.parent)
        self.grant_file = grant_file
        self.clock = clock

    def destination(self) -> str:
        grant = bundle._grant(self.grant_file)
        claim = bundle._entitlement(grant)
        return _receiver_ref(claim, grant["recipient_custody"]["owner_key_sha256"])

    def _scope(self, release: ApprovedRelease, grant: bundle.Json) -> tuple[Path, bundle.Json]:
        claim = bundle._entitlement(grant)
        bundle._require(
            grant["role"] == "recipient"
            and (claim["organization"], claim["engagement"], grant["store_engagement"])
            == (release.tenant, release.engagement, release.engagement)
            and _receiver_ref(claim, grant["recipient_custody"]["owner_key_sha256"])
            == release.destination_ref
            and release.approved_at <= self.clock() < release.expires_at,
            "recipient_release_scope_mismatch",
        )
        directory = bundle._withdrawal_path(grant).parent
        slot = "delivery-" + digest([release.destination_ref, claim["spec_sha256"]])[:48]
        return directory / slot, claim

    @staticmethod
    def _binding(release: ApprovedRelease, claim: bundle.Json) -> bundle.Json:
        return {
            "schema_version": SCHEMA,
            "release_ref": release.reference,
            "tenant": release.tenant,
            "engagement": release.engagement,
            "destination_ref": release.destination_ref,
            "artifact_sha256": release.artifact_sha256,
            "package_sha256": claim["package_sha256"],
        }

    @staticmethod
    def _verify_package(
        data: bytes, release: ApprovedRelease, grant: bundle.Json, claim: bundle.Json
    ) -> None:
        bundle._require(
            bundle._sha(data) == claim["package_sha256"],
            "package_not_in_owner_entitlement",
        )
        spec, _ = bundle._unpack(data, grant)
        bundle._require(
            spec.get("release_required") is True
            and spec.get("release")
            == {
                "run_id": release.selection.proposal_ref,
                "run_revision": release.proposal_revision,
                "artifact_sha": release.artifact_sha256,
            },
            "recipient_release_scope_mismatch",
        )

    def _receipt(
        self, slot: Path, release: ApprovedRelease, grant: bundle.Json, claim: bundle.Json
    ) -> bundle.Json:
        bundle._private_directory(slot)
        receipt = parse_manifest(bundle._read(slot / "receipt.json"))
        body = self._binding(release, claim)
        when = receipt.get("received_at")
        bundle._require(
            type(when) is int and release.approved_at <= when <= self.clock() < release.expires_at,
            "recipient_delivery_receipt_mismatch",
        )
        body["received_at"] = when
        expected = body | {"receipt_ref": "received-" + digest(body)[:48]}
        bundle._require(receipt == expected, "recipient_delivery_receipt_mismatch")
        self._verify_package(
            bundle._read(slot / "artifact.zip", bundle.MAX_ARCHIVE), release, grant, claim
        )
        return receipt

    def _receive_claimed(
        self,
        release: ApprovedRelease,
        offered: Path,
        *,
        sender_current: Callable[[], AbstractContextManager[None]],
    ) -> str:
        """Install once after the actual signed bundle and live recipient grant verify."""
        bundle._require(offered.parent == self.grant_file.parent, "recipient_offer_scope_mismatch")
        with sender_current():
            grant = bundle._grant(self.grant_file)
        slot, claim = self._scope(release, grant)
        offered_bytes = bundle._read(offered, bundle.MAX_ARCHIVE)
        self._verify_package(offered_bytes, release, grant, claim)
        with bundle._current(self.grant_file, grant):
            bundle._require(self._scope(release, grant) == (slot, claim))
            if os.path.lexists(slot):
                return str(self._receipt(slot, release, grant, claim)["receipt_ref"])
            with tempfile.TemporaryDirectory(dir=slot.parent) as staged:
                staged_path = Path(staged)
                bundle._install(staged_path / "artifact.zip", offered_bytes)
                body = self._binding(release, claim) | {"received_at": self.clock()}
                receipt = body | {"receipt_ref": "received-" + digest(body)[:48]}
                bundle._install(staged_path / "receipt.json", bundle._bytes(receipt))
                with bundle._directory_lock(staged_path), sender_current():
                    bundle._require(
                        bundle._grant(self.grant_file) == grant
                        and self._scope(release, grant) == (slot, claim)
                    )
                    os.rename(staged_path, slot)
            return str(receipt["receipt_ref"])

    def lookup(self, release: ApprovedRelease, lookup_ref: str) -> ReceiverObservation:
        require_ref(lookup_ref)
        try:
            grant = bundle._grant(self.grant_file)
            with bundle._current(self.grant_file, grant):
                slot, claim = self._scope(release, grant)
                receipt = (
                    self._receipt(slot, release, grant, claim) if os.path.lexists(slot) else None
                )
                bundle._require(
                    bundle._grant(self.grant_file) == grant
                    and self._scope(release, grant) == (slot, claim)
                )
                args = (
                    release.reference,
                    release.destination_ref,
                    release.artifact_sha256,
                    self.clock(),
                    lookup_ref,
                )
                if receipt is None:
                    return ReceiverObservation("absent", *args)
                return ReceiverObservation(
                    "delivered", *args, receipt["receipt_ref"], receipt["received_at"]
                )
        except (OSError, KeyError, TypeError, ValueError):
            return ReceiverObservation(
                "unknown",
                release.reference,
                release.destination_ref,
                release.artifact_sha256,
                self.clock(),
                lookup_ref,
            )


def dispatch_local_release(
    outbox: ReleaseOutbox,
    selection: ReleaseSelection,
    actor: str,
    receiver: LocalRecipientDelivery,
    offered: Path,
) -> ReleaseStatus:
    """Consume a committed claim once. Delivery state comes from independent lookup."""
    claim = outbox.claim(selection, actor, receiver)
    if not claim.dispatch:
        return claim.status
    with outbox._read() as db:
        release = outbox._current(db, selection, actor, "claim")
        if release.reference != claim.status.reference:
            raise ReleaseOutboxError("release_intent_changed")

    @contextmanager
    def sender_current() -> Iterator[None]:
        with outbox.store._db(write=True) as db:
            outbox._check_current(db, release, actor, "claim")
            row = outbox._stage(db, release)
            if (
                row is None
                or row["stage"] != "delivery_unconfirmed"
                or row["claimed_at"] != claim.status.claimed_at
            ):
                raise ReleaseOutboxError("release_claim_is_no_longer_current")
            yield

    try:
        receiver._receive_claimed(release, offered, sender_current=sender_current)
    except OSError:
        # A lost reply can follow a durable destination write. Retain the claim.
        pass
    return claim.status
