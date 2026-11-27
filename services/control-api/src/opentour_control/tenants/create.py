"""Explicitly composed organization routes. No provider job or app starts on import."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from opentour_control.auth.config import AuthError, identifier
from opentour_control.auth.session import IdentityUse, SessionService

from .models import (
    CreateReceipt,
    CreateRequest,
    Creator,
    OperationView,
    RevisionRequest,
    SetupError,
    TermsView,
    decode_create,
    key_digest,
    server_time,
)
from .provider import ProvisioningProvider
from .repository import OrganizationRepository


class OrganizationService:
    def __init__(
        self,
        sessions: SessionService,
        repository: OrganizationRepository,
        provider: ProvisioningProvider,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.sessions, self.repository, self.provider, self.clock = (
            sessions,
            repository,
            provider,
            clock,
        )

    def _creator(self, token: str, binding: str, use: IdentityUse) -> Creator:
        verified = self.sessions.verified_identity(token, binding, use)
        return Creator.from_verified(verified, self.sessions.config.issuer)

    def terms(self) -> TermsView:
        now = server_time(self.clock())
        return self.repository.terms(now).current_view(now)

    def create(
        self, token: str, binding: str, body: CreateRequest, key: str, request_id: str
    ) -> CreateReceipt:
        creator = self._creator(token, binding, IdentityUse.ORGANIZATION_CREATE)
        key_hash = key_digest(key)
        previous = self.repository.replay(creator, key_hash, body.digest())
        if previous is not None:
            creator.same_session(self._creator(token, binding, IdentityUse.ORGANIZATION_CREATE))
            return previous
        if not self.provider.email_verified(creator.subject):
            raise SetupError("EMAIL_VERIFICATION_REQUIRED", 403)
        creator.same_session(self._creator(token, binding, IdentityUse.ORGANIZATION_CREATE))
        return self.repository.create(
            creator,
            body,
            key_hash,
            identifier(request_id),
            self.provider.environment,
            server_time(self.clock()),
        )

    def progress(
        self, token: str, binding: str, operation_id: str, request_id: str
    ) -> OperationView:
        creator = self._creator(token, binding, IdentityUse.OWN_SETUP_OPERATION)
        value = self.repository.get(creator, identifier(operation_id))
        value.belongs_to(creator)
        creator.same_session(self._creator(token, binding, IdentityUse.OWN_SETUP_OPERATION))
        return value.view(request_id)

    def change(
        self,
        token: str,
        binding: str,
        operation_id: str,
        action: Literal["retry", "cancel"],
        body: RevisionRequest,
        key: str,
        request_id: str,
    ) -> OperationView:
        creator = self._creator(token, binding, IdentityUse.OWN_SETUP_OPERATION)
        return self.repository.change(
            creator,
            identifier(operation_id),
            action,
            body.expected_revision,
            key_digest(key),
            identifier(request_id),
            server_time(self.clock()),
        )


def create_organization_terms_router(read_terms: Callable[[], TermsView] | None) -> APIRouter:
    """Expose only the current public terms projection."""
    router = APIRouter()
    headers = {"Cache-Control": "no-store", "Vary": "Authorization, Cookie"}

    @router.get("/v1/organization-terms")
    def terms() -> JSONResponse:
        request_id = secrets.token_hex(16)
        try:
            if read_terms is None:
                raise SetupError("TERMS_UNAVAILABLE", 503)
            return JSONResponse(read_terms().model_dump(), headers=headers)
        except (AuthError, SetupError) as error:
            return JSONResponse(error.wire(request_id), status_code=error.status, headers=headers)
        except (psycopg.Error, ValidationError):
            unavailable = SetupError("TERMS_UNAVAILABLE", 503)
            return JSONResponse(unavailable.wire(request_id), status_code=503, headers=headers)

    return router


def create_organization_router(service: OrganizationService) -> APIRouter:
    """BFF sends a verified bearer token plus opaque session binding; never AuthContext.

    Cookie/Origin/CSRF enforcement for browser BFF mutations belongs to297. These
    control API handlers require the bearer credential as well as the binding.
    """
    router = APIRouter()
    router.include_router(create_organization_terms_router(service.terms))
    headers = {"Cache-Control": "no-store", "Vary": "Authorization, Cookie"}

    def credentials(request: Request) -> tuple[str, str]:
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise AuthError("SESSION_INVALID")
        return authorization[7:], request.cookies.get("opentour-session", "")

    def error_response(error: SetupError | AuthError, request_id: str) -> JSONResponse:
        extra = dict(headers)
        if isinstance(error, SetupError) and error.retry_after is not None:
            extra["Retry-After"] = str(error.retry_after)
        return JSONResponse(error.wire(request_id), status_code=error.status, headers=extra)

    async def bounded_body(request: Request) -> bytes:
        chunks: list[bytes] = []
        length = 0
        async for chunk in request.stream():
            length += len(chunk)
            if length > 16_384:
                raise SetupError("SCHEMA_INVALID", 422)
            chunks.append(chunk)
        return b"".join(chunks)

    @router.post("/v1/organizations")
    async def create(request: Request) -> JSONResponse:
        request_id = secrets.token_hex(16)
        try:
            token, binding = credentials(request)
            body = decode_create(await bounded_body(request))
            # Offload the bounded blocking identity/DB/provider ports from ASGI's event loop.
            from starlette.concurrency import run_in_threadpool

            result = await run_in_threadpool(
                service.create,
                token,
                binding,
                body,
                request.headers.get("idempotency-key", ""),
                request_id,
            )
            return JSONResponse(result.model_dump(), status_code=202, headers=headers)
        except (SetupError, AuthError) as error:
            return error_response(error, request_id)
        except psycopg.Error:
            return error_response(
                SetupError("SETUP_ADMISSION_UNCONFIRMED", 503, retryable=True), request_id
            )

    @router.get("/v1/operations/{operation_id}")
    def progress(operation_id: str, request: Request) -> JSONResponse:
        request_id = secrets.token_hex(16)
        try:
            token, binding = credentials(request)
            return JSONResponse(
                service.progress(token, binding, operation_id, request_id).model_dump(),
                headers=headers,
            )
        except (SetupError, AuthError) as error:
            return error_response(error, request_id)
        except psycopg.Error:
            return error_response(
                SetupError("SETUP_STATE_UNAVAILABLE", 503, retryable=True), request_id
            )

    async def change(
        operation_id: str, request: Request, action: Literal["retry", "cancel"]
    ) -> JSONResponse:
        request_id = secrets.token_hex(16)
        try:
            token, binding = credentials(request)
            raw = await bounded_body(request)
            # One-field object: pair parsing rejects duplicate expected_revision as well as extras.
            pairs = json.loads(raw, object_pairs_hook=list)
            if (
                not isinstance(pairs, list)
                or len(pairs) != 1
                or not isinstance(pairs[0], tuple)
                or pairs[0][0] != "expected_revision"
            ):
                raise ValueError()
            body = RevisionRequest.model_validate(dict(pairs))
            from starlette.concurrency import run_in_threadpool

            result = await run_in_threadpool(
                service.change,
                token,
                binding,
                operation_id,
                action,
                body,
                request.headers.get("idempotency-key", ""),
                request_id,
            )
            return JSONResponse(
                result.model_dump(), status_code=202 if action == "retry" else 200, headers=headers
            )
        except (SetupError, AuthError) as error:
            return error_response(error, request_id)
        except (ValueError, TypeError, UnicodeError, ValidationError):
            return error_response(SetupError("SCHEMA_INVALID", 422), request_id)
        except psycopg.Error:
            return error_response(
                SetupError("SETUP_CHANGE_UNCONFIRMED", 503, retryable=True), request_id
            )

    @router.post("/v1/operations/{operation_id}/retry")
    async def retry(operation_id: str, request: Request) -> JSONResponse:
        return await change(operation_id, request, "retry")

    @router.post("/v1/operations/{operation_id}/cancel")
    async def cancel(operation_id: str, request: Request) -> JSONResponse:
        return await change(operation_id, request, "cancel")

    return router
