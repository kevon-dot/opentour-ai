"""Commit a signed handoff under separate recipient custody and read back its digest."""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from zipfile import BadZipFile

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
MAX_RECEIPT = 4096


def _receiver_ref(entitlement: bundle.Json, owner_key_sha256: str) -> str:
    return (
        "recipient-"
        + digest(
            [entitlement[k] for k in ("organization", "engagement", "audience")]
            + [owner_key_sha256]
        )[:48]
    )


def _verify_receipt_signature(certificate: bytes, signature: bytes, receipt: bundle.Json) -> None:
    public = bundle._crypto(
        ["x509", "-pubkey", "-noout"], certificate, "recipient_signer_unavailable"
    )
    with tempfile.TemporaryDirectory() as private:
        key, sealed = Path(private) / "public", Path(private) / "signature"
        bundle._write_private(key, public)
        bundle._write_private(sealed, signature)
        bundle._crypto(
            ["dgst", "-sha256", "-verify", str(key), "-signature", str(sealed)],
            bundle._bytes(receipt),
            "invalid_recipient_delivery_signature",
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
    ) -> bytes:
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
        certificate = bundle._read(Path(grant["recipient_certificate"]))
        sealed_certificates = {
            asset["certificate_sha256"]
            for asset in spec["assets"].values()
            if asset["mode"] == "sealed"
        }
        bundle._require(
            sealed_certificates == {bundle._sha(certificate)}, "recipient_certificate_mismatch"
        )
        return certificate

    @staticmethod
    def _sign_receipt(receipt: bundle.Json, grant: bundle.Json, certificate: bytes) -> str:
        with tempfile.TemporaryDirectory() as private:
            key = Path(private) / "recipient-key"
            bundle._write_private(key, bundle._read(Path(grant["recipient_key"])))
            signature = bundle._crypto(
                ["dgst", "-sha256", "-sign", str(key), "-passin", "pass:"],
                bundle._bytes(receipt),
                "recipient_receipt_signing_failed",
            )
        _verify_receipt_signature(certificate, signature, receipt)
        return base64.b64encode(signature).decode("ascii")

    def _receipt(
        self, slot: Path, release: ApprovedRelease, grant: bundle.Json, claim: bundle.Json
    ) -> bundle.Json:
        bundle._private_directory(slot)
        receipt = parse_manifest(bundle._read(slot / "receipt.json", MAX_RECEIPT))
        encoded_signature = receipt.pop("receipt_signature", None)
        bundle._require(isinstance(encoded_signature, str), "recipient_delivery_receipt_mismatch")
        assert isinstance(encoded_signature, str)
        body = self._binding(release, claim)
        when = receipt.get("received_at")
        bundle._require(
            type(when) is int and release.approved_at <= when <= self.clock() < release.expires_at,
            "recipient_delivery_receipt_mismatch",
        )
        body["received_at"] = when
        expected = body | {"receipt_ref": "received-" + digest(body)[:48]}
        bundle._require(receipt == expected, "recipient_delivery_receipt_mismatch")
        certificate = self._verify_package(
            bundle._read(slot / "artifact.zip", bundle.MAX_ARCHIVE), release, grant, claim
        )
        _verify_receipt_signature(
            certificate, base64.b64decode(encoded_signature, validate=True), receipt
        )
        return receipt

    def _receive_claimed(
        self,
        release: ApprovedRelease,
        offered: Path,
        *,
        sender_current: Callable[[], AbstractContextManager[Callable[[], None]]],
    ) -> str:
        """Install once after the actual signed bundle and live recipient grant verify."""
        bundle._require(offered.parent == self.grant_file.parent, "recipient_offer_scope_mismatch")
        grant = bundle._grant(self.grant_file)
        slot, claim = self._scope(release, grant)
        with bundle._current(self.grant_file, grant), sender_current() as check_sender:
            bundle._require(self._scope(release, grant) == (slot, claim))
            check_sender()
            if os.path.lexists(slot):
                return str(self._receipt(slot, release, grant, claim)["receipt_ref"])
            offered_bytes = bundle._read(offered, bundle.MAX_ARCHIVE)
            certificate = self._verify_package(offered_bytes, release, grant, claim)
            check_sender()
            with tempfile.TemporaryDirectory(dir=slot.parent) as staged:
                staged_path = Path(staged)
                bundle._install(staged_path / "artifact.zip", offered_bytes)
                body = self._binding(release, claim) | {"received_at": self.clock()}
                receipt = body | {"receipt_ref": "received-" + digest(body)[:48]}
                signature = self._sign_receipt(receipt, grant, certificate)
                bundle._install(
                    staged_path / "receipt.json",
                    bundle._bytes(receipt | {"receipt_signature": signature}),
                )
                with bundle._directory_lock(staged_path):
                    bundle._require(
                        bundle._grant(self.grant_file) == grant
                        and self._scope(release, grant) == (slot, claim)
                    )
                    check_sender()
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
        except (BadZipFile, OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError):
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
    def sender_current() -> Iterator[Callable[[], None]]:
        with outbox.store._db(write=True) as db:

            def check_claim() -> None:
                outbox._check_current(db, release, actor, "claim")
                row = outbox._stage(db, release)
                if (
                    row is None
                    or row["stage"] != "delivery_unconfirmed"
                    or row["claimed_at"] != claim.status.claimed_at
                ):
                    raise ReleaseOutboxError("release_claim_is_no_longer_current")

            yield check_claim

    try:
        receiver._receive_claimed(release, offered, sender_current=sender_current)
    except OSError:
        # A lost reply can follow a durable destination write. Retain the claim.
        pass
    return claim.status
