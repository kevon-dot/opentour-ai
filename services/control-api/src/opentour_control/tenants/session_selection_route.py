"""Canonical control route for selection after the trusted BFF verifies the provider context."""

from __future__ import annotations

import json
import re
import secrets
from typing import Protocol

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from opentour_control.auth.config import AuthError
from opentour_control.auth.session import VerifiedIdentityContext
from opentour_control.auth.workos_provider import VerifiedToken

from .models import SetupError, key_digest
from .session_selection import PostgresSessionSelection, SelectionReceipt, SessionSelectionRequest


class _SelectionEvidence(Protocol):
    def selection_evidence(
        self, token: str, binding: str
    ) -> tuple[VerifiedIdentityContext, VerifiedToken, str]: ...


def _credentials(request: Request) -> tuple[str, str]:
    authorization = request.headers.getlist("authorization")
    bindings = [
        cookie.partition("=")[2].strip()
        for header in request.headers.getlist("cookie")
        for cookie in header.split(";")
        if cookie.partition("=")[0].strip() == "opentour-session"
    ]
    if (
        len(authorization) != 1
        or not authorization[0].startswith("Bearer ")
        or not authorization[0][7:]
        or len(bindings) != 1
        or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", bindings[0])
    ):
        raise AuthError("SESSION_INVALID")
    return authorization[0][7:], bindings[0]


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise AuthError("SCHEMA_INVALID", 422)
    return value


async def _body(request: Request) -> SessionSelectionRequest:
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 16_384:
            raise AuthError("SCHEMA_INVALID", 422)
        raw.extend(chunk)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        raise AuthError("SCHEMA_INVALID", 422) from None
    return SessionSelectionRequest.parse(value)


def create_session_selection_router(
    browser_state: _SelectionEvidence, selections: PostgresSessionSelection
) -> APIRouter:
    """Compose against durable ports after the BFF's browser Origin and CSRF checks."""
    router = APIRouter()
    headers = {"Cache-Control": "no-store", "Vary": "Authorization, Cookie"}

    def select(
        token: str, binding: str, key: str, body: SessionSelectionRequest, request_id: str
    ) -> SelectionReceipt:
        identity, claims, sealed_digest = browser_state.selection_evidence(token, binding)
        decision = selections.admit(identity, binding, body, key)
        if isinstance(decision, SelectionReceipt):
            current, _, _ = browser_state.selection_evidence(token, binding)
            if current.actor != identity.actor or current.session_id != identity.session_id:
                raise AuthError("SESSION_INVALID")
            if current.session_generation != identity.session_generation:
                raise AuthError("STALE_SESSION_CONTEXT", 409)
            return decision
        return selections.commit(
            identity, binding, body, key, decision, claims, sealed_digest, request_id
        )

    @router.post("/v1/session:select-organization")
    async def select_organization(request: Request) -> JSONResponse:
        request_id = secrets.token_hex(16)
        try:
            token, binding = _credentials(request)
            keys = request.headers.getlist("idempotency-key")
            if len(keys) != 1 or request.query_params:
                raise AuthError("SCHEMA_INVALID", 422)
            try:
                key_digest(keys[0])
            except SetupError:
                raise AuthError("SCHEMA_INVALID", 422) from None
            body = await _body(request)
            result = await run_in_threadpool(select, token, binding, keys[0], body, request_id)
            return JSONResponse(result.wire(), headers=headers)
        except AuthError as error:
            return JSONResponse(error.wire(request_id), status_code=error.status, headers=headers)
        except psycopg.Error:
            unavailable = AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True)
            return JSONResponse(unavailable.wire(request_id), status_code=503, headers=headers)

    return router
