"""Serialize pilot capacity and reconcile effects in the owner-local qualification register."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TypeGuard

from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.qualification import (
    Json,
    QualificationError,
    QualificationRegister,
    _private_json,
    _private_parent,
    _private_record,
    _ref,
    _revision,
)

AUTHORITY_VERSION = "opentour.pilot.lease-authority.v1"
COMMAND_VERSION = "opentour.pilot.lease-command.v1"
RECEIPT_VERSION = "opentour.pilot.lease-receipt.v1"
SNAPSHOT_VERSION = "opentour.pilot.cohort-register-snapshot.v1"
_ROLE = {"acquire": "operator", "begin": "operator", "reconcile": "reconciler", "close": "closer"}
_OUTCOMES = ("observed_applied", "observed_no_effect")
_ROUTES = {"manual", "automated"}
_FIELDS = {
    "schema_version",
    "event_id",
    "action",
    "organization_ref",
    "engagement_revision",
    "generation",
    "slot",
    "signer_ref",
    "route",
    "evidence_digest",
}
_EXTRAS = {
    "acquire": {"qualification_digest", "expires_at"},
    "begin": {"lease_ref"},
    "reconcile": {"lease_ref", "effect_event_id", "outcome"},
    "close": {"lease_ref", "terminal_kind", "reconciliation_revision", "reconciliation_digest"},
}
_AUTHORITY_FIELDS = {
    "schema_version",
    "organization_ref",
    "signer_ref",
    "route",
    "expires_at",
    "authorized_actions",
    "verification_key_hex",
}
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS cohort_waiter (
        sequence INTEGER PRIMARY KEY,
        organization_ref TEXT NOT NULL,
        engagement_revision INTEGER NOT NULL,
        slot INTEGER NOT NULL,
        qualification_digest TEXT NOT NULL,
        ready_at TEXT NOT NULL,
        expires_at INTEGER NOT NULL,
        UNIQUE (organization_ref, engagement_revision)
    )""",
    """CREATE TABLE IF NOT EXISTS cohort_lease (
        generation INTEGER PRIMARY KEY,
        organization_ref TEXT NOT NULL,
        engagement_revision INTEGER NOT NULL,
        slot INTEGER NOT NULL,
        qualification_digest TEXT NOT NULL,
        owner_acceptance_digest TEXT NOT NULL,
        expires_at INTEGER NOT NULL,
        lease_ref TEXT NOT NULL,
        revision INTEGER NOT NULL,
        revision_nonce TEXT NOT NULL,
        terminal_kind TEXT,
        terminal_digest TEXT,
        terminal_reconciliation_digest TEXT,
        UNIQUE (organization_ref, engagement_revision)
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS one_open_cohort_lease
        ON cohort_lease ((1)) WHERE terminal_kind IS NULL""",
    """CREATE TABLE IF NOT EXISTS cohort_command (
        organization_ref TEXT NOT NULL,
        event_id TEXT NOT NULL,
        command_digest TEXT NOT NULL,
        generation INTEGER NOT NULL,
        signer_ref TEXT NOT NULL,
        role TEXT NOT NULL,
        key_fingerprint TEXT NOT NULL,
        observed_at INTEGER NOT NULL,
        receipt TEXT NOT NULL,
        PRIMARY KEY (organization_ref, event_id)
    )""",
    """CREATE TABLE IF NOT EXISTS cohort_effect (
        generation INTEGER NOT NULL,
        event_id TEXT NOT NULL,
        effect_scope_digest TEXT NOT NULL,
        reconciliation_digest TEXT,
        outcome TEXT,
        reconciled_by TEXT,
        PRIMARY KEY (generation, event_id)
    )""",
)


def _hex(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _generation(value: object) -> bool:
    return type(value) is int and 0 <= value < 2**63 - 1


def _command(value: object) -> TypeGuard[Json]:
    if not isinstance(value, dict):
        return False
    action = value.get("action")
    if (
        not isinstance(action, str)
        or action not in _ROLE
        or set(value) != _FIELDS | _EXTRAS[action]
    ):
        return False
    if (
        value["schema_version"] != COMMAND_VERSION
        or not all(_ref(value[key]) for key in ("organization_ref", "signer_ref", "event_id"))
        or not isinstance(value["route"], str)
        or value["route"] not in _ROUTES
        or not _revision(value["engagement_revision"])
        or not _generation(value["generation"])
        or type(value["slot"]) is not int
        or not 1 <= value["slot"] <= 10
        or not _hex(value["evidence_digest"])
    ):
        return False
    if action == "acquire":
        return _hex(value["qualification_digest"]) and _revision(value["expires_at"])
    if not _hex(value["lease_ref"]):
        return False
    if action == "reconcile":
        return _ref(value["effect_event_id"]) and value["outcome"] in _OUTCOMES
    if action == "close":
        return (
            value["terminal_kind"] in ("closeout", "withdrawal")
            and _generation(value["reconciliation_revision"])
            and _hex(value["reconciliation_digest"])
        )
    return True


@dataclass(frozen=True)
class LeaseAuthority:
    organization_ref: str
    signer_ref: str
    route: str
    expires_at: int
    actions: frozenset[str]
    key: bytes = field(repr=False)

    @classmethod
    def parse(cls, raw: Json) -> LeaseAuthority:
        actions = raw.get("authorized_actions")
        if (
            set(raw) != _AUTHORITY_FIELDS
            or raw["schema_version"] != AUTHORITY_VERSION
            or not _ref(raw["organization_ref"])
            or not _ref(raw["signer_ref"])
            or not isinstance(raw["route"], str)
            or raw["route"] not in _ROUTES
            or not _revision(raw["expires_at"])
            or not _hex(raw["verification_key_hex"])
            or not isinstance(actions, list)
            or not actions
            or not all(isinstance(action, str) and action in _ROLE for action in actions)
            or len({_ROLE[action] for action in actions}) != 1
        ):
            raise QualificationError("invalid_lease_authority")
        return cls(
            raw["organization_ref"],
            raw["signer_ref"],
            raw["route"],
            raw["expires_at"],
            frozenset(actions),
            bytes.fromhex(raw["verification_key_hex"]),
        )

    def verifies(self, command: Json, signature: object, now: int) -> bool:
        return (
            now < self.expires_at
            and len(self.key) == 32
            and (command["organization_ref"], command["signer_ref"], command["route"])
            == (self.organization_ref, self.signer_ref, self.route)
            and command["action"] in self.actions
            and all(_ROLE.get(action) == _ROLE[command["action"]] for action in self.actions)
            and _hex(signature)
            and isinstance(signature, str)
            and hmac.compare_digest(
                hmac.digest(
                    self.key,
                    (COMMAND_VERSION + "\n" + canonical(command)).encode("ascii"),
                    "sha256",
                ).hex(),
                signature,
            )
        )


def _receipt(disposition: str, reason: str, **fields: object) -> Json:
    return {
        "schema_version": RECEIPT_VERSION,
        "disposition": disposition,
        "reason": reason,
        **fields,
    }


def _qualification(db: sqlite3.Connection, organization: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = db.execute(
        """SELECT * FROM qualification_event WHERE organization_ref = ?
           ORDER BY engagement_revision DESC, source_revision DESC LIMIT 1""",
        (organization,),
    ).fetchone()
    return row


def _qualification_matches(row: sqlite3.Row | None, epoch: int, qualification_digest: str) -> bool:
    return (
        row is not None
        and row["engagement_revision"] == epoch
        and row["event_digest"] == qualification_digest
        and json.loads(row["receipt"])["disposition"] == "qualified"
    )


def _current(db: sqlite3.Connection) -> sqlite3.Row | None:
    row: sqlite3.Row | None = db.execute(
        "SELECT * FROM cohort_lease ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    return row


def _matches(row: sqlite3.Row | None, command: Json) -> bool:
    return row is not None and all(
        row[key] == command[key]
        for key in (
            "organization_ref",
            "engagement_revision",
            "generation",
            "slot",
            "lease_ref",
        )
    )


def _queue_head(db: sqlite3.Connection, now: int) -> sqlite3.Row | None:
    waiting = db.execute(
        """SELECT w.* FROM cohort_waiter AS w
           WHERE expires_at > ? AND NOT EXISTS (
               SELECT 1 FROM cohort_lease AS l
               WHERE l.organization_ref = w.organization_ref
                 AND l.engagement_revision = w.engagement_revision
           )
           ORDER BY ready_at, sequence""",
        (now,),
    )
    rows: list[sqlite3.Row] = waiting.fetchall()
    for row in rows:
        if _qualification_matches(
            _qualification(db, row["organization_ref"]),
            row["engagement_revision"],
            row["qualification_digest"],
        ):
            return row
    return None


def _advance(db: sqlite3.Connection, generation: int) -> None:
    db.execute(
        "UPDATE cohort_lease SET revision = revision + 1, revision_nonce = ? WHERE generation = ?",
        (secrets.token_hex(32), generation),
    )


def _checkpoint(db: sqlite3.Connection, generation: int) -> Json:
    lease = db.execute(
        "SELECT lease_ref, revision, revision_nonce FROM cohort_lease WHERE generation = ?",
        (generation,),
    ).fetchone()
    # Include ordered outcomes and a revision secret minted at commit. An earlier
    # terminal signature cannot anticipate the post-reconciliation checkpoint.
    effects = db.execute(
        """SELECT event_id, effect_scope_digest, reconciliation_digest, outcome, reconciled_by
           FROM cohort_effect WHERE generation = ? ORDER BY event_id""",
        (generation,),
    ).fetchall()
    return {
        "reconciliation_revision": lease["revision"],
        "reconciliation_digest": digest([list(lease), [list(effect) for effect in effects]]),
    }


class CohortLeaseRegister:
    """Use one signed command protocol for both acquisition routes."""

    def __init__(self, register: QualificationRegister):
        self.register = register

    def submit(self, envelope: Mapping[str, Any], authority: LeaseAuthority) -> Json:
        command = envelope.get("command")
        signature = envelope.get("signature")
        if set(envelope) != {"command", "signature"} or not _command(command):
            return _receipt("held", "invalid_lease_command")
        if not authority.verifies(command, signature, int(time.time())):
            return _receipt("held", "lease_authority_unverified")
        with self.register._transaction(existing_only=True) as db:
            now = int(time.time())
            if now >= authority.expires_at:
                return _receipt("held", "lease_authority_unverified")
            for statement in _SCHEMA:
                db.execute(statement)
            return self._dispatch(db, command, now, authority)

    @contextmanager
    def guard_in_flight(self, envelope: Json, authority: LeaseAuthority) -> Iterator[None]:
        """Fence one short provider dispatch or publication against register transitions."""
        command = envelope.get("command")
        if not _command(command) or command["action"] != "begin":
            raise QualificationError("workplace_lease_scope_mismatch")
        with self.register._transaction(existing_only=True) as db:
            now = int(time.time())
            active = _current(db)
            if (
                not authority.verifies(command, envelope.get("signature"), now)
                or active is None
                or not _matches(active, command)
                or active["terminal_kind"] is not None
                or now >= active["expires_at"]
                or not _qualification_matches(
                    _qualification(db, command["organization_ref"]),
                    command["engagement_revision"],
                    active["qualification_digest"],
                )
            ):
                raise QualificationError("workplace_lease_no_longer_current")
            effect = db.execute(
                """SELECT 1 FROM cohort_effect WHERE generation = ? AND event_id = ?
                   AND effect_scope_digest = ? AND reconciliation_digest IS NULL""",
                (command["generation"], command["event_id"], command["evidence_digest"]),
            ).fetchone()
            if effect is None:
                raise QualificationError("workplace_lease_no_longer_current")
            yield

    def _dispatch(
        self, db: sqlite3.Connection, command: Json, now: int, authority: LeaseAuthority
    ) -> Json:
        org = command["organization_ref"]
        event_id = command["event_id"]
        token = digest(command)
        active = _current(db)
        prior = db.execute(
            "SELECT * FROM cohort_command WHERE organization_ref = ? AND event_id = ?",
            (org, event_id),
        ).fetchone()
        if prior is not None:
            if token != prior["command_digest"]:
                return _receipt("held", "lease_event_conflict")
            return self._replay(db, active, command, json.loads(prior["receipt"]), now)

        role = _ROLE[command["action"]]
        fingerprint = hashlib.sha256(authority.key).hexdigest()
        overlap = db.execute(
            """SELECT 1 FROM cohort_command WHERE organization_ref = ? AND role <> ?
               AND (signer_ref = ? OR key_fingerprint = ?) LIMIT 1""",
            (org, role, authority.signer_ref, fingerprint),
        ).fetchone()
        if overlap is not None:
            return _receipt("held", "lease_authorities_not_independent")
        if command["action"] == "acquire":
            result = self._acquire(db, active, command, now, authority.expires_at)
        elif not _matches(active, command) or active is None:
            result = _receipt("held", "lease_scope_stale_or_foreign")
        elif active["terminal_kind"] is not None:
            result = _receipt("held", "lease_terminal")
        elif command["action"] == "begin":
            result = self._begin(db, active, command, now)
        elif command["action"] == "reconcile":
            result = self._reconcile(db, command)
        else:
            result = self._close(db, command)

        if result["disposition"] != "held":
            result["operation_ref"] = token
            db.execute(
                """INSERT INTO cohort_command
                   (organization_ref, event_id, command_digest, generation, signer_ref,
                    role, key_fingerprint, observed_at, receipt)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    org,
                    event_id,
                    token,
                    result.get("generation", command["generation"]),
                    authority.signer_ref,
                    role,
                    fingerprint,
                    now,
                    canonical(result),
                ),
            )
        return result

    def _replay(
        self,
        db: sqlite3.Connection,
        active: sqlite3.Row | None,
        command: Json,
        result: Json,
        now: int,
    ) -> Json:
        if result["disposition"] == "begin_authorized":
            return _receipt("held", "effect_authority_consumed")
        if result["disposition"] == "acquired":
            bound = {
                **command,
                "lease_ref": result["lease_ref"],
                "generation": result["generation"],
            }
            if active is None or not _matches(active, bound):
                return _receipt("held", "lease_scope_stale_or_foreign")
            if active["terminal_kind"] is not None or now >= active["expires_at"]:
                return _receipt("held", "lease_requires_reconciliation")
            if not _qualification_matches(
                _qualification(db, command["organization_ref"]),
                command["engagement_revision"],
                command["qualification_digest"],
            ):
                return _receipt("held", "qualification_scope_changed")
        return result

    def _acquire(
        self,
        db: sqlite3.Connection,
        active: sqlite3.Row | None,
        command: Json,
        now: int,
        authority_expiry: int,
    ) -> Json:
        org, epoch, slot = (
            command["organization_ref"],
            command["engagement_revision"],
            command["slot"],
        )
        qualification = _qualification(db, org)
        if qualification is None or not _qualification_matches(
            qualification, epoch, command["qualification_digest"]
        ):
            return _receipt("held", "qualification_scope_changed")
        if not now < command["expires_at"] <= authority_expiry:
            return _receipt("held", "lease_expiry_invalid")
        used = db.execute(
            "SELECT 1 FROM cohort_lease WHERE organization_ref = ? AND engagement_revision = ?",
            (org, epoch),
        ).fetchone()
        if used is not None:
            return _receipt("held", "engagement_already_leased")

        generation = active["generation"] if active is not None else 0
        occupied = active is not None and active["terminal_kind"] is None
        expected = command["generation"]
        # A concurrent caller that saw the empty slot can join the queue, never take over.
        if expected != generation and not (occupied and expected == generation - 1):
            return _receipt("held", "lease_generation_changed")

        waiter = db.execute(
            "SELECT slot FROM cohort_waiter WHERE organization_ref = ? AND engagement_revision = ?",
            (org, epoch),
        ).fetchone()
        if waiter is not None and waiter["slot"] != slot:
            return _receipt("held", "queue_slot_changed")
        db.execute(
            """INSERT INTO cohort_waiter
               (organization_ref, engagement_revision, slot, qualification_digest,
                ready_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(organization_ref, engagement_revision) DO UPDATE SET
                   qualification_digest = excluded.qualification_digest,
                   ready_at = excluded.ready_at,
                   expires_at = excluded.expires_at""",
            (
                org,
                epoch,
                slot,
                command["qualification_digest"],
                qualification["observed_at"],
                command["expires_at"],
            ),
        )
        if occupied:
            reason = (
                "lease_requires_reconciliation"
                if active is not None and now >= active["expires_at"]
                else "active_lease"
            )
            return _receipt("queued", reason, generation=generation)
        head = _queue_head(db, now)
        if head is None:
            return _receipt("held", "qualification_scope_changed")
        if (head["organization_ref"], head["engagement_revision"]) != (org, epoch):
            return _receipt("queued", "earlier_qualified_candidate", generation=generation)

        next_generation = generation + 1
        lease_ref = digest([COMMAND_VERSION, next_generation, digest(command)])
        db.execute(
            """INSERT INTO cohort_lease
               (generation, organization_ref, engagement_revision, slot, qualification_digest,
                owner_acceptance_digest, expires_at, lease_ref, revision, revision_nonce)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (
                next_generation,
                org,
                epoch,
                slot,
                command["qualification_digest"],
                command["evidence_digest"],
                command["expires_at"],
                lease_ref,
                secrets.token_hex(32),
            ),
        )
        return _receipt(
            "acquired",
            "live_lease_current",
            generation=next_generation,
            lease_ref=lease_ref,
            expires_at=command["expires_at"],
            **_checkpoint(db, next_generation),
        )

    def _begin(self, db: sqlite3.Connection, active: sqlite3.Row, command: Json, now: int) -> Json:
        if now >= active["expires_at"]:
            return _receipt("held", "lease_requires_reconciliation")
        if not _qualification_matches(
            _qualification(db, command["organization_ref"]),
            command["engagement_revision"],
            active["qualification_digest"],
        ):
            return _receipt("held", "qualification_scope_changed")
        prior = db.execute(
            "SELECT 1 FROM cohort_effect WHERE generation = ? AND effect_scope_digest = ?",
            (command["generation"], command["evidence_digest"]),
        ).fetchone()
        if prior is not None:
            return _receipt("held", "effect_authority_consumed")
        db.execute(
            """INSERT INTO cohort_effect (generation, event_id, effect_scope_digest)
               VALUES (?, ?, ?)""",
            (command["generation"], command["event_id"], command["evidence_digest"]),
        )
        _advance(db, command["generation"])
        return _receipt("begin_authorized", "connect_once_now")

    def _reconcile(self, db: sqlite3.Connection, command: Json) -> Json:
        changed = db.execute(
            """UPDATE cohort_effect SET reconciliation_digest = ?, outcome = ?, reconciled_by = ?
               WHERE generation = ? AND event_id = ? AND reconciliation_digest IS NULL""",
            (
                command["evidence_digest"],
                command["outcome"],
                command["signer_ref"],
                command["generation"],
                command["effect_event_id"],
            ),
        ).rowcount
        if changed != 1:
            return _receipt("held", "effect_not_reconcilable")
        number = command["generation"]
        _advance(db, number)
        return _receipt(
            "reconciled",
            "effect_reconciliation_recorded",
            outcome=command["outcome"],
            **_checkpoint(db, number),
        )

    def _close(self, db: sqlite3.Connection, command: Json) -> Json:
        pending = db.execute(
            """SELECT 1 FROM cohort_effect
               WHERE generation = ? AND reconciliation_digest IS NULL LIMIT 1""",
            (command["generation"],),
        ).fetchone()
        if pending is not None:
            return _receipt("held", "live_effect_reconciliation_required")
        number = command["generation"]
        current = _checkpoint(db, number)
        if any(command[key] != value for key, value in current.items()):
            return _receipt("held", "terminal_reconciliation_changed")
        db.execute(
            """UPDATE cohort_lease SET terminal_kind = ?, terminal_digest = ?,
               terminal_reconciliation_digest = ? WHERE generation = ?""",
            (
                command["terminal_kind"],
                command["evidence_digest"],
                command["reconciliation_digest"],
                number,
            ),
        )
        return _receipt("closed", "terminal_engagement_recorded", generation=number, **current)


def claim_workplace_connection(
    binding: Json,
    *,
    directory: Path,
    organization: str,
    engagement: str,
    connection_revision: str,
    operation: str,
) -> Callable[[], AbstractContextManager[None]]:
    """Consume the owner-selected lease command for one exact source operation."""
    if (
        set(binding)
        != {
            "register",
            "authority_file",
            "acquire_submission_file",
            "discover_submission_file",
        }
        or not isinstance(binding["register"], str)
        or not all(_ref(value) for name, value in binding.items() if name != "register")
        or operation not in ("acquire", "discover")
    ):
        raise QualificationError("invalid_workplace_lease_binding")
    path = Path(binding["register"])
    if not path.is_absolute() or path.resolve() != path:
        raise QualificationError("invalid_workplace_lease_binding")
    register = CohortLeaseRegister(QualificationRegister(path))
    authority_path = directory / binding["authority_file"]
    envelope = _private_json(directory / binding[operation + "_submission_file"])
    command = envelope.get("command")
    requested = digest(
        [
            "opentour.pilot.workplace-lease-effect.v1",
            organization,
            engagement,
            connection_revision,
            operation,
        ]
    )
    if (
        not _command(command)
        or command["action"] != "begin"
        or command["organization_ref"] != organization
        or command["evidence_digest"] != requested
    ):
        raise QualificationError("workplace_lease_scope_mismatch")
    authority = LeaseAuthority.parse(_private_json(authority_path))
    result = register.submit(envelope, authority)
    if result["disposition"] != "begin_authorized":
        raise QualificationError("workplace_lease_not_authorized")

    def guard() -> AbstractContextManager[None]:
        return register.guard_in_flight(
            envelope, LeaseAuthority.parse(_private_json(authority_path))
        )

    return guard


_COHORT_TABLES = frozenset({"cohort_waiter", "cohort_lease", "cohort_command", "cohort_effect"})


def _readable(condition: bool) -> None:
    if not condition:
        raise QualificationError("register_unreadable")


def _stored_int(value: object, low: int = 0) -> int:
    _readable(isinstance(value, int) and type(value) is int and low <= value < 2**63)
    assert isinstance(value, int)
    return value


def _stored_ref(value: object) -> str:
    _readable(_ref(value))
    assert isinstance(value, str)
    return value


def _stored_hex(value: object) -> str:
    _readable(_hex(value))
    assert isinstance(value, str)
    return value


def _instant(value: object) -> int:
    """Convert a stored ISO-8601 UTC observation into whole Unix seconds."""
    _readable(isinstance(value, str))
    assert isinstance(value, str)
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise QualificationError("register_unreadable") from None
    offset = moment.utcoffset()
    _readable(offset is not None and not offset)
    seconds = moment.timestamp()
    _readable(seconds.is_integer() and 0 <= seconds < 2**63)
    return int(seconds)


def _qualification_rows(db: sqlite3.Connection) -> list[Json]:
    rows: list[Json] = []
    for row in db.execute(
        """SELECT organization_ref, engagement_revision, source_revision, event_digest,
                  observed_at, receipt FROM qualification_event
           ORDER BY organization_ref, engagement_revision, source_revision"""
    ):
        try:
            receipt = json.loads(row["receipt"])
        except (TypeError, ValueError, RecursionError):
            raise QualificationError("register_unreadable") from None
        _readable(isinstance(receipt, dict))
        disposition = receipt.get("disposition")
        mode = receipt.get("mode")
        operation = receipt.get("operation_ref")
        _readable(
            disposition in ("qualified", "lost")
            and (mode in ("starter", "historical") if disposition == "qualified" else mode is None)
            and isinstance(operation, str)
            and re.fullmatch(r"qualification-[0-9a-f]{64}", operation) is not None
        )
        rows.append(
            {
                "organization_ref": _stored_ref(row["organization_ref"]),
                "engagement_revision": _stored_int(row["engagement_revision"], 1),
                "source_revision": _stored_int(row["source_revision"], 1),
                "event_digest": _stored_hex(row["event_digest"]),
                "observed_at": _instant(row["observed_at"]),
                "disposition": disposition,
                "mode": mode,
                "operation_ref": operation,
            }
        )
    return rows


def _cohort_rows(db: sqlite3.Connection) -> tuple[list[Json], list[Json], list[Json]]:
    waiters = [
        {
            "organization_ref": _stored_ref(row["organization_ref"]),
            "engagement_revision": _stored_int(row["engagement_revision"], 1),
            "slot": _stored_int(row["slot"], 1),
            "qualification_digest": _stored_hex(row["qualification_digest"]),
            "ready_at": _instant(row["ready_at"]),
            "expires_at": _stored_int(row["expires_at"]),
        }
        for row in db.execute(
            """SELECT organization_ref, engagement_revision, slot, qualification_digest,
                      ready_at, expires_at FROM cohort_waiter
               ORDER BY organization_ref, engagement_revision"""
        )
    ]
    leases = []
    for row in db.execute(
        """SELECT generation, organization_ref, engagement_revision, slot, qualification_digest,
                  expires_at, lease_ref, terminal_kind FROM cohort_lease ORDER BY generation"""
    ):
        _readable(row["terminal_kind"] in (None, "closeout", "withdrawal"))
        leases.append(
            {
                "generation": _stored_int(row["generation"], 1),
                "organization_ref": _stored_ref(row["organization_ref"]),
                "engagement_revision": _stored_int(row["engagement_revision"], 1),
                "slot": _stored_int(row["slot"], 1),
                "qualification_digest": _stored_hex(row["qualification_digest"]),
                "expires_at": _stored_int(row["expires_at"]),
                "lease_ref": _stored_hex(row["lease_ref"]),
                "terminal_kind": row["terminal_kind"],
            }
        )
    effects = []
    for row in db.execute(
        """SELECT generation, event_id, reconciliation_digest IS NULL AS pending, outcome
           FROM cohort_effect ORDER BY generation, event_id"""
    ):
        pending = row["pending"] == 1
        _readable(row["outcome"] is None if pending else row["outcome"] in _OUTCOMES)
        effects.append(
            {
                "generation": _stored_int(row["generation"], 1),
                "event_id": _stored_ref(row["event_id"]),
                "pending": pending,
                "outcome": row["outcome"],
            }
        )
    return waiters, leases, effects


def read_register_snapshot(path: Path) -> Json:
    """Read the authoritative register for private reporting without writing to it.

    One read-only transaction (``mode=ro`` plus ``query_only``) covers every table, so the rows
    agree with each other. A register that has never seen a lease command has no cohort tables
    yet and reads as having no waiters or leases; a register holding only some of them is
    refused. A row of an unexpected type or value refuses the whole read, because a partial
    register would silently miscount organizations. Rows were HMAC-checked at admission by the
    party that holds the keys; reading them back re-checks nothing, so the snapshot is
    ``digest_self_consistent_only`` evidence of what the file held at read time.
    """
    _private_parent(path)
    _private_record(path.lstat())
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only = ON")
            db.execute("BEGIN")
            try:
                tables = {
                    row[0]
                    for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                }
                cohort = _COHORT_TABLES & tables
                if cohort and (cohort != _COHORT_TABLES or "qualification_event" not in tables):
                    raise QualificationError("register_inconsistent")
                events = _qualification_rows(db) if "qualification_event" in tables else []
                waiters, leases, effects = _cohort_rows(db) if cohort else ([], [], [])
            finally:
                db.rollback()
    except sqlite3.Error:
        raise QualificationError("register_unreadable") from None
    body: Json = {
        "schema_version": SNAPSHOT_VERSION,
        "qualification_events": events,
        "waiters": waiters,
        "leases": leases,
        "effects": effects,
    }
    return {**body, "snapshot_digest": digest(body)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register", required=True, type=Path)
    parser.add_argument("--authority", required=True, type=Path)
    parser.add_argument("--submission", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        register = CohortLeaseRegister(QualificationRegister(args.register))
        authority = LeaseAuthority.parse(_private_json(args.authority))
        receipt = register.submit(_private_json(args.submission), authority)
    except (OSError, ValueError, RecursionError, sqlite3.Error):
        print("Lease held: private register or input unavailable.", file=sys.stderr)
        return 1
    print(canonical(receipt))
    return int(receipt["disposition"] == "held")


if __name__ == "__main__":
    raise SystemExit(main())
