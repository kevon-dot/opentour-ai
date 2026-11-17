"""Actual disposable PostgreSQL only; provider identity/journal facts are generated.

This file requires its finite local-integration supervisor before public evidence.
It never accesses an existing database, service, provider account or customer row.
"""

import hashlib
import os
import runpy
from concurrent.futures import ThreadPoolExecutor
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
from opentour_control.authority.coordinator import (
    GrantCoordinator,
    JournalCallerContext,
    PostgresAuthorityResolver,
)
from opentour_control.authority.journal_client import TransportResponse
from opentour_journal.crypto import canonical
from opentour_journal.models import AppendIntent, AppendOutcome, ImmutableMetadataRef, JournalError
from opentour_journal.ports import ApprovedRegistration, AuthorizedScope
from psycopg.types.json import Jsonb

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


@pytest.fixture(scope="module")
def generated_audit():
    # Exercise the already qualified signed journal with generated provider doubles.
    return runpy.run_path(str(ROOT / "services/audit-journal/tests/test_append_protocol.py"))


@pytest.fixture
def generated_grant_facts():
    return runpy.run_path(str(Path(__file__).with_name("test_grants.py")))


class ScopedGeneratedService:
    def __init__(self, context, principal):
        from opentour_control.auth.grant_repository import RepositoryScope

        self.scope = RepositoryScope.from_context(context)
        self.principal = principal

    def current(self, principal, organization_id, capability):
        if principal != self.principal or organization_id != self.scope.organization_id:
            raise JournalError("JOURNAL_FORBIDDEN", 403)
        return JournalCallerContext(
            principal,
            AuthorizedScope(principal.service_id, organization_id, capability),
            self.scope,
        )


class GeneratedApproval:
    def __init__(self, context):
        self.registration = ApprovedRegistration(
            context.organization_id,
            context.revocation_epoch,
            context.authority_revision,
            ImmutableMetadataRef(record_id="generated-enrollment", revision="1", sha256="a" * 64),
        )

    def current(self, principal, organization_id):
        assert self.registration.organization_id == organization_id
        return self.registration


class ConfiguredTestTransport:
    def __init__(self, transport):
        self.transport = transport
        self.after = lambda path, body: None
        self.head_available = True

    def post(self, path, body):
        if not self.head_available and path.endswith("/heads/read"):
            raise JournalError("JOURNAL_UNAVAILABLE", 503)
        try:
            result = self.transport.post(path, body)
        except JournalError as error:
            return TransportResponse(error.status, canonical({"code": error.code}))
        self.after(path, body)
        return result


@pytest.fixture
def saga(durable, generated_audit, generated_grant_facts):
    f, d = generated_grant_facts, durable
    org = d.context.organization_id
    system = generated_audit["build_system"]()
    callers = ScopedGeneratedService(d.context, system.principal)
    enrollments = GeneratedApproval(d.context)
    sql_resolver = PostgresAuthorityResolver(d.coordinator, callers, enrollments)
    # Sign a generated preexisting delegated-administration baseline with the
    # real 217 service, then require native SQL sources for every tested mutation.
    generated = system.authority
    generated.authorize = lambda principal, organization, capability: (
        callers.current(principal, organization, capability).authorization
    )
    generated.registration = enrollments.current
    system.service.authority = generated
    genesis = system.service.register(system.principal, org)
    system.checkpoints.checkpoints.update(
        generated_audit["GeneratedCheckpoints"](
            system.client.verifier.trust.journal, [genesis]
        ).checkpoints
    )
    genesis_intent = AppendIntent(
        organization_id=org,
        operation_id="generated-delegated-admin",
        expected_head_sequence=0,
        expected_head_digest=genesis.chain_sha256,
        mutation_kind="grant",
        proposed_epoch=genesis.latest_epoch,
        proposed_authority_revision="generated-current-authority",
        canonical_intent_digest="a" * 64,
        scoped_intent_ref=enrollments.registration.enrollment_ref,
        actor_ref=enrollments.registration.enrollment_ref,
        created_at=timestamp(NOW),
    )
    intent_receipt = system.client.append_intent(genesis_intent)
    system.client.append_outcome(
        AppendOutcome(
            organization_id=org,
            operation_id=genesis_intent.operation_id,
            intent_sequence=1,
            expected_head_sequence=intent_receipt.sequence,
            expected_head_digest=intent_receipt.chain_sha256,
            result="committed",
            control_transaction_ref=enrollments.registration.enrollment_ref,
            resulting_epoch=genesis.latest_epoch,
            resulting_authority_revision=genesis_intent.proposed_authority_revision,
        )
    )
    existing = system.client.require_issuance_frontier(org, system.evidence)
    system.service.authority = sql_resolver
    transport = ConfiguredTestTransport(system.client.transport)
    system.client.transport = transport

    resources, principals = f["GeneratedResources"](), f["GeneratedPrincipals"]()
    resources.rows = {
        key: replace(v, organization_id=org) if v.organization_id == f["ORG"] else v
        for key, v in resources.rows.items()
    }
    principals.rows = {key: replace(v, organization_id=org) for key, v in principals.rows.items()}
    principals.rows["local-user-a"] = replace(
        principals.rows["local-user-a"],
        identity_issuer=d.context.identity.actor["issuer"],
        identity_subject=d.context.identity.actor["subject"],
    )
    principals.current_human = lambda organization, issuer, subject, member: (
        principals.rows["local-user-a"]
        if (organization, issuer, subject, member)
        == (
            org,
            d.context.identity.actor["issuer"],
            d.context.identity.actor["subject"],
            d.context.membership_id,
        )
        else None
    )
    authorized = f["grant"](
        "generated-parent",
        capabilities=["access.manage", "training.run"],
        purposes=["administration", "development"],
        data_classes=["control_metadata", "customer_confidential"],
        resource_scope={
            **f["scope"](workspace_ids=["workspace-a"], project_ids=["project-a"]),
            "organization_id": org,
        },
        creation_operation_id=genesis_intent.operation_id,
        creation_authority_revision=existing.head.latest_authority_revision,
    )
    context = replace(d.context, authority_revision=existing.head.latest_authority_revision)
    request = CreateGrant.model_validate(
        f["request"](expected_authority_revision=context.authority_revision)
    )
    scope = callers.scope
    with psycopg.connect(host=str(d.postgres.socket), dbname="postgres", user="c03_owner") as owner:
        owner.execute(
            "INSERT INTO opentour_authority.organizations "
            "VALUES(%s,%s,%s,null,'current',%s,%s,%s,%s)",
            (
                org,
                context.authority_revision,
                context.revocation_epoch,
                Jsonb(existing.head.wire()),
                Jsonb(existing.proof_ref.wire()),
                Jsonb(enrollments.registration.enrollment_ref.wire()),
                NOW,
            ),
        )
    with d.coordinator.transaction(scope) as conn:
        d.coordinator.retain_request(
            conn, context, "workspace-a", "c" * 64, request, "fixture", NOW
        )
        conn.execute(
            "INSERT INTO opentour_authority.operations "
            "(organization_id,operation_id,grant_id,issuer,subject,workspace_id,key_sha256,"
            "context,request_body,plan,proposed_grant,proposed_authority_revision,proposed_epoch,"
            "intent_body,state,revision,creation_receipt,created_at,updated_at) "
            "VALUES(%s,%s,%s,%s,%s,'workspace-a',%s,%s,%s,%s,%s,%s,%s,%s,'succeeded',1,%s,%s,%s)",
            (
                org,
                genesis_intent.operation_id,
                authorized.grant_id,
                scope.issuer,
                scope.subject,
                "c" * 64,
                Jsonb({}),
                Jsonb(request.wire()),
                Jsonb({}),
                Jsonb(authorized.wire()),
                context.authority_revision,
                context.revocation_epoch,
                canonical(genesis_intent.wire()),
                Jsonb({"grant_id": authorized.grant_id}),
                NOW,
                NOW,
            ),
        )
        conn.execute(
            "INSERT INTO opentour_authority.grants VALUES(%s,%s,%s,%s,'active',1,%s)",
            (org, authorized.grant_id, genesis_intent.operation_id, Jsonb(authorized.wire()), NOW),
        )

    def identity():
        with d.coordinator.transaction(scope) as conn:
            state, _ = d.coordinator.authority(conn, org)
        return replace(context, authority_revision=state.authority_revision)

    from opentour_control.auth.grant_models import GrantVocabulary
    from opentour_control.auth.grants import GrantResolver

    resolver = GrantResolver(
        resources,
        principals,
        GrantVocabulary(
            frozenset({"administration", "development"}),
            frozenset({"scoped-work"}),
            lambda principal, templates: False,
        ),
        lambda: d.coordinator.clock(),
    )
    coordinator = GrantCoordinator(
        d.coordinator, resolver, system.client, system.evidence, management_purpose="administration"
    )
    return SimpleNamespace(
        create=lambda value=request: coordinator.create(
            identity, "workspace-a", "new-grant", "request-id", canonical(value.wire())
        ),
        service=coordinator,
        identity=identity,
        request=request,
        resources=resources,
        principals=principals,
        repository=d.coordinator,
        scope=scope,
        transport=transport,
    )


def test_actual_grant_saga_idempotency_and_stable_receipt(saga):
    created = saga.create()
    assert created.status == 201
    view = created.operation
    assert view.state == "succeeded"
    assert view.creation_receipt.revision == "1"
    assert view.current_grant_status.state == "active"
    assert view.current_grant_status.authority_status == "current"
    replay = saga.create()
    assert replay.status == 200
    assert replay.operation.creation_receipt == view.creation_receipt
    assert replay.operation.operation_revision == view.operation_revision
    with pytest.raises(GrantError, match="IDEMPOTENCY_CONFLICT"):
        saga.create(saga.request.model_copy(update={"reason_code": "different"}))
    with saga.repository.transaction(saga.scope) as conn:
        rows = conn.execute(
            "SELECT state,result_sha FROM (SELECT g.state,t.sha256 AS result_sha "
            "FROM opentour_authority.grants g JOIN opentour_authority.sources t "
            "ON g.organization_id=t.organization_id AND g.creation_operation_id=t.operation_id "
            "WHERE g.grant_id=%s AND t.kind='transaction') validated",
            (view.grant_id,),
        ).fetchall()
        assert len(rows) == 1 and rows[0]["state"] == "active"
        assert len(rows[0]["result_sha"]) == 64


def test_actual_grant_saga_signed_outcome_with_unavailable_live_head(saga):
    def lose_head_after_outcome(path, body):
        if path.endswith("/outcomes"):
            saga.transport.head_available = False

    saga.transport.after = lose_head_after_outcome
    pending = saga.create()
    assert pending.status == 202
    assert pending.operation.state == "reconciliation_required"
    assert pending.operation.creation_receipt is None
    assert pending.operation.current_grant_status is None
    with saga.repository.transaction(saga.scope) as conn:
        state = conn.execute(
            "SELECT state FROM opentour_authority.grants WHERE grant_id=%s",
            (pending.operation.grant_id,),
        ).fetchone()
        assert state == {"state": "prepared"}
    saga.transport.head_available = True
    saga.transport.after = lambda path, body: None
    reconciled = saga.create()
    assert reconciled.status == 200
    assert reconciled.operation.state == "succeeded"
    assert reconciled.operation.grant_id == pending.operation.grant_id
    saga.transport.head_available = False
    historical = saga.service.progress(saga.identity, reconciled.operation.operation_id, "read-id")
    assert historical.creation_receipt == reconciled.operation.creation_receipt
    assert historical.current_grant_status is None
    assert historical.current_status_unavailable_reason == "AUTHORITY_UNAVAILABLE"


def test_actual_grant_saga_known_abort_is_journaled_and_retained(saga):
    def change_scope_revision_after_intent(path, body):
        if path.endswith("/intents"):
            key = ("project", "project-a")
            saga.resources.rows[key] = replace(saga.resources.rows[key], revision="2")

    saga.transport.after = change_scope_revision_after_intent
    aborted = saga.create().operation
    assert aborted.state == "aborted"
    assert aborted.creation_receipt is None
    assert aborted.current_grant_status is None
    with saga.repository.transaction(saga.scope) as conn:
        row = saga.repository.retained_operation(
            conn, saga.scope.organization_id, aborted.operation_id
        )
        assert AppendOutcome.parse(bytes(row["outcome_body"])).result == "aborted"
        assert row["outcome_response"]["phase"] == "outcome"
        assert (
            conn.execute(
                "SELECT state FROM opentour_authority.grants WHERE grant_id=%s", (aborted.grant_id,)
            ).fetchone()
            is None
        )


def test_actual_grant_saga_twenty_way_exact_replay(saga):
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda number: saga.create(), range(20)))
    operations = {result.operation.operation_id for result in results}
    assert len(operations) == 1
    assert all(result.status in {200, 201, 202} for result in results)
    assert saga.create().operation.state == "succeeded"
    with saga.repository.transaction(saga.scope) as conn:
        assert conn.execute(
            "SELECT count(*) AS size FROM opentour_authority.operations WHERE operation_id=%s",
            (operations.pop(),),
        ).fetchone() == {"size": 1}


def test_actual_grant_saga_future_start_and_exact_expiry_preserve_receipt(saga):
    body = saga.request.model_copy(
        update={
            "valid_from": timestamp(NOW + timedelta(seconds=5)),
            "expires_at": timestamp(NOW + timedelta(seconds=20)),
        }
    )
    created = saga.create(body).operation
    assert created.state == "succeeded"
    assert created.current_grant_status.state == "not_yet_valid"
    for delta, expected in [(5, "active"), (20, "expired")]:
        saga.repository.clock = lambda offset=delta: NOW + timedelta(seconds=offset)
        current = saga.service.progress(saga.identity, created.operation_id, "read-id")
        assert current.current_grant_status.state == expected
        assert current.creation_receipt == created.creation_receipt
