"""Actual disposable PostgreSQL, generated identities and provider claims."""

import json
import secrets
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import jwt
import psycopg
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from opentour_control.api.schema import create_control_api
from opentour_control.auth import workos_provider
from opentour_control.auth.browser_state import (
    BrowserAuthTransaction,
    BrowserCredentials,
    BrowserSelectionIntent,
    BrowserSelectionTarget,
)
from opentour_control.auth.config import MAX_COUNTER, AuthError, IdentityConfig, digest, timestamp
from opentour_control.auth.repository import PostgresIdentityRepository, connection_factory
from opentour_control.auth.runtime import create_control_auth_runtime
from opentour_control.auth.session import (
    SessionService,
    VerifiedIdentityContext,
    create_session_router,
)
from opentour_control.auth.workos_provider import VerifiedToken
from opentour_control.tenants.session_selection import (
    PostgresSelectionMembershipAuthority,
    PostgresSessionSelection,
    SelectionReceipt,
    SelectionTarget,
    SessionSelectionRequest,
)
from psycopg.types.json import Jsonb

from tools.fixtures.postgres import DisposablePostgres
from tools.schema_validate import schema_registry, valid_instance

ROOT = Path(__file__).resolve().parents[4]
ISSUER = "https://identity.example.invalid"
GENERATED_SEALED = "generated-sealed-envelope"
pytestmark = pytest.mark.local_integration


@pytest.fixture(scope="module")
def postgres():
    fixture = DisposablePostgres()
    with fixture:
        fixture.sql(
            "CREATE ROLE c172_migration LOGIN CREATEROLE NOSUPERUSER NOBYPASSRLS;"
            "GRANT CREATE ON DATABASE postgres TO c172_migration;",
            role="c03_owner",
        )
        with psycopg.connect(
            host=str(fixture.socket),
            dbname="postgres",
            user="c172_migration",
            autocommit=True,
            options="-c statement_timeout=5000 -c lock_timeout=2000",
        ) as owner:
            for name in (
                "0001_identity_state.sql",
                "0002_organization_creation.sql",
                "0003_authority_grants.sql",
                "0004_session_selection.sql",
            ):
                migration = ROOT / "services/control-api/migrations" / name
                owner.execute(migration.read_text())
        fixture.sql(
            "CREATE ROLE c172_runtime LOGIN NOSUPERUSER NOBYPASSRLS "
            "IN ROLE opentour_selection_runtime;"
            "CREATE ROLE c172_identity LOGIN NOSUPERUSER NOBYPASSRLS;"
            "GRANT USAGE ON SCHEMA opentour_identity TO c172_identity;"
            "GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA "
            "opentour_identity TO c172_identity;",
            role="c03_owner",
        )
        yield fixture
    assert fixture.cleanup_complete


@pytest.fixture(params=["customer_controlled", "opentour_managed"])
def mode(request):
    return request.param


@pytest.fixture
def durable(postgres, mode):
    suffix = uuid4().hex
    now = datetime.now(UTC)
    subject = "generated-subject-" + suffix
    sid = "generated-session-" + suffix
    binding = "binding_" + suffix
    other = "other_binding_" + suffix
    owner = connection_factory(f"host={postgres.socket} dbname=postgres user=c03_owner")
    selector = PostgresSessionSelection(
        connection_factory(f"host={postgres.socket} dbname=postgres user=c172_runtime")
    )
    authority = PostgresSelectionMembershipAuthority(selector.connect, ISSUER)
    identity_repository = PostgresIdentityRepository(
        connection_factory(f"host={postgres.socket} dbname=postgres user=c172_identity")
    )
    identity = VerifiedIdentityContext(
        {"kind": "human", "issuer": ISSUER, "subject": subject},
        sid,
        0,
        timestamp(now),
        timestamp(now),
    )
    orgs = {}
    valid_until = now + timedelta(minutes=10)
    with owner() as conn:
        conn.execute(
            "INSERT INTO opentour_tenants.terms_versions "
            "VALUES ('generated-terms', 'https://example.invalid/terms', %s) "
            "ON CONFLICT DO NOTHING",
            (now,),
        )
        conn.execute(
            "INSERT INTO opentour_identity.sessions "
            "(issuer,session_id,subject,created_at,expires_at,deny_until) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (ISSUER, sid, subject, now, valid_until, valid_until),
        )
        for candidate in (binding, other):
            conn.execute(
                "INSERT INTO opentour_identity.session_bindings VALUES (%s,%s,%s,0,%s)",
                (digest(candidate), ISSUER, sid, valid_until),
            )
        conn.execute(
            "INSERT INTO opentour_identity.session_credentials VALUES (%s,%s,0,%s,%s)",
            (ISSUER, sid, GENERATED_SEALED, valid_until),
        )
        for letter in ("a", "b"):
            org, membership, remote_org, remote_member = (
                f"{label}-{mode}-{letter}-{suffix}"
                for label in ("local", "membership", "identity-org", "identity-membership")
            )
            orgs[letter] = SimpleNamespace(
                local=org, membership=membership, remote_org=remote_org, remote_member=remote_member
            )
            conn.execute(
                "INSERT INTO opentour_tenants.organizations "
                "VALUES (%s,%s,'Generated',%s,%s,'generated-terms',%s,'active',1,%s,%s)",
                (org, remote_org, ISSUER, subject, now, now, now),
            )
            conn.execute(
                "INSERT INTO opentour_tenants.memberships "
                "VALUES(%s,%s,%s,%s,%s,'active','owner',1,%s)",
                (org, ISSUER, subject, membership, remote_member, now),
            )
            conn.execute(
                "INSERT INTO opentour_authority.organizations "
                "VALUES(%s,%s,3,NULL,'current',%s,%s,%s,%s)",
                (org, "authority-one", Jsonb({}), Jsonb({}), Jsonb({}), now),
            )
            conn.execute(
                "INSERT INTO opentour_identity.membership_observations "
                "VALUES(%s,%s,%s,%s,'active',false,%s,%s,1)",
                (ISSUER, remote_member, remote_org, subject, now, now),
            )
    return SimpleNamespace(
        now=now,
        owner=owner,
        selector=selector,
        authority=authority,
        identity=identity,
        identity_repository=identity_repository,
        binding=binding,
        other=other,
        orgs=orgs,
    )


def counts(durable, session_id=None):
    with durable.owner() as conn:
        return tuple(
            conn.execute(
                f"SELECT count(*) AS n FROM opentour_selection.{table} WHERE session_id=%s",
                (session_id or durable.identity.session_id,),
            ).fetchone()["n"]
            for table in ("receipts", "outbox")
        )


def request(durable, letter="a", generation=0):
    return SessionSelectionRequest(durable.orgs[letter].local, generation)


def claims(durable, letter="a"):
    return VerifiedToken(
        ISSUER,
        durable.identity.actor["subject"],
        durable.identity.session_id,
        durable.orgs[letter].remote_org,
        durable.now,
        durable.now + timedelta(minutes=5),
    )


def commit(
    durable,
    requested,
    key,
    target,
    verified=None,
    *,
    sealed=GENERATED_SEALED,
    request_id="request-original",
    identity=None,
):
    assert isinstance(target, SelectionTarget)
    return durable.selector.commit(
        identity or durable.identity,
        durable.binding,
        requested,
        key,
        target,
        verified or claims(durable),
        digest(sealed),
        request_id,
    )


def expect(code, action):
    with pytest.raises(AuthError, match=code) as failure:
        action()
    return failure.value


def session_service(durable, verified):
    config = IdentityConfig(
        ISSUER,
        "https://api.example.invalid",
        "https://api.workos.com/jwks/test",
        "client_test",
        3600,
    )
    return SessionService(
        config,
        SimpleNamespace(verify=lambda _token, _now: verified),
        durable.identity_repository,
        durable.authority,
        SimpleNamespace(current_membership=lambda *_: pytest.fail("unexpected provider read")),
    )


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"organization_id": "a", "expected_session_generation": True},
        {"organization_id": "a", "expected_session_generation": 1.0},
        {"organization_id": "a", "expected_session_generation": "1"},
        {"organization_id": "a", "expected_session_generation": -1},
        {"organization_id": "a", "expected_session_generation": MAX_COUNTER + 1},
        {"organization_id": "a", "expected_session_generation": 1, "workspace_id": "foreign"},
    ],
)
def test_request_is_closed_and_exact(body):
    assert expect("SCHEMA_INVALID", lambda: SessionSelectionRequest.parse(body)).status == 422


def test_commit_mounts_active_context_and_fences_other_binding(durable):
    requested = request(durable)
    selector = durable.selector
    target = selector.admit(durable.identity, durable.binding, requested, "key-a")
    assert target == SelectionTarget(
        requested.organization_id,
        durable.orgs["a"].remote_org,
        ISSUER,
        durable.orgs["a"].membership,
        "authority-one",
    )
    verified = claims(durable)
    app = FastAPI()
    app.include_router(create_session_router(session_service(durable, verified)))
    with TestClient(app) as client:
        client.cookies.set("opentour-session", durable.binding)
        path = "/v1/session/context"
        headers = {"Authorization": "Bearer generated"}
        assert client.get(path, headers=headers).json()["state"] == "unselected"
        receipt = commit(durable, requested, "key-a", target)
        selected = client.get(path, headers=headers)
        assert selected.status_code == 200
        assert {key: value for key, value in selected.json().items() if key != "request_id"} == {
            key: value for key, value in receipt.wire().items() if key != "request_id"
        }
    assert receipt == SelectionReceipt(
        requested.organization_id, target.membership_id, 1, "request-original"
    )
    mapping = durable.authority.current(
        durable.identity.actor["subject"], requested.organization_id
    )
    assert mapping and (mapping.provider_membership_id, mapping.identity_organization_id) == (
        durable.orgs["a"].remote_member,
        durable.orgs["a"].remote_org,
    )
    assert mapping.organization_id != mapping.identity_organization_id
    row = durable.identity_repository.load_session(digest(durable.binding), datetime.now(UTC))
    assert durable.identity_repository.read_credentials(row, datetime.now(UTC)) == GENERATED_SEALED
    expect(
        "STALE_SESSION_CONTEXT",
        lambda: durable.identity_repository.load_session(digest(durable.other), datetime.now(UTC)),
    )
    assert counts(durable) == (1, 1)


@contextmanager
def generated_browser_auth(durable, monkeypatch):
    subject = durable.identity.actor["subject"]
    org = durable.orgs["a"]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="generated", alg="RS256", use="sig")
    fetched = []

    def fetch(config):
        fetched.append(config.issuer)
        return {"keys": [public]}

    def unexpected_provider_request(*args, **kwargs):
        pytest.fail("Generated composition test must not use provider networking")

    monkeypatch.setattr(workos_provider, "fetch_jwks", fetch)
    monkeypatch.setattr(
        workos_provider.BoundedWorkOSBackend, "request", unexpected_provider_request
    )
    config = IdentityConfig(
        ISSUER,
        "https://api.example.invalid",
        "https://api.workos.com/jwks/test",
        "client_test",
        3600,
    )
    now = int(durable.now.timestamp())
    claims = {
        "iss": ISSUER,
        "aud": config.audience,
        "sub": subject,
        "sid": "generated-runtime-" + uuid4().hex,
        "org_id": org.remote_org,
        "auth_time": now - 60,
        "iat": now - 30,
        "exp": now + 300,
    }

    def issue_token(organization_id, **changes):
        return jwt.encode(
            {**claims, "org_id": organization_id, **changes},
            key,
            algorithm="RS256",
            headers={"kid": "generated"},
        )

    token = issue_token(org.remote_org)
    other = issue_token(durable.orgs["b"].remote_org)
    runtime = create_control_auth_runtime(
        config=config,
        workos_api_key="test-only",
        sealing_keys=(Fernet.generate_key(),),
        csrf_key=b"g" * 32,
        identity_connect=durable.identity_repository.connect,
        selection_connect=durable.selector.connect,
    )
    assert fetched == []
    try:
        yield SimpleNamespace(
            runtime=runtime,
            token=token,
            other=other,
            expires_at=claims["exp"],
            fetched=fetched,
            issue_token=issue_token,
        )
    finally:
        assert runtime.close()


def test_composed_control_auth_routes_share_the_durable_session(durable, monkeypatch):
    registry = schema_registry()
    context_contract = "urn:opentour:auth-context:1.0#/$defs/SessionContextView"
    error_contract = "urn:opentour:common-contract:1.0#/$defs/Error"
    subject = durable.identity.actor["subject"]
    org = durable.orgs["a"]
    with generated_browser_auth(durable, monkeypatch) as auth:
        runtime = auth.runtime
        browser = runtime.browser_state.admit(
            BrowserCredentials(
                access_token=auth.token, refresh_token="generated-refresh", user_id=subject
            )
        )
        app = create_control_api(runtime)
        headers = {
            "Authorization": f"Bearer {auth.token}",
            "Cookie": f"opentour-session={browser.handle}",
            "Idempotency-Key": "generated-runtime-selection",
        }
        context_path = "/v1/session/context"
        selection_path = "/v1/session:select-organization"
        assert Counter(
            (route.path, method)
            for route in iter_route_contexts(app.routes)
            for method in route.methods or ()
        ) == {(context_path, "GET"): 1, (selection_path, "POST"): 1}
        body = {"organization_id": org.local, "expected_session_generation": 0}
        with TestClient(app) as client:
            malformed = client.post(
                selection_path, json={**body, "unexpected": "generated"}, headers=headers
            )
            assert (malformed.status_code, malformed.json()["code"]) == (422, "SCHEMA_INVALID")
            assert valid_instance(error_contract, malformed.json(), registry)
            unselected = client.get(context_path, headers=headers)
            assert (unselected.status_code, unselected.json()["state"]) == (200, "unselected")
            assert unselected.json()["session_generation"] == 0
            assert valid_instance(context_contract, unselected.json(), registry)
            denied = client.post(
                selection_path,
                json=body,
                headers={**headers, "Authorization": f"Bearer {auth.other}"},
            )
            assert (denied.status_code, denied.json()["code"]) == (
                409,
                "CONTEXT_RECONCILIATION_REQUIRED",
            )
            assert valid_instance(error_contract, denied.json(), registry)
            selected = client.post(selection_path, json=body, headers=headers)
            assert selected.status_code == 200
            receipt = selected.json()
            assert valid_instance(context_contract, receipt, registry)
            assert receipt == {
                "state": "active",
                "organization_id": org.local,
                "membership_id": org.membership,
                "session_generation": 1,
                "request_id": receipt["request_id"],
            }
            current = client.get(context_path, headers=headers)
            assert current.status_code == 200
            assert valid_instance(context_contract, current.json(), registry)
            assert {**current.json(), "request_id": receipt["request_id"]} == receipt
            replay = client.post(selection_path, json=body, headers=headers)
            assert replay.status_code == 200
            assert valid_instance(context_contract, replay.json(), registry)
            assert replay.json() == receipt
            assert runtime.browser_state.end(browser.handle, 1).generation == 2
            ended = client.get(context_path, headers=headers)
            assert (ended.status_code, ended.json()["code"]) == (401, "SESSION_ENDED")
            assert valid_instance(error_contract, ended.json(), registry)
        assert auth.fetched == [ISSUER]


def test_private_selection_binding_uses_current_sealed_credentials_and_replays(
    durable, monkeypatch
):
    org_a, org_b = durable.orgs["a"], durable.orgs["b"]
    subject = durable.identity.actor["subject"]
    with generated_browser_auth(durable, monkeypatch) as auth:
        runtime = auth.runtime
        sessions = runtime.browser_state
        binding = runtime.selection_binding
        admitted = sessions.admit(
            BrowserCredentials(
                access_token=auth.token, refresh_token="generated-initial", user_id=subject
            )
        )
        handle = admitted.handle
        identity = admitted.identity
        assert identity is not None
        initial = BrowserSelectionIntent(
            organization_id=org_a.local, expected_generation=0, idempotency_key="binding-initial"
        )
        admit = Mock(wraps=runtime.selection.admit)
        commit = Mock(wraps=runtime.selection.commit)
        monkeypatch.setattr(runtime.selection, "admit", admit)
        monkeypatch.setattr(runtime.selection, "commit", commit)

        foreign = replace(identity, actor={**identity.actor, "subject": "generated-foreign"})
        wrong_sid = replace(identity, session_id="generated-foreign-session")
        for invalid in (foreign, wrong_sid):
            assert (
                expect(
                    "SESSION_INVALID",
                    lambda invalid=invalid: binding.authorize_current(handle, invalid, initial),
                ).status
                == 401
            )
        stale = replace(identity, session_generation=1)
        assert (
            expect(
                "STALE_SESSION_CONTEXT", lambda: binding.authorize_current(handle, stale, initial)
            ).status
            == 409
        )
        admit.assert_not_called()

        target = binding.authorize_current(handle, identity, initial)
        assert target == SelectionTarget(
            org_a.local, org_a.remote_org, ISSUER, org_a.membership, "authority-one"
        )
        original_digest = sessions.current(handle).credential_digest
        assert original_digest is not None
        for invalid in ("malformed", digest("different-ciphertext")):
            assert (
                expect(
                    "STALE_SESSION_CONTEXT",
                    lambda invalid=invalid: binding.commit(
                        handle, identity, initial, target, invalid
                    ),
                ).status
                == 409
            )
        commit.assert_not_called()

        first = binding.commit(handle, identity, initial, target, original_digest)
        assert (first.organization_id, first.membership_id, first.session_generation) == (
            org_a.local,
            org_a.membership,
            1,
        )
        current = sessions.current(handle)
        current_identity = current.identity
        assert current_identity is not None
        assert binding.authorize_current(handle, current_identity, initial) == first

        followup = BrowserSelectionIntent(
            organization_id=org_b.local, expected_generation=1, idempotency_key="binding-followup"
        )
        other = binding.authorize_current(handle, current_identity, followup)
        assert isinstance(other, SelectionTarget) and not other.already_current
        sessions.require_reconciliation(handle, 1)
        replacement = BrowserCredentials(
            access_token=auth.other, refresh_token="generated-refresh-a", user_id=subject
        )
        staged = sessions.replace_credentials(handle, 1, replacement)
        latest = sessions.replace_credentials(
            handle, 1, replacement.model_copy(update={"refresh_token": "generated-refresh-b"})
        )
        assert staged != latest
        assert binding.authorize_current(handle, current_identity, followup) == other
        commit.reset_mock()
        assert (
            expect(
                "STALE_SESSION_CONTEXT",
                lambda: binding.commit(handle, current_identity, followup, other, staged),
            ).status
            == 409
        )
        commit.assert_not_called()
        assert sessions.current(handle).reconciliation_required

        second = binding.commit(handle, current_identity, followup, other, latest)
        assert (second.organization_id, second.membership_id, second.session_generation) == (
            org_b.local,
            org_b.membership,
            2,
        )
        active = sessions.current(handle)
        assert active.identity is not None and not active.reconciliation_required
        assert binding.authorize_current(handle, active.identity, initial) == first
        assert binding.authorize_current(handle, active.identity, followup) == second
        assert sessions.end(handle, 2).generation == 3
        ended = sessions.current(handle)
        assert ended.ended and ended.generation == 3
        assert ended.identity is ended.credentials is ended.credential_digest is None
        assert ended.csrf_token == admitted.csrf_token
        expect("SESSION_ENDED", lambda: binding.authorize_current(handle, active.identity, initial))
        assert auth.fetched == [ISSUER]


@pytest.mark.parametrize("expiry_point", ["before_admission", "after_target", "after_replay"])
def test_private_selection_binding_refuses_an_expired_current_bearer(
    durable, monkeypatch, expiry_point
):
    subject = durable.identity.actor["subject"]
    with generated_browser_auth(durable, monkeypatch) as auth:
        runtime = auth.runtime
        admitted = runtime.browser_state.admit(
            BrowserCredentials(
                access_token=auth.token, refresh_token="generated-expiry", user_id=subject
            )
        )
        identity = admitted.identity
        assert identity is not None
        binding = runtime.selection_binding
        handle = admitted.handle
        intent = BrowserSelectionIntent(
            organization_id=durable.orgs["a"].local,
            expected_generation=0,
            idempotency_key="binding-expiry",
        )
        if expiry_point == "after_replay":
            target = binding.authorize_current(handle, identity, intent)
            sealed = runtime.browser_state.current(handle).credential_digest
            assert isinstance(target, SelectionTarget) and sealed is not None
            first = binding.commit(handle, identity, intent, target, sealed)
            identity = runtime.browser_state.current(handle).identity
            assert identity is not None and identity.session_generation == first.session_generation

        expires_at = datetime.fromtimestamp(auth.expires_at, UTC)

        def expire():
            monkeypatch.setattr(runtime.session_service, "clock", lambda: expires_at)
            monkeypatch.setattr(runtime.session_service.verifier, "clock", lambda: expires_at)

        admit_real = runtime.selection.admit
        observed = []

        def admit_then_expire(*args):
            observed.append(admit_real(*args))
            expire()
            return observed[-1]

        if expiry_point == "before_admission":
            expire()
        admit = Mock(side_effect=admit_then_expire)
        monkeypatch.setattr(runtime.selection, "admit", admit)

        assert (
            expect(
                "SESSION_INVALID", lambda: binding.authorize_current(handle, identity, intent)
            ).status
            == 401
        )
        if expiry_point == "before_admission":
            admit.assert_not_called()
        else:
            admit.assert_called_once()
            if expiry_point == "after_target":
                assert isinstance(observed[0], SelectionTarget)
            else:
                assert observed == [first]


@pytest.mark.parametrize("prior_token", ["live", "expired"])
def test_sealed_continuation_obeys_current_bearer_boundary(durable, monkeypatch, prior_token):
    subject = durable.identity.actor["subject"]
    org = durable.orgs["b"]
    with generated_browser_auth(durable, monkeypatch) as auth:
        sessions = auth.runtime.browser_state
        binding = auth.runtime.selection_binding
        initial = sessions.admit(
            BrowserCredentials(
                access_token=auth.token, refresh_token="generated-before-switch", user_id=subject
            )
        )
        handle, identity = initial.handle, initial.identity
        assert identity is not None
        intent = BrowserSelectionIntent(
            organization_id=org.local,
            expected_generation=0,
            idempotency_key="generated-continuation",
        )
        target = binding.authorize_current(handle, identity, intent)
        assert (
            isinstance(target, SelectionTarget)
            and target.identity_organization_id == org.remote_org
        )
        sessions.require_reconciliation(handle, 0)
        prior_digest = sessions.current(handle).credential_digest
        assert prior_digest is not None
        expires = datetime.fromtimestamp(auth.expires_at, UTC)
        flow = BrowserAuthTransaction(
            state=secrets.token_urlsafe(32),
            nonce=secrets.token_urlsafe(32),
            browser_digest=digest("generated-switch-browser"),
            verifier=secrets.token_urlsafe(32),
            expires_at=expires + timedelta(seconds=60),
            return_path="/",
            kind="switch",
            session_handle=handle,
            subject=subject,
            session_id=identity.session_id,
            generation=identity.session_generation,
            selection_target=BrowserSelectionTarget(
                organization_id=target.organization_id,
                identity_organization_id=target.identity_organization_id,
                identity_issuer=target.identity_issuer,
                membership_id=target.membership_id,
                authority_revision=target.authority_revision,
            ),
            selection_intent=intent,
        )
        sessions.create_flow(flow)
        if prior_token == "expired":
            monkeypatch.setattr(auth.runtime.session_service, "clock", lambda: expires)
            monkeypatch.setattr(auth.runtime.session_service.verifier, "clock", lambda: expires)

        combined = f"{flow.state}.{flow.nonce}"
        expect(
            "CALLBACK_INVALID",
            lambda: sessions.consume_flow(combined, digest("generated-wrong-browser")),
        )
        claimed = sessions.consume_flow(combined, flow.browser_digest)
        assert claimed == flow and claimed.selection_intent is not None
        expect("CALLBACK_INVALID", lambda: sessions.consume_flow(combined, flow.browser_digest))
        assert counts(durable, identity.session_id) == (0, 0)

        if prior_token == "expired":
            admit = Mock(wraps=auth.runtime.selection.admit)
            monkeypatch.setattr(auth.runtime.selection, "admit", admit)
            expect(
                "SESSION_INVALID",
                lambda: binding.authorize_current(handle, identity, claimed.selection_intent),
            )
            admit.assert_not_called()
            blocked = sessions.current(handle)
            assert blocked.reconciliation_required and blocked.generation == 0
            assert blocked.credential_digest == prior_digest
            assert counts(durable, identity.session_id) == (0, 0)

            signin = BrowserAuthTransaction(
                state=secrets.token_urlsafe(32),
                nonce=secrets.token_urlsafe(32),
                browser_digest=digest("generated-fresh-signin-browser"),
                verifier=secrets.token_urlsafe(32),
                expires_at=expires + timedelta(minutes=10),
                return_path="/",
                kind="signin",
                session_handle=None,
                subject=None,
                session_id=None,
                generation=None,
                selection_target=None,
            )
            sessions.create_flow(signin)
            signin_state = f"{signin.state}.{signin.nonce}"
            assert sessions.consume_flow(signin_state, signin.browser_digest) == signin
            recovery_sid = f"generated-recovery-{uuid4().hex}"
            recovered_token = auth.issue_token(
                org.remote_org,
                sid=recovery_sid,
                iat=auth.expires_at,
                auth_time=auth.expires_at,
                exp=auth.expires_at + 300,
            )
            recovered = sessions.admit(
                BrowserCredentials(
                    access_token=recovered_token,
                    refresh_token="generated-fresh-signin",
                    user_id=subject,
                )
            )
            assert recovery_sid != identity.session_id and recovered.handle != handle
            assert recovered.session_id == recovery_sid and recovered.generation == 0
            context = auth.runtime.session_service.current_context
            assert context(recovered_token, recovered.handle, "generated-recovery") == {
                "state": "unselected",
                "organization_id": None,
                "membership_id": None,
                "session_generation": 0,
                "request_id": "generated-recovery",
            }
            expect("SESSION_INVALID", lambda: context(recovered_token, handle, "generated-old"))
            expect("SESSION_INVALID", lambda: context(auth.token, handle, "generated-expired"))
            expect(
                "CALLBACK_INVALID",
                lambda: sessions.consume_flow(signin_state, signin.browser_digest),
            )
            expect("CALLBACK_INVALID", lambda: sessions.consume_flow(combined, flow.browser_digest))
            previous = sessions.current(handle)
            assert previous == blocked and previous.credential_digest == prior_digest
            assert counts(durable, identity.session_id) == counts(durable, recovery_sid) == (0, 0)
            admit.assert_not_called()
            return

        assert binding.authorize_current(handle, identity, claimed.selection_intent) == target
        now = int(datetime.now(UTC).timestamp())
        returned = auth.issue_token(
            org.remote_org, iat=now, exp=now + 600, jti="generated-continuation-exchange"
        )
        staged = sessions.replace_credentials(
            handle,
            identity.session_generation,
            BrowserCredentials(
                access_token=returned, refresh_token="generated-after-switch", user_id=subject
            ),
        )
        assert staged != prior_digest
        assert binding.authorize_current(handle, identity, claimed.selection_intent) == target
        receipt = binding.commit(handle, identity, claimed.selection_intent, target, staged)
        assert (receipt.organization_id, receipt.membership_id, receipt.session_generation) == (
            org.local,
            org.membership,
            1,
        )
        completed = sessions.current(handle)
        assert completed.identity is not None and not completed.reconciliation_required
        assert completed.credential_digest == staged and completed.generation == 1
        assert (
            binding.authorize_current(handle, completed.identity, claimed.selection_intent)
            == receipt
        )
        assert counts(durable, identity.session_id) == (1, 1)


def test_exact_historical_receipt_after_later_switch(durable):
    selector = durable.selector
    initial = request(durable)
    first_target = selector.admit(durable.identity, durable.binding, initial, "key-a")
    first = commit(durable, initial, "key-a", first_target)
    current = replace(durable.identity, session_generation=1)
    same_target = selector.admit(current, durable.binding, request(durable, "a", 1), "same")
    assert isinstance(same_target, SelectionTarget) and same_target.already_current
    same = commit(durable, request(durable, "a", 1), "same", same_target, identity=current)
    assert same.session_generation == 1 and counts(durable) == (2, 1)
    second_request = request(durable, "b", 1)
    second_target = selector.admit(current, durable.binding, second_request, "key-b")
    assert isinstance(second_target, SelectionTarget) and not second_target.already_current
    second = commit(
        durable,
        second_request,
        "key-b",
        second_target,
        claims(durable, "b"),
        identity=current,
        request_id="request-second",
    )
    assert second.session_generation == 2 and counts(durable) == (3, 2)
    current = replace(current, session_generation=2)
    assert selector.admit(current, durable.binding, initial, "key-a") == first
    assert selector.admit(current, durable.binding, second_request, "key-b") == second
    expect(
        "IDEMPOTENCY_CONFLICT",
        lambda: selector.admit(current, durable.binding, request(durable, "b", 0), "key-a"),
    )
    expect(
        "STALE_SESSION_CONTEXT", lambda: commit(durable, initial, "late-continuation", first_target)
    )
    assert counts(durable) == (3, 2)


def test_same_organization_cannot_clear_pending_reconciliation(durable):
    selector = durable.selector
    initial = request(durable)
    initial_target = selector.admit(durable.identity, durable.binding, initial, "initial")
    first = commit(durable, initial, "initial", initial_target)
    current = replace(durable.identity, session_generation=first.session_generation)
    selected = request(durable, generation=current.session_generation)
    captured = selector.admit(current, durable.binding, selected, "captured")
    assert isinstance(captured, SelectionTarget) and captured.already_current
    expect(
        "STALE_SESSION_CONTEXT",
        lambda: commit(durable, selected, "captured", captured, sealed="wrong", identity=current),
    )

    binding = digest(durable.binding)
    repository = durable.identity_repository
    session = repository.load_session(binding, datetime.now(UTC))
    repository.require_reconciliation(session)

    assert selector.admit(current, durable.binding, initial, "initial") == first
    refused_admit = expect(
        "CONTEXT_RECONCILIATION_REQUIRED",
        lambda: selector.admit(current, durable.binding, selected, "retry"),
    )
    refused_commit = expect(
        "CONTEXT_RECONCILIATION_REQUIRED",
        lambda: commit(durable, selected, "captured", captured, identity=current),
    )
    assert refused_admit.status == refused_commit.status == 409
    pending = repository.load_session(binding, datetime.now(UTC))
    assert pending.reconciliation_required
    assert pending.generation == current.session_generation
    assert repository.read_credentials(pending, datetime.now(UTC)) == GENERATED_SEALED
    assert counts(durable) == (1, 1)


def test_lost_commit_acknowledgment_recovers_through_current_context(durable):
    selector = durable.selector
    requested = request(durable)
    target = selector.admit(durable.identity, durable.binding, requested, "unconfirmed")
    assert isinstance(target, SelectionTarget)

    @contextmanager
    def commit_then_disconnect(conn):
        with conn.transaction():
            yield
        raise psycopg.OperationalError("generated lost acknowledgment")

    @contextmanager
    def lost_ack_connection():
        with selector.connect() as conn:
            yield SimpleNamespace(
                info=conn.info,
                execute=conn.execute,
                transaction=lambda: commit_then_disconnect(conn),
            )

    verified = claims(durable)
    lost = PostgresSessionSelection(lost_ack_connection)
    assert (
        expect(
            "IDENTITY_STATE_UNAVAILABLE",
            lambda: lost.commit(
                durable.identity,
                durable.binding,
                requested,
                "unconfirmed",
                target,
                verified,
                digest(GENERATED_SEALED),
                "request-unconfirmed",
            ),
        ).status
        == 503
    )
    recovered = session_service(durable, verified).current_context(
        "generated", durable.binding, "read-after-lost-ack"
    )
    assert recovered == {
        "state": "active",
        "organization_id": target.organization_id,
        "membership_id": target.membership_id,
        "session_generation": 1,
        "request_id": "read-after-lost-ack",
    }
    reauthenticated = replace(durable.identity, session_generation=1)
    assert selector.admit(reauthenticated, durable.binding, requested, "unconfirmed") == (
        SelectionReceipt(target.organization_id, target.membership_id, 1, "request-unconfirmed")
    )
    assert counts(durable) == (1, 1)


def test_competing_targets_share_one_generation_winner(durable):
    gate = Barrier(2)

    def competing(letter):
        requested = request(durable, letter)
        target = durable.selector.admit(durable.identity, durable.binding, requested, letter)
        gate.wait(timeout=7)
        try:
            return commit(durable, requested, letter, target, claims(durable, letter))
        except AuthError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(competing, ("a", "b")))
    winners = [r for r in results if isinstance(r, SelectionReceipt)]
    assert len(winners) == 1 and winners[0].session_generation == 1
    assert "STALE_SESSION_CONTEXT" in results and counts(durable) == (1, 1)


@pytest.mark.parametrize(
    "fault,code",
    [
        ("credentials", "STALE_SESSION_CONTEXT"),
        ("provider_org", "CONTEXT_RECONCILIATION_REQUIRED"),
        ("mapping", "STALE_SESSION_CONTEXT"),
        ("authority", "STALE_SESSION_CONTEXT"),
        ("observation_time", "IDENTITY_FRESHNESS_UNAVAILABLE"),
        ("removed", "MEMBERSHIP_INACTIVE"),
        ("expired_claim", "SESSION_INVALID"),
        ("ended", "SESSION_INVALID"),
    ],
)
def test_commit_rechecks_live_fences(durable, fault, code):
    requested = request(durable)
    target = durable.selector.admit(durable.identity, durable.binding, requested, "key-a")
    verified = claims(durable)
    org = durable.orgs["a"]
    with durable.owner() as conn:
        if fault == "credentials":
            conn.execute(
                "UPDATE opentour_identity.session_credentials SET sealed_payload='replacement' "
                "WHERE session_id=%s",
                (durable.identity.session_id,),
            )
        elif fault == "provider_org":
            verified = replace(verified, organization_id=durable.orgs["b"].remote_org)
        elif fault == "mapping":
            conn.execute(
                "UPDATE opentour_tenants.memberships SET membership_id='new_' || "
                "membership_id WHERE organization_id=%s",
                (org.local,),
            )
        elif fault == "authority":
            conn.execute(
                "UPDATE opentour_authority.organizations SET authority_revision="
                "'authority-two' WHERE organization_id=%s",
                (org.local,),
            )
        elif fault == "observation_time":
            conn.execute(
                "UPDATE opentour_identity.membership_observations SET checked_at=%s, "
                "observed_at=%s WHERE membership_id=%s",
                (
                    durable.now - timedelta(seconds=31),
                    durable.now - timedelta(seconds=31),
                    org.remote_member,
                ),
            )
        elif fault == "removed":
            conn.execute(
                "UPDATE opentour_identity.membership_observations SET status='removed', "
                "locally_denied=true WHERE membership_id=%s",
                (org.remote_member,),
            )
        elif fault == "expired_claim":
            verified = replace(verified, expires_at=durable.now - timedelta(seconds=1))
        elif fault == "ended":
            conn.execute(
                "UPDATE opentour_identity.sessions SET ended_at=clock_timestamp() "
                "WHERE session_id=%s",
                (durable.identity.session_id,),
            )
    expect(code, lambda: commit(durable, requested, "key-a", target, verified))
    assert counts(durable) == (0, 0)
    with durable.owner() as conn:
        row = conn.execute(
            "SELECT generation, selected_organization_id FROM opentour_identity.sessions "
            "WHERE session_id=%s",
            (durable.identity.session_id,),
        ).fetchone()
        assert row == {"generation": 0, "selected_organization_id": None}


def test_foreign_absent_and_exhausted_have_no_effects(durable):
    foreign_org = durable.orgs["b"].local
    with durable.owner() as conn:
        conn.execute(
            "UPDATE opentour_tenants.memberships SET subject='generated-foreign-subject' "
            "WHERE organization_id=%s",
            (foreign_org,),
        )
    for org in ("nonexistent", durable.orgs["a"].remote_org, foreign_org):
        assert (
            expect(
                "RESOURCE_NOT_FOUND",
                lambda org=org: durable.selector.admit(
                    durable.identity, durable.binding, SessionSelectionRequest(org, 0), "key-a"
                ),
            ).status
            == 404
        )
    sid = durable.identity.session_id
    with durable.owner() as conn:
        for table in ("sessions", "session_bindings", "session_credentials"):
            conn.execute(
                f"UPDATE opentour_identity.{table} SET generation=%s WHERE session_id=%s",
                (MAX_COUNTER, sid),
            )
    capped = replace(durable.identity, session_generation=MAX_COUNTER)
    expect(
        "SESSION_GENERATION_EXHAUSTED",
        lambda: durable.selector.admit(
            capped, durable.binding, request(durable, "a", MAX_COUNTER), "key-a"
        ),
    )
    assert counts(durable) == (0, 0)


def test_lookup_fails_closed_on_missing_mapping_and_unavailable_authority(durable):
    target = durable.orgs["a"]
    subject = durable.identity.actor["subject"]
    lookup = durable.authority.current
    assert lookup("different-subject", target.local) is None
    assert lookup(subject, target.remote_org) is None
    owner_authority = PostgresSelectionMembershipAuthority(durable.owner, ISSUER)
    expect("IDENTITY_STATE_UNAVAILABLE", lambda: owner_authority.current(subject, target.local))
    owner_selector = PostgresSessionSelection(durable.owner)
    expect(
        "IDENTITY_STATE_UNAVAILABLE",
        lambda: owner_selector.admit(durable.identity, durable.binding, request(durable), "key-a"),
    )
    with durable.owner() as conn:
        conn.execute(
            "UPDATE opentour_tenants.organizations SET identity_org_id='' WHERE id=%s",
            (target.local,),
        )
    expect("IDENTITY_MEMBERSHIP_CONFLICT", lambda: lookup(subject, target.local))
    with durable.owner() as conn:
        conn.execute(
            "UPDATE opentour_tenants.organizations SET identity_org_id=%s WHERE id=%s",
            (target.remote_org, target.local),
        )
        conn.execute(
            "UPDATE opentour_authority.organizations SET state='unavailable' "
            "WHERE organization_id=%s",
            (target.local,),
        )
    assert expect("AUTHORITY_UNAVAILABLE", lambda: lookup(subject, target.local)).status == 503
    with durable.selector.connect() as conn:
        row = conn.execute(
            "SELECT rolsuper,bypass.rolbypassrls FROM pg_roles bypass WHERE rolname=current_user"
        ).fetchone()
        assert row == {"rolsuper": False, "rolbypassrls": False}
    for sql in (
        "UPDATE opentour_identity.sessions SET generation=0",
        "SELECT sealed_payload FROM opentour_identity.session_credentials",
        "UPDATE opentour_identity.membership_observations SET status='active'",
        "SELECT key_sha256 FROM opentour_selection.receipts",
    ):
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            durable.selector.connect() as conn,
        ):
            conn.execute(sql)
