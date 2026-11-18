"""Actual private PostgreSQL qualification; finite local profile only, never unit evidence."""

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
from opentour_control.auth.browser_state import (
    BrowserAuthState,
    BrowserAuthTransaction,
    BrowserCredentials,
    BrowserSelectionIntent,
    BrowserSelectionTarget,
)
from opentour_control.auth.config import AuthError, FernetSealingPort, digest, timestamp
from opentour_control.auth.repository import (
    IdentityEvent,
    PostgresIdentityRepository,
    SessionRevocationEvent,
    connection_factory,
    lock_membership_observation,
)
from opentour_control.auth.session import SessionService, create_session_router
from pydantic import ValidationError

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
        fixture.sql(
            "CREATE ROLE c03_selection LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE; "
            "GRANT USAGE ON SCHEMA opentour_identity TO c03_selection; "
            "GRANT SELECT ON opentour_identity.membership_observations TO c03_selection; "
            "CREATE SCHEMA generated_membership_authority; "
            "CREATE TABLE generated_membership_authority.activation_probe "
            "(probe_id text PRIMARY KEY, admitted boolean NOT NULL); "
            "GRANT USAGE ON SCHEMA generated_membership_authority TO c03_selection; "
            "GRANT SELECT,INSERT ON generated_membership_authority.activation_probe "
            "TO c03_selection;",
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


@pytest.fixture
def browser_state(durable):
    return BrowserAuthState(
        durable.service, FernetSealingPort((Fernet.generate_key(),), 86_400), os.urandom(32)
    )


def test_browser_flow_is_sealed_browser_bound_and_single_use(browser_state, durable, identity):
    flow = BrowserAuthTransaction(
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        browser_digest=digest(secrets.token_urlsafe(32)),
        verifier=secrets.token_urlsafe(32),
        expires_at=identity.now + timedelta(seconds=600),
        return_path="/signed-out",
        kind="signin",
        session_handle=None,
        subject=None,
        session_id=None,
        generation=None,
        selection_target=None,
    )
    combined = f"{flow.state}.{flow.nonce}"
    browser_state.create_flow(flow)
    with durable.connect() as connection:
        row = connection.execute(
            "SELECT sealed_payload FROM opentour_identity.auth_transactions WHERE state_sha256=%s",
            (digest(combined),),
        ).fetchone()
        assert row is not None
        assert all(part not in row["sealed_payload"] for part in (flow.state, flow.verifier))

    with pytest.raises(AuthError, match="CALLBACK_INVALID"):
        browser_state.consume_flow(combined, "b" * 64)

    def consume():
        try:
            return browser_state.consume_flow(combined, flow.browser_digest)
        except AuthError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: consume(), range(2)))
    assert results.count(flow) == 1
    assert results.count("CALLBACK_INVALID") == 1


@pytest.mark.parametrize("expired", [False, True])
def test_browser_switch_flow_binds_exact_intent(browser_state, durable, identity, expired):
    intent = BrowserSelectionIntent(
        organization_id="org_a", expected_generation=0, idempotency_key="generated-selection"
    )
    flow = BrowserAuthTransaction(
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        browser_digest=digest(secrets.token_urlsafe(32)),
        verifier=secrets.token_urlsafe(32),
        expires_at=identity.now + timedelta(seconds=1),
        return_path="/",
        kind="switch",
        session_handle=durable.binding,
        subject="user_a",
        session_id=durable.sid,
        generation=0,
        selection_target=BrowserSelectionTarget(
            organization_id="org_a",
            identity_organization_id="identity_org_a",
            identity_issuer=identity.issuer,
            membership_id="member_a",
            authority_revision="authority_a",
        ),
        selection_intent=intent,
    )
    fields = flow.model_dump()
    for invalid_intent in (
        None,
        intent.model_copy(update={"organization_id": "org_b"}),
        intent.model_copy(update={"expected_generation": 1}),
    ):
        with pytest.raises(ValidationError):
            BrowserAuthTransaction.model_validate({**fields, "selection_intent": invalid_intent})
    browser_state.create_flow(flow)
    combined = f"{flow.state}.{flow.nonce}"
    if expired:
        durable.service.clock = lambda: flow.expires_at
        with pytest.raises(AuthError, match="CALLBACK_INVALID"):
            browser_state.consume_flow(combined, flow.browser_digest)
    else:
        assert browser_state.consume_flow(combined, flow.browser_digest) == flow


@pytest.mark.parametrize("selected", [False, True])
def test_same_sid_callback_cannot_replace_initialized_credentials(
    browser_state, durable, identity, selected
):
    original = BrowserCredentials(
        access_token=durable.token,
        refresh_token="generated-original",
        user_id=durable.context.actor["subject"],
    )
    admitted = browser_state.admit(original)
    if selected:
        with durable.connect() as conn:
            conn.execute(
                "UPDATE opentour_identity.sessions SET generation=1, "
                "selected_organization_id='local', selected_membership_id='member' "
                "WHERE issuer=%s AND session_id=%s",
                (identity.issuer, durable.sid),
            )
            for table in ("session_bindings", "session_credentials"):
                conn.execute(
                    f"UPDATE opentour_identity.{table} SET generation=1 "
                    "WHERE issuer=%s AND session_id=%s",
                    (identity.issuer, durable.sid),
                )
    duplicate = BrowserCredentials(
        access_token=durable.token,
        refresh_token="generated-duplicate",
        user_id=durable.context.actor["subject"],
    )
    with pytest.raises(AuthError, match="STALE_SESSION_CONTEXT"):
        browser_state.admit(duplicate)
    current = browser_state.current(admitted.handle)
    assert current.generation == int(selected)
    assert current.credentials == original
    with durable.connect() as conn:
        count = conn.execute(
            "SELECT count(*) AS count FROM opentour_identity.session_bindings "
            "WHERE issuer=%s AND session_id=%s",
            (identity.issuer, durable.sid),
        ).fetchone()
    assert count["count"] == 2


def test_expired_browser_admission_rolls_back_credentials_and_binding(
    browser_state, durable, identity, monkeypatch
):
    sid = "session_" + uuid4().hex
    credentials = BrowserCredentials(
        access_token=identity.token(sid=sid),
        refresh_token="GENERATED_REFRESH",
        user_id=durable.context.actor["subject"],
    )
    write_credentials = durable.repository._write_credentials

    def write_then_expire(connection, session, sealed_payload):
        write_credentials(connection, session, sealed_payload)
        durable.service.clock = lambda: identity.now + timedelta(seconds=600)

    monkeypatch.setattr(durable.repository, "_write_credentials", write_then_expire)
    with pytest.raises(AuthError, match="SESSION_INVALID"):
        browser_state.admit(credentials)
    with durable.connect() as conn:
        for table in ("sessions", "session_credentials", "session_bindings"):
            assert conn.execute(
                f"SELECT 1 FROM opentour_identity.{table} WHERE issuer=%s AND session_id=%s",
                (identity.issuer, sid),
            ).fetchone() is None


def test_browser_session_round_trip_and_exact_refresh_receipt(browser_state, durable, identity):
    sid = "session_" + uuid4().hex
    original = BrowserCredentials(
        access_token=identity.token(sid=sid),
        refresh_token="GENERATED_INITIAL_REFRESH",
        user_id="user_a",
    )
    admitted = browser_state.admit(original)
    current = browser_state.current(admitted.handle)
    assert current == admitted
    assert admitted.credentials == original and admitted.identity is not None
    assert admitted.identity.wire() == {
        "actor": {"kind": "human", "subject": "user_a", "issuer": identity.issuer},
        "session_id": sid,
        "session_generation": 0,
        "identity_checked_at": timestamp(identity.now),
        "authentication_time": timestamp(identity.now - timedelta(seconds=120)),
        "assurance_level": "unknown",
    }
    assert admitted.csrf_token == current.csrf_token
    assert admitted.handle not in repr(admitted)
    context = durable.service.current_context(original.access_token, admitted.handle, "generated")
    assert context["state"] == "unselected" and context["organization_id"] is None

    replacement = BrowserCredentials(
        access_token=identity.token(sid=sid, org_id="identity_org_b"),
        refresh_token="GENERATED_REPLACEMENT_REFRESH",
        user_id="user_a",
    )
    with pytest.raises(AuthError, match="STALE_SESSION_CONTEXT"):
        browser_state.replace_credentials(admitted.handle, 1, replacement)
    assert browser_state.current(admitted.handle).credentials == original

    receipt = browser_state.replace_credentials(admitted.handle, 0, replacement)
    session = durable.repository.load_session(digest(admitted.handle), identity.now)
    ciphertext = durable.repository.read_credentials(session, identity.now)
    assert receipt == digest(ciphertext)
    assert replacement.refresh_token not in ciphertext
    assert replacement.access_token not in ciphertext
    assert browser_state.current(admitted.handle).credentials == replacement
    current_identity, claim, sealed_digest = browser_state.selection_evidence(
        replacement.access_token, admitted.handle
    )
    assert current_identity.session_generation == 0
    assert claim.organization_id == "identity_org_b"
    assert sealed_digest == receipt
    with pytest.raises(AuthError, match="CONTEXT_RECONCILIATION_REQUIRED"):
        browser_state.selection_evidence(original.access_token, admitted.handle)

    browser_state.require_reconciliation(admitted.handle, 0)
    assert browser_state.current(admitted.handle).reconciliation_required
    ended = browser_state.end(admitted.handle, 0)
    assert (ended.session_id, ended.generation) == (sid, 1)
    assert browser_state.end(admitted.handle, 0) == ended
    ended_view = browser_state.current(admitted.handle)
    assert ended_view.ended and ended_view.credentials is None and ended_view.identity is None
    assert ended_view.csrf_token == admitted.csrf_token
    with pytest.raises(AuthError, match="SESSION_ENDED"):
        browser_state.replace_credentials(admitted.handle, 0, replacement)
    with pytest.raises(AuthError, match="SESSION_ENDED"):
        browser_state.require_reconciliation(admitted.handle, 0)


def test_browser_admission_validates_signed_subject_and_cannot_confirm_unknown_commit(
    browser_state, durable, identity, monkeypatch
):
    sid = "session_" + uuid4().hex
    rejected = BrowserCredentials(
        access_token=identity.token(sid=sid), refresh_token="GENERATED_REFRESH", user_id="other"
    )
    with pytest.raises(AuthError, match="SESSION_INVALID"):
        browser_state.admit(rejected)
    with durable.connect() as connection:
        assert connection.execute(
            "SELECT count(*) AS n FROM opentour_identity.sessions WHERE session_id=%s", (sid,)
        ).fetchone() == {"n": 0}

    write = durable.repository.create_session

    def lost_ack(*args, **kwargs):
        write(*args, **kwargs)
        raise psycopg.OperationalError("GENERATED_DATABASE_SENTINEL")

    monkeypatch.setattr(durable.repository, "create_session", lost_ack)
    trusted = BrowserCredentials(
        access_token=identity.token(sid=sid), refresh_token="GENERATED_REFRESH", user_id="user_a"
    )
    with pytest.raises(AuthError, match="^SESSION_ADMISSION_UNCONFIRMED$"):
        browser_state.admit(trusted)
    with durable.connect() as connection:
        assert connection.execute(
            "SELECT s.session_id FROM opentour_identity.sessions s "
            "JOIN opentour_identity.session_credentials c USING (issuer, session_id) "
            "JOIN opentour_identity.session_bindings b USING (issuer, session_id) "
            "WHERE s.session_id=%s",
            (sid,),
        ).fetchone() == {"session_id": sid}


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
    """The exported account handlers use generated WorkOS replies and actual PostgreSQL."""
    pnpm, node = shutil.which("pnpm"), shutil.which("node")
    assert pnpm and node, "Qualified pinned Node/pnpm are required by this finite target"
    with tempfile.TemporaryDirectory(prefix="opentour-auth-port-") as directory:
        private = Path(directory)
        private.chmod(0o700)
        output = private / "compiled"
        web = ROOT / "apps/web"
        project = private / "tsconfig.json"
        project.write_text(
            json.dumps(
                {
                    "extends": str(web / "lib/server/tsconfig.json"),
                    "compilerOptions": {
                        "typeRoots": [str(ROOT / "node_modules/@types")],
                        "rootDir": str(web),
                        "outDir": str(output),
                        "noEmit": False,
                        "noEmitOnError": True,
                    },
                    "files": [
                        str(web / "features/account-entry/server.ts"),
                        str(web / "app/auth/signin/route.ts"),
                        str(web / "app/auth/callback/route.ts"),
                    ],
                    "include": [],
                }
            )
        )
        subprocess.run(
            [pnpm, "exec", "tsc", "-p", str(project)],
            cwd=ROOT,
            check=True,
            timeout=30,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        (output / "package.json").write_text('{"type":"module"}')
        (output / "node_modules").symlink_to(web / "node_modules", target_is_directory=True)
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
        bridge = Path(__file__).with_name("auth_postgres_bridge.py")
        probe = subprocess.run(
            [
                node,
                "--conditions=react-server",
                str(Path(__file__).with_name("authkit_postgres_probe.mjs")),
                str(output),
                sys.executable,
                str(bridge),
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
        assert report["exported_signin_callback"] and report["unconfigured_exports_refused"]
        assert report["pipe_database_readiness"] and report["pipe_invalid_commands_refused"]
        assert report["pipe_expired_proof_refused"]
        assert report["provider_acceptance"] == "NOT_RUN" and report["mode"] == durable.mode

        with tempfile.TemporaryDirectory(prefix="opentour-c03-", dir="/tmp") as absent:
            empty = Path(absent).resolve()
            socket = empty / "socket"
            socket.mkdir(mode=0o700)
            (empty / ".owned-c03-fixture").write_text(empty.name)
            unavailable = private / "unavailable.json"
            unavailable.touch(mode=0o600)
            unavailable.write_text(
                json.dumps(
                    {
                        **json.loads(config.read_text()),
                        "dsn": f"host={socket} port=5432 dbname=postgres user=c03_runtime",
                    }
                )
            )
            health = subprocess.run(
                [sys.executable, str(bridge), str(unavailable)],
                input=b'{"action":"health"}\n',
                timeout=10,
                check=True,
                capture_output=True,
            )
            assert not health.stderr
            assert json.loads(health.stdout) == {
                "error": {"code": "TEST_PORT_UNAVAILABLE", "status": 503}
            }


@pytest.fixture
def mapped_selection(durable, identity):
    """Generated171/172 authority mapping;142 session/observation persistence is actual PG."""
    provider_member = "provider_" + uuid4().hex
    identity.members["org_a"] = replace(
        identity.members["org_a"], provider_membership_id=provider_member
    )
    observed = replace(
        identity.provider.current_membership("provider_a", identity.now),
        membership_id=provider_member,
    )
    durable.service.provider = SimpleNamespace(current_membership=lambda *args: observed)
    with durable.connect() as connection:
        connection.execute(
            "UPDATE opentour_identity.sessions SET selected_organization_id=%s, "
            "selected_membership_id=%s WHERE issuer=%s AND session_id=%s",
            ("org_a", "member_a", identity.issuer, durable.sid),
        )
    return observed


def test_distinct_provider_mapping_returns_local_context_with_actual_pg(
    durable, identity, mapped_selection
):
    context = durable.service.authorization_context(durable.token, durable.binding)
    assert context.organization_id == "org_a"
    assert context.organization_id != mapped_selection.organization_id == "identity_org_a"
    view = durable.service.current_context(durable.token, durable.binding, "mapped_pg")
    assert view["state"] == "active" and view["organization_id"] == "org_a"
    observed = durable.repository.observation(identity.issuer, mapped_selection.membership_id)
    assert observed.organization_id == "identity_org_a"


def test_wrong_provider_mapping_never_clears_committed_local_selection(
    durable, identity, mapped_selection
):
    durable.repository.record_observation(mapped_selection)
    before = durable.repository.load_session(digest(durable.binding), identity.now)
    identity.members["org_a"] = replace(
        identity.members["org_a"], identity_organization_id="identity_org_b"
    )
    with pytest.raises(AuthError, match="CONTEXT_RECONCILIATION_REQUIRED"):
        durable.service.authorization_context(durable.token, durable.binding)
    view = durable.service.current_context(durable.token, durable.binding, "wrong_pg_mapping")
    assert view["state"] == "reconciliation_required" and view["organization_id"] is None
    assert durable.repository.load_session(digest(durable.binding), identity.now) == before


def test_mapping_changed_during_provider_wait_denies_actual_pg_context(
    durable, identity, mapped_selection
):
    def changed(*args):
        identity.members["org_a"] = replace(
            identity.members["org_a"], identity_organization_id="identity_org_b"
        )
        return mapped_selection

    durable.service.provider.current_membership = changed
    with pytest.raises(AuthError, match="STALE_SESSION_CONTEXT"):
        durable.service.authorization_context(durable.token, durable.binding)
    session = durable.repository.load_session(digest(durable.binding), identity.now)
    assert session.selected_organization_id == "org_a" and session.generation == 0


def test_wrong_provider_issuer_cannot_enter_actual_pg_observation(
    durable, identity, mapped_selection
):
    durable.service.provider.current_membership = lambda *args: replace(
        mapped_selection, issuer="https://other-identity.example.invalid/"
    )
    with pytest.raises(AuthError, match="IDENTITY_MEMBERSHIP_CONFLICT"):
        durable.service.authorization_context(durable.token, durable.binding)
    assert durable.repository.observation(identity.issuer, mapped_selection.membership_id) is None


@pytest.mark.parametrize("first", ["denial", "activation"])
@pytest.mark.parametrize("initial", ["absent", "active"])
def test_membership_observation_lock_linearizes_activation_and_denial(
    durable, identity, first, initial
):
    """Real lock/142 writer; generated activation table is not171's production SQL saga."""
    membership = "lock_" + uuid4().hex
    probe_id = uuid4().hex
    observed = replace(
        identity.provider.current_membership("provider_a", identity.now),
        membership_id=membership,
    )
    if initial == "active":
        durable.repository.record_observation(observed)
    selection_connect = connection_factory(
        f"host={durable.postgres.socket} port=5432 dbname=postgres user=c03_selection"
    )
    # The consumer can coordinate its transaction without receiving142 UPDATE authority.
    with pytest.raises(psycopg.errors.InsufficientPrivilege), selection_connect() as connection:
        connection.execute("UPDATE opentour_identity.membership_observations SET status='active'")
    ready, release = threading.Event(), threading.Event()
    started = {name: threading.Event() for name in ["activation", "denial"]}
    pids = {}

    @contextmanager
    def writer_connection():
        with durable.connect() as connection:
            connection.execute("SET LOCAL statement_timeout='5s'")
            pids["denial"] = connection.info.backend_pid
            started["denial"].set()
            yield connection
            if first == "denial":
                ready.set()
                assert release.wait(4), "Test did not release denial transaction"

    writer = PostgresIdentityRepository(writer_connection)

    def denial():
        return writer.record_observation(replace(observed, status="removed", locally_denied=True))

    def activation():
        with selection_connect() as connection:
            connection.execute("SET LOCAL statement_timeout='5s'")
            pids["activation"] = connection.info.backend_pid
            started["activation"].set()
            lock_membership_observation(connection, identity.issuer, membership)
            row = connection.execute(
                "SELECT status,locally_denied FROM opentour_identity.membership_observations "
                "WHERE issuer=%s AND membership_id=%s", (identity.issuer, membership)
            ).fetchone()
            admitted = row is None or (not row["locally_denied"] and row["status"] == "active")
            connection.execute(
                "INSERT INTO generated_membership_authority.activation_probe VALUES (%s,%s)",
                (probe_id, admitted),
            )
            if first == "activation":
                ready.set()
                assert release.wait(4), "Test did not release activation transaction"
        return admitted

    actions = {"activation": activation, "denial": denial}
    second = "activation" if first == "denial" else "denial"
    with ThreadPoolExecutor(max_workers=2) as pool:
        earlier = pool.submit(actions[first])
        later = None
        try:
            assert ready.wait(3), "First transaction did not reach its commit boundary"
            later = pool.submit(actions[second])
            assert started[second].wait(2)
            deadline = time.monotonic() + 2
            blocked = False
            while time.monotonic() < deadline:
                with durable.connect() as connection:
                    blocked = connection.execute(
                        "SELECT count(*) AS n FROM pg_locks WHERE pid=%s "
                        "AND locktype='advisory' AND NOT granted", (pids[second],)
                    ).fetchone()["n"] == 1
                if blocked:
                    break
                time.sleep(0.01)
            assert blocked and not later.done(), "Second transaction bypassed the membership lock"
        finally:
            release.set()
        first_result = earlier.result(timeout=6)
        second_result = later.result(timeout=6)
    admitted = first_result if first == "activation" else second_result
    assert admitted is (first == "activation")
    assert durable.repository.observation(identity.issuer, membership).locally_denied
    with selection_connect() as connection:
        assert connection.execute(
            "SELECT admitted FROM generated_membership_authority.activation_probe "
            "WHERE probe_id=%s",
            (probe_id,),
        ).fetchone()["admitted"] is admitted
