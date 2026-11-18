"""Actual disposable PostgreSQL, generated identities and provider claims."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentour_control.auth.config import MAX_COUNTER, AuthError, IdentityConfig, digest, timestamp
from opentour_control.auth.repository import PostgresIdentityRepository, connection_factory
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


def counts(durable):
    with durable.owner() as conn:
        return tuple(
            conn.execute(
                f"SELECT count(*) AS n FROM opentour_selection.{table} WHERE session_id=%s",
                (durable.identity.session_id,),
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
