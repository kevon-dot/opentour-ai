"""Local startup, authentication wiring and lifecycle checks using generated inputs."""

import subprocess
import sys
from datetime import UTC, datetime
from unittest.mock import Mock

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentour_control.api.schema import create_control_api
from opentour_control.auth import runtime, workos_provider
from opentour_control.auth.browser_private_pilot import (
    PRIVATE_BROWSER_PILOT_ROUTES,
    lookup_private_browser_pilot_route,
)
from opentour_control.auth.browser_state import BrowserAuthState
from opentour_control.auth.session import SessionService
from opentour_control.tenants.session_selection import PostgresSessionSelection

from tools.schema_validate import schema_registry, valid_instance

CONTEXT = "/v1/session/context"
SELECTION = "/v1/session:select-organization"
PRIVATE_PILOT_PATHS = (
    ("/internal/browser-identity/v1/flows/create", "create_flow"),
    ("/internal/browser-identity/v1/flows/consume", "consume_flow"),
    ("/internal/browser-identity/v1/sessions/admit", "admit"),
    ("/internal/browser-identity/v1/sessions/current", "current"),
    ("/internal/browser-identity/v1/sessions/end", "end"),
)


@pytest.fixture
def configured(identity):
    return {
        "config": identity.config,
        "workos_api_key": "generated-workos-key",
        "sealing_keys": (Fernet.generate_key(),),
        "csrf_key": b"g" * 32,
        "identity_connect": Mock(
            side_effect=psycopg.OperationalError("generated-private-identity-detail")
        ),
        "selection_connect": Mock(
            side_effect=psycopg.OperationalError("generated-private-selection-detail")
        ),
    }


@pytest.fixture
def provider_dependencies(monkeypatch):
    client, backend, verifier = Mock(), Mock(), Mock()
    backend.close_complete.return_value = True
    verifier.close.return_value = True
    monkeypatch.setattr(runtime, "create_workos_client", Mock(return_value=(client, backend)))
    monkeypatch.setattr(runtime, "WorkOSTokenVerifier", Mock(return_value=verifier))
    return client, backend, verifier


def test_import_starts_no_io_or_clients():
    script = """
import socket
import threading
import urllib.request

import psycopg
from opentour_control.auth import workos_provider

def forbidden(*args, **kwargs):
    raise AssertionError("Control authentication import attempted I/O or created a client")

socket.socket.connect = forbidden
socket.getaddrinfo = forbidden
threading.Thread.start = forbidden
urllib.request.OpenerDirector.open = forbidden
psycopg.connect = forbidden
workos_provider.create_workos_client = forbidden

from opentour_control.auth.runtime import create_control_auth_runtime

assert callable(create_control_auth_runtime)
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=15, check=False
    )
    assert result.returncode == 0, result.stderr


def test_runtime_mounts_canonical_routers_and_refuses_missing_state(
    configured, identity, monkeypatch
):
    registry = schema_registry()
    error_contract = "urn:opentour:common-contract:1.0#/$defs/Error"
    fetch = Mock(return_value=identity.keys)
    provider_request = Mock(side_effect=AssertionError("Generated runtime must stay offline"))
    monkeypatch.setattr(workos_provider, "fetch_jwks", fetch)
    monkeypatch.setattr(workos_provider.BoundedWorkOSBackend, "request", provider_request)
    control = runtime.create_control_auth_runtime(**configured)
    try:
        assert isinstance(control.browser_state, BrowserAuthState)
        assert isinstance(control.session_service, SessionService)
        assert isinstance(control.selection, PostgresSessionSelection)
        assert control.browser_state.sessions is control.session_service
        assert control.session_service.repository.connect is configured["identity_connect"]
        assert control.selection.connect is configured["selection_connect"]
        assert control.session_service.authority.connect is configured["selection_connect"]
        fetch.assert_not_called()
        for name in ("identity_connect", "selection_connect"):
            configured[name].assert_not_called()
        app = FastAPI()
        app.include_router(control.router)
        assert {path: set(operations) for path, operations in app.openapi()["paths"].items()} == {
            CONTEXT: {"get"},
            SELECTION: {"post"},
        }
        with TestClient(app) as client:
            missing = client.get(CONTEXT)
            refused = client.post(
                SELECTION,
                json={"organization_id": "generated", "expected_session_generation": 0},
            )
            for response in (missing, refused):
                assert (response.status_code, response.json()["code"]) == (401, "SESSION_INVALID")
                assert valid_instance(error_contract, response.json(), registry)
            fetch.assert_not_called()
            now = int(datetime.now(UTC).timestamp())
            token = identity.token(iat=now - 10, auth_time=now - 20, exp=now + 120)
            unavailable = client.get(
                CONTEXT,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Cookie": "opentour-session=generated_" + "a" * 32,
                },
            )
            assert (unavailable.status_code, unavailable.json()["code"]) == (
                503,
                "IDENTITY_STATE_UNAVAILABLE",
            )
            assert valid_instance(error_contract, unavailable.json(), registry)
            assert unavailable.headers["cache-control"] == "no-store"
            assert "generated-private-identity-detail" not in unavailable.text
            assert token not in unavailable.text
            fetch.assert_called_once_with(identity.config)
            configured["identity_connect"].assert_called_once_with()
            configured["selection_connect"].assert_not_called()
    finally:
        assert control.close()
    provider_request.assert_not_called()


def test_private_browser_pilot_inventory_is_exact_and_bounded():
    assert isinstance(PRIVATE_BROWSER_PILOT_ROUTES, tuple)
    route_keys = tuple((route.path, route.operation) for route in PRIVATE_BROWSER_PILOT_ROUTES)
    assert route_keys == PRIVATE_PILOT_PATHS
    for route in PRIVATE_BROWSER_PILOT_ROUTES:
        assert (route.method, route.max_request_bytes, route.max_response_bytes) == (
            "POST",
            65_536,
            131_072,
        )
        assert lookup_private_browser_pilot_route("POST", route.path) is route


def test_private_browser_pilot_lookup_refuses_aliases_and_unadmitted_features():
    for path, _ in PRIVATE_PILOT_PATHS:
        for method, raw_path in (
            ("GET", path),
            ("post", path),
            ("POST", path.upper()),
            ("POST", path + "/"),
            ("POST", path + ".extra"),
            ("POST", path.replace("/internal/", "/%69nternal/", 1)),
            ("POST", path + "?probe=generated"),
            ("POST", "/dev" + path),
        ):
            assert lookup_private_browser_pilot_route(method, raw_path) is None

    for path in (
        "/internal/browser-identity/v1/sessions/refresh",
        "/internal/browser-identity/v1/sessions/replace-credentials",
        "/internal/browser-identity/v1/sessions/require-reconciliation",
        "/internal/browser-identity/v1/selection/authorize",
        "/internal/browser-identity/v1/selection/commit",
        SELECTION,
    ):
        assert lookup_private_browser_pilot_route("POST", path) is None


def test_public_control_factory_does_not_expose_private_browser_pilot(
    configured, provider_dependencies
):
    control = runtime.create_control_auth_runtime(**configured)
    try:
        app = create_control_api(control)
        assert {path: set(operations) for path, operations in app.openapi()["paths"].items()} == {
            CONTEXT: {"get"},
            SELECTION: {"post"},
        }
        with TestClient(app) as client:
            assert client.get(CONTEXT).status_code == 401
            assert client.post(SELECTION, json={}).status_code == 401
            for path, _ in PRIVATE_PILOT_PATHS:
                assert client.post(path, json={}).status_code == 404
        for name in ("identity_connect", "selection_connect"):
            configured[name].assert_not_called()
        for dependency in provider_dependencies:
            assert not dependency.mock_calls
    finally:
        assert control.close()


@pytest.mark.parametrize(
    ("field", "invalid"),
    [("workos_api_key", ""), ("sealing_keys", ()), ("csrf_key", b"short")],
)
def test_invalid_configuration_refuses_before_client_construction(
    configured, monkeypatch, field, invalid
):
    client_factory = Mock()
    monkeypatch.setattr(runtime, "create_workos_client", client_factory)
    configured[field] = invalid
    with pytest.raises(ValueError):
        runtime.create_control_auth_runtime(**configured)
    client_factory.assert_not_called()


@pytest.mark.parametrize(
    "fault",
    [
        "client_error",
        "backend_incomplete",
        "backend_error",
        "verifier_incomplete",
        "verifier_error",
        "none",
    ],
)
def test_shutdown_reports_incomplete_and_attempts_all_dependencies(
    configured, provider_dependencies, fault
):
    client, backend, verifier = provider_dependencies
    backend.close_complete.return_value = fault != "backend_incomplete"
    verifier.close.return_value = fault != "verifier_incomplete"
    if fault == "client_error":
        client.close.side_effect = RuntimeError("generated-client-close-detail")
    if fault == "backend_error":
        backend.close_complete.side_effect = RuntimeError("generated-backend-close-detail")
    if fault == "verifier_error":
        verifier.close.side_effect = RuntimeError("generated-verifier-close-detail")

    control = runtime.create_control_auth_runtime(**configured)
    assert control.close() is (fault == "none")
    client.close.assert_called_once()
    backend.close_complete.assert_called_once()
    verifier.close.assert_called_once()


def test_failed_route_mount_releases_constructed_provider_dependencies(
    configured, provider_dependencies, monkeypatch
):
    client, backend, verifier = provider_dependencies
    mount = Mock(side_effect=ValueError("generated route mount failure"))
    monkeypatch.setattr(runtime, "create_session_router", mount)

    with pytest.raises(ValueError, match="generated route mount failure"):
        runtime.create_control_auth_runtime(**configured)

    client.close.assert_called_once()
    backend.close_complete.assert_called_once()
    verifier.close.assert_called_once()
