"""Actual private PostgreSQL qualification; finite local profile only, never unit evidence."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentour_control.auth.config import AuthError, FernetSealingPort, digest
from opentour_control.auth.repository import (
    IdentityEvent,
    PostgresIdentityRepository,
    SessionRevocationEvent,
    connection_factory,
)
from opentour_control.auth.session import SessionService, create_session_router

from tools.fixtures.postgres import DisposablePostgres

TARGET = "services/control-api/tests/auth/test_identity_postgres.py"
ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture(scope="module")
def postgres():
    if os.environ.get("OPENTOUR_POSTGRES_TARGET") != TARGET:
        raise RuntimeError("Real PostgreSQL requires its registered local-integration target")
    fixture = DisposablePostgres()
    with fixture:
        migration = ROOT / "services/control-api/migrations/0001_identity_state.sql"
        fixture.sql(migration.read_text(), role="c03_owner")
        fixture.sql(
            "CREATE ROLE c03_runtime LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE; "
            "GRANT USAGE ON SCHEMA opentour_identity TO c03_runtime; "
            "GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA opentour_identity TO c03_runtime;",
            role="c03_owner",
        )
        fixture.receipt["identity_migration_sha256"] = hashlib.sha256(
            migration.read_bytes()
        ).hexdigest()
        yield fixture
    assert fixture.cleanup_complete


@pytest.fixture(params=["customer_controlled", "opentour_managed"])
def mode(request):
    return request.param


@pytest.fixture
def durable(postgres, identity, mode):
    # Each case has independent generated provider subject/session identities.
    sid = "session_" + uuid4().hex
    connect = connection_factory(
        f"host={postgres.socket} port=5432 dbname=postgres user=c03_runtime"
    )
    repository = PostgresIdentityRepository(connect)
    service = SessionService(
        identity.config,
        identity.verifier,
        repository,
        identity.service.authority,
        identity.provider,
        clock=lambda: identity.now,
    )
    context, binding = service.admit(identity.token(sid=sid))
    return SimpleNamespace(
        repository=repository,
        service=service,
        binding=binding,
        token=identity.token(sid=sid),
        context=context,
        sid=sid,
        connect=connect,
        mode=mode,
        postgres=postgres,
    )


def test_runtime_role_and_migration_are_real(durable):
    with durable.connect() as connection:
        role = connection.execute(
            "SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole FROM pg_roles "
            "WHERE rolname=current_user"
        ).fetchone()
        assert role and not any(role.values())
        assert connection.execute(
            "SELECT version FROM opentour_identity.schema_version"
        ).fetchone() == {"version": 1}
        assert connection.execute(
            "SELECT count(*) AS owned FROM pg_tables WHERE schemaname='opentour_identity' "
            "AND tableowner=current_user"
        ).fetchone() == {"owned": 0}
    with pytest.raises(psycopg.errors.InsufficientPrivilege), durable.connect() as connection:
        connection.execute("DELETE FROM opentour_identity.sessions")


def test_router_uses_committed_state_and_durable_end(durable, identity):
    app = FastAPI()
    app.include_router(create_session_router(durable.service))
    with TestClient(app) as client:
        client.cookies.set("opentour-session", durable.binding)
        first = client.get(
            "/v1/session/context", headers={"Authorization": f"Bearer {durable.token}"}
        )
        assert first.status_code == 200 and first.json()["state"] == "unselected"
        ended = durable.service.end(durable.token, durable.binding)
        assert ended["local_ended"] and ended["session_generation"] == 1
        assert durable.service.end(durable.token, durable.binding) == ended
        denied = client.get(
            "/v1/session/context", headers={"Authorization": f"Bearer {durable.token}"}
        )
        assert denied.status_code == 401
    restarted = PostgresIdentityRepository(durable.connect)
    with pytest.raises(AuthError, match="SESSION_ENDED"):
        restarted.load_session(digest(durable.binding), identity.now)
    with durable.connect() as connection:
        row = connection.execute(
            "SELECT deny_until FROM opentour_identity.session_denials WHERE session_id=%s",
            (durable.sid,),
        ).fetchone()
        assert row["deny_until"] == identity.now + timedelta(seconds=86_400)


def test_atomic_admission_credentials_and_late_refresh_denial(durable, identity):
    session = durable.repository.load_session(digest(durable.binding), identity.now)
    scope = f"credentials:{session.issuer}:{session.subject}:{session.session_id}"
    sealer = FernetSealingPort((Fernet.generate_key(),), 86_400)
    sealed = sealer.seal("GENERATED_REFRESH_SENTINEL", scope, identity.now, session.expires_at)
    durable.repository.replace_credentials(session, sealed, identity.now)
    assert (
        sealer.open(durable.repository.read_credentials(session, identity.now), scope, identity.now)
        == "GENERATED_REFRESH_SENTINEL"
    )
    with durable.connect() as connection:
        assert (
            "SENTINEL"
            not in connection.execute(
                "SELECT sealed_payload FROM opentour_identity.session_credentials "
                "WHERE session_id=%s",
                (durable.sid,),
            ).fetchone()["sealed_payload"]
        )
    durable.repository.end_session(session, identity.now)
    with pytest.raises(AuthError):
        durable.repository.replace_credentials(session, sealed, identity.now)
    with pytest.raises(AuthError):
        durable.repository.read_credentials(session, identity.now)
    with pytest.raises(AuthError):
        durable.repository.create_session(session, digest("late-binding"), sealed_payload=sealed)


def test_failed_admission_rolls_back_binding_and_session(durable, identity):
    session = durable.repository.load_session(digest(durable.binding), identity.now)
    proposed = replace(session, session_id="rollback_" + uuid4().hex)
    with pytest.raises(psycopg.errors.CheckViolation):
        durable.repository.create_session(proposed, digest("rollback"), sealed_payload="")
    with durable.connect() as connection:
        assert connection.execute(
            "SELECT count(*) AS n FROM opentour_identity.sessions WHERE session_id=%s",
            (proposed.session_id,),
        ).fetchone() == {"n": 0}


def test_duplicate_callback_claim_has_one_durable_winner(durable, identity):
    state, browser = digest(uuid4().hex), digest(uuid4().hex)
    sealed = FernetSealingPort((Fernet.generate_key(),), 600).seal(
        "generated PKCE verifier", state, identity.now, identity.now + timedelta(seconds=600)
    )
    durable.repository.create_auth_transaction(
        state, browser, sealed, identity.now, identity.now + timedelta(seconds=600)
    )

    def consume():
        try:
            return durable.repository.consume_auth_transaction(state, browser, identity.now)
        except AuthError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: consume(), range(2)))
    assert results.count(sealed) == 1 and results.count("CALLBACK_INVALID") == 1


def test_cursor_batch_rollback_replay_and_sticky_denial(durable, identity):
    issuer = identity.issuer + uuid4().hex
    observed = replace(
        identity.provider.current_membership("provider_a", identity.now), issuer=issuer
    )
    first = IdentityEvent("event_1", "a" * 64, identity.now, observed)
    conflict = IdentityEvent(
        "event_2", "b" * 64, identity.now, replace(observed, subject="foreign")
    )
    with pytest.raises(AuthError, match="IDENTITY_EVENT_SCOPE_CONFLICT"):
        durable.repository.commit_batch(issuer, None, "event_2", [first, conflict])
    assert durable.repository.cursor(issuer) is None
    assert durable.repository.observation(issuer, "provider_a") is None
    durable.repository.commit_batch(issuer, None, "event_1", [first])
    durable.repository.commit_batch(issuer, "event_1", "event_1", [first])
    removed = IdentityEvent("event_3", "c" * 64, identity.now, replace(observed, status="removed"))
    late = IdentityEvent("event_4", "d" * 64, identity.now, observed)
    durable.repository.commit_batch(issuer, "event_1", "event_4", [removed, late])
    assert durable.repository.observation(issuer, "provider_a").locally_denied
    with durable.connect() as connection:
        assert connection.execute(
            "SELECT count(*) AS n FROM opentour_identity.event_inbox WHERE issuer=%s", (issuer,)
        ).fetchone() == {"n": 3}
        assert connection.execute(
            "SELECT count(*) AS n FROM opentour_identity.outbox "
            "WHERE issuer=%s AND kind='identity.revocation_requested'",
            (issuer,),
        ).fetchone() == {"n": 2}


def test_unknown_session_revocation_prevents_future_admission(durable, identity):
    sid = "revoked_" + uuid4().hex
    event = SessionRevocationEvent(
        "event_" + uuid4().hex,
        "e" * 64,
        identity.now,
        identity.issuer,
        sid,
        "user_a",
        identity.now + timedelta(days=1),
    )
    cursor = durable.repository.cursor(identity.issuer)
    durable.repository.commit_batch(identity.issuer, cursor, event.event_id, [event])
    with pytest.raises(AuthError, match="SESSION_ENDED"):
        durable.service.admit(identity.token(sid=sid))


def test_stale_subject_generation_and_expired_credentials_refuse(durable, identity):
    session = durable.repository.load_session(digest(durable.binding), identity.now)
    for invalid in (replace(session, subject="foreign"), replace(session, generation=1)):
        with pytest.raises(AuthError):
            durable.repository.replace_credentials(invalid, "untrusted-ciphertext", identity.now)
    with pytest.raises(AuthError):
        durable.repository.replace_credentials(session, "untrusted-ciphertext", session.expires_at)


def test_configured_typescript_factories_with_actual_durable_ports(durable, identity):
    """Node SDK HTTP is generated; callback claims, sessions and denial commit to real PG."""
    pnpm, node = shutil.which("pnpm"), shutil.which("node")
    assert pnpm and node, "Qualified pinned Node/pnpm are required by this finite target"
    with tempfile.TemporaryDirectory(prefix="opentour-auth-port-") as directory:
        private = Path(directory)
        private.chmod(0o700)
        output = private / "compiled"
        subprocess.run(
            [
                pnpm,
                "exec",
                "tsc",
                "-p",
                "apps/web/lib/server/tsconfig.json",
                "--noEmit",
                "false",
                "--noEmitOnError",
                "--rootDir",
                "apps/web",
                "--outDir",
                str(output),
            ],
            cwd=ROOT,
            check=True,
            timeout=30,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        (output / "package.json").write_text('{"type":"module"}')
        (output / "lib/server/node_modules").symlink_to(
            ROOT / "apps/web/node_modules", target_is_directory=True
        )
        config = private / "fixture.json"
        config.write_text(
            json.dumps(
                {
                    "dsn": (
                        f"host={durable.postgres.socket} port=5432 dbname=postgres user=c03_runtime"
                    ),
                    "key": Fernet.generate_key().decode(),
                    "now": int(identity.now.timestamp() * 1000),
                    "issuer": identity.issuer,
                    "sid": "node_" + uuid4().hex,
                    "mode": durable.mode,
                }
            )
        )
        config.chmod(0o600)
        probe = subprocess.run(
            [
                node,
                "--conditions=react-server",
                str(Path(__file__).with_name("authkit_postgres_probe.mjs")),
                str(output / "lib/server/authkit-adapter.js"),
                sys.executable,
                str(Path(__file__).with_name("auth_postgres_bridge.py")),
                str(config),
            ],
            cwd=ROOT,
            timeout=45,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert len(probe.stdout) < 4096
        report = json.loads(probe.stdout)
        assert report["configured_factories"] == "PASS" and report["actual_postgresql"]
        assert report["provider_acceptance"] == "NOT_RUN" and report["mode"] == durable.mode
