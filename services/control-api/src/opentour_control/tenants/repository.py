"""One-database setup transactions; provider calls never run inside these locks."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, Protocol
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from .models import (
    CreateReceipt,
    CreateRequest,
    Creator,
    Operation,
    OperationView,
    PublishedTerms,
    SetupError,
    advance_revision,
    correlation,
    server_time,
)

Connection = psycopg.Connection[dict[str, Any]]
ConnectionFactory = Callable[[], Connection]


class AdmissionPolicy(Protocol):
    """Server-owned policy checked while holding the per-person transaction lock."""

    def check(self, connection: Connection, creator: Creator, now: datetime) -> None: ...


class RollingOrganizationAdmissionPolicy:
    """Five durable admissions per person in (now - 24 hours, now], in every state.

    The original organization and provisioning operation are admitted atomically.
    Use the existing creator/time index; never count mutable status, dedup actions,
    display names, browser sessions, or provider calls as new attempts.
    """

    def check(self, connection: Connection, creator: Creator, now: datetime) -> None:
        now = server_time(now)
        window = timedelta(hours=24)
        rows = connection.execute(
            "SELECT created_at FROM opentour_tenants.organizations "
            "WHERE creator_issuer=%s AND creator_subject=%s "
            "AND created_at>%s AND created_at<=%s ORDER BY created_at DESC LIMIT 5",
            (creator.issuer, creator.subject, now - window, now),
        ).fetchall()
        if len(rows) < 5:
            return
        # The fifth newest must leave the window before another attempt fits.
        # This also safely handles history admitted before this policy existed.
        retry_after = max(1, math.ceil((rows[-1]["created_at"] + window - now).total_seconds()))
        raise SetupError("ADMISSION_LIMIT", 429, retryable=True, retry_after=retry_after)


_DEFAULT_ADMISSION_POLICY: Final = RollingOrganizationAdmissionPolicy()


class OrganizationRepository(Protocol):
    def terms(self, now: datetime) -> PublishedTerms: ...
    def replay(self, creator: Creator, key_hash: str, body_hash: str) -> CreateReceipt | None: ...
    def create(
        self,
        creator: Creator,
        body: CreateRequest,
        key_hash: str,
        request_id: str,
        provider_environment: str,
        now: datetime,
    ) -> CreateReceipt: ...
    def get(self, creator: Creator, operation_id: str) -> Operation: ...
    def change(
        self,
        creator: Creator,
        operation_id: str,
        action: Literal["retry", "cancel"],
        expected_revision: str,
        key_hash: str,
        request_id: str,
        now: datetime,
    ) -> OperationView: ...


_OPERATION_SELECT = """
SELECT p.id AS operation_id, p.organization_id, p.creator_issuer, p.creator_subject,
       p.requested_display_name AS display_name, o.terms_revision,
       p.provider_correlation AS correlation,
       p.provider_environment, o.state, o.revision, p.stage, p.stage_outcome,
       p.provider_org_id, p.provider_membership_id, p.last_error AS reason_code,
       p.created_at, p.updated_at
FROM opentour_tenants.provisioning_operations p
JOIN opentour_tenants.organizations o ON o.id=p.organization_id
WHERE p.id=%s
"""


def operation(row: dict[str, Any] | None) -> Operation:
    if row is None:
        raise SetupError("RESOURCE_NOT_FOUND", 404)
    return Operation(**row)


class PostgresOrganizationRepository:
    def __init__(
        self,
        connect: ConnectionFactory,
        policy: AdmissionPolicy | None = _DEFAULT_ADMISSION_POLICY,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.connect, self.policy = connect, policy
        self.clock = clock

    def _now(self, started: datetime) -> datetime:
        current = server_time(self.clock())
        if current < started:
            raise SetupError("CLOCK_UNAVAILABLE", 503)
        return current

    @contextmanager
    def _connection(self, creator: Creator | None = None) -> Iterator[Connection]:
        with self.connect() as conn:
            role = conn.execute(
                "SELECT rolsuper,rolbypassrls,pg_has_role(current_user,"
                "'opentour_tenant_runtime','MEMBER') AS allowed "
                "FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
            if role is None or role["rolsuper"] or role["rolbypassrls"] or not role["allowed"]:
                raise SetupError("DATABASE_ROLE_INVALID", 503)
            if creator is not None:
                conn.execute(
                    "SELECT set_config('opentour.identity_issuer',%s,true)", (creator.issuer,)
                )
                conn.execute(
                    "SELECT set_config('opentour.identity_subject',%s,true)", (creator.subject,)
                )
            yield conn

    def _person_lock(self, conn: Connection, creator: Creator, now: datetime) -> datetime:
        conn.execute(
            "INSERT INTO opentour_tenants.admission_locks VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
            (creator.issuer, creator.subject, now),
        )
        row = conn.execute(
            "SELECT last_checked_at FROM opentour_tenants.admission_locks "
            "WHERE issuer=%s AND subject=%s FOR UPDATE",
            (creator.issuer, creator.subject),
        ).fetchone()
        now = self._now(now)
        if row is None or now < row["last_checked_at"]:
            raise SetupError("CLOCK_UNAVAILABLE", 503)
        conn.execute(
            "UPDATE opentour_tenants.admission_locks SET last_checked_at=%s "
            "WHERE issuer=%s AND subject=%s",
            (now, creator.issuer, creator.subject),
        )
        return now

    def _identity_guard(self, conn: Connection, creator: Creator, started: datetime) -> datetime:
        # This is142's same session lock, not a new authorization algorithm. Holding
        # it through admission prevents logout/generation change between check/commit.
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"opentour-identity:{creator.issuer}:{creator.session_id}",),
        )
        now = self._now(started)
        row = conn.execute(
            "SELECT s.subject,s.generation,s.ended_at,s.created_at,s.expires_at,"
            "EXISTS(SELECT 1 FROM opentour_identity.session_denials d "
            "WHERE d.issuer=s.issuer AND d.session_id=s.session_id AND d.deny_until>%s) AS denied "
            "FROM opentour_identity.sessions s WHERE s.issuer=%s AND s.session_id=%s",
            (now, creator.issuer, creator.session_id),
        ).fetchone()
        if row is None or row["subject"] != creator.subject or row["ended_at"] or row["denied"]:
            raise SetupError("SESSION_INVALID", 401)
        if row["generation"] != creator.session_generation:
            raise SetupError("STALE_SESSION_CONTEXT", 409)
        now = self._now(now)
        if not row["created_at"] <= now < row["expires_at"]:
            raise SetupError("SESSION_INVALID", 401)
        return now

    def _terms(self, conn: Connection, now: datetime, *, lock: bool = False) -> PublishedTerms:
        row = conn.execute(
            "SELECT revision,document_url,effective_at FROM opentour_tenants.locked_current_terms()"
            if lock
            else "SELECT v.revision,v.document_url,v.effective_at "
            "FROM opentour_tenants.terms_publication p JOIN opentour_tenants.terms_versions v "
            "ON p.revision=v.revision WHERE p.singleton"
        ).fetchone()
        if row is None:
            raise SetupError("TERMS_UNAVAILABLE", 503)
        terms = PublishedTerms(**row)
        terms.current_view(self._now(now))
        return terms

    def terms(self, now: datetime) -> PublishedTerms:
        with self._connection() as conn:
            return self._terms(conn, server_time(now))

    @staticmethod
    def _replay(
        conn: Connection,
        creator: Creator,
        name: str,
        key_hash: str,
        body_hash: str,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT body_sha256,response FROM opentour_tenants.request_dedup "
            "WHERE issuer=%s AND subject=%s AND operation=%s AND key_sha256=%s",
            (creator.issuer, creator.subject, name, key_hash),
        ).fetchone()
        if row is None:
            return None
        if row["body_sha256"] != body_hash:
            raise SetupError("IDEMPOTENCY_CONFLICT", 409)
        return dict(row["response"])

    def replay(self, creator: Creator, key_hash: str, body_hash: str) -> CreateReceipt | None:
        with self._connection(creator) as conn:
            found = self._replay(conn, creator, "organization.create", key_hash, body_hash)
            return CreateReceipt.model_validate(found) if found is not None else None

    def create(
        self,
        creator: Creator,
        body: CreateRequest,
        key_hash: str,
        request_id: str,
        provider_environment: str,
        now: datetime,
    ) -> CreateReceipt:
        now = server_time(now)
        with self._connection(creator) as conn:
            now = self._person_lock(conn, creator, now)
            now = self._identity_guard(conn, creator, now)
            previous = self._replay(conn, creator, "organization.create", key_hash, body.digest())
            if previous is not None:
                self._identity_guard(conn, creator, now)
                return CreateReceipt.model_validate(previous)
            terms = self._terms(conn, now, lock=True)
            # Terms acquisition can wait. The acceptance record and admission
            # policy must use time after the publication lock was acquired.
            now = self._now(now)
            if terms.revision != body.accepted_terms_revision:
                raise SetupError("TERMS_REVISION_STALE", 422, field="accepted_terms_revision")
            if self.policy is None:
                raise SetupError("ADMISSION_POLICY_UNAVAILABLE", 503)
            self.policy.check(conn, creator, now)
            org_id, op_id, member_id = (str(uuid4()) for _ in range(3))
            receipt = CreateReceipt(
                organization_id=org_id, operation_id=op_id, request_id=request_id
            )
            conn.execute(
                "INSERT INTO opentour_tenants.organizations "
                "(id,display_name,creator_issuer,creator_subject,terms_revision,terms_accepted_at,"
                "state,revision,created_at,updated_at) VALUES(%s,%s,%s,%"
                "s,%s,%s,'identity_provisioning',1,%s,%s)",
                (
                    org_id,
                    body.display_name,
                    creator.issuer,
                    creator.subject,
                    terms.revision,
                    now,
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO opentour_tenants.memberships "
                "(organization_id,issuer,subject,membership_id,status,ro"
                "le,grant_revision,updated_at) "
                "VALUES(%s,%s,%s,%s,'provisional','owner',1,%s)",
                (org_id, creator.issuer, creator.subject, member_id, now),
            )
            conn.execute(
                "INSERT INTO opentour_tenants.provisioning_operations "
                "(id,organization_id,creator_issuer,creator_subject,provider_correlation,"
                "requested_display_name,provider_environment,stage,stage"
                "_outcome,created_at,updated_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,'organization','not_dispatched',%s,%s)",
                (
                    op_id,
                    org_id,
                    creator.issuer,
                    creator.subject,
                    correlation(org_id, op_id),
                    body.display_name,
                    provider_environment,
                    now,
                    now,
                ),
            )
            self._record(
                conn,
                creator,
                "organization.create",
                key_hash,
                body.digest(),
                op_id,
                receipt.model_dump(),
                now,
            )
            self._event(conn, op_id, org_id, 1, "organization.requested", now)
            now = self._identity_guard(conn, creator, now)
            # Terms/session waits can advance time after _person_lock's first
            # observation. Persist the final watermark under that same lock, so
            # a later clock rollback cannot hide an already admitted attempt.
            conn.execute(
                "UPDATE opentour_tenants.admission_locks SET last_checked_at=%s "
                "WHERE issuer=%s AND subject=%s",
                (now, creator.issuer, creator.subject),
            )
            return receipt

    @staticmethod
    def _record(
        conn: Connection,
        creator: Creator,
        name: str,
        key_hash: str,
        body_hash: str,
        op_id: str,
        response: dict[str, Any],
        now: datetime,
    ) -> None:
        conn.execute(
            "INSERT INTO opentour_tenants.request_dedup VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                creator.issuer,
                creator.subject,
                name,
                key_hash,
                body_hash,
                op_id,
                Jsonb(response),
                now,
            ),
        )

    @staticmethod
    def _event(
        conn: Connection, op_id: str, org_id: str, rev: int, kind: str, now: datetime
    ) -> None:
        conn.execute(
            "INSERT INTO opentour_tenants.outbox VALUES(%s,%s,%s,%s,%s)",
            (op_id, org_id, rev, kind, now),
        )

    @staticmethod
    def _load(conn: Connection, operation_id: str, *, lock: bool = False) -> Operation:
        if lock:
            # All owner changes lock organization before membership/operation rows.
            row = conn.execute(
                "SELECT organization_id FROM opentour_tenants.provisioning_operations WHERE id=%s",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise SetupError("RESOURCE_NOT_FOUND", 404)
            conn.execute(
                "SELECT id FROM opentour_tenants.organizations WHERE id=%s FOR UPDATE",
                (row["organization_id"],),
            )
        return operation(
            conn.execute(
                _OPERATION_SELECT + (" FOR UPDATE OF p" if lock else ""), (operation_id,)
            ).fetchone()
        )

    def get(self, creator: Creator, operation_id: str) -> Operation:
        with self._connection(creator) as conn:
            found = self._load(conn, operation_id)
            found.belongs_to(creator)
            return found

    def change(
        self,
        creator: Creator,
        operation_id: str,
        action: Literal["retry", "cancel"],
        expected_revision: str,
        key_hash: str,
        request_id: str,
        now: datetime,
    ) -> OperationView:
        now = server_time(now)
        name = "setup." + action
        body_hash = hashlib.sha256(
            json.dumps([operation_id, action, expected_revision], separators=(",", ":")).encode()
        ).hexdigest()
        with self._connection(creator) as conn:
            now = self._person_lock(conn, creator, now)
            now = self._identity_guard(conn, creator, now)
            previous = self._replay(conn, creator, name, key_hash, body_hash)
            if previous is not None:
                self._identity_guard(conn, creator, now)
                return OperationView.model_validate(previous)
            current = self._load(conn, operation_id, lock=True)
            current.belongs_to(creator)
            if str(current.revision) != expected_revision:
                raise SetupError("STALE_REVISION", 409, current_revision=str(current.revision))
            if current.state in {"active", "cancelled"} or (
                action == "retry" and current.state == "action_required"
            ):
                raise SetupError("STATE_CONFLICT", 409, current_revision=str(current.revision))
            rev = advance_revision(current.revision)
            state = "cancelled" if action == "cancel" else current.state
            conn.execute(
                "UPDATE opentour_tenants.organizations SET state=%s,revi"
                "sion=%s,updated_at=%s WHERE id=%s",
                (state, rev, now, current.organization_id),
            )
            conn.execute(
                "UPDATE opentour_tenants.provisioning_operations SET upd"
                "ated_at=%s,next_attempt_at=%s WHERE id=%s",
                (now, now if action == "retry" else None, operation_id),
            )
            if action == "cancel":
                conn.execute(
                    "UPDATE opentour_tenants.memberships SET status='removed"
                    "',updated_at=%s WHERE organization_id=%s",
                    (now, current.organization_id),
                )
            self._event(
                conn,
                operation_id,
                current.organization_id,
                rev,
                "organization.cancelled"
                if action == "cancel"
                else "organization.reconcile_requested",
                now,
            )
            response = self._load(conn, operation_id).view(request_id)
            self._record(
                conn, creator, name, key_hash, body_hash, operation_id, response.model_dump(), now
            )
            self._identity_guard(conn, creator, now)
            return response
