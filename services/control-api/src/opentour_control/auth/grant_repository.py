"""PostgreSQL grant state and canonical-source fences, using non-owner roles.

Connections are supplied by trusted runtime composition. No caller chooses a DSN,
role, organization context or SQL expression. Provider/journal I/O runs elsewhere.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import psycopg
from opentour_journal.crypto import canonical, sha256
from opentour_journal.models import HeadState, Id, Revision
from opentour_journal.replay import VerifiedAuthorityFrontier
from psycopg.pq import TransactionStatus
from pydantic import ValidationError

from .config import identifier
from .grant_models import (
    AccessGrant,
    ClosedModel,
    CreateGrant,
    GrantCurrentStatus,
    GrantError,
    GrantOperationView,
    instant,
    server_time,
    timestamp,
)
from .grants import AuthorityState, GrantAncestry
from .repository import lock_membership_observation
from .session import AuthContext, VerifiedIdentityContext

Connection = psycopg.Connection[dict[str, Any]]
ConnectionFactory = Callable[[], Connection]
MAX_CURRENT_GRANTS = 1000


@dataclass(frozen=True)
class RepositoryScope:
    """Trusted SQL scoping metadata, never a substitute for an AuthContext.

    Background reconciliation uses its authenticated service registration. Current
    human admission separately consumes142 AuthContext and canonical_identity().
    """

    organization_id: str
    issuer: str
    subject: str
    session_id: str

    @classmethod
    def from_context(cls, context: AuthContext) -> RepositoryScope:
        return cls(
            context.organization_id,
            context.identity.actor["issuer"],
            context.identity.actor["subject"],
            context.identity.session_id,
        )

    @classmethod
    def from_identity(
        cls, organization_id: str, identity: VerifiedIdentityContext
    ) -> RepositoryScope:
        return cls(
            organization_id,
            identity.actor["issuer"],
            identity.actor["subject"],
            identity.session_id,
        )


class CompletedOrganizationFact(ClosedModel):
    organization_id: Id
    organization_revision: Revision
    provisioning_operation_id: Id
    creator_issuer: Id
    creator_subject: Id
    terms_revision: Id
    membership_id: Id
    membership_revision: Revision
    provider_organization_id: Id
    provider_membership_id: Id
    lifecycle: Literal["active"]


def set_context(connection: Connection, context: AuthContext | RepositoryScope) -> None:
    """Inside an owned transaction only; all settings automatically expire at its end."""
    scope = RepositoryScope.from_context(context) if isinstance(context, AuthContext) else context
    values = (
        ("opentour.organization_id", scope.organization_id),
        ("opentour.identity_issuer", scope.issuer),
        ("opentour.identity_subject", scope.subject),
        ("opentour.session_id", scope.session_id),
    )
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise GrantError("DATABASE_TRANSACTION_REQUIRED", 503)
    for key, value in values:
        connection.execute("SELECT set_config(%s,%s,true)", (key, identifier(value)))


class PostgresGrantRepository:
    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        role: Literal["request", "coordinator"],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.connect, self.role, self.clock = connect, role, clock

    def now(self, started: datetime) -> datetime:
        value = server_time(self.clock())
        if value < started:
            raise GrantError("CLOCK_UNAVAILABLE", 503)
        return value

    @contextmanager
    def transaction(self, context: AuthContext | RepositoryScope) -> Iterator[Connection]:
        try:
            with self.connect() as conn:
                if conn.info.transaction_status != TransactionStatus.IDLE:
                    raise GrantError("DATABASE_TRANSACTION_ALREADY_OPEN", 503)
                with conn.transaction():
                    row = conn.execute(
                        "SELECT r.rolsuper,r.rolbypassrls,"
                        "pg_has_role(current_user,'opentour_grant_runtime','MEMBER') AS request,"
                        "pg_has_role(current_user,'opentour_authority_coordinator','MEMBER') "
                        "AS coordinator,"
                        "EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n "
                        "ON n.oid=c.relnamespace "
                        "WHERE n.nspname IN "
                        "('opentour_authority','opentour_tenants','opentour_identity') "
                        "AND c.relowner=r.oid) AS owns_source "
                        "FROM pg_roles r WHERE r.rolname=current_user"
                    ).fetchone()
                    if (
                        row is None
                        or row["rolsuper"]
                        or row["rolbypassrls"]
                        or row["owns_source"]
                        or not row[self.role]
                        or (self.role == "request" and row["coordinator"])
                    ):
                        raise GrantError("DATABASE_ROLE_INVALID", 503)
                    set_context(conn, context)
                    yield conn
        except psycopg.Error:
            # In particular, a failed COMMIT acknowledgement is never an abort receipt.
            raise GrantError("AUTHORITY_SQL_UNCONFIRMED", 503, retryable=True) from None

    def canonical_identity(
        self,
        conn: Connection,
        context: AuthContext,
        started: datetime,
    ) -> dict[str, Any]:
        return self._canonical_membership(
            conn,
            RepositoryScope.from_context(context),
            context.identity,
            started,
            selected_membership=context.membership_id,
        )

    def canonical_completed_organization(
        self,
        conn: Connection,
        scope: RepositoryScope,
        identity: VerifiedIdentityContext,
        started: datetime,
    ) -> CompletedOrganizationFact:
        source = self._canonical_membership(
            conn, scope, identity, started, selected_membership=None
        )
        provisioning = conn.execute(
            "SELECT * FROM opentour_authority.completed_provisioning"
        ).fetchone()
        if provisioning is None or (
            source["creator_issuer"],
            source["creator_subject"],
            provisioning["organization_id"],
            provisioning["creator_issuer"],
            provisioning["creator_subject"],
            provisioning["provider_org_id"],
            provisioning["provider_membership_id"],
        ) != (
            scope.issuer,
            scope.subject,
            scope.organization_id,
            scope.issuer,
            scope.subject,
            source["identity_organization_id"],
            source["provider_membership_id"],
        ):
            raise GrantError("CREATOR_INSTALLATION_SOURCE_UNAVAILABLE", 503, retryable=True)
        self.now(started)
        return CompletedOrganizationFact(
            organization_id=scope.organization_id,
            organization_revision=str(source["organization_revision"]),
            provisioning_operation_id=provisioning["provisioning_operation_id"],
            creator_issuer=scope.issuer,
            creator_subject=scope.subject,
            terms_revision=source["terms_revision"],
            membership_id=source["membership_id"],
            membership_revision=str(source["membership_revision"]),
            provider_organization_id=source["identity_organization_id"],
            provider_membership_id=source["provider_membership_id"],
            lifecycle="active",
        )

    def _canonical_membership(
        self,
        conn: Connection,
        scope: RepositoryScope,
        identity: VerifiedIdentityContext,
        started: datetime,
        *,
        selected_membership: str | None,
    ) -> dict[str, Any]:
        if self.role != "coordinator":
            raise GrantError("DATABASE_ROLE_INVALID", 503)
        source = conn.execute("SELECT * FROM opentour_authority.locked_membership()").fetchone()
        if source is None or (
            source["organization_id"] != scope.organization_id
            or (selected_membership is not None and source["membership_id"] != selected_membership)
            or source["identity_issuer"] != identity.actor["issuer"]
            or source["identity_subject"] != identity.actor["subject"]
        ):
            raise GrantError("RESOURCE_NOT_FOUND", 404)
        if (
            source["organization_state"] != "active"
            or source["membership_status"] != "active"
            or not source["identity_organization_id"]
            or not source["provider_membership_id"]
        ):
            raise GrantError("MEMBERSHIP_INACTIVE", 403)
        # Same142 transaction-scoped session fence; no token verifier or secret reader.
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"opentour-identity:{scope.issuer}:{scope.session_id}",),
        )
        session = conn.execute("SELECT * FROM opentour_authority.identity_session").fetchone()
        lock_membership_observation(conn, scope.issuer, source["provider_membership_id"])
        observation = conn.execute(
            "SELECT * FROM opentour_authority.identity_membership WHERE membership_id=%s",
            (source["provider_membership_id"],),
        ).fetchone()
        now = self.now(started)
        allowed_selection: list[tuple[str | None, str | None]] = [
            (scope.organization_id, source["membership_id"])
        ]
        if selected_membership is None:
            allowed_selection.append((None, None))
        if session is None or (
            session["generation"] != identity.session_generation
            or (session["selected_organization_id"], session["selected_membership_id"])
            not in allowed_selection
            or session["ended_at"] is not None
            or session["reconciliation_required"]
            or not session["created_at"] <= now < session["expires_at"]
            or (session["deny_until"] is not None and now < session["deny_until"])
        ):
            raise GrantError("SESSION_INVALID", 401)
        if observation is None or (
            observation["organization_id"] != source["identity_organization_id"]
            or observation["subject"] != source["identity_subject"]
        ):
            raise GrantError("IDENTITY_FRESHNESS_UNAVAILABLE", 503)
        if observation["locally_denied"] or observation["status"] == "removed":
            raise GrantError("MEMBERSHIP_INACTIVE", 403)
        if observation["status"] != "active" or not (
            now - timedelta(seconds=30) <= observation["checked_at"] <= now
            and observation["observed_at"] <= now
            and now - timedelta(seconds=30) <= instant(identity.identity_checked_at) <= now
        ):
            raise GrantError("IDENTITY_FRESHNESS_UNAVAILABLE", 503)
        return source

    @staticmethod
    def authority(
        conn: Connection,
        organization_id: str,
        *,
        lock: bool = False,
    ) -> tuple[AuthorityState, dict[str, Any]]:
        row = conn.execute(
            "SELECT * FROM opentour_authority.organizations WHERE organization_id=%s"
            + (" FOR UPDATE" if lock else ""),
            (organization_id,),
        ).fetchone()
        if row is None:
            raise GrantError("AUTHORITY_UNAVAILABLE", 503)
        return AuthorityState(
            row["organization_id"],
            row["authority_revision"],
            row["revocation_epoch"],
            row["state"],
            row["current_operation_id"],
        ), row

    @staticmethod
    def grants(
        conn: Connection,
        organization_id: str,
        *,
        lock: bool = False,
    ) -> dict[str, AccessGrant]:
        # Deterministic lock order. A bounded incomplete result is denied, not truncated.
        rows = conn.execute(
            "SELECT grant_id,revision,state,body FROM opentour_authority.grants "
            "WHERE organization_id=%s ORDER BY grant_id LIMIT %s" + (" FOR UPDATE" if lock else ""),
            (organization_id, MAX_CURRENT_GRANTS + 1),
        ).fetchall()
        if len(rows) > MAX_CURRENT_GRANTS:
            raise GrantError("AUTHORITY_RESOLUTION_LIMIT", 503)
        result = {}
        for row in rows:
            value = AccessGrant.model_validate(
                {**row["body"], "revision": str(row["revision"]), "state": row["state"]}
            )
            if (
                value.grant_id != row["grant_id"]
                or value.resource_scope.organization_id != organization_id
            ):
                raise GrantError("AUTHORITY_STATE_INVALID", 503)
            result[value.grant_id] = value
        return result

    def submit(
        self,
        context: AuthContext,
        workspace_id: str,
        key_hash: str,
        body: CreateGrant,
        request_id: str,
    ) -> None:
        with self.transaction(context) as conn:
            self.retain_request(
                conn, context, workspace_id, key_hash, body, request_id, server_time(self.clock())
            )

    @staticmethod
    def retain_request(
        conn: Connection,
        context: AuthContext,
        workspace_id: str,
        key_hash: str,
        body: CreateGrant,
        request_id: str,
        now: datetime,
    ) -> None:
        raw = canonical(body.wire())
        body_hash = sha256(raw)
        owner = (
            context.organization_id,
            context.identity.actor["issuer"],
            context.identity.actor["subject"],
            workspace_id,
            key_hash,
        )
        conn.execute(
            "INSERT INTO opentour_authority.requests "
            "(organization_id,issuer,subject,workspace_id,key_sha256,"
            "request_sha256,body,request_id,created_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (*owner, body_hash, raw, request_id, now),
        )
        row = conn.execute(
            "SELECT request_sha256,body FROM opentour_authority.requests "
            "WHERE organization_id=%s "
            "AND issuer=%s AND subject=%s AND workspace_id=%s AND key_sha256=%s",
            owner,
        ).fetchone()
        if row is None or row["request_sha256"] != body_hash or bytes(row["body"]) != raw:
            raise GrantError("IDEMPOTENCY_CONFLICT", 409)

    @staticmethod
    def operation_for_key(
        conn: Connection, context: AuthContext, workspace_id: str, key_hash: str
    ) -> dict[str, Any] | None:
        return conn.execute(
            "SELECT * FROM opentour_authority.operations WHERE organization_id=%s "
            "AND issuer=%s AND subject=%s AND workspace_id=%s AND key_sha256=%s",
            (
                context.organization_id,
                context.identity.actor["issuer"],
                context.identity.actor["subject"],
                workspace_id,
                key_hash,
            ),
        ).fetchone()

    @staticmethod
    def retained_operation(
        conn: Connection,
        organization_id: str,
        operation_id: str,
        *,
        lock: bool = False,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM opentour_authority.operations "
            "WHERE organization_id=%s AND operation_id=%s" + (" FOR UPDATE" if lock else ""),
            (organization_id, operation_id),
        ).fetchone()
        if row is None:
            raise GrantError("RESOURCE_NOT_FOUND", 404)
        return row

    def view(
        self,
        conn: Connection,
        row: dict[str, Any],
        *,
        request_id: str,
        now: datetime,
        frontier: VerifiedAuthorityFrontier | None = None,
        target_current: bool = False,
    ) -> GrantOperationView:
        current = None
        unavailable: Literal["AUTHORITY_UNAVAILABLE", "RECONCILIATION_REQUIRED"] | None = None
        if row["state"] == "succeeded":
            authority, source = self.authority(conn, row["organization_id"])
            grants = self.grants(conn, row["organization_id"])
            grant = grants.get(row["grant_id"])
            if grant is None or grant.creation_operation_id != row["operation_id"]:
                raise GrantError("AUTHORITY_STATE_INVALID", 503)
            now = self.now(max(now, source["updated_at"], row["updated_at"]))
            if authority.state == "reconciliation_required" or (
                frontier is not None and frontier.head.unresolved_operation_id is not None
            ):
                unavailable = "RECONCILIATION_REQUIRED"
            elif not self.matches_frontier(authority, source, frontier):
                unavailable = "AUTHORITY_UNAVAILABLE"
            else:
                state = grant.temporal_state(now)
                ancestry = GrantAncestry(grants, now, authority.organization_id)
                if state == "revoked" or (
                    target_current
                    and (
                        state == "expired"
                        or (state == "active" and ancestry.valid(grant))
                        or (state == "not_yet_valid" and ancestry.parents_valid(grant))
                    )
                ):
                    current = GrantCurrentStatus(
                        grant_id=grant.grant_id,
                        revision=grant.revision,
                        state=state,
                        authority_status="current",
                        observed_at=timestamp(now),
                    )
                else:
                    unavailable = "AUTHORITY_UNAVAILABLE"
        return GrantOperationView.model_validate(
            {
                "operation_id": row["operation_id"],
                "operation_kind": "access_grant_create",
                "operation_revision": str(row["revision"]),
                "grant_id": row["grant_id"],
                "state": row["state"],
                "creation_receipt": row["creation_receipt"],
                "current_grant_status": current.wire() if current else None,
                "current_status_unavailable_reason": unavailable,
                "request_id": request_id,
            }
        )

    @staticmethod
    def matches_frontier(
        authority: AuthorityState,
        row: dict[str, Any],
        frontier: VerifiedAuthorityFrontier | None,
    ) -> bool:
        if frontier is None or authority.state != "current" or authority.current_operation_id:
            return False
        try:
            head = HeadState.model_validate(row["journal_head"])
        except ValidationError:
            return False
        return (
            head == frontier.head
            and head.unresolved_operation_id is None
            and head.organization_id == authority.organization_id
            and head.latest_epoch == authority.revocation_epoch
            and head.latest_authority_revision == authority.authority_revision
        )
