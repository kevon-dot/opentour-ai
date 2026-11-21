"""Own-recipient admission for the canonical Control invitation acceptance route."""

from __future__ import annotations

import re
import secrets
from typing import Annotated, Literal, Protocol

import psycopg
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
    ) -> InvitationAcceptanceReceipt:
        """Verify the current session and recipient, then commit or replay the scoped acceptance."""
        ...


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
        current = sessions.verified_identity(bearer, binding, IdentityUse.RECIPIENT_INVITATION)
        if current.actor != initial.actor or current.session_id != initial.session_id:
            raise AuthError("SESSION_INVALID")
        if current.session_generation != initial.session_generation:
            raise AuthError("STALE_SESSION_CONTEXT", 409)
        return store.accept(
            current,
            session_binding=binding,
            recipient_address=address,
            token_sha256=token_sha256,
            request_id=request_id,
        )

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
            if type(receipt) is not InvitationAcceptanceReceipt:
                raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True)
            return JSONResponse(
                receipt.model_dump(),
                status_code=202 if receipt.state == "awaiting_identity" else 200,
                headers=headers,
            )
        except AuthError as error:
            return JSONResponse(error.wire(request_id), status_code=error.status, headers=headers)
        except (psycopg.Error, ValidationError):
            unavailable = AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True)
            return JSONResponse(unavailable.wire(request_id), status_code=503, headers=headers)

    return router
