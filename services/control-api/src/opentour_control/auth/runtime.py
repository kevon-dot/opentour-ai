"""In-process composition of the control session routes and trusted browser-state services."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import psycopg
from fastapi import APIRouter
from workos import WorkOSClient

from opentour_control.tenants.session_selection import (
    PostgresSelectionMembershipAuthority,
    PostgresSessionSelection,
)
from opentour_control.tenants.session_selection_route import create_session_selection_router

from .browser_state import BrowserAuthState
from .config import FernetSealingPort, IdentityConfig
from .repository import PostgresIdentityRepository
from .session import SessionService, create_session_router
from .workos_provider import (
    BoundedWorkOSBackend,
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
class ControlAuthRuntime:
    browser_state: BrowserAuthState
    session_service: SessionService
    selection: PostgresSessionSelection
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
    return ControlAuthRuntime(browser_state, service, selection, router, client, backend, verifier)
