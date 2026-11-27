"""Persist one dispatch intent for a current, explicitly approved artifact release."""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Literal, Protocol

from opentour_cell.pilot.intake_mcp import require_ref
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.receipt_recovery import _open_db
from opentour_cell.pilot.slack_adapter import Refused
from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.training import LocalTrainingStore

Action = Literal["reserve", "inspect", "claim", "reconcile"]
Stage = Literal["queued", "delivery_unconfirmed", "delivered"]
Observation = Literal["absent", "unknown", "delivered"]


class ReleaseOutboxError(ValueError):
    """A private action can report a fixed error code without exposing receipt contents."""


def _require(condition: object, reason: str = "release_outbox_unavailable") -> None:
    if not condition:
        raise ReleaseOutboxError(reason)


def _now() -> int:
    return int(time.time())


@dataclass(frozen=True)
class ReleaseSelection:
    workflow_ref: str
    proposal_ref: str
    approval_ref: str

    def __post_init__(self) -> None:
        for ref in (self.workflow_ref, self.proposal_ref, self.approval_ref):
            require_ref(ref)


@dataclass(frozen=True)
class ApprovedRelease:
    tenant: str
    engagement: str
    selection: ReleaseSelection
    proposal_revision: str
    artifact_sha256: str
    approving_grant_revision: str
    destination_ref: str
    approved_at: int
    expires_at: int

    def __post_init__(self) -> None:
        for ref in (
            self.tenant,
            self.engagement,
            self.approving_grant_revision,
            self.destination_ref,
        ):
            require_ref(ref)
        for sha in (self.proposal_revision, self.artifact_sha256):
            _require(re.fullmatch(r"[0-9a-f]{64}", sha))
        _require(
            type(self.approved_at) is int
            and type(self.expires_at) is int
            and 0 < self.approved_at < self.expires_at < 2**63
        )

    @property
    def reference(self) -> str:
        return "release-" + digest(["artifact-handoff-v1", asdict(self)])[:48]


class ReleaseAuthority(Protocol):
    """Resolve current approval and role from trusted sources while the local store is held.

    Connectors use the supplied transaction for the training register. They cannot
    take a second training writer lock or confer permission from selection fields.
    """

    def resolve(
        self,
        db: sqlite3.Connection,
        selection: ReleaseSelection,
        actor: str,
        action: Action,
    ) -> ApprovedRelease | None: ...


@dataclass(frozen=True)
class ReceiverObservation:
    state: Observation
    release_ref: str
    destination_ref: str
    artifact_sha256: str
    observed_at: int
    lookup_ref: str
    receipt_ref: str | None = None
    received_at: int | None = None


class ReceiverLookup(Protocol):
    """Read independently authenticated recipient state for one exact effect key."""

    def lookup(self, grant: ApprovedRelease, lookup_ref: str) -> ReceiverObservation: ...


@dataclass(frozen=True)
class ReleaseStatus:
    reference: str
    destination_ref: str
    stage: Stage
    claimed_at: int | None
    last_checked_at: int | None
    receiver_receipt_ref: str | None
    received_at: int | None


@dataclass(frozen=True)
class Claim:
    status: ReleaseStatus
    dispatch: bool


class ReleaseOutbox:
    """Use the owner/engagement store transaction and a recipient-owned reconciliation port."""

    def __init__(
        self,
        store: LocalTrainingStore,
        authority: ReleaseAuthority,
        clock: Callable[[], int] = _now,
    ):
        self.store = store
        self.authority = authority
        self.clock = clock

    def _current(
        self,
        db: sqlite3.Connection,
        selection: ReleaseSelection,
        actor: str,
        action: Action,
    ) -> ApprovedRelease:
        grant = self.authority.resolve(db, selection, require_ref(actor), action)
        scope = db.execute("SELECT id FROM engagement WHERE singleton=1").fetchone()[0]
        _require(
            isinstance(grant, ApprovedRelease)
            and grant.selection == selection
            and grant.tenant == self.store.organization
            and grant.engagement == scope
            and grant.approved_at <= self.clock() < grant.expires_at
        )
        assert grant is not None
        return grant

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        custody = self.store.path.with_name("custody.sqlite3")
        try:
            with _open_db(custody) as closed, _open_db(self.store.path) as db:
                if closed is not None:
                    raise TrainingError("engagement_withdrawn")
                _require(db is not None)
                assert db is not None
                self.store._check_owner(db)
                engagement = db.execute(
                    "SELECT withdrawn_at FROM engagement WHERE singleton=1"
                ).fetchone()
                if engagement is None or engagement[0] is not None:
                    raise TrainingError("engagement_withdrawn")
                yield db
            if os.path.lexists(custody):
                raise TrainingError("engagement_withdrawn")
        except (OSError, sqlite3.Error, Refused):
            raise ReleaseOutboxError("release_outbox_unavailable") from None

    def _stage(self, db: sqlite3.Connection, grant: ApprovedRelease) -> sqlite3.Row | None:
        if (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='release_outbox'"
            ).fetchone()
            is None
        ):
            return None
        row: sqlite3.Row | None = db.execute(
            "SELECT * FROM release_outbox WHERE engagement=? AND approval_ref=?",
            (grant.engagement, grant.selection.approval_ref),
        ).fetchone()
        _require(
            row is None
            or (row["ref"] == grant.reference and row["binding"] == canonical(asdict(grant))),
            "release_intent_changed",
        )
        if row is not None:
            self._validate_stage(row, grant)
        return row

    @staticmethod
    def _validate_stage(row: sqlite3.Row, grant: ApprovedRelease) -> None:
        stage, claimed, checked, receipt, received = (
            row["stage"],
            row["claimed_at"],
            row["last_checked_at"],
            row["receiver_receipt_ref"],
            row["received_at"],
        )
        _require(
            stage in {"queued", "delivery_unconfirmed", "delivered"}
            and all(
                date is None or (type(date) is int and grant.approved_at <= date < grant.expires_at)
                for date in (claimed, checked, received)
            ),
            "release_stage_integrity_unavailable",
        )
        if stage == "delivered":
            _require(
                checked is not None
                and received is not None
                and received <= checked
                and isinstance(receipt, str),
                "release_stage_integrity_unavailable",
            )
            require_ref(receipt)
        else:
            _require(
                receipt is None
                and received is None
                and (checked is not None if stage == "delivery_unconfirmed" else claimed is None),
                "release_stage_integrity_unavailable",
            )

    def _status(self, row: sqlite3.Row, grant: ApprovedRelease) -> ReleaseStatus:
        return ReleaseStatus(
            grant.reference,
            grant.destination_ref,
            row["stage"],
            row["claimed_at"],
            row["last_checked_at"],
            row["receiver_receipt_ref"],
            row["received_at"],
        )

    def reserve(
        self,
        selection: ReleaseSelection,
        actor: str,
        operation_key: str,
        *,
        authenticated_client: str | None = None,
    ) -> ReleaseStatus:
        require_ref(operation_key)
        actor = require_ref(actor)
        client = actor if authenticated_client is None else require_ref(authenticated_client)
        with self.store._db(write=True) as db:
            grant = self._current(db, selection, actor, "reserve")
            db.execute(
                "CREATE TABLE IF NOT EXISTS release_outbox("
                "ref TEXT PRIMARY KEY, engagement TEXT NOT NULL, approval_ref TEXT NOT NULL,"
                "binding TEXT NOT NULL, stage TEXT NOT NULL, claimed_at INTEGER,"
                "last_checked_at INTEGER, receiver_receipt_ref TEXT, received_at INTEGER,"
                "UNIQUE(engagement,approval_ref))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS release_operation_keys("
                "engagement TEXT NOT NULL, actor TEXT NOT NULL, client TEXT NOT NULL,"
                "operation_key TEXT NOT NULL, ref TEXT NOT NULL,"
                "PRIMARY KEY(engagement,actor,client,operation_key))"
            )
            alias = db.execute(
                "SELECT ref FROM release_operation_keys "
                "WHERE engagement=? AND actor=? AND client=? AND operation_key=?",
                (grant.engagement, actor, client, operation_key),
            ).fetchone()
            _require(alias is None or alias[0] == grant.reference, "release_idempotency_conflict")
            row = self._stage(db, grant)
            if row is None:
                db.execute(
                    "INSERT INTO release_outbox(ref,engagement,approval_ref,binding,stage) "
                    "VALUES (?,?,?,?,'queued')",
                    (
                        grant.reference,
                        grant.engagement,
                        selection.approval_ref,
                        canonical(asdict(grant)),
                    ),
                )
                row = self._stage(db, grant)
            db.execute(
                "INSERT OR IGNORE INTO release_operation_keys VALUES(?,?,?,?,?)",
                (grant.engagement, actor, client, operation_key, grant.reference),
            )
            assert row is not None
            return self._status(row, grant)

    def status(self, selection: ReleaseSelection, actor: str) -> ReleaseStatus | None:
        with self._read() as db:
            grant = self._current(db, selection, actor, "inspect")
            row = self._stage(db, grant)
            return None if row is None else self._status(row, grant)

    def claim(self, selection: ReleaseSelection, actor: str, receiver: ReceiverLookup) -> Claim:
        return self._lookup(selection, actor, receiver, action="claim")

    def reconcile(
        self, selection: ReleaseSelection, actor: str, receiver: ReceiverLookup
    ) -> ReleaseStatus:
        return self._lookup(selection, actor, receiver, action="reconcile").status

    def _lookup(
        self,
        selection: ReleaseSelection,
        actor: str,
        receiver: ReceiverLookup,
        *,
        action: Literal["claim", "reconcile"],
    ) -> Claim:
        with self._read() as db:
            grant = self._current(db, selection, actor, action)
            row = self._stage(db, grant)
            _require(row is not None)
            assert row is not None
            if row["stage"] == "delivered" or (action == "claim" and row["claimed_at"] is not None):
                return Claim(self._status(row, grant), False)
        requested_at = self.clock()
        lookup_ref = "lookup-" + secrets.token_hex(16)
        try:
            observed = receiver.lookup(grant, lookup_ref)
        except OSError:
            observed = ReceiverObservation(
                "unknown",
                grant.reference,
                grant.destination_ref,
                grant.artifact_sha256,
                self.clock(),
                lookup_ref,
            )
        with self.store._db(write=True) as db:
            current = self._current(db, selection, actor, action)
            _require(current == grant, "release_intent_changed")
            row = self._stage(db, grant)
            _require(row is not None)
            assert row is not None
            if row["stage"] == "delivered":
                return Claim(self._status(row, grant), False)
            _require(
                isinstance(observed, ReceiverObservation) and observed.lookup_ref == lookup_ref,
                "receiver_observation_scope_mismatch",
            )
            return self._apply(db, row, grant, observed, action, requested_at)

    def _apply(
        self,
        db: sqlite3.Connection,
        row: sqlite3.Row,
        grant: ApprovedRelease,
        observed: ReceiverObservation,
        action: Literal["claim", "reconcile"],
        requested_at: int,
    ) -> Claim:
        now = self.clock()
        _require(
            (observed.release_ref, observed.destination_ref, observed.artifact_sha256)
            == (grant.reference, grant.destination_ref, grant.artifact_sha256)
            and observed.state in {"absent", "unknown", "delivered"}
            and type(observed.observed_at) is int
            and grant.approved_at <= observed.observed_at <= now < grant.expires_at,
            "receiver_observation_scope_mismatch",
        )
        if observed.observed_at < requested_at or (
            row["last_checked_at"] is not None and observed.observed_at < row["last_checked_at"]
        ):
            return Claim(self._status(row, grant), False)
        if observed.state == "delivered":
            _require(
                observed.receipt_ref is not None
                and type(observed.received_at) is int
                and grant.approved_at <= observed.received_at <= observed.observed_at,
                "receiver_receipt_unavailable",
            )
            assert observed.receipt_ref is not None
            db.execute(
                "UPDATE release_outbox SET stage='delivered',last_checked_at=?,"
                "receiver_receipt_ref=?,received_at=? WHERE ref=?",
                (
                    observed.observed_at,
                    require_ref(observed.receipt_ref),
                    observed.received_at,
                    grant.reference,
                ),
            )
            dispatch = False
        else:
            _require(observed.receipt_ref is None and observed.received_at is None)
            dispatch = (
                action == "claim" and row["claimed_at"] is None and observed.state == "absent"
            )
            stage = (
                "delivery_unconfirmed"
                if dispatch or row["claimed_at"] is not None or observed.state == "unknown"
                else "queued"
            )
            db.execute(
                "UPDATE release_outbox SET stage=?,claimed_at=?,last_checked_at=? WHERE ref=?",
                (
                    stage,
                    now if dispatch else row["claimed_at"],
                    observed.observed_at,
                    grant.reference,
                ),
            )
        updated = self._stage(db, grant)
        assert updated is not None
        return Claim(self._status(updated, grant), dispatch)
