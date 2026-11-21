"""Transaction-bound PostgreSQL adapters for protected invitation operations."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.pq import TransactionStatus
from pydantic import ValidationError

from opentour_control.auth.config import AuthError, counter, digest, identifier, utc
from opentour_control.auth.session import VerifiedIdentityContext

from .invitations import _STORE_DENIALS, InvitationAcceptanceReceipt


def _invitation_identity(
    identity: VerifiedIdentityContext, session_binding: str
) -> tuple[str, str, str, int, str]:
    if (
        not isinstance(identity, VerifiedIdentityContext)
        or identity.actor.get("kind") != "human"
        or set(identity.actor) != {"issuer", "subject", "kind"}
        or not isinstance(session_binding, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", session_binding)
    ):
        raise AuthError("SESSION_INVALID")
    return (
        identifier(identity.actor["issuer"]),
        identifier(identity.actor["subject"]),
        identifier(identity.session_id),
        counter(identity.session_generation),
        digest(session_binding),
    )


def _require_runtime(conn: psycopg.Connection[dict[str, Any]], role: str) -> None:
    if conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True)
    allowed = conn.execute(
        "SELECT NOT rolsuper AND NOT rolbypassrls "
        "AND pg_has_role(current_user, %s, 'MEMBER') "
        "AND NOT EXISTS (SELECT 1 FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='opentour_invitation' "
        "AND pg_has_role(current_user,c.relowner,'MEMBER')) AS allowed "
        "FROM pg_roles WHERE rolname=current_user",
        (role,),
    ).fetchone()
    if allowed is None or allowed["allowed"] is not True:
        raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True)


class PostgresInvitationAcceptanceStore:
    """Use the dedicated EXECUTE-only role and a fresh owned transaction."""

    def __init__(self, connect: Callable[[], psycopg.Connection[dict[str, Any]]]) -> None:
        self._connect = connect

    def accept(
        self,
        identity: VerifiedIdentityContext,
        *,
        session_binding: str,
        recipient_address: str,
        token_sha256: str,
        request_id: str,
        bearer_expires_at: datetime,
    ) -> InvitationAcceptanceReceipt:
        scope = _invitation_identity(identity, session_binding)

        try:
            with self._connect() as conn:
                _require_runtime(conn, "opentour_invitation_runtime")
                row = conn.execute(
                    "SELECT * FROM opentour_invitation.accept(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        *scope,
                        recipient_address,
                        token_sha256,
                        uuid4().hex,
                        uuid4().hex,
                        identifier(request_id),
                        utc(bearer_expires_at),
                    ),
                ).fetchone()
                if row is None:
                    raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True)
                return InvitationAcceptanceReceipt.model_validate(row)
        except psycopg.errors.RaiseException as error:
            code = error.diag.message_primary
            if code is not None and (status := _STORE_DENIALS.get(code)) is not None:
                raise AuthError(code, status) from None
            raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True) from None
        except (psycopg.Error, ValidationError):
            # A lost database acknowledgment does not prove the transaction rolled back.
            raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True) from None


class PostgresInvitationProgressReader:
    """Read an exact pending receipt through a separate execute-only role."""

    def __init__(self, connect: Callable[[], psycopg.Connection[dict[str, Any]]]) -> None:
        self._connect = connect

    def progress(
        self,
        identity: VerifiedIdentityContext,
        *,
        session_binding: str,
        operation_id: str,
        bearer_expires_at: datetime,
    ) -> InvitationAcceptanceReceipt:
        scope = _invitation_identity(identity, session_binding)
        try:
            with self._connect() as conn:
                _require_runtime(conn, "opentour_invitation_progress_runtime")
                row = conn.execute(
                    "SELECT * FROM opentour_invitation.progress(%s,%s,%s,%s,%s,%s,%s)",
                    (*scope, operation_id, utc(bearer_expires_at)),
                ).fetchone()
                if row is None:
                    raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True)
                return InvitationAcceptanceReceipt.model_validate(row)
        except psycopg.errors.RaiseException as error:
            code = error.diag.message_primary
            if code is not None and (status := _STORE_DENIALS.get(code)) in (401, 404, 409):
                raise AuthError(code, status) from None
            raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True) from None
        except (psycopg.Error, ValidationError):
            raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True) from None
