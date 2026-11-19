"""In-process composition of the control session routes and trusted browser-state services."""

from __future__ import annotations

import hmac
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import psycopg
from fastapi import APIRouter
from workos import WorkOSClient

from opentour_control.tenants.session_selection import (
    PostgresSelectionMembershipAuthority,
    PostgresSessionSelection,
    SelectionReceipt,
    SelectionTarget,
    SessionSelectionRequest,
)
from opentour_control.tenants.session_selection_route import create_session_selection_router

from .browser_state import BrowserAuthState, BrowserSelectionIntent
from .config import AuthError, FernetSealingPort, IdentityConfig, counter
from .repository import PostgresIdentityRepository
from .session import SessionService, VerifiedIdentityContext, create_session_router
from .workos_provider import (
    BoundedWorkOSBackend,
    VerifiedToken,
    WorkOSMembershipProvider,
    WorkOSTokenVerifier,
    create_workos_client,
)

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, Any]]]


def _close_provider(
    client: WorkOSClient, backend: BoundedWorkOSBackend, verifier: WorkOSTokenVerifier
) -> bool:
    complete = True
    # The SDK does not close the HTTP backend that was injected into it.
    shutdown: tuple[Callable[[], bool | None], ...] = (
        client.close,
        backend.close_complete,
        verifier.close,
    )
    for close in shutdown:
        try:
            if close() is False:
                complete = False
        except Exception:
            # Raw provider errors can contain sensitive request diagnostics.
            complete = False
    return complete


@dataclass(frozen=True)
class BrowserSelectionBinding:
    """In-process selection operations for a trusted caller with a live browser binding."""

    browser_state: BrowserAuthState
    selection: PostgresSessionSelection

    def authorize_current(
        self, handle: str, identity: VerifiedIdentityContext, intent: BrowserSelectionIntent
    ) -> SelectionTarget | SelectionReceipt:
        """Verify a live stored token for ordinary or post-refresh admission.

        Pre-exchange SSO/MFA continuation with an expired prior token has a separate proof.
        """
        fresh, _, digest = self._evidence(handle, identity)
        request = SessionSelectionRequest(intent.organization_id, intent.expected_generation)
        decision = self.selection.admit(fresh, handle, request, intent.idempotency_key)
        # Selection may have waited on SQL. Recheck the bound proof before disclosing its result.
        _, _, current_digest = self._evidence(handle, identity)
        if not hmac.compare_digest(digest, current_digest):
            raise AuthError("STALE_SESSION_CONTEXT", 409)
        return decision

    def commit(
        self,
        handle: str,
        identity: VerifiedIdentityContext,
        intent: BrowserSelectionIntent,
        target: SelectionTarget,
        credential_digest: str,
    ) -> SelectionReceipt:
        if not isinstance(credential_digest, str) or not re.fullmatch(
            r"[a-f0-9]{64}", credential_digest
        ):
            raise AuthError("STALE_SESSION_CONTEXT", 409)
        fresh, verified, stored_digest = self._evidence(handle, identity)
        if not hmac.compare_digest(credential_digest, stored_digest):
            raise AuthError("STALE_SESSION_CONTEXT", 409)
        request = SessionSelectionRequest(intent.organization_id, intent.expected_generation)
        return self.selection.commit(
            fresh,
            handle,
            request,
            intent.idempotency_key,
            target,
            verified,
            credential_digest,
            secrets.token_hex(16),
        )

    def _evidence(
        self, handle: str, identity: VerifiedIdentityContext
    ) -> tuple[VerifiedIdentityContext, VerifiedToken, str]:
        if not isinstance(identity, VerifiedIdentityContext):
            raise AuthError("SESSION_INVALID")
        current = self.browser_state.current(handle)
        if current.ended:
            raise AuthError("SESSION_ENDED")
        if current.credentials is None:
            raise AuthError("SESSION_CREDENTIALS_UNAVAILABLE", 503, True)
        fresh, verified, digest = self.browser_state.selection_evidence(
            current.credentials.access_token, handle
        )
        if fresh.actor != identity.actor or fresh.session_id != identity.session_id:
            raise AuthError("SESSION_INVALID")
        if fresh.session_generation != counter(identity.session_generation):
            raise AuthError("STALE_SESSION_CONTEXT", 409)
        return fresh, verified, digest


@dataclass(frozen=True)
class ControlAuthRuntime:
    browser_state: BrowserAuthState
    session_service: SessionService
    selection: PostgresSessionSelection
    selection_binding: BrowserSelectionBinding
    router: APIRouter
    _client: WorkOSClient = field(repr=False)
    _backend: BoundedWorkOSBackend = field(repr=False)
    _verifier: WorkOSTokenVerifier = field(repr=False)

    def close(self) -> bool:
        """After draining calls, report whether every owned provider resource is closed."""
        return _close_provider(self._client, self._backend, self._verifier)


def create_control_auth_runtime(
    *,
    config: IdentityConfig,
    workos_api_key: str,
    sealing_keys: tuple[bytes, ...],
    csrf_key: bytes,
    identity_connect: ConnectionFactory,
    selection_connect: ConnectionFactory,
) -> ControlAuthRuntime:
    """Build a local runtime from trusted configuration without opening connections.

    The caller supplies the identity and selection database roles, owns admission
    and draining, and installs the router through the control plane registration.
    """
    if (
        not isinstance(workos_api_key, str)
        or not workos_api_key
        or any(char.isspace() for char in workos_api_key)
    ):
        raise ValueError("An explicit WorkOS API key is required")
    if not isinstance(csrf_key, bytes) or len(csrf_key) < 32:
        raise ValueError("A dedicated browser CSRF key is required")

    sealer = FernetSealingPort(sealing_keys, max(600, config.credential_max_lifetime_seconds))
    repository = PostgresIdentityRepository(identity_connect)
    authority = PostgresSelectionMembershipAuthority(selection_connect, config.issuer)
    selection = PostgresSessionSelection(selection_connect)
    verifier = WorkOSTokenVerifier(config)
    client, backend = create_workos_client(workos_api_key, config)
    try:
        service = SessionService(
            config, verifier, repository, authority, WorkOSMembershipProvider(client, config)
        )
        browser_state = BrowserAuthState(service, sealer, csrf_key)
        router = APIRouter()
        router.include_router(create_session_router(service))
        router.include_router(create_session_selection_router(browser_state, selection))
    except Exception:
        _close_provider(client, backend, verifier)
        raise
    return ControlAuthRuntime(
        browser_state,
        service,
        selection,
        BrowserSelectionBinding(browser_state, selection),
        router,
        client,
        backend,
        verifier,
    )
