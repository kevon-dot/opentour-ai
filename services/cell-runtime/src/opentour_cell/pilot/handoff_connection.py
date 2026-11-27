"""Owner-scoped handoff authority and recipient-signed observations."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import BadZipFile

from opentour_cell.pilot import repair_bundle as bundle
from opentour_cell.pilot.intake_mcp import require_ref
from opentour_cell.pilot.local_handoff_permissions import LocalHandoffPermissions, handoff_outbox
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.readout import parse_manifest
from opentour_cell.pilot.recipient_delivery import (
    MAX_RECEIPT,
    LocalRecipientDelivery,
    _verify_receipt_signature,
)
from opentour_cell.pilot.release_outbox import (
    Action,
    ApprovedRelease,
    ReceiverObservation,
    ReleaseSelection,
    ReleaseStatus,
)
from opentour_cell.pilot.training import LocalTrainingStore

CONNECTION = "opentour.pilot.handoff-connection.v1"
DISPATCH = "opentour.pilot.handoff-dispatch.v1"
READBACK = "opentour.pilot.handoff-readback.v1"
ERROR = "private_handoff_unavailable"
LEASE_SECONDS = 20
RELEASE = re.compile(r"release-[0-9a-f]{48}")
LOOKUP = re.compile(r"lookup-[0-9a-f]{32}")
TICKET = re.compile(r"[0-9a-f]{64}")
TOKEN = re.compile(r"[A-Za-z0-9_-]{43,128}")
IDENTITIES = (
    "tenant",
    "engagement",
    "audience",
    "dispatcher_actor",
    "recipient",
    "destination_ref",
    "connection_ref",
)
FINGERPRINTS = ("dispatcher_token_sha256", "recipient_tls_sha256", "recipient_signer_sha256")
FIELDS = {"schema_version", "origin", "expires_at", *IDENTITIES, *FINGERPRINTS}
FAILURES = (
    BadZipFile,
    KeyError,
    OSError,
    TypeError,
    ValueError,
    sqlite3.Error,
    subprocess.SubprocessError,
)
Json = bundle.Json


def require(valid: object) -> None:
    if not valid:
        raise ValueError(ERROR)


def match(value: object, pattern: re.Pattern[str]) -> str:
    require(isinstance(value, str) and pattern.fullmatch(value))
    assert isinstance(value, str)
    return value


def _revocation(path: Path, grant: Json) -> Path:
    scope = digest([grant[k] for k in ("tenant", "engagement", "connection_ref")])
    return path.parent / f"handoff-connection-revoked-{scope}.json"


def load_connection(path: Path, now: int, *, for_revocation: bool = False) -> Json:
    envelope = _read_private(path, 8192)
    require(set(envelope) == {"grant", "seal_sha256"})
    grant = envelope["grant"]
    require(isinstance(grant, dict) and set(grant) == FIELDS)
    assert isinstance(grant, dict)
    require(grant["schema_version"] == CONNECTION)
    for field in IDENTITIES:
        require_ref(grant[field])
    for field in FINGERPRINTS:
        match(grant[field], bundle.SHA)
    expires_at = grant["expires_at"]
    require(type(expires_at) is int and 0 < expires_at < 2**63)
    origin = grant["origin"]
    require(isinstance(origin, str) and len(origin) <= 240)
    url = urlsplit(origin)
    require(
        url.scheme == "https"
        and url.hostname is not None
        and re.fullmatch(r"[a-z0-9][a-z0-9.-]*", url.hostname)
        and (url.port is None or url.port > 0)
        and origin
        == f"https://{url.hostname}" + (f":{url.port}" if url.port not in (None, 443) else "")
    )
    permissions = LocalHandoffPermissions(path.parent, grant["tenant"])
    expected = hmac.digest(permissions._key(), canonical(grant).encode(), "sha256").hex()
    require(hmac.compare_digest(match(envelope["seal_sha256"], bundle.SHA), expected))
    if not for_revocation:
        require(now < expires_at and not os.path.lexists(_revocation(path, grant)))
    return grant


def revoke_connection(path: Path, now: int) -> None:
    """Serialize a local owner's connection withdrawal with custody publication."""
    with bundle._directory_lock(path.parent):
        grant = load_connection(path, now, for_revocation=True)
        bundle._install(_revocation(path, grant), bundle._bytes({"state": "revoked"}))


@dataclass(frozen=True)
class Offer:
    connection: Json
    release: ApprovedRelease
    claimed_at: int
    package_sha256: str
    expires_at: int


class HandoffConnection:
    """Use the worker's committed ledger and the recipient's independently enrolled custody."""

    def __init__(
        self,
        config: Path,
        recipient_grant: Path,
        clock: Callable[[], int],
    ):
        bundle._private_directory(config.parent)
        self.config = config
        self.clock = clock
        self.receiver = LocalRecipientDelivery(recipient_grant, clock)
        home = config.parent
        destination = recipient_grant.parent
        require(not destination.is_relative_to(home) and not home.is_relative_to(destination))
        self.installation = self.connection()
        self.outbox = handoff_outbox(
            LocalTrainingStore(
                home / "training.sqlite3", self.installation["tenant"], existing_only=True
            ),
            clock,
        )
        require(self.outbox.store.engagement_id == self.installation["engagement"])
        self._offers: dict[str, Offer] = {}
        self._offers_lock = threading.Lock()

    def connection(self) -> Json:
        return load_connection(self.config, self.clock())

    def check_connection(self, initial: Json) -> None:
        require(self.connection() == initial)
        for field in ("tenant", "engagement", "origin", "audience"):
            require(initial[field] == self.installation[field])

    def dispatcher(self, conn: Json, header: str, audience: str) -> None:
        token = match(header.removeprefix("Bearer "), TOKEN)
        require(
            header == f"Bearer {token}"
            and audience == conn["audience"]
            and hmac.compare_digest(
                hashlib.sha256(token.encode()).hexdigest(), conn["dispatcher_token_sha256"]
            )
        )

    def recipient_peer(self, conn: Json, certificate: bytes | None) -> None:
        require(
            certificate
            and hmac.compare_digest(bundle._sha(certificate), conn["recipient_tls_sha256"])
        )
        assert certificate is not None
        grant = bundle._grant(self.receiver.grant_file)
        custody_certificate = bundle._read(Path(grant["recipient_certificate"]))
        require(bundle._sha(custody_certificate) == conn["recipient_signer_sha256"])
        client_key = bundle._crypto(
            ["x509", "-inform", "DER", "-pubkey", "-noout"], certificate, ERROR
        )
        custody_key = bundle._crypto(["x509", "-pubkey", "-noout"], custody_certificate, ERROR)
        require(not hmac.compare_digest(client_key, custody_key))

    def _selection(self, conn: Json, ref: str) -> ReleaseSelection:
        with self.outbox._read() as db:
            row = db.execute(
                "SELECT binding FROM release_outbox WHERE ref=? AND engagement=?",
                (ref, conn["engagement"]),
            ).fetchone()
            require(row is not None)
            assert row is not None
            selected = parse_manifest(row["binding"].encode())["selection"]
            return ReleaseSelection(**selected)

    def _resolve(
        self,
        db: sqlite3.Connection,
        conn: Json,
        selection: ReleaseSelection,
        ref: str,
        action: Action,
    ) -> ApprovedRelease:
        release = self.outbox._current(db, selection, conn["dispatcher_actor"], action)
        require(
            (release.reference, release.tenant, release.engagement, release.destination_ref)
            == (ref, conn["tenant"], conn["engagement"], conn["destination_ref"])
        )
        self.outbox._check_current(db, release, conn["dispatcher_actor"], action)
        self.check_connection(conn)
        return release

    def _recipient_scope(
        self, conn: Json, release: ApprovedRelease, grant: Json
    ) -> tuple[Path, Json, bytes]:
        slot, entitlement = self.receiver._scope(release, grant)
        certificate = bundle._read(Path(grant["recipient_certificate"]))
        require(
            entitlement["audience"] == conn["recipient"]
            and bundle._sha(certificate) == conn["recipient_signer_sha256"]
        )
        return slot, entitlement, certificate

    def _check_claim(self, db: sqlite3.Connection, offer: Offer) -> None:
        conn, release = offer.connection, offer.release
        require(self.clock() < offer.expires_at)
        require(self._resolve(db, conn, release.selection, release.reference, "claim") == release)
        row = self.outbox._stage(db, release)
        require(
            row is not None
            and row["stage"] == "delivery_unconfirmed"
            and row["claimed_at"] == offer.claimed_at
            and self.clock() < offer.expires_at
        )

    @contextmanager
    def _sender_current(self, offer: Offer) -> Iterator[Callable[[], None]]:
        with bundle._directory_lock(self.config.parent), self.outbox.store._db(write=True) as db:
            yield lambda: self._check_claim(db, offer)

    def _issue_offer(
        self, conn: Json, selection: ReleaseSelection, status: ReleaseStatus
    ) -> tuple[str, Offer]:
        require(status.claimed_at is not None)
        assert status.claimed_at is not None
        with self.outbox._read() as db:
            release = self._resolve(db, conn, selection, status.reference, "claim")
        grant = bundle._grant(self.receiver.grant_file)
        _, entitlement, _ = self._recipient_scope(conn, release, grant)
        expires_at = min(
            self.clock() + LEASE_SECONDS,
            conn["expires_at"],
            release.expires_at,
            grant["expires_at"],
            entitlement["expires_at"],
        )
        require(self.clock() < expires_at)
        offer = Offer(conn, release, status.claimed_at, entitlement["package_sha256"], expires_at)
        ref = secrets.token_hex(32)
        with self._offers_lock:
            require(len(self._offers) < 64)
            self._offers[ref] = offer
        return ref, offer

    def fetch(self, conn: Json, ticket: str) -> tuple[bytes, Callable[[], None]]:
        with self._offers_lock:
            offer = self._offers.get(ticket)
            require(offer is not None and offer.connection == conn)
            assert offer is not None
            del self._offers[ticket]
        release = offer.release
        grant = bundle._grant(self.receiver.grant_file)
        with bundle._current(self.receiver.grant_file, grant):
            _, entitlement, _ = self._recipient_scope(conn, release, grant)
            require(entitlement["package_sha256"] == offer.package_sha256)
            payload = bundle._read(
                self.config.parent / f"handoff-package-{release.reference}.zip",
                bundle.MAX_ARCHIVE,
            )
            self.receiver._verify_package(payload, release, grant, entitlement)
            spec, _ = bundle._unpack(payload, grant)
            require(all(asset["mode"] != "included" for asset in spec["assets"].values()))

        def before_send() -> None:
            with (
                bundle._current(self.receiver.grant_file, grant),
                bundle._directory_lock(self.config.parent),
            ):
                with self.outbox._read() as db:
                    self._check_claim(db, offer)
                require(bundle._grant(self.receiver.grant_file) == grant)
            require(self.clock() < offer.expires_at)

        return payload, before_send

    def _deliver(self, offer: Offer, payload: bytes) -> None:
        require(bundle._sha(payload) == offer.package_sha256 and self.clock() < offer.expires_at)
        with tempfile.NamedTemporaryFile(
            dir=self.receiver.grant_file.parent, prefix=".handoff-"
        ) as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
            self.receiver._receive_claimed(
                offer.release,
                Path(file.name),
                sender_current=lambda: self._sender_current(offer),
            )

    def dispatch(
        self,
        conn: Json,
        ref: str,
        request: Callable[[Json, str, Json], bytes],
        after_receive: Callable[[], None] | None,
    ) -> Json:
        selection = self._selection(conn, ref)
        self.check_connection(conn)
        with self.outbox._read() as db:
            self._resolve(db, conn, selection, ref, "inspect")
        actor = conn["dispatcher_actor"]
        status = self.outbox.status(selection, actor)
        require(status is not None and status.reference == ref)
        assert status is not None
        lookup = SignedLookup(self, conn, request)
        if status.stage == "delivery_unconfirmed":
            status = self.outbox.reconcile(selection, actor, lookup)
        elif status.stage == "queued":
            claim = self.outbox.claim(selection, actor, lookup)
            status = claim.status
            if claim.dispatch:
                try:
                    ticket, offer = self._issue_offer(conn, selection, status)
                    try:
                        self._deliver(offer, request(conn, "fetch", {"ticket": ticket}))
                    finally:
                        with self._offers_lock:
                            self._offers.pop(ticket, None)
                except FAILURES:
                    pass
                else:
                    if after_receive is not None:
                        after_receive()
        self.check_connection(conn)
        return {
            "schema_version": DISPATCH,
            "release_ref": ref,
            "state": status.stage,
            "receiver_receipt_ref": status.receiver_receipt_ref,
            "owner_action": (
                "authorized_operator_reconcile" if status.stage == "delivery_unconfirmed" else None
            ),
        }

    def readback(self, conn: Json, ref: str, lookup: str) -> Json:
        selection = self._selection(conn, ref)
        with self.outbox._read() as db:
            release = self._resolve(db, conn, selection, ref, "reconcile")
        grant = bundle._grant(self.receiver.grant_file)
        with bundle._current(self.receiver.grant_file, grant):
            slot, entitlement, certificate = self._recipient_scope(conn, release, grant)
            receipt = None
            if os.path.lexists(slot):
                self.receiver._receipt(slot, release, grant, entitlement)
                receipt = parse_manifest(bundle._read(slot / "receipt.json", MAX_RECEIPT))
            proof = self.receiver._binding(release, entitlement) | {
                "schema_version": READBACK,
                "lookup_ref": lookup,
                "state": "absent" if receipt is None else "delivered",
                "observed_at": self.clock(),
                "receipt": receipt,
            }
            signature = self.receiver._sign_receipt(proof, grant, certificate)
            require(bundle._grant(self.receiver.grant_file) == grant)
            self.check_connection(conn)
        return {"observation": proof, "signature": signature}


class SignedLookup:
    def __init__(
        self, connection: HandoffConnection, conn: Json, request: Callable[[Json, str, Json], bytes]
    ):
        self.connection = connection
        self.conn = conn
        self.request = request

    def lookup(self, release: ApprovedRelease, lookup_ref: str) -> ReceiverObservation:
        source = self.connection
        try:
            submitted_at = source.clock()
            signed = parse_manifest(
                self.request(
                    self.conn,
                    "readback",
                    {"release_ref": release.reference, "lookup_ref": lookup_ref},
                )
            )
            require(set(signed) == {"observation", "signature"})
            proof = signed["observation"]
            require(isinstance(proof, dict))
            grant = bundle._grant(source.receiver.grant_file)
            with bundle._current(source.receiver.grant_file, grant):
                _, entitlement, certificate = source._recipient_scope(self.conn, release, grant)
                body = source.receiver._binding(release, entitlement)
                when, state, receipt = proof["observed_at"], proof["state"], proof["receipt"]
                require(
                    type(when) is int
                    and max(submitted_at, release.approved_at)
                    <= when
                    <= source.clock()
                    < release.expires_at
                    and state in {"absent", "delivered"}
                    and proof
                    == (
                        body
                        | {
                            "schema_version": READBACK,
                            "lookup_ref": lookup_ref,
                            "state": state,
                            "observed_at": when,
                            "receipt": receipt,
                        }
                    )
                )
                _verify_receipt_signature(
                    certificate, base64.b64decode(signed["signature"], validate=True), proof
                )
                result = self._verified_observation(
                    release, lookup_ref, when, state, receipt, body, certificate
                )
                require(bundle._grant(source.receiver.grant_file) == grant)
                source.check_connection(self.conn)
                return result
        except FAILURES:
            return ReceiverObservation(
                "unknown",
                release.reference,
                release.destination_ref,
                release.artifact_sha256,
                source.clock(),
                lookup_ref,
            )

    @staticmethod
    def _verified_observation(
        release: ApprovedRelease,
        lookup_ref: str,
        when: int,
        state: str,
        receipt: Json | None,
        body: Json,
        certificate: bytes,
    ) -> ReceiverObservation:
        args = (
            release.reference,
            release.destination_ref,
            release.artifact_sha256,
            when,
            lookup_ref,
        )
        if state == "absent":
            require(receipt is None)
            return ReceiverObservation("absent", *args)
        require(isinstance(receipt, dict))
        assert isinstance(receipt, dict)
        received = receipt["received_at"]
        require(type(received) is int and release.approved_at <= received <= when)
        expected = body | {"received_at": received}
        ref = "received-" + digest(expected)[:48]
        expected["receipt_ref"] = ref
        require({k: v for k, v in receipt.items() if k != "receipt_signature"} == expected)
        _verify_receipt_signature(
            certificate, base64.b64decode(receipt["receipt_signature"], validate=True), expected
        )
        return ReceiverObservation("delivered", *args, ref, received)
