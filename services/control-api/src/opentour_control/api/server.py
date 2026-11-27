"""Run the control API with a trusted deployment profile and live schema readiness."""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Callable, Mapping
from typing import Any

import psycopg
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from opentour_control.api.schema import create_control_api
from opentour_control.auth.config import AuthError, IdentityConfig
from opentour_control.auth.repository import connection_factory
from opentour_control.auth.runtime import ControlAuthRuntime, create_control_auth_runtime
from opentour_control.auth.runtime_server import serve_auth_runtime
from opentour_control.platform import schema_compatibility
from opentour_control.tenants.models import TermsView
from opentour_control.tenants.repository import read_public_organization_terms
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row


def create_control_service(
    runtime: ControlAuthRuntime,
    schema_connect: Callable[[], psycopg.Connection[Any]],
    reader_version: int,
    *,
    read_terms: Callable[[], TermsView] | None = None,
) -> FastAPI:
    """Attach the infrastructure probe to the registered account-entry API."""
    app = create_control_api(runtime, read_terms=read_terms)

    @app.get("/health/ready", include_in_schema=False)
    def readiness() -> JSONResponse:
        headers = {"Cache-Control": "no-store"}
        try:
            with schema_connect() as connection:
                schema_compatibility.readiness(connection, reader_version)
        except (psycopg.Error, schema_compatibility.CompatibilityError):
            return JSONResponse({"status": "unavailable"}, status_code=503, headers=headers)
        return JSONResponse({"status": "ready"}, headers=headers)

    return app


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise ValueError(f"Missing deployment setting {name}")
    return value


def _positive_integer(env: Mapping[str, str], name: str) -> int:
    value = _required(env, name)
    if not re.fullmatch(r"[1-9][0-9]{0,9}", value) or int(value) > 2_147_483_647:
        raise ValueError(f"Invalid deployment setting {name}")
    return int(value)


def _database_profile(dsn: str) -> dict[str, str]:
    profile = {key: str(value) for key, value in conninfo_to_dict(dsn).items() if value is not None}
    host = profile.get("host", "")
    if (
        not all(profile.get(key) for key in ("host", "dbname", "user"))
        or not re.fullmatch(r"[A-Za-z0-9.-]+", host)
        or any(key in profile for key in ("service", "hostaddr"))
        or profile.get("sslmode") != "verify-full"
    ):
        raise ValueError("Configure one explicit TLS-verified PostgreSQL endpoint")
    profile.setdefault("port", "5432")
    if not re.fullmatch(r"[1-9][0-9]{0,4}", profile["port"]) or int(profile["port"]) > 65535:
        raise ValueError("Invalid PostgreSQL port")
    # GSS encryption is preferred over TLS by libpq unless explicitly disabled.
    profile["gssencmode"] = "disable"
    return profile


def _session_keys(env: Mapping[str, str]) -> tuple[tuple[bytes, ...], bytes]:
    sealing_keys = json.loads(_required(env, "OPENTOUR_SESSION_SEALING_KEYS"))
    if not isinstance(sealing_keys, list) or not all(isinstance(key, str) for key in sealing_keys):
        raise ValueError("Configure a JSON array of session sealing keys")
    csrf_key = _required(env, "OPENTOUR_SESSION_CSRF_KEY").encode("ascii")
    decoded = base64.b64decode(csrf_key, altchars=b"-_", validate=True)
    if len(decoded) != 32 or base64.urlsafe_b64encode(decoded) != csrf_key:
        raise ValueError("Configure a URL-safe base64-encoded 32-byte CSRF key")
    return tuple(key.encode("ascii") for key in sealing_keys), decoded


def configure_auth_runtime(env: Mapping[str, str]) -> tuple[ControlAuthRuntime, str]:
    """Build the shared auth runtime and return its trusted identity database DSN."""
    identity = _database_profile(_required(env, "OPENTOUR_CONTROL_DATABASE_URL"))
    selection = _database_profile(_required(env, "OPENTOUR_SELECTION_DATABASE_URL"))
    if (
        any(identity[key] != selection[key] for key in ("host", "port", "dbname"))
        or identity["user"] == selection["user"]
    ):
        raise ValueError("Identity and selection require distinct principals on one database")

    identity_dsn = make_conninfo(**identity)
    selection_dsn = make_conninfo(**selection)

    sealing_keys, csrf_key = _session_keys(env)
    runtime = create_control_auth_runtime(
        config=IdentityConfig(
            issuer=_required(env, "OPENTOUR_WORKOS_ISSUER"),
            audience=_required(env, "OPENTOUR_WORKOS_AUDIENCE"),
            jwks_url=_required(env, "OPENTOUR_WORKOS_JWKS_URL"),
            client_id=_required(env, "OPENTOUR_WORKOS_CLIENT_ID"),
            credential_max_lifetime_seconds=_positive_integer(
                env, "OPENTOUR_CREDENTIAL_MAX_LIFETIME_SECONDS"
            ),
        ),
        workos_api_key=_required(env, "OPENTOUR_WORKOS_API_KEY"),
        sealing_keys=sealing_keys,
        csrf_key=csrf_key,
        identity_connect=connection_factory(identity_dsn),
        selection_connect=connection_factory(selection_dsn),
    )
    return runtime, identity_dsn


def _configure_terms_reader(env: Mapping[str, str]) -> Callable[[], TermsView] | None:
    if "OPENTOUR_TERMS_DATABASE_URL" not in env:
        return None
    terms = _database_profile(_required(env, "OPENTOUR_TERMS_DATABASE_URL"))
    identity = _database_profile(_required(env, "OPENTOUR_CONTROL_DATABASE_URL"))
    selection = _database_profile(_required(env, "OPENTOUR_SELECTION_DATABASE_URL"))
    if any(terms[key] != identity[key] for key in ("host", "port", "dbname")) or terms["user"] in {
        identity["user"],
        selection["user"],
    }:
        raise ValueError("Terms reads require their own principal on the control database")
    dsn = make_conninfo(**terms)

    def connect() -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(
            dsn,
            row_factory=dict_row,
            connect_timeout=3,
            options="-c statement_timeout=2000 -c default_transaction_read_only=on",
        )

    def read() -> TermsView:
        return read_public_organization_terms(connect)

    return read


def _configure(
    env: Mapping[str, str],
) -> tuple[ControlAuthRuntime, Callable[[], psycopg.Connection[Any]], int, int]:
    reader_version = _positive_integer(env, "OPENTOUR_SCHEMA_READER_VERSION")
    connection_limit = _positive_integer(env, "OPENTOUR_DATABASE_POOL_MAX")
    if connection_limit > 1000:
        raise ValueError("Connection limit exceeds the deployment contract")
    runtime, identity_dsn = configure_auth_runtime(env)

    def schema_connect() -> psycopg.Connection[Any]:
        return psycopg.connect(
            identity_dsn,
            autocommit=True,
            connect_timeout=3,
            options="-c statement_timeout=2000 -c default_transaction_read_only=on",
        )

    return runtime, schema_connect, reader_version, connection_limit


def main() -> None:
    try:
        read_terms = _configure_terms_reader(os.environ)
        runtime, schema_connect, reader_version, connection_limit = _configure(os.environ)
    except (AuthError, ValueError, psycopg.Error):
        raise SystemExit("Control API deployment configuration is missing or invalid") from None

    def configure() -> uvicorn.Config:
        app = create_control_service(runtime, schema_connect, reader_version, read_terms=read_terms)
        # Each admitted handler holds at most one connection at a time. Uvicorn
        # counts the incoming connection in the concurrency threshold as well.
        return uvicorn.Config(
            app,
            host="0.0.0.0",
            port=8000,
            workers=1,
            http="h11",
            ws="none",
            lifespan="on",
            limit_concurrency=connection_limit + 1,
            proxy_headers=False,
            access_log=False,
        )

    serve_auth_runtime(runtime, configure)


if __name__ == "__main__":
    main()
