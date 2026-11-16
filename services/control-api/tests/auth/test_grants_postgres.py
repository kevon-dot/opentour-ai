"""Actual disposable PostgreSQL only; provider identity/journal facts are generated.

This file requires its finite local-integration supervisor before public evidence.
It never accesses an existing database, service, provider account or customer row.
"""

import hashlib
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from opentour_control.auth.grant_models import CreateGrant, GrantError, timestamp
from opentour_control.auth.grant_repository import PostgresGrantRepository, set_context
from opentour_control.auth.repository import connection_factory
from opentour_control.auth.session import AuthContext, VerifiedIdentityContext

from tools.fixtures.postgres import DisposablePostgres

TARGET = "services/control-api/tests/auth/test_grants_postgres.py"
ROOT = Path(__file__).resolve().parents[4]
NOW = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def postgres():
    if os.environ.get("OPENTOUR_POSTGRES_TARGET") != TARGET:
        raise RuntimeError("Real PostgreSQL requires the registered local-integration target")
    fixture = DisposablePostgres()
    with fixture:
        fixture.sql(
            "CREATE ROLE c143_migration LOGIN CREATEROLE NOSUPERUSER NOBYPASSRLS;"
            "GRANT CREATE ON DATABASE postgres TO c143_migration;",
            role="c03_owner",
        )
        with psycopg.connect(
            host=str(fixture.socket),
            dbname="postgres",
            user="c143_migration",
            autocommit=True,
            options="-c statement_timeout=5000 -c lock_timeout=2000",
        ) as owner:
            for name in [
                "0001_identity_state.sql",
                "0002_organization_creation.sql",
                "0003_authority_grants.sql",
            ]:
                path = ROOT / "services/control-api/migrations" / name
                sql = path.read_bytes()
                assert len(sql) < 65536
                owner.execute(sql.decode())
                fixture.receipt[name + "_sha256"] = hashlib.sha256(sql).hexdigest()
        fixture.sql(
            "CREATE ROLE c143_request LOGIN NOSUPERUSER NOBYPASSRLS IN ROLE opentour_grant_runtime;"
            "CREATE ROLE c143_coordinator LOGIN NOSUPERUSER NOBYPASSRLS IN "
            "ROLE opentour_authority_coordinator;",
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
    org, subject, member, remote_org, remote_member, sid = [
        prefix + suffix
        for prefix in [
            "local-org-",
            "subject-",
            "member-",
            "workos-org-",
            "workos-member-",
            "session-",
        ]
    ]
    issuer = "https://identity.example.invalid"
    with psycopg.connect(host=str(postgres.socket), dbname="postgres", user="c03_owner") as owner:
        owner.execute(
            "INSERT INTO opentour_tenants.terms_versions "
            "VALUES('generated-terms','https://example.invalid/terms',%s) "
            "ON CONFLICT DO NOTHING",
            (NOW,),
        )
        owner.execute(
            "INSERT INTO opentour_tenants.organizations "
            "VALUES(%s,%s,'Generated "
            "organization',%s,%s,'generated-terms',%s,'active',1,%s,%s)",
            (org, remote_org, issuer, subject, NOW, NOW, NOW),
        )
        owner.execute(
            "INSERT INTO opentour_tenants.memberships VALUES(%s,%s,%s,%s,%s,'active','owner',1,%s)",
            (org, issuer, subject, member, remote_member, NOW),
        )
        owner.execute(
            "INSERT INTO opentour_identity.membership_observations "
            "VALUES(%s,%s,%s,%s,'active',false,%s,%s,1)",
            (issuer, remote_member, remote_org, subject, NOW, NOW),
        )
        owner.execute(
            "INSERT INTO opentour_identity.sessions VALUES(%s,%s,%s,2,%s,%s,false,%s,%s,null,%s)",
            (
                issuer,
                sid,
                subject,
                org,
                member,
                NOW,
                NOW + timedelta(days=1),
                NOW + timedelta(days=1),
            ),
        )
    context = AuthContext(
        VerifiedIdentityContext(
            {"kind": "human", "issuer": issuer, "subject": subject},
            sid,
            2,
            timestamp(NOW),
            timestamp(NOW),
        ),
        org,
        member,
        "generated-authority",
        7,
    )
    request_connect = connection_factory(
        f"host={postgres.socket} dbname=postgres user=c143_request"
    )
    coordinator_connect = connection_factory(
        f"host={postgres.socket} dbname=postgres user=c143_coordinator"
    )
    return SimpleNamespace(
        postgres=postgres,
        context=context,
        mode=mode,
        remote_org=remote_org,
        remote_member=remote_member,
        request=PostgresGrantRepository(request_connect, role="request", clock=lambda: NOW),
        coordinator=PostgresGrantRepository(
            coordinator_connect, role="coordinator", clock=lambda: NOW
        ),
    )


def body():
    return CreateGrant.model_validate(
        {
            "principal": {"type": "user", "id": "generated-target"},
            "capabilities": ["training.run"],
            "resource_scope": {
                "workspace_ids": [],
                "project_ids": ["generated-project"],
                "environment_ids": [],
                "artifact_refs": [],
                "support_note_scopes": [],
            },
            "purposes": ["development"],
            "data_classes": ["customer_confidential"],
            "valid_from": timestamp(NOW),
            "expires_at": timestamp(NOW + timedelta(hours=1)),
            "reason_code": "scoped-work",
            "expected_authority_revision": "generated-authority",
        }
    )


def test_actual_non_owner_roles_and_force_rls(durable):
    for repository in [durable.request, durable.coordinator]:
        with repository.transaction(durable.context) as connection:
            row = connection.execute(
                "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
            assert row == {"rolsuper": False, "rolbypassrls": False}
            tables = connection.execute(
                "SELECT relrowsecurity,relforcerowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE "
                "n.nspname='opentour_authority' AND c.relkind='r' AND "
                "c.relname<>'schema_version'"
            ).fetchall()
            assert len(tables) == 9 and all(
                r["relrowsecurity"] and r["relforcerowsecurity"] for r in tables
            )


def test_canonical_helper_preserves_distinct_local_provider_mapping(durable):
    with durable.coordinator.transaction(durable.context) as conn:
        source = durable.coordinator.canonical_identity(conn, durable.context, NOW)
        assert source["organization_id"] == durable.context.organization_id
        assert source["identity_organization_id"] == durable.remote_org
        assert source["identity_organization_id"] != source["organization_id"]
        assert source["provider_membership_id"] == durable.remote_member


def test_canonical_source_privileges_are_read_only(durable):
    for repository in [durable.request, durable.coordinator]:
        with pytest.raises(GrantError, match="AUTHORITY_SQL_UNCONFIRMED"):
            with repository.transaction(durable.context) as conn:
                conn.execute("UPDATE opentour_tenants.memberships SET status='active' WHERE false")
        with pytest.raises(GrantError, match="AUTHORITY_SQL_UNCONFIRMED"):
            with repository.transaction(durable.context) as conn:
                conn.execute(
                    "UPDATE opentour_identity.membership_observations SET "
                    "locally_denied=false WHERE false"
                )
        with pytest.raises(GrantError, match="AUTHORITY_SQL_UNCONFIRMED"):
            with repository.transaction(durable.context) as conn:
                conn.execute("SELECT sealed_payload FROM opentour_identity.session_credentials")


def test_request_role_cannot_call_source_lock_or_mutate_authority(durable):
    for sql in [
        "SELECT * FROM opentour_authority.locked_membership()",
        "UPDATE opentour_authority.organizations SET revocation_epoch=0 WHERE false",
        "DELETE FROM opentour_authority.requests WHERE false",
    ]:
        with pytest.raises(GrantError, match="AUTHORITY_SQL_UNCONFIRMED"):
            with durable.request.transaction(durable.context) as conn:
                conn.execute(sql)


def test_missing_where_still_filters_foreign_requests(durable):
    durable.request.submit(durable.context, "workspace", "a" * 64, body(), "req")
    with durable.request.transaction(durable.context) as conn:
        rows = conn.execute("SELECT organization_id FROM opentour_authority.requests").fetchall()
        assert rows == [{"organization_id": durable.context.organization_id}]
    with durable.request.connect() as conn:
        assert conn.execute("SELECT * FROM opentour_authority.requests").fetchall() == []


def test_exact_request_replay_and_conflicting_body(durable):
    durable.request.submit(durable.context, "workspace", "a" * 64, body(), "first")
    durable.request.submit(durable.context, "workspace", "a" * 64, body(), "second")
    changed = body().model_copy(update={"reason_code": "other"})
    with pytest.raises(GrantError, match="IDEMPOTENCY_CONFLICT"):
        durable.request.submit(durable.context, "workspace", "a" * 64, changed, "third")
    with durable.request.transaction(durable.context) as conn:
        row = conn.execute(
            "SELECT count(*) AS count,min(request_id) AS id FROM opentour_authority.requests"
        ).fetchone()
        assert row == {"count": 1, "id": "first"}


def test_transaction_context_reset_after_success_rollback_exception_timeout(durable):
    with durable.request.connect() as conn:
        for outcome in ["success", "rollback", "exception", "timeout"]:
            try:
                with conn.transaction():
                    set_context(conn, durable.context)
                    assert (
                        conn.execute(
                            "SELECT current_setting('opentour.organization_id') AS org"
                        ).fetchone()["org"]
                        == durable.context.organization_id
                    )
                    if outcome == "rollback":
                        raise psycopg.Rollback()
                    if outcome == "exception":
                        raise ValueError("generated failure")
                    if outcome == "timeout":
                        conn.execute("SET LOCAL statement_timeout='1ms'")
                        conn.execute("SELECT pg_sleep(0.02)")
            except (ValueError, psycopg.errors.QueryCanceled):
                pass
            with conn.transaction():
                assert not conn.execute(
                    "SELECT current_setting('opentour.organization_id',true) AS org"
                ).fetchone()["org"]


def test_helper_foreign_or_unset_context_returns_no_metadata(durable):
    with durable.coordinator.connect() as conn:
        assert conn.execute("SELECT * FROM opentour_authority.locked_membership()").fetchall() == []
    foreign = replace(durable.context, organization_id="nonexistent")
    with durable.coordinator.transaction(foreign) as conn:
        assert conn.execute("SELECT * FROM opentour_authority.locked_membership()").fetchall() == []
        assert conn.execute("SELECT * FROM opentour_authority.identity_membership").fetchall() == []
        assert conn.execute("SELECT * FROM opentour_authority.identity_session").fetchall() == []


def test_helper_ignores_temporary_search_path_substitution(durable):
    with durable.coordinator.transaction(durable.context) as conn:
        conn.execute("CREATE TEMP TABLE organizations(id text)")
        conn.execute("INSERT INTO organizations VALUES('attacker-controlled')")
        conn.execute("SET LOCAL search_path=pg_temp,public")
        row = conn.execute("SELECT * FROM opentour_authority.locked_membership()").fetchone()
        assert row["organization_id"] == durable.context.organization_id
