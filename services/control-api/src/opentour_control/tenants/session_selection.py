"""Server-only organization selection against current session and membership state."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import psycopg
from psycopg.pq import TransactionStatus

from opentour_control.auth.config import AuthError, counter, digest, identifier, utc
from opentour_control.auth.grant_repository import RepositoryScope, set_context
from opentour_control.auth.repository import lock_membership_observation
from opentour_control.auth.session import LocalMembership, VerifiedIdentityContext
from opentour_control.auth.workos_provider import VerifiedToken

from .models import SetupError, key_digest

_DATABASE_REFUSALS = {
    "SESSION_INVALID": 401,
    "RESOURCE_NOT_FOUND": 404,
    "MEMBERSHIP_INACTIVE": 403,
    "STALE_SESSION_CONTEXT": 409,
    "IDEMPOTENCY_CONFLICT": 409,
    "SESSION_GENERATION_EXHAUSTED": 409,
    "CONTEXT_RECONCILIATION_REQUIRED": 409,
    "IDENTITY_FRESHNESS_UNAVAILABLE": 503,
    "AUTHORITY_UNAVAILABLE": 503,
    "SESSION_CREDENTIALS_UNAVAILABLE": 503,
}
SelectionConnectionFactory = Callable[[], psycopg.Connection[dict[str, Any]]]


def _require_runtime_role(conn: psycopg.Connection[dict[str, Any]]) -> None:
    row = conn.execute(
        "SELECT r.rolsuper, r.rolbypassrls, r.rolcreatedb, r.rolcreaterole, "
        "pg_has_role(current_user, 'opentour_selection_runtime', 'MEMBER') AS selector, "
        "EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname IN ('opentour_identity', 'opentour_tenants', "
        "'opentour_authority', 'opentour_selection') AND c.relowner=r.oid) AS owns_source "
        "FROM pg_roles r WHERE r.rolname=current_user"
    ).fetchone()
    if row != {
        "rolsuper": False,
        "rolbypassrls": False,
        "rolcreatedb": False,
        "rolcreaterole": False,
        "selector": True,
        "owns_source": False,
    }:
        raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True)


@dataclass(frozen=True)
class SessionSelectionRequest:
    organization_id: str
    expected_session_generation: int

    def __post_init__(self) -> None:
        organization_id = identifier(self.organization_id)
        try:
            organization_id.encode("utf-8")
        except UnicodeEncodeError:
            raise AuthError("SCHEMA_INVALID", 422) from None
        if "\x00" in organization_id:
            raise AuthError("SCHEMA_INVALID", 422)
        counter(self.expected_session_generation)

    @classmethod
    def parse(cls, value: object) -> SessionSelectionRequest:
        if not isinstance(value, dict) or set(value) != {
            "organization_id",
            "expected_session_generation",
        }:
            raise AuthError("SCHEMA_INVALID", 422)
        return cls(value["organization_id"], value["expected_session_generation"])

    def digest(self) -> str:
        return digest(
            json.dumps(
                [self.organization_id, self.expected_session_generation], separators=(",", ":")
            )
        )


@dataclass(frozen=True)
class SelectionTarget:
    organization_id: str
    identity_organization_id: str
    identity_issuer: str
    membership_id: str
    authority_revision: str
    already_current: bool = False


@dataclass(frozen=True)
class SelectionReceipt:
    organization_id: str
    membership_id: str
    session_generation: int
    request_id: str

    def wire(self) -> dict[str, object]:
        return {
            "state": "active",
            "organization_id": self.organization_id,
            "membership_id": self.membership_id,
            "session_generation": self.session_generation,
            "request_id": self.request_id,
        }


class PostgresSelectionMembershipAuthority:
    """Read-only canonical local membership for OPE-142 under a fixed verified issuer."""

    def __init__(self, connect: SelectionConnectionFactory, issuer: str) -> None:
        self.connect, self.issuer = connect, identifier(issuer)

    def current(self, subject: str, organization_id: str) -> LocalMembership | None:
        try:
            with self.connect() as conn:
                if conn.info.transaction_status != TransactionStatus.IDLE:
                    raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True)
                with conn.transaction():
                    _require_runtime_role(conn)
                    row = conn.execute(
                        "SELECT * FROM opentour_selection.current_membership(%s,%s,%s)",
                        (self.issuer, identifier(subject), identifier(organization_id)),
                    ).fetchone()
        except psycopg.Error:
            raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True) from None
        if row is None or row["organization_state"] != "active" or row["status"] != "active":
            return None
        if row["authority_state"] != "current":
            raise AuthError("AUTHORITY_UNAVAILABLE", 503, True)
        try:
            return LocalMembership(
                subject,
                organization_id,
                identifier(row["identity_organization_id"]),
                identifier(row["membership_id"]),
                identifier(row["provider_membership_id"]),
                True,
                identifier(row["authority_revision"]),
                counter(row["revocation_epoch"]),
            )
        except AuthError:
            raise AuthError("IDENTITY_MEMBERSHIP_CONFLICT", 403) from None


class PostgresSessionSelection:
    """The caller supplies verified server identity and provider claims, never client JSON."""

    def __init__(self, connect: SelectionConnectionFactory) -> None:
        self.connect = connect

    def admit(
        self,
        identity: VerifiedIdentityContext,
        binding: str,
        request: SessionSelectionRequest,
        idempotency_key: str,
    ) -> SelectionTarget | SelectionReceipt:
        return self._resolve(identity, binding, request, idempotency_key, None)

    def commit(
        self,
        identity: VerifiedIdentityContext,
        binding: str,
        request: SessionSelectionRequest,
        idempotency_key: str,
        target: SelectionTarget,
        verified_claims: VerifiedToken,
        sealed_credentials_sha256: str,
        request_id: str,
    ) -> SelectionReceipt:
        if (
            target.organization_id != request.organization_id
            or target.identity_issuer != identity.actor.get("issuer")
            or (verified_claims.issuer, verified_claims.subject, verified_claims.session_id)
            != (identity.actor.get("issuer"), identity.actor.get("subject"), identity.session_id)
        ):
            raise AuthError("SESSION_INVALID")
        if verified_claims.organization_id != target.identity_organization_id:
            raise AuthError("CONTEXT_RECONCILIATION_REQUIRED", 409)
        if not re.fullmatch(r"[a-f0-9]{64}", sealed_credentials_sha256):
            raise AuthError("SESSION_CREDENTIALS_UNAVAILABLE", 503, True)
        values = (
            identifier(target.identity_organization_id),
            identifier(target.membership_id),
            identifier(target.authority_revision),
            utc(verified_claims.expires_at),
            sealed_credentials_sha256,
            identifier(request_id),
        )
        result = self._resolve(identity, binding, request, idempotency_key, values)
        if not isinstance(result, SelectionReceipt):
            raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True)
        return result

    def _resolve(
        self,
        identity: VerifiedIdentityContext,
        binding: str,
        request: SessionSelectionRequest,
        idempotency_key: str,
        commit: tuple[Any, ...] | None,
    ) -> SelectionTarget | SelectionReceipt:
        if (
            not isinstance(identity, VerifiedIdentityContext)
            or identity.actor.get("kind") != "human"
            or set(identity.actor) != {"kind", "subject", "issuer"}
            or not isinstance(binding, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", binding)
        ):
            raise AuthError("SESSION_INVALID")
        scope = RepositoryScope.from_identity(request.organization_id, identity)
        for value in (scope.issuer, scope.subject, scope.session_id):
            identifier(value)
        try:
            key = key_digest(idempotency_key)
        except SetupError:
            raise AuthError("SCHEMA_INVALID", 422) from None
        fields = (
            counter(identity.session_generation),
            digest(binding),
            request.expected_session_generation,
            key,
            request.digest(),
            *(commit or (None,) * 6),
        )
        try:
            with self.connect() as conn:
                if conn.info.transaction_status != TransactionStatus.IDLE:
                    raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True)
                with conn.transaction():
                    _require_runtime_role(conn)
                    set_context(conn, scope)
                    member = conn.execute(
                        "SELECT opentour_selection.lock_target_membership() "
                        "AS provider_membership_id"
                    ).fetchone()
                    conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                        (f"opentour-identity:{scope.issuer}:{scope.session_id}",),
                    )
                    provider_membership_id = (
                        None if member is None else member["provider_membership_id"]
                    )
                    if provider_membership_id is not None:
                        lock_membership_observation(conn, scope.issuer, provider_membership_id)
                    row = conn.execute(
                        "SELECT * FROM opentour_selection.resolve_selection("
                        "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        fields,
                    ).fetchone()
                    if row is None:
                        raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True)
                    outcome: Literal["receipt", "target"] = row["outcome"]
                    if outcome == "receipt":
                        return SelectionReceipt(
                            row["organization_id"],
                            row["membership_id"],
                            row["session_generation"],
                            row["request_id"],
                        )
                    if outcome == "target":
                        return SelectionTarget(
                            row["organization_id"],
                            row["identity_organization_id"],
                            scope.issuer,
                            row["membership_id"],
                            row["authority_revision"],
                            already_current=row["already_current"],
                        )
                    raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True)
        except psycopg.errors.RaiseException as error:
            code = error.diag.message_primary
            if code in _DATABASE_REFUSALS:
                status = _DATABASE_REFUSALS[code]
                raise AuthError(code, status, status == 503) from None
            raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True) from None
        except psycopg.Error:
            # A failed commit acknowledgment does not prove the transaction aborted.
            raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True) from None
