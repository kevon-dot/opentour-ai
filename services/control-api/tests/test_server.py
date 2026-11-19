"""Control-service startup and probe wiring with generated configuration."""

from __future__ import annotations

import base64
import json
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import Mock

import psycopg
import pytest
import uvicorn
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from opentour_control.api import server
from opentour_control.auth.repository import PostgresIdentityRepository
from opentour_control.auth.runtime import ControlAuthRuntime
from psycopg.conninfo import conninfo_to_dict


@pytest.fixture
def deployment() -> dict[str, str]:
    database = "@generated-database.example/control?sslmode=verify-full"
    return {
        "OPENTOUR_CONTROL_DATABASE_URL": "postgresql://identity" + database,
        "OPENTOUR_SELECTION_DATABASE_URL": "postgresql://selection" + database,
        "OPENTOUR_SCHEMA_READER_VERSION": "1",
        "OPENTOUR_DATABASE_POOL_MAX": "20",
        "OPENTOUR_WORKOS_ISSUER": "https://identity.example/",
        "OPENTOUR_WORKOS_AUDIENCE": "https://control.example/",
        "OPENTOUR_WORKOS_JWKS_URL": "https://identity.example/jwks",
        "OPENTOUR_WORKOS_CLIENT_ID": "generated-client",
        "OPENTOUR_WORKOS_API_KEY": "test-only",
        "OPENTOUR_CREDENTIAL_MAX_LIFETIME_SECONDS": "600",
        "OPENTOUR_SESSION_SEALING_KEYS": json.dumps([Fernet.generate_key().decode()]),
        "OPENTOUR_SESSION_CSRF_KEY": base64.urlsafe_b64encode(b"k" * 32).decode(),
    }


def test_server_uses_scoped_database_profiles_and_sanitizes_connection_failure(
    deployment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    connect = Mock(side_effect=psycopg.OperationalError("generated database diagnostic"))
    monkeypatch.setattr(psycopg, "connect", connect)
    runtime, schema_connect, version, budget = server._configure(deployment)
    try:
        assert (version, budget) == (1, 20)
        connect.assert_not_called()
        app = server.create_control_service(runtime, schema_connect, version)
        with TestClient(app) as client:
            response = client.get("/health/ready?reader_version=2")
            assert (response.status_code, response.json()) == (503, {"status": "unavailable"})
            assert response.headers["cache-control"] == "no-store"
            assert client.get("/v1/session/context").json()["code"] == "SESSION_INVALID"
            assert client.get("/openapi.json").status_code == 404
        assert "/health/ready" not in app.openapi()["paths"]
        probe = connect.call_args
        assert conninfo_to_dict(probe.args[0])["user"] == "identity"
        assert conninfo_to_dict(probe.args[0])["sslmode"] == "verify-full"
        assert conninfo_to_dict(probe.args[0])["gssencmode"] == "disable"
        assert probe.kwargs["autocommit"] is True
        assert probe.kwargs["connect_timeout"] == 3
        assert "default_transaction_read_only=on" in probe.kwargs["options"]
        repository = runtime.session_service.repository
        assert isinstance(repository, PostgresIdentityRepository)
        for connector, role in (
            (repository.connect, "identity"),
            (runtime.selection.connect, "selection"),
        ):
            with pytest.raises(psycopg.OperationalError):
                connector()
            assert conninfo_to_dict(connect.call_args.args[0])["user"] == role
    finally:
        assert runtime.close()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OPENTOUR_SCHEMA_READER_VERSION", "01"),
        ("OPENTOUR_DATABASE_POOL_MAX", "1001"),
        ("OPENTOUR_WORKOS_ISSUER", "http://identity.example/"),
        ("OPENTOUR_SESSION_SEALING_KEYS", "[]"),
        ("OPENTOUR_SESSION_CSRF_KEY", "invalid"),
        ("OPENTOUR_CONTROL_DATABASE_URL", "postgresql://identity@other.example/control"),
        (
            "OPENTOUR_SELECTION_DATABASE_URL",
            "postgresql://other@other.example/control?sslmode=verify-full",
        ),
        (
            "OPENTOUR_SELECTION_DATABASE_URL",
            "postgresql://identity@generated-database.example/control?sslmode=verify-full",
        ),
    ],
)
def test_malformed_deployment_configuration_refuses_start(
    name: str,
    value: str,
    deployment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, setting in {**deployment, name: value}.items():
        monkeypatch.setenv(key, setting)
    start = Mock()
    monkeypatch.setattr(uvicorn.Server, "run", start)
    with pytest.raises(SystemExit, match="^Control API deployment configuration is missing"):
        server.main()
    start.assert_not_called()


@pytest.mark.parametrize("cleanup_complete", [True, False])
def test_entrypoint_closes_the_auth_runtime_before_signal_replay(
    deployment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    cleanup_complete: bool,
) -> None:
    for key, setting in deployment.items():
        monkeypatch.setenv(key, setting)
    close = Mock(return_value=cleanup_complete)
    monkeypatch.setattr(ControlAuthRuntime, "close", close)

    @contextmanager
    def capture(_: uvicorn.Server) -> Iterator[None]:
        yield
        close.assert_called_once()
        raise SystemExit(-signal.SIGTERM)

    def run(listener: uvicorn.Server) -> None:
        assert listener.config.port == 8000
        assert listener.config.workers == 1
        assert listener.config.limit_concurrency == 21
        assert listener.config.proxy_headers is False
        assert listener.config.timeout_graceful_shutdown == 20
        with listener.capture_signals():
            close.assert_not_called()

    monkeypatch.setattr(uvicorn.Server, "capture_signals", capture)
    monkeypatch.setattr(uvicorn.Server, "run", run)
    with pytest.raises(SystemExit) as terminated:
        server.main()
    assert terminated.value.code == -signal.SIGTERM
    close.assert_called_once()
    assert ("Control auth runtime shutdown is incomplete" in caplog.text) is not cleanup_complete
