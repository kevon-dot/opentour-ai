"""Own-recipient admission for the canonical Control invitation acceptance route."""

from __future__ import annotations

import re
import secrets
from datetime import datetime
from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import Field, ValidationError, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from opentour_control.auth.config import AuthError, digest
from opentour_control.auth.session import IdentityUse, SessionService, VerifiedIdentityContext

from ._request import credentials, json_body
from .models import ClosedModel, validate_revision

_TOKEN_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_ID = Annotated[str, Field(min_length=1, max_length=256)]
_STORE_DENIALS = {
    "SESSION_INVALID": 401,
    "SESSION_ENDED": 401,
    "STALE_SESSION_CONTEXT": 409,
    "CAPABILITY_DENIED": 403,
    "RESOURCE_NOT_FOUND": 404,
    "INVITATION_EXPIRED": 410,
}


class _AcceptRequest(ClosedModel):
    token: str = Field(min_length=1, max_length=512, repr=False)

    @field_validator("token")
    @classmethod
    def validate_token(cls, token: str) -> str:
        if _TOKEN_CONTROLS.search(token):
            raise ValueError("Invalid invitation token")
        token.encode("utf-8")
        return token


class InvitationAcceptanceReceipt(ClosedModel):
    invitation_id: _ID
    membership_id: _ID
    operation_id: _ID | None
    state: Literal["awaiting_identity", "active", "already_member"]
    revision: str
    request_id: _ID

    _check_revision = field_validator("revision")(validate_revision)

    @model_validator(mode="after")
    def validate_operation(self) -> InvitationAcceptanceReceipt:
        if (self.operation_id is None) != (self.state == "already_member"):
            raise ValueError("Invalid invitation operation binding")
        return self


class VerifiedRecipientProvider(Protocol):
    def verified_recipient_address(self, issuer: str, subject: str) -> str: ...


class InvitationAcceptanceStore(Protocol):
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
        """Verify and commit, or replay the stored receipt with its original request ID."""
        ...


class InvitationProgressReader(Protocol):
    def progress(
        self,
        identity: VerifiedIdentityContext,
        *,
        session_binding: str,
        operation_id: str,
        bearer_expires_at: datetime,
    ) -> InvitationAcceptanceReceipt: ...


class InvitationProgressService:
    """Authorize reading the recipient's own pending operation before organization selection."""

    def __init__(self, sessions: SessionService, reader: InvitationProgressReader) -> None:
        self._sessions = sessions
        self._reader = reader

    def read(
        self, bearer: str, session_binding: str, operation_id: str
    ) -> InvitationAcceptanceReceipt:
        identity, bearer_expires_at = self._sessions.verified_identity_with_expiry(
            bearer, session_binding, IdentityUse.RECIPIENT_INVITATION
        )
        if (
            not isinstance(operation_id, str)
            or not 1 <= len(operation_id) <= 256
            or _TOKEN_CONTROLS.search(operation_id)
        ):
            raise AuthError("RESOURCE_NOT_FOUND", 404)
        try:
            operation_id.encode("utf-8")
        except UnicodeEncodeError:
            raise AuthError("RESOURCE_NOT_FOUND", 404) from None
        receipt = self._reader.progress(
            identity,
            session_binding=session_binding,
            operation_id=operation_id,
            bearer_expires_at=bearer_expires_at,
        )
        if (
            type(receipt) is not InvitationAcceptanceReceipt
            or receipt.operation_id != operation_id
            or receipt.state != "awaiting_identity"
        ):
            raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True)
        try:
            return InvitationAcceptanceReceipt.model_validate(receipt.model_dump(warnings=False))
        except ValidationError:
            raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True) from None


def create_invitation_acceptance_router(
    sessions: SessionService,
    recipients: VerifiedRecipientProvider,
    store: InvitationAcceptanceStore,
) -> APIRouter:
    """Bind the protected route to a provider read and an atomic acceptance store.

    The browser BFF owns Origin, CSRF and the guarded invitation handoff. Register
    this router together with the existing operation route's recipient scope.
    """
    router = APIRouter()
    headers = {"Cache-Control": "no-store", "Vary": "Authorization, Cookie"}

    def accept(
        bearer: str, binding: str, token_sha256: str, request_id: str
    ) -> InvitationAcceptanceReceipt:
        initial = sessions.verified_identity(bearer, binding, IdentityUse.RECIPIENT_INVITATION)
        address = recipients.verified_recipient_address(
            initial.actor["issuer"], initial.actor["subject"]
        )
        current, bearer_expires_at = sessions.verified_identity_with_expiry(
            bearer, binding, IdentityUse.RECIPIENT_INVITATION
        )
        if current.actor != initial.actor or current.session_id != initial.session_id:
            raise AuthError("SESSION_INVALID")
        if current.session_generation != initial.session_generation:
            raise AuthError("STALE_SESSION_CONTEXT", 409)
        try:
            receipt = store.accept(
                current,
                session_binding=binding,
                recipient_address=address,
                token_sha256=token_sha256,
                request_id=request_id,
                bearer_expires_at=bearer_expires_at,
            )
        except AuthError as error:
            if _STORE_DENIALS.get(error.code) == error.status:
                raise AuthError(error.code, error.status) from None
            raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True) from None
        if type(receipt) is not InvitationAcceptanceReceipt:
            raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True)
        return InvitationAcceptanceReceipt.model_validate(receipt.model_dump(warnings=False))

    @router.post("/v1/invitations:accept")
    async def accept_invitation(request: Request) -> JSONResponse:
        request_id = secrets.token_hex(16)
        try:
            bearer, binding = credentials(request)
            if request.query_params:
                raise AuthError("SCHEMA_INVALID", 422)
            try:
                body = _AcceptRequest.model_validate(await json_body(request))
            except ValidationError:
                raise AuthError("SCHEMA_INVALID", 422) from None
            receipt = await run_in_threadpool(
                accept, bearer, binding, digest(body.token), request_id
            )
            return JSONResponse(
                receipt.model_dump(),
                status_code=202 if receipt.state == "awaiting_identity" else 200,
                headers=headers,
            )
        except AuthError as error:
            return JSONResponse(error.wire(request_id), status_code=error.status, headers=headers)
        except Exception:
            unavailable = AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True)
            return JSONResponse(unavailable.wire(request_id), status_code=503, headers=headers)

    return router
