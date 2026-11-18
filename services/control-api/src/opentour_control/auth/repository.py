"""Transactional identity state. Local grants/selection intent belong to their owners."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from .config import MAX_COUNTER, AuthError, digest, identifier, timestamp


def lock_membership_observation(
    connection: psycopg.Connection[dict[str, Any]], issuer: str, provider_membership_id: str
) -> None:
    """Serialize142 observation writes with171/172 activation checks, including absent rows.

    The caller must hold this transaction-scoped lock through its observation read and
    local activation commit. It grants no UPDATE privilege on identity state.142 writers
    keep their existing cursor-before-membership lock order; consumers using both must
    use that same order. Issuer and membership are exact provider identity coordinates.
    """
    key = json.dumps(
        ["opentour-identity-membership-v1", identifier(issuer), identifier(provider_membership_id)],
        separators=(",", ":"),
    )
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (key,))

IdentityStatus = Literal["pending_identity", "active", "removed", "reconciliation_unknown"]


@dataclass(frozen=True)
class Session:
    issuer: str
    session_id: str
    subject: str
    generation: int
    selected_organization_id: str | None
    selected_membership_id: str | None
    reconciliation_required: bool
    created_at: datetime
    expires_at: datetime
    ended_at: datetime | None
    deny_until: datetime


@dataclass(frozen=True)
class Observation:
    issuer: str
    membership_id: str
    organization_id: str
    subject: str
    status: IdentityStatus
    checked_at: datetime
    observed_at: datetime
    locally_denied: bool = False
    revision: int = 1


@dataclass(frozen=True)
class IdentityEvent:
    event_id: str
    body_sha256: str
    observed_at: datetime
    observation: Observation
    source: Literal["events_api", "reconciliation"] = "events_api"


@dataclass(frozen=True)
class SessionRevocationEvent:
    event_id: str
    body_sha256: str
    observed_at: datetime
    issuer: str
    session_id: str
    subject: str
    deny_until: datetime
    source: Literal["events_api"] = "events_api"


class IdentityRepository(Protocol):
    def load_session(
        self, binding_sha256: str, now: datetime, *, allow_ended: bool = False
    ) -> Session: ...
    def create_session(self, session: Session, binding_sha256: str) -> Session: ...
    def end_session(self, session: Session, now: datetime) -> Session: ...
    def clear_selection(self, session: Session, binding_sha256: str) -> Session: ...
    def require_reconciliation(self, session: Session) -> None: ...
    def observation(self, issuer: str, membership_id: str) -> Observation | None: ...
    def record_observation(self, observation: Observation) -> Observation: ...
    def cursor(self, issuer: str) -> str | None: ...
    def commit_batch(
        self,
        issuer: str,
        expected_cursor: str | None,
        cursor: str | None,
        events: Sequence[IdentityEvent | SessionRevocationEvent],
    ) -> None: ...


def _session(row: dict[str, Any] | None) -> Session:
    if row is None:
        raise AuthError("SESSION_INVALID")
    return Session(**{name: row[name] for name in Session.__dataclass_fields__})


def _increment(session: Session) -> int:
    if session.generation == MAX_COUNTER:
        raise AuthError("SESSION_GENERATION_EXHAUSTED", 409)
    return session.generation + 1


class PostgresIdentityRepository:
    """Factory must create a dedicated non-owner runtime connection, never a caller DSN.

    The caller grants this role only USAGE plus SELECT/INSERT/UPDATE on this schema.
    Session credential verification precedes repository use. No tenant grant is stored here.
    """

    def __init__(self, connect: Callable[[], psycopg.Connection[dict[str, Any]]]) -> None:
        self.connect = connect

    @staticmethod
    def _locked(connection: psycopg.Connection[dict[str, Any]], session: Session) -> Session:
        PostgresIdentityRepository._session_lock(connection, session.issuer, session.session_id)
        return _session(
            connection.execute(
                "SELECT * FROM opentour_identity.sessions "
                "WHERE issuer=%s AND session_id=%s FOR UPDATE",
                (session.issuer, session.session_id),
            ).fetchone()
        )

    @staticmethod
    def _session_lock(
        connection: psycopg.Connection[dict[str, Any]], issuer: str, session_id: str
    ) -> None:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"opentour-identity:{issuer}:{session_id}",),
        )

    def load_session(
        self, binding_sha256: str, now: datetime, *, allow_ended: bool = False
    ) -> Session:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT s.*, b.generation AS binding_generation, b.expires_at AS binding_expiry "
                "FROM opentour_identity.sessions s JOIN opentour_identity.session_bindings b "
                "USING (issuer,session_id) WHERE b.binding_sha256=%s",
                (binding_sha256,),
            ).fetchone()
            session = _session(row)
            assert row is not None
            if now < session.created_at:
                raise AuthError("IDENTITY_CLOCK_UNAVAILABLE", 503)
            if now >= session.expires_at or now >= row["binding_expiry"]:
                raise AuthError("SESSION_INVALID")
            if session.ended_at is not None:
                if allow_ended:
                    return session
                raise AuthError("SESSION_ENDED")
            if connection.execute(
                "SELECT 1 FROM opentour_identity.session_denials WHERE issuer=%s AND session_id=%s",
                (session.issuer, session.session_id),
            ).fetchone():
                raise AuthError("SESSION_ENDED")
            if row["binding_generation"] != session.generation:
                raise AuthError("STALE_SESSION_CONTEXT", 409)
            return session

    def create_session(
        self,
        session: Session,
        binding_sha256: str,
        *,
        sealed_payload: str | None = None,
        credential_check: Callable[[], datetime] | None = None,
    ) -> Session:
        with self.connect() as connection:
            self._session_lock(connection, session.issuer, session.session_id)
            if connection.execute(
                "SELECT 1 FROM opentour_identity.session_denials WHERE issuer=%s AND session_id=%s",
                (session.issuer, session.session_id),
            ).fetchone():
                raise AuthError("SESSION_ENDED")
            connection.execute(
                "INSERT INTO opentour_identity.sessions "
                "(issuer,session_id,subject,created_at,expires_at,deny_until) "
                "VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (issuer,session_id) DO NOTHING",
                (
                    session.issuer,
                    session.session_id,
                    session.subject,
                    session.created_at,
                    session.expires_at,
                    session.deny_until,
                ),
            )
            current = self._locked(connection, session)
            if (
                current.subject != session.subject
                or current.ended_at is not None
                or session.created_at >= current.expires_at
            ):
                raise AuthError("SESSION_INVALID")
            if sealed_payload is not None:
                # Ordinary callback has no selection fence. It may initialize browser
                # credentials exactly once, including a legacy unselected identity row.
                if (
                    current.generation != 0
                    or current.selected_organization_id is not None
                    or current.reconciliation_required
                    or connection.execute(
                        "SELECT 1 FROM opentour_identity.session_credentials "
                        "WHERE issuer=%s AND session_id=%s",
                        (current.issuer, current.session_id),
                    ).fetchone()
                ):
                    raise AuthError("STALE_SESSION_CONTEXT", 409)
            connection.execute(
                "INSERT INTO opentour_identity.session_bindings VALUES (%s,%s,%s,%s,%s)",
                (
                    binding_sha256,
                    current.issuer,
                    current.session_id,
                    current.generation,
                    current.expires_at,
                ),
            )
            if sealed_payload is not None:
                self._write_credentials(connection, current, sealed_payload)
            if credential_check is not None and credential_check() >= current.expires_at:
                raise AuthError("SESSION_INVALID")
            return current

    @staticmethod
    def _write_credentials(
        connection: psycopg.Connection[dict[str, Any]],
        session: Session,
        sealed_payload: str,
    ) -> None:
        connection.execute(
            "INSERT INTO opentour_identity.session_credentials VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (issuer,session_id) DO UPDATE SET generation=EXCLUDED.generation, "
            "sealed_payload=EXCLUDED.sealed_payload,expires_at=EXCLUDED.expires_at",
            (
                session.issuer,
                session.session_id,
                session.generation,
                sealed_payload,
                session.expires_at,
            ),
        )

    def read_credentials(self, session: Session, now: datetime) -> str:
        with self.connect() as connection:
            current = self._locked(connection, session)
            self._credential_fence(current, session, now)
            row = connection.execute(
                "SELECT sealed_payload FROM opentour_identity.session_credentials "
                "WHERE issuer=%s AND session_id=%s AND generation=%s AND expires_at>%s",
                (current.issuer, current.session_id, current.generation, now),
            ).fetchone()
            if row is None:
                raise AuthError("SESSION_CREDENTIALS_UNAVAILABLE", 503)
            return str(row["sealed_payload"])

    @staticmethod
    def _credential_fence(current: Session, expected: Session, now: datetime) -> None:
        if current.subject != expected.subject or current.ended_at is not None:
            raise AuthError("SESSION_ENDED")
        if now >= current.expires_at:
            raise AuthError("SESSION_INVALID")
        if current.generation != expected.generation:
            raise AuthError("STALE_SESSION_CONTEXT", 409)

    def replace_credentials(self, session: Session, sealed_payload: str, now: datetime) -> None:
        with self.connect() as connection:
            current = self._locked(connection, session)
            self._credential_fence(current, session, now)
            self._write_credentials(connection, current, sealed_payload)

    def end_session(self, session: Session, now: datetime) -> Session:
        with self.connect() as connection:
            current = self._locked(connection, session)
            if current.subject != session.subject:
                raise AuthError("SESSION_INVALID")
            if current.ended_at is not None:
                return current
            if current.generation != session.generation:
                raise AuthError("STALE_SESSION_CONTEXT", 409)
            connection.execute(
                "INSERT INTO opentour_identity.session_denials VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (current.issuer, current.session_id, current.subject, now, current.deny_until),
            )
            row = connection.execute(
                "UPDATE opentour_identity.sessions SET ended_at=%s,generation=%s, "
                "selected_organization_id=NULL,selected_membership_id=NULL, "
                "reconciliation_required=false WHERE issuer=%s AND session_id=%s RETURNING *",
                (now, _increment(current), current.issuer, current.session_id),
            ).fetchone()
            return _session(row)

    def clear_selection(self, session: Session, binding_sha256: str) -> Session:
        with self.connect() as connection:
            current = self._locked(connection, session)
            if current.ended_at is not None or current.generation != session.generation:
                raise AuthError("STALE_SESSION_CONTEXT", 409)
            if current.selected_organization_id is None:
                return current
            row = connection.execute(
                "UPDATE opentour_identity.sessions SET selected_organization_id=NULL, "
                "selected_membership_id=NULL,reconciliation_required=false,generation=%s "
                "WHERE issuer=%s AND session_id=%s RETURNING *",
                (_increment(current), current.issuer, current.session_id),
            ).fetchone()
            cleared = _session(row)
            # Advance only the authenticated binding in the same transaction. Other
            # bindings and already-created contexts remain stale; own-session recovery
            # can continue with no selected tenant after the removal is acknowledged.
            updated = connection.execute(
                "UPDATE opentour_identity.session_bindings SET generation=%s "
                "WHERE binding_sha256=%s AND issuer=%s AND session_id=%s AND generation=%s",
                (
                    cleared.generation,
                    binding_sha256,
                    current.issuer,
                    current.session_id,
                    current.generation,
                ),
            )
            if updated.rowcount != 1:
                raise AuthError("STALE_SESSION_CONTEXT", 409)
            connection.execute(
                "UPDATE opentour_identity.session_credentials SET generation=%s "
                "WHERE issuer=%s AND session_id=%s AND generation=%s",
                (cleared.generation, current.issuer, current.session_id, current.generation),
            )
            return cleared

    def require_reconciliation(self, session: Session) -> None:
        with self.connect() as connection:
            current = self._locked(connection, session)
            if current.ended_at is not None or current.generation != session.generation:
                raise AuthError("STALE_SESSION_CONTEXT", 409)
            connection.execute(
                "UPDATE opentour_identity.sessions SET reconciliation_required=true "
                "WHERE issuer=%s AND session_id=%s",
                (session.issuer, session.session_id),
            )

    def observation(self, issuer: str, membership_id: str) -> Observation | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM opentour_identity.membership_observations "
                "WHERE issuer=%s AND membership_id=%s",
                (issuer, membership_id),
            ).fetchone()
            return Observation(**row) if row else None

    def cursor(self, issuer: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT cursor FROM opentour_identity.sync_cursors WHERE issuer=%s",
                (issuer,),
            ).fetchone()
            return row["cursor"] if row else None

    def record_observation(self, observation: Observation) -> Observation:
        # This is an explicitly identified local reconciliation, not a fabricated provider event.
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO opentour_identity.sync_cursors VALUES (%s,NULL) "
                "ON CONFLICT DO NOTHING",
                (observation.issuer,),
            )
            connection.execute(
                "SELECT cursor FROM opentour_identity.sync_cursors WHERE issuer=%s FOR UPDATE",
                (observation.issuer,),
            )
            event = IdentityEvent(
                str(uuid4()),
                digest(
                    f"{observation.membership_id}:{observation.status}:"
                    f"{timestamp(observation.checked_at)}"
                ),
                observation.observed_at,
                observation,
                "reconciliation",
            )
            self._apply_event(connection, observation.issuer, event)
            row = connection.execute(
                "SELECT * FROM opentour_identity.membership_observations "
                "WHERE issuer=%s AND membership_id=%s",
                (observation.issuer, observation.membership_id),
            ).fetchone()
            assert row is not None
            return Observation(**row)

    def commit_batch(
        self,
        issuer: str,
        expected_cursor: str | None,
        cursor: str | None,
        events: Sequence[IdentityEvent | SessionRevocationEvent],
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO opentour_identity.sync_cursors VALUES (%s,NULL) "
                "ON CONFLICT DO NOTHING",
                (issuer,),
            )
            current = connection.execute(
                "SELECT cursor FROM opentour_identity.sync_cursors WHERE issuer=%s FOR UPDATE",
                (issuer,),
            ).fetchone()
            if current is None or current["cursor"] != expected_cursor:
                raise AuthError("IDENTITY_SYNC_CONFLICT", 409)
            for event in events:
                self._apply_event(connection, issuer, event)
            connection.execute(
                "UPDATE opentour_identity.sync_cursors SET cursor=%s WHERE issuer=%s",
                (cursor, issuer),
            )

    @staticmethod
    def _apply_event(
        connection: psycopg.Connection[dict[str, Any]],
        issuer: str,
        event: IdentityEvent | SessionRevocationEvent,
    ) -> None:
        event_issuer = (
            event.issuer if isinstance(event, SessionRevocationEvent) else event.observation.issuer
        )
        if event_issuer != issuer:
            raise AuthError("IDENTITY_EVENT_SCOPE_CONFLICT", 409)
        prior = connection.execute(
            "SELECT body_sha256 FROM opentour_identity.event_inbox "
            "WHERE issuer=%s AND source=%s AND event_id=%s",
            (issuer, event.source, event.event_id),
        ).fetchone()
        if prior:
            if prior["body_sha256"] != event.body_sha256:
                raise AuthError("IDENTITY_EVENT_CONFLICT", 409)
            return
        connection.execute(
            "INSERT INTO opentour_identity.event_inbox VALUES (%s,%s,%s,%s,%s)",
            (issuer, event.source, event.event_id, event.body_sha256, event.observed_at),
        )
        if isinstance(event, SessionRevocationEvent):
            PostgresIdentityRepository._session_lock(connection, issuer, event.session_id)
            known = connection.execute(
                "SELECT subject FROM opentour_identity.sessions WHERE issuer=%s AND session_id=%s",
                (issuer, event.session_id),
            ).fetchone()
            if known and known["subject"] != event.subject:
                raise AuthError("IDENTITY_EVENT_SCOPE_CONFLICT", 409)
            connection.execute(
                "INSERT INTO opentour_identity.session_denials VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (issuer,session_id) DO UPDATE SET deny_until=GREATEST("
                "opentour_identity.session_denials.deny_until,EXCLUDED.deny_until)",
                (issuer, event.session_id, event.subject, event.observed_at, event.deny_until),
            )
            connection.execute(
                "UPDATE opentour_identity.sessions SET ended_at=%s,"
                "generation=LEAST(generation+1,9007199254740991),selected_organization_id=NULL,"
                "selected_membership_id=NULL,reconciliation_required=false "
                "WHERE issuer=%s AND session_id=%s AND ended_at IS NULL",
                (event.observed_at, issuer, event.session_id),
            )
            connection.execute(
                "INSERT INTO opentour_identity.outbox VALUES (%s,%s,%s,%s,NULL,NULL,%s,%s)",
                (
                    issuer,
                    event.source,
                    event.event_id,
                    "identity.session_ended",
                    event.subject,
                    event.observed_at,
                ),
            )
            return
        observation = event.observation
        lock_membership_observation(connection, issuer, observation.membership_id)
        existing = connection.execute(
            "SELECT * FROM opentour_identity.membership_observations "
            "WHERE issuer=%s AND membership_id=%s FOR UPDATE",
            (issuer, observation.membership_id),
        ).fetchone()
        if existing and (
            existing["subject"] != observation.subject
            or existing["organization_id"] != observation.organization_id
        ):
            raise AuthError("IDENTITY_EVENT_SCOPE_CONFLICT", 409)
        denied = observation.status == "removed" or observation.locally_denied
        if existing:
            denied = denied or existing["locally_denied"]
        status = "removed" if denied else observation.status
        if not existing or observation.checked_at >= existing["checked_at"] or denied:
            if existing and existing["revision"] == 9_223_372_036_854_775_807:
                raise AuthError("REVISION_EXHAUSTED", 409)
            connection.execute(
                "INSERT INTO opentour_identity.membership_observations "
                "(issuer,membership_id,organization_id,subject,status,locally_denied,"
                "checked_at,observed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (issuer,membership_id) DO UPDATE SET status=EXCLUDED.status,"
                "locally_denied=EXCLUDED.locally_denied,checked_at=GREATEST("
                "opentour_identity.membership_observations.checked_at,EXCLUDED.checked_at),"
                "observed_at=EXCLUDED.observed_at,revision="
                "opentour_identity.membership_observations.revision+1",
                (
                    issuer,
                    observation.membership_id,
                    observation.organization_id,
                    observation.subject,
                    status,
                    denied,
                    observation.checked_at,
                    observation.observed_at,
                ),
            )
        kinds = ["identity.observed"]
        if denied:
            kinds.append("identity.revocation_requested")
        for kind in kinds:
            connection.execute(
                "INSERT INTO opentour_identity.outbox VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    issuer,
                    event.source,
                    event.event_id,
                    kind,
                    observation.membership_id,
                    observation.organization_id,
                    observation.subject,
                    event.observed_at,
                ),
            )

    def create_auth_transaction(
        self,
        state_sha256: str,
        browser_sha256: str,
        sealed_payload: str,
        now: datetime,
        expires: datetime,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO opentour_identity.auth_transactions "
                "(state_sha256,browser_sha256,sealed_payload,created_at,expires_at) "
                "VALUES (%s,%s,%s,%s,%s)",
                (state_sha256, browser_sha256, sealed_payload, now, expires),
            )

    def consume_auth_transaction(
        self,
        state_sha256: str,
        browser_sha256: str,
        now: datetime,
        *,
        check: Callable[[str], None] | None = None,
    ) -> str:
        """Claim once. A rejected trusted payload check rolls back the claim."""
        with self.connect() as connection, connection.transaction():
            row = connection.execute(
                "UPDATE opentour_identity.auth_transactions SET consumed_at=%s "
                "WHERE state_sha256=%s AND browser_sha256=%s AND expires_at>%s AND created_at<=%s "
                "AND consumed_at IS NULL RETURNING sealed_payload",
                (now, state_sha256, browser_sha256, now, now),
            ).fetchone()
            if row is None:
                raise AuthError("CALLBACK_INVALID")
            sealed = str(row["sealed_payload"])
            if check is not None:
                check(sealed)
            return sealed


def connection_factory(dsn: str) -> Callable[[], psycopg.Connection[dict[str, Any]]]:
    """DSN is deployment configuration, never request input; do not log it."""

    def connect() -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(
            dsn,
            row_factory=dict_row,
            connect_timeout=5,
            options="-c statement_timeout=5000 -c lock_timeout=2000",
        )

    return connect
