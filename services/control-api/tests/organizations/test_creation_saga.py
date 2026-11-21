"""Actual local PostgreSQL with generated provider doubles, never WorkOS acceptance."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from opentour_control.auth.repository import (
    Observation,
    PostgresIdentityRepository,
    lock_membership_observation,
)
from opentour_control.tenants.models import CreateRequest, Creator, SetupError, key_digest
from opentour_control.tenants.provider import (
    MembershipObservation,
    OrganizationObservation,
    ProviderRejected,
    ProviderUnknown,
)
from opentour_control.tenants.repository import (
    PostgresOrganizationRepository,
    read_public_organization_terms,
)
from opentour_control.tenants.worker import PostgresProvisioningStore, ProvisioningWorker
from psycopg.rows import dict_row

from tools.fixtures.postgres import DisposablePostgres

TARGET = "services/control-api/tests/organizations/test_creation_saga.py"
ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture(scope="module")
def postgres():
    if os.environ.get("OPENTOUR_POSTGRES_TARGET") != TARGET:
        raise RuntimeError("Actual database fixture requires the registered local target")
    fixture = DisposablePostgres()
    with fixture:
        for name in ("0001_identity_state.sql", "0002_organization_creation.sql"):
            path = ROOT / "services/control-api/migrations" / name
            fixture.sql(path.read_text(), role="c03_owner")
            fixture.receipt[name + ":sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        fixture.sql(
            "CREATE ROLE c03_runtime LOGIN NOSUPERUSER NOBYPASSRLS N"
            "OCREATEDB NOCREATEROLE IN ROLE opentour_tenant_runtime;"
            "CREATE ROLE c03_worker LOGIN NOSUPERUSER NOBYPASSRLS NO"
            "CREATEDB NOCREATEROLE IN ROLE opentour_provisioner;"
            "CREATE ROLE c03_identity LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;"
            "GRANT USAGE ON SCHEMA opentour_identity TO c03_identity;"
            "GRANT SELECT,INSERT,UPDATE ON opentour_identity.sync_cursors,"
            "opentour_identity.membership_observations TO c03_identity;"
            "GRANT SELECT,INSERT ON opentour_identity.event_inbox,"
            "opentour_identity.outbox TO c03_identity;"
            "CREATE ROLE c03_terms LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;"
            "GRANT USAGE ON SCHEMA opentour_tenants TO c03_terms;"
            "GRANT SELECT ON opentour_tenants.terms_versions,"
            "opentour_tenants.terms_publication TO c03_terms;"
            "GRANT INSERT ON opentour_tenants.terms_versions TO c03_terms;"
            "GRANT UPDATE(revision) ON opentour_tenants.terms_publication TO c03_terms;"
            "CREATE ROLE c03_terms_reader LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;"
            "GRANT USAGE ON SCHEMA opentour_tenants TO c03_terms_reader;"
            "GRANT SELECT ON opentour_tenants.terms_versions,"
            "opentour_tenants.terms_publication TO c03_terms_reader;"
            "CREATE ROLE c03_terms_mixed LOGIN NOSUPERUSER IN ROLE c03_terms_reader;"
            "GRANT USAGE ON SCHEMA opentour_identity TO c03_terms_mixed;"
            "GRANT SELECT(subject) ON opentour_identity.sessions TO c03_terms_mixed;"
            "CREATE ROLE c03_terms_all LOGIN NOSUPERUSER IN ROLE pg_read_all_data;"
            "INSERT INTO opentour_tenants.terms_versions VALUES('gen"
            "erated-terms-v1','https://terms.example.invalid/generat"
            "ed','2026-01-01T00:00:00Z');"
            "INSERT INTO opentour_tenants.terms_publication VALUES(true,'generated-terms-v1');",
            role="c03_owner",
        )
        yield fixture
    assert fixture.cleanup_complete


@pytest.fixture(params=["customer_controlled", "opentour_managed"])
def mode(request):
    return request.param


class GeneratedAdmission:
    """Test-only policy port for focused lock/transport cases, never the default."""

    def check(self, connection, creator, now):
        assert connection.execute("SELECT 1 AS ok").fetchone()["ok"] == 1


class GeneratedProvider:
    environment = "generated_environment"

    def __init__(self, clock):
        self.clock = clock
        self.organizations = {}
        self.memberships = {}
        self.creates = 0
        self.member_creates = 0
        self.lose_organization_response = False
        self.lose_member_response = False

    def find_organization(self, operation):
        return self.organizations.get(operation.correlation)

    def create_organization(self, operation):
        self.creates += 1
        value = OrganizationObservation(
            "provider_org_" + uuid4().hex, operation.correlation, self.environment
        )
        self.organizations[operation.correlation] = value
        if self.lose_organization_response:
            raise ProviderUnknown()
        return value

    def find_membership(self, operation):
        return self.memberships.get((operation.provider_org_id, operation.creator_subject))

    def create_membership(self, operation):
        self.member_creates += 1
        value = MembershipObservation(
            "provider_member_" + uuid4().hex,
            operation.provider_org_id,
            operation.creator_subject,
            "active",
            self.clock(),
        )
        self.memberships[(operation.provider_org_id, operation.creator_subject)] = value
        if self.lose_member_response:
            raise ProviderUnknown()
        return value


@pytest.fixture
def durable(postgres, mode):
    now = datetime.now(UTC)
    creator = Creator(
        "https://identity.example.invalid", "user_" + uuid4().hex, "session_" + uuid4().hex, 0
    )

    def connection(role="c03_runtime", *, read_only=False):
        return psycopg.connect(
            host=str(postgres.socket),
            port=5432,
            dbname="postgres",
            user=role,
            row_factory=dict_row,
            options="-c default_transaction_read_only=on" if read_only else "",
        )

    with connection("c03_owner") as conn:
        conn.execute(
            "INSERT INTO opentour_identity.sessions(issuer,session_i"
            "d,subject,created_at,expires_at,deny_until) VALUES(%s,%"
            "s,%s,%s,%s,%s)",
            (
                creator.issuer,
                creator.session_id,
                creator.subject,
                now - timedelta(seconds=1),
                now + timedelta(hours=24),
                now + timedelta(hours=24),
            ),
        )

    def clock():
        return datetime.now(UTC)

    repository = PostgresOrganizationRepository(connection, clock=clock)
    store = PostgresProvisioningStore(lambda: connection("c03_worker"), clock)
    provider = GeneratedProvider(clock)
    body = CreateRequest(
        display_name="Generated buyer", accepted_terms_revision="generated-terms-v1"
    )
    key = key_digest(uuid4().hex)

    def create(request_id="generated_request"):
        return repository.create(creator, body, key, request_id, provider.environment, clock())

    return SimpleNamespace(
        postgres=postgres,
        mode=mode,
        creator=creator,
        connection=connection,
        clock=clock,
        repository=repository,
        store=store,
        provider=provider,
        worker=ProvisioningWorker(store, provider),
        body=body,
        key=key,
        create=create,
    )


def test_twenty_duplicate_arrivals_have_one_atomic_receipt_and_no_name_dedup(durable):
    barrier = threading.Barrier(20)
    pool_limit = threading.BoundedSemaphore(5)  # The qualified local cluster admits8connections.

    def submit(i):
        barrier.wait(timeout=5)
        with pool_limit:
            return durable.create("generated_request_" + str(i))

    with ThreadPoolExecutor(max_workers=20) as pool:
        receipts = list(pool.map(submit, range(20)))
    assert all(receipt == receipts[0] for receipt in receipts)
    with durable.connection("c03_owner") as conn:
        for table in ("organizations", "provisioning_operations"):
            assert (
                conn.execute(
                    f"SELECT count(*) AS n FROM opentour_tenants.{table} WHERE creator_subject=%s",
                    (durable.creator.subject,),
                ).fetchone()["n"]
                == 1
            )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM opentour_tenants.memberships WHERE subject=%s",
                (durable.creator.subject,),
            ).fetchone()["n"]
            == 1
        )
    second = durable.repository.create(
        durable.creator,
        durable.body,
        key_digest(uuid4().hex),
        "request_second",
        durable.provider.environment,
        durable.clock(),
    )
    assert second.organization_id != receipts[0].organization_id


def test_original_request_and_receipt_survive_terms_and_display_metadata_changes(durable):
    first = durable.create()
    with durable.connection("c03_owner") as conn:
        conn.execute(
            "UPDATE opentour_tenants.organizations SET display_name="
            "'Later generated name' WHERE id=%s",
            (first.organization_id,),
        )
    assert durable.store.load(first.operation_id).display_name == "Generated buyer"
    assert durable.repository.replay(durable.creator, durable.key, durable.body.digest()) == first
    with pytest.raises(SetupError, match="IDEMPOTENCY_CONFLICT"):
        durable.repository.create(
            durable.creator,
            replace_body(durable.body, "Changed request"),
            durable.key,
            "another_request",
            durable.provider.environment,
            durable.clock(),
        )


def replace_body(body, name):
    return CreateRequest(display_name=name, accepted_terms_revision=body.accepted_terms_revision)


def test_stale_terms_and_missing_policy_roll_back_all_setup_records(durable):
    wrong = CreateRequest(display_name="Generated buyer", accepted_terms_revision="unpublished")
    with pytest.raises(SetupError, match="TERMS_REVISION_STALE"):
        durable.repository.create(
            durable.creator,
            wrong,
            durable.key,
            "request",
            durable.provider.environment,
            durable.clock(),
        )
    absent = PostgresOrganizationRepository(durable.connection, None, durable.clock)
    with pytest.raises(SetupError, match="ADMISSION_POLICY_UNAVAILABLE"):
        absent.create(
            durable.creator,
            durable.body,
            durable.key,
            "request",
            durable.provider.environment,
            durable.clock(),
        )
    with durable.connection("c03_owner") as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM opentour_tenants.organizations WHERE creator_subject=%s",
                (durable.creator.subject,),
            ).fetchone()["n"]
            == 0
        )


def test_crash_after_provider_acceptance_reconciles_original_org_and_owner(durable):
    receipt = durable.create()
    durable.provider.lose_organization_response = True
    assert durable.worker.step(receipt.operation_id).state == "provider_outcome_unknown"
    durable.provider.lose_organization_response = False
    assert durable.worker.step(receipt.operation_id).state == "identity_bound"
    assert durable.worker.step(receipt.operation_id).state == "active"
    assert durable.worker.step(receipt.operation_id).state == "active"
    assert durable.provider.creates == durable.provider.member_creates == 1
    assert durable.repository.replay(durable.creator, durable.key, durable.body.digest()) == receipt
    with durable.connection("c03_owner") as conn:
        member = conn.execute(
            "SELECT status,provider_membership_id FROM opentour_tena"
            "nts.memberships WHERE organization_id=%s",
            (receipt.organization_id,),
        ).fetchone()
        assert member["status"] == "active" and member["provider_membership_id"]
        assert (
            conn.execute(
                "SELECT to_regclass('opentour_tenants.workspaces') AS table_name"
            ).fetchone()["table_name"]
            is None
        )


def test_claimed_but_unobserved_create_and_missing_inventory_never_resubmits(durable):
    receipt = durable.create()
    assert durable.store.claim(receipt.operation_id, "organization") is not None
    assert durable.store.claim(receipt.operation_id, "organization") is None
    for _ in range(3):
        current = durable.worker.step(receipt.operation_id)
        assert current.state == "provider_outcome_unknown"
    assert durable.provider.creates == 0


def test_cancel_during_unknown_retains_effect_and_never_activates(durable):
    receipt = durable.create()
    durable.provider.lose_organization_response = True
    unknown = durable.worker.step(receipt.operation_id)
    body_key = key_digest(uuid4().hex)
    cancelled = durable.repository.change(
        durable.creator,
        receipt.operation_id,
        "cancel",
        str(unknown.revision),
        body_key,
        "cancel_request",
        durable.clock(),
    )
    assert cancelled.state == "cancelled" and cancelled.remote_effect_state == "outcome_unknown"
    assert (
        durable.repository.change(
            durable.creator,
            receipt.operation_id,
            "cancel",
            str(unknown.revision),
            body_key,
            "another_request",
            durable.clock(),
        )
        == cancelled
    )
    resolved = durable.worker.step(receipt.operation_id)
    assert (
        resolved.state == "cancelled"
        and resolved.view("read").remote_effect_state == "known_retained"
    )
    assert durable.worker.step(receipt.operation_id).state == "cancelled"
    assert durable.provider.creates == 1 and durable.provider.member_creates == 0


def test_unknown_membership_recovers_without_second_create(durable):
    receipt = durable.create()
    durable.worker.step(receipt.operation_id)
    durable.provider.lose_member_response = True
    assert durable.worker.step(receipt.operation_id).state == "provider_outcome_unknown"
    durable.provider.lose_member_response = False
    assert durable.worker.step(receipt.operation_id).state == "active"
    assert durable.provider.member_creates == 1


@pytest.mark.parametrize("status", ["inactive", "pending"])
def test_known_nonactive_provider_membership_has_actionable_progress_without_owner_grant(
    durable, status
):
    receipt = durable.create()
    bound = durable.worker.step(receipt.operation_id)
    durable.provider.memberships[bound.provider_org_id, bound.creator_subject] = (
        MembershipObservation(
            "provider_member_" + uuid4().hex,
            bound.provider_org_id,
            bound.creator_subject,
            status,
            durable.clock(),
        )
    )
    result = durable.worker.step(receipt.operation_id)
    assert result.state == "action_required"
    view = result.view("read")
    assert view.reason_code == "PROVIDER_MEMBERSHIP_NOT_ACTIVE"
    assert view.remote_effect_state == "known_retained" and not view.retry_available
    assert durable.worker.step(receipt.operation_id) == result
    assert durable.provider.member_creates == 0
    with durable.connection("c03_owner") as conn:
        assert (
            conn.execute(
                "SELECT status FROM opentour_tenants.memberships WHERE organization_id=%s",
                (receipt.organization_id,),
            ).fetchone()["status"]
            == "provisional"
        )


def test_request_role_cannot_reopen_cancelled_or_advance_provider_state(durable):
    receipt = durable.create()
    with pytest.raises(psycopg.errors.InsufficientPrivilege), durable.connection() as conn:
        conn.execute(
            "SELECT set_config('opentour.identity_issuer',%s,true)", (durable.creator.issuer,)
        )
        conn.execute(
            "SELECT set_config('opentour.identity_subject',%s,true)", (durable.creator.subject,)
        )
        conn.execute(
            "UPDATE opentour_tenants.organizations SET state='identity_bound' WHERE id=%s",
            (receipt.organization_id,),
        )
    cancelled = durable.repository.change(
        durable.creator,
        receipt.operation_id,
        "cancel",
        "1",
        key_digest(uuid4().hex),
        "cancel",
        durable.clock(),
    )
    with durable.connection() as conn:
        conn.execute(
            "SELECT set_config('opentour.identity_issuer',%s,true)", (durable.creator.issuer,)
        )
        conn.execute(
            "SELECT set_config('opentour.identity_subject',%s,true)", (durable.creator.subject,)
        )
        result = conn.execute(
            "UPDATE opentour_tenants.organizations SET state='identity_provisioning' WHERE id=%s",
            (receipt.organization_id,),
        )
        assert result.rowcount == 0
    assert durable.store.load(receipt.operation_id).view("cancel") == cancelled
    assert durable.worker.step(receipt.operation_id).state == "cancelled"
    assert durable.provider.creates == 0


def test_cross_creator_reads_membership_insert_and_mixed_event_refs_are_denied(durable):
    receipt = durable.create()
    foreign = replace(durable.creator, subject="foreign_generated")
    with pytest.raises(SetupError, match="RESOURCE_NOT_FOUND"):
        durable.repository.get(foreign, receipt.operation_id)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), durable.connection() as conn:
        conn.execute("SELECT set_config('opentour.identity_issuer',%s,true)", (foreign.issuer,))
        conn.execute("SELECT set_config('opentour.identity_subject',%s,true)", (foreign.subject,))
        conn.execute(
            "INSERT INTO opentour_tenants.memberships VALUES(%s,%s,%"
            "s,%s,NULL,'provisional','owner',1,%s)",
            (
                receipt.organization_id,
                foreign.issuer,
                foreign.subject,
                uuid4().hex,
                durable.clock(),
            ),
        )
    second = durable.repository.create(
        durable.creator,
        durable.body,
        key_digest(uuid4().hex),
        "another",
        durable.provider.environment,
        durable.clock(),
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation), durable.connection("c03_owner") as conn:
        conn.execute(
            "INSERT INTO opentour_tenants.outbox VALUES(%s,%s,99,'organization.progressed',%s)",
            (receipt.operation_id, second.organization_id, durable.clock()),
        )


def test_runtime_permissions_refuse_identity_writes_terms_and_deletion(durable):
    for statement in (
        "DELETE FROM opentour_tenants.organizations",
        "UPDATE opentour_identity.sessions SET generation=1",
        "UPDATE opentour_tenants.terms_publication SET revision='other'",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), durable.connection() as conn:
            conn.execute(statement)
    with pytest.raises(SetupError, match="DATABASE_ROLE_INVALID"):
        PostgresOrganizationRepository(
            lambda: durable.connection("c03_owner"), GeneratedAdmission()
        ).terms(durable.clock())


def test_public_terms_reader_only_admits_the_read_only_metadata_login(durable):
    published = durable.repository.terms(durable.clock()).current_view(durable.clock())
    assert (
        read_public_organization_terms(
            lambda: durable.connection("c03_terms_reader", read_only=True)
        )
        == published
    )
    for role, read_only in (
        ("c03_terms_reader", False),
        ("c03_terms", True),
        ("c03_runtime", True),
        ("c03_worker", True),
        ("c03_terms_mixed", True),
        ("c03_terms_all", True),
    ):
        with pytest.raises(SetupError, match="TERMS_UNAVAILABLE"):
            read_public_organization_terms(partial(durable.connection, role, read_only=read_only))
    with durable.connection("c03_terms_mixed", read_only=True) as conn:
        assert (
            conn.execute("SELECT subject FROM opentour_identity.sessions LIMIT 0").fetchall() == []
        )
    for statement in (
        "SELECT subject FROM opentour_identity.sessions",
        "SELECT id FROM opentour_tenants.organizations",
        "UPDATE opentour_tenants.terms_publication SET revision='other'",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with durable.connection("c03_terms_reader") as conn:
                conn.execute(statement)


@pytest.mark.parametrize(
    "statement,resource",
    [
        ("UPDATE opentour_tenants.organizations SET identity_org_id='forged' WHERE id=%s", "org"),
        ("UPDATE opentour_tenants.organizations SET state='active' WHERE id=%s", "org"),
        ("UPDATE opentour_tenants.memberships SET status='active' WHERE organization_id=%s", "org"),
        (
            "UPDATE opentour_tenants.memberships SET provider_membership_id='forged' "
            "WHERE organization_id=%s",
            "org",
        ),
        (
            "UPDATE opentour_tenants.provisioning_operations SET stage_outcome='observed' "
            "WHERE id=%s",
            "operation",
        ),
        (
            "UPDATE opentour_tenants.provisioning_operations SET requested_display_name='forged' "
            "WHERE id=%s",
            "operation",
        ),
    ],
)
def test_request_role_cannot_bind_activate_or_rewrite_provider_intent(durable, statement, resource):
    receipt = durable.create()
    resource_id = receipt.organization_id if resource == "org" else receipt.operation_id
    with pytest.raises(psycopg.errors.InsufficientPrivilege), durable.connection() as conn:
        conn.execute(
            "SELECT set_config('opentour.identity_issuer',%s,true)", (durable.creator.issuer,)
        )
        conn.execute(
            "SELECT set_config('opentour.identity_subject',%s,true)", (durable.creator.subject,)
        )
        conn.execute(statement, (resource_id,))
    assert durable.store.load(receipt.operation_id).state == "identity_provisioning"


def test_request_role_cannot_publish_false_activation_event(durable):
    receipt = durable.create()
    with pytest.raises(psycopg.errors.InsufficientPrivilege), durable.connection() as conn:
        conn.execute(
            "SELECT set_config('opentour.identity_issuer',%s,true)", (durable.creator.issuer,)
        )
        conn.execute(
            "SELECT set_config('opentour.identity_subject',%s,true)", (durable.creator.subject,)
        )
        conn.execute(
            "INSERT INTO opentour_tenants.outbox VALUES(%s,%s,2,'organization.active',%s)",
            (receipt.operation_id, receipt.organization_id, durable.clock()),
        )


def test_metrics_report_unknown_cancellation_without_changing_durable_state(durable):
    receipt = durable.create()
    assert durable.store.claim(receipt.operation_id, "organization") is not None
    current = durable.store.load(receipt.operation_id)
    cancelled = durable.repository.change(
        durable.creator,
        receipt.operation_id,
        "cancel",
        str(current.revision),
        key_digest(uuid4().hex),
        "cancel_metrics",
        durable.clock(),
    )
    with durable.connection("c03_owner") as conn:
        conn.execute(
            "UPDATE opentour_tenants.provisioning_operations SET created_at=%s WHERE id=%s",
            (durable.clock() - timedelta(seconds=901), receipt.operation_id),
        )
    metrics = durable.store.metrics()
    assert metrics["unknown_count"] >= 1 and metrics["unknown_max_age_seconds"] >= 901
    assert metrics["attention_required"] is True
    after = durable.store.load(receipt.operation_id).view("cancel_metrics")
    assert after == cancelled and after.remote_effect_state == "outcome_unknown"


def test_session_expiry_after_lock_wait_cannot_admit_stale_timestamp(durable):
    started = durable.clock()
    with durable.connection("c03_owner") as conn:
        conn.execute(
            "UPDATE opentour_identity.sessions SET expires_at=%s,deny_until=%s WHERE session_id=%s",
            (
                started + timedelta(seconds=30),
                started + timedelta(seconds=30),
                durable.creator.session_id,
            ),
        )
    # A controlled trusted clock represents elapsed time during a lock/I/O wait;
    # real PostgreSQL takes the same person/session locks and rolls back all writes.
    repository = PostgresOrganizationRepository(
        durable.connection, GeneratedAdmission(), lambda: started + timedelta(seconds=31)
    )
    with pytest.raises(SetupError, match="SESSION_INVALID"):
        repository.create(
            durable.creator,
            durable.body,
            durable.key,
            "late",
            durable.provider.environment,
            started,
        )
    with durable.connection("c03_owner") as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM opentour_tenants.organizations WHERE creator_subject=%s",
                (durable.creator.subject,),
            ).fetchone()["n"]
            == 0
        )


def wait_for_database_block(durable, waiting_pid, blocking_pid):
    """Observe PostgreSQL's actual wait dependency, rather than infer it from a sleep."""
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with durable.connection("c03_owner") as connection:
            blockers = connection.execute(
                "SELECT pg_blocking_pids(%s) AS blockers", (waiting_pid,)
            ).fetchone()["blockers"]
        if blocking_pid in blockers:
            return
        time.sleep(0.01)
    pytest.fail("The expected PostgreSQL transaction did not block the competing operation")


def prepared_membership(durable):
    receipt = durable.create()
    durable.worker.step(receipt.operation_id)
    claimed = durable.store.claim(receipt.operation_id, "membership")
    assert claimed is not None
    observed = durable.provider.create_membership(claimed)
    return receipt, claimed, observed


@pytest.mark.parametrize("first", ["denial", "activation"])
@pytest.mark.parametrize("initial", ["absent", "active"])
def test_actual_activation_and_identity_writer_observe_the_same_denial_lock(
    durable, first, initial
):
    """Real171 SQL and142 writer; only the upstream provider response is generated."""
    receipt, _, observed = prepared_membership(durable)
    identity = PostgresIdentityRepository(lambda: durable.connection("c03_identity"))
    observation = Observation(
        durable.creator.issuer,
        observed.membership_id,
        observed.organization_id,
        observed.subject,
        "active",
        observed.checked_at,
        observed.checked_at,
    )
    if initial == "active":
        identity.record_observation(observation)
    ready, release = threading.Event(), threading.Event()
    started = {name: threading.Event() for name in ("denial", "activation")}
    pids = {}

    @contextmanager
    def guarded_connection(name, role):
        with durable.connection(role) as connection:
            connection.execute("SET LOCAL statement_timeout='5s'")
            pids[name] = connection.info.backend_pid
            started[name].set()
            yield connection
            if name == first:
                # Both real writers have finished their SQL but still own their
                # transaction locks. No synthetic activation table substitutes171.
                ready.set()
                assert release.wait(4), "Test did not release the first commit"

    writer = PostgresIdentityRepository(lambda: guarded_connection("denial", "c03_identity"))
    store = PostgresProvisioningStore(
        lambda: guarded_connection("activation", "c03_worker"), durable.clock
    )
    actions = {
        "denial": lambda: writer.record_observation(
            replace(observation, status="removed", locally_denied=True)
        ),
        "activation": lambda: store.activate(receipt.operation_id, observed),
    }
    second = "activation" if first == "denial" else "denial"
    with ThreadPoolExecutor(max_workers=2) as pool:
        earlier = pool.submit(actions[first])
        try:
            assert ready.wait(3), "First transaction did not reach its commit boundary"
            later = pool.submit(actions[second])
            assert started[second].wait(2)
            wait_for_database_block(durable, pids[second], pids[first])
            assert not later.done()
        finally:
            release.set()
        earlier.result(timeout=6)
        later.result(timeout=6)
    final = durable.store.load(receipt.operation_id)
    assert final.state == ("cancelled" if first == "denial" else "active")
    assert identity.observation(observation.issuer, observation.membership_id).locally_denied
    with durable.connection("c03_owner") as connection:
        member = connection.execute(
            "SELECT status FROM opentour_tenants.memberships WHERE organization_id=%s",
            (receipt.organization_id,),
        ).fetchone()
        assert member["status"] == ("removed" if first == "denial" else "active")
        active_events = connection.execute(
            "SELECT count(*) AS n FROM opentour_tenants.outbox "
            "WHERE operation_id=%s AND kind='organization.active'",
            (receipt.operation_id,),
        ).fetchone()["n"]
        assert active_events == (0 if first == "denial" else 1)
    # Activation-first establishes only that earlier commit. The later sticky
    # identity denial remains current; this test does not mint a capability lease.
    assert durable.provider.creates == durable.provider.member_creates == 1


@pytest.mark.parametrize("held_lock", ["membership", "observation"])
def test_provider_observation_expiring_during_actual_lock_wait_cannot_activate(durable, held_lock):
    receipt, claimed, observed = prepared_membership(durable)
    clock = [observed.checked_at]
    started = threading.Event()
    pids = {}

    @contextmanager
    def activation_connection():
        with durable.connection("c03_worker") as connection:
            connection.execute("SET LOCAL statement_timeout='5s'")
            pids["activation"] = connection.info.backend_pid
            started.set()
            yield connection

    store = PostgresProvisioningStore(activation_connection, lambda: clock[0])
    with ThreadPoolExecutor(max_workers=1) as pool:
        with durable.connection("c03_worker") as blocker:
            blocker.execute("SET LOCAL statement_timeout='5s'")
            if held_lock == "membership":
                blocker.execute(
                    "SELECT membership_id FROM opentour_tenants.memberships "
                    "WHERE organization_id=%s FOR UPDATE",
                    (receipt.organization_id,),
                )
            else:
                lock_membership_observation(blocker, durable.creator.issuer, observed.membership_id)
            activation = pool.submit(store.activate, receipt.operation_id, observed)
            assert started.wait(2)
            wait_for_database_block(durable, pids["activation"], blocker.info.backend_pid)
            assert not activation.done()
            # The wait is real; only elapsed trusted time is controlled, avoiding
            # an unnecessary31second sleep while testing the post-wait guard.
            clock[0] += timedelta(seconds=31)
        with pytest.raises(SetupError, match="PROVIDER_OBSERVATION_STALE"):
            activation.result(timeout=6)
    assert durable.store.load(receipt.operation_id) == claimed
    with durable.connection("c03_owner") as connection:
        assert (
            connection.execute(
                "SELECT status FROM opentour_tenants.memberships WHERE organization_id=%s",
                (receipt.organization_id,),
            ).fetchone()["status"]
            == "provisional"
        )
        assert (
            connection.execute(
                "SELECT count(*) AS n FROM opentour_tenants.outbox "
                "WHERE operation_id=%s AND kind='organization.active'",
                (receipt.operation_id,),
            ).fetchone()["n"]
            == 0
        )


def test_terms_publication_waits_for_locked_admission_and_preserves_original_receipt(durable):
    ready, release, publishing = threading.Event(), threading.Event(), threading.Event()
    pids = {}
    new_revision = "generated-terms-" + uuid4().hex

    class PausedAdmission:
        def check(self, connection, creator, now):
            pids["admission"] = connection.info.backend_pid
            ready.set()
            assert release.wait(4), "Test did not release the terms-locked admission"

    @contextmanager
    def admission_connection():
        with durable.connection() as connection:
            connection.execute("SET LOCAL statement_timeout='5s'")
            yield connection

    repository = PostgresOrganizationRepository(
        admission_connection, PausedAdmission(), durable.clock
    )

    def admit():
        return repository.create(
            durable.creator,
            durable.body,
            durable.key,
            "original_terms_receipt",
            durable.provider.environment,
            durable.clock(),
        )

    def publish():
        with durable.connection("c03_terms") as connection:
            connection.execute("SET LOCAL statement_timeout='5s'")
            connection.execute(
                "INSERT INTO opentour_tenants.terms_versions VALUES(%s,%s,%s)",
                (new_revision, "https://terms.example.invalid/generated-next", durable.clock()),
            )
            pids["publisher"] = connection.info.backend_pid
            publishing.set()
            connection.execute(
                "UPDATE opentour_tenants.terms_publication SET revision=%s WHERE singleton",
                (new_revision,),
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            admission = pool.submit(admit)
            try:
                assert ready.wait(3)
                publication = pool.submit(publish)
                assert publishing.wait(2)
                wait_for_database_block(durable, pids["publisher"], pids["admission"])
                assert not publication.done()
            finally:
                release.set()
            original = admission.result(timeout=6)
            publication.result(timeout=6)
        assert durable.repository.terms(durable.clock()).revision == new_revision
        assert durable.create("later_request_id") == original
        with pytest.raises(SetupError, match="TERMS_REVISION_STALE"):
            durable.repository.create(
                durable.creator,
                durable.body,
                key_digest(uuid4().hex),
                "stale_new_intent",
                durable.provider.environment,
                durable.clock(),
            )
        with durable.connection("c03_owner") as connection:
            assert (
                connection.execute(
                    "SELECT terms_revision FROM opentour_tenants.organizations WHERE id=%s",
                    (original.organization_id,),
                ).fetchone()["terms_revision"]
                == durable.body.accepted_terms_revision
            )
        assert durable.provider.creates == durable.provider.member_creates == 0
    finally:
        release.set()
        with durable.connection("c03_terms") as connection:
            connection.execute(
                "UPDATE opentour_tenants.terms_publication SET revision=%s WHERE singleton",
                (durable.body.accepted_terms_revision,),
            )


def test_committed_admission_acknowledgement_loss_replays_one_original_intent(durable):
    committed = []

    @contextmanager
    def lose_commit_acknowledgement():
        with durable.connection() as connection:
            yield connection
        committed.append(True)  # psycopg committed and closed before the simulated lost reply.
        raise psycopg.OperationalError("Generated commit acknowledgement loss")

    repository = PostgresOrganizationRepository(
        lose_commit_acknowledgement, clock=durable.clock
    )
    with pytest.raises(psycopg.OperationalError, match="Generated commit acknowledgement loss"):
        repository.create(
            durable.creator,
            durable.body,
            durable.key,
            "original_lost_reply",
            durable.provider.environment,
            durable.clock(),
        )
    assert committed == [True]
    replayed = durable.create("later_replay_request")
    assert replayed.request_id == "original_lost_reply" and replayed.revision == "1"
    assert durable.create("third_request") == replayed
    with durable.connection("c03_owner") as connection:
        for table in ("organizations", "provisioning_operations"):
            assert (
                connection.execute(
                    f"SELECT count(*) AS n FROM opentour_tenants.{table} WHERE creator_subject=%s",
                    (durable.creator.subject,),
                ).fetchone()["n"]
                == 1
            )
        assert (
            connection.execute(
                "SELECT count(*) AS n FROM opentour_tenants.request_dedup "
                "WHERE issuer=%s AND subject=%s",
                (durable.creator.issuer, durable.creator.subject),
            ).fetchone()["n"]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) AS n FROM opentour_tenants.outbox WHERE operation_id=%s",
                (replayed.operation_id,),
            ).fetchone()["n"]
            == 1
        )
    assert durable.provider.creates == durable.provider.member_creates == 0


def test_malformed_published_terms_roll_back_admission_and_acknowledgement(durable):
    bad_revision = "generated-bad-terms-" + uuid4().hex
    with durable.connection("c03_terms") as connection:
        connection.execute(
            "INSERT INTO opentour_tenants.terms_versions VALUES(%s,%s,%s)",
            (bad_revision, "http://terms.example.invalid", durable.clock()),
        )
        connection.execute(
            "UPDATE opentour_tenants.terms_publication SET revision=%s WHERE singleton",
            (bad_revision,),
        )
    try:
        with pytest.raises(SetupError, match="TERMS_UNAVAILABLE") as error:
            durable.repository.create(
                durable.creator,
                replace_body(durable.body, "Generated invalid publication"),
                durable.key,
                "generated_request",
                durable.provider.environment,
                durable.clock(),
            )
        assert error.value.status == 503
        with durable.connection("c03_owner") as connection:
            for table, column in (
                ("organizations", "creator_subject"),
                ("provisioning_operations", "creator_subject"),
                ("request_dedup", "subject"),
            ):
                assert (
                    connection.execute(
                        f"SELECT count(*) AS n FROM opentour_tenants.{table} WHERE {column}=%s",
                        (durable.creator.subject,),
                    ).fetchone()["n"]
                    == 0
                )
        assert durable.provider.creates == durable.provider.member_creates == 0
    finally:
        with durable.connection("c03_terms") as connection:
            connection.execute(
                "UPDATE opentour_tenants.terms_publication SET revision=%s WHERE singleton",
                (durable.body.accepted_terms_revision,),
            )


def test_waited_terms_admission_uses_post_lock_time_for_policy_and_acceptance(durable):
    start = durable.clock()
    effective = start + timedelta(seconds=10)
    clock = [start]
    revision = "generated-future-terms-" + uuid4().hex
    policy_times, pids = [], {}
    connecting = threading.Event()

    class RecordedPolicy:
        def check(self, connection, creator, now):
            policy_times.append(now)

    @contextmanager
    def admission_connection():
        with durable.connection() as connection:
            connection.execute("SET LOCAL statement_timeout='5s'")
            pids["admission"] = connection.info.backend_pid
            connecting.set()
            yield connection

    with durable.connection("c03_terms") as connection:
        connection.execute(
            "INSERT INTO opentour_tenants.terms_versions VALUES(%s,%s,%s)",
            (revision, "https://terms.example.invalid/generated-future", effective),
        )
        connection.execute(
            "UPDATE opentour_tenants.terms_publication SET revision=%s WHERE singleton",
            (revision,),
        )
    repository = PostgresOrganizationRepository(
        admission_connection, RecordedPolicy(), lambda: clock[0]
    )
    body = CreateRequest(display_name="Generated future terms", accepted_terms_revision=revision)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with durable.connection("c03_owner") as holder:
                holder.execute(
                    "SELECT revision FROM opentour_tenants.terms_versions "
                    "WHERE revision=%s FOR UPDATE",
                    (revision,),
                )
                admission = pool.submit(
                    repository.create,
                    durable.creator,
                    body,
                    durable.key,
                    "generated_after_lock",
                    durable.provider.environment,
                    start,
                )
                assert connecting.wait(2)
                wait_for_database_block(durable, pids["admission"], holder.info.backend_pid)
                assert not admission.done()
                clock[0] = effective + timedelta(seconds=1)
            receipt = admission.result(timeout=6)
        assert policy_times == [clock[0]]
        with durable.connection("c03_owner") as connection:
            row = connection.execute(
                "SELECT terms_accepted_at,created_at "
                "FROM opentour_tenants.organizations WHERE id=%s",
                (receipt.organization_id,),
            ).fetchone()
            assert row["terms_accepted_at"] == row["created_at"] == clock[0]
            assert row["terms_accepted_at"] >= effective
    finally:
        with durable.connection("c03_terms") as connection:
            connection.execute(
                "UPDATE opentour_tenants.terms_publication SET revision=%s WHERE singleton",
                (durable.body.accepted_terms_revision,),
            )


def test_two_verified_buyers_with_same_display_name_get_independent_owned_organizations(durable):
    first = durable.create()
    other = Creator(durable.creator.issuer, "user_" + uuid4().hex, "session_" + uuid4().hex, 0)
    now = durable.clock()
    with durable.connection("c03_owner") as connection:
        connection.execute(
            "INSERT INTO opentour_identity.sessions(issuer,session_id,subject,"
            "created_at,expires_at,deny_until) "
            "VALUES(%s,%s,%s,%s,%s,%s)",
            (
                other.issuer,
                other.session_id,
                other.subject,
                now - timedelta(seconds=1),
                now + timedelta(hours=24),
                now + timedelta(hours=24),
            ),
        )
    second = durable.repository.create(
        other,
        durable.body,
        durable.key,
        "generated_distinct_buyer",
        durable.provider.environment,
        durable.clock(),
    )
    assert first.organization_id != second.organization_id
    assert first.operation_id != second.operation_id
    assert durable.repository.get(durable.creator, first.operation_id).display_name == (
        durable.repository.get(other, second.operation_id).display_name
    )
    for creator, foreign in ((other, first), (durable.creator, second)):
        with pytest.raises(SetupError, match="RESOURCE_NOT_FOUND"):
            durable.repository.get(creator, foreign.operation_id)


def admit_new(durable, *, creator=None, key=None, body=None):
    return durable.repository.create(
        creator or durable.creator,
        body or durable.body,
        key or key_digest(uuid4().hex),
        "generated_rolling_admission",
        durable.provider.environment,
        durable.repository.clock(),
    )


def admission_counts(durable):
    with durable.connection("c03_owner") as connection:
        return tuple(
            connection.execute(query, (durable.creator.issuer, durable.creator.subject)).fetchone()[
                "n"
            ]
            for query in (
                "SELECT count(*) AS n FROM opentour_tenants.organizations "
                "WHERE creator_issuer=%s AND creator_subject=%s",
                "SELECT count(*) AS n FROM opentour_tenants.provisioning_operations "
                "WHERE creator_issuer=%s AND creator_subject=%s",
                "SELECT count(*) AS n FROM opentour_tenants.request_dedup "
                "WHERE issuer=%s AND subject=%s AND operation='organization.create'",
                "SELECT count(*) AS n FROM opentour_tenants.outbox e "
                "JOIN opentour_tenants.organizations o ON o.id=e.organization_id "
                "WHERE o.creator_issuer=%s AND o.creator_subject=%s",
            )
        )


def another_verified_session(durable, *, issuer=None, subject=None):
    creator = Creator(
        issuer or durable.creator.issuer,
        subject or durable.creator.subject,
        "session_" + uuid4().hex,
        0,
    )
    now = durable.repository.clock()
    with durable.connection("c03_owner") as connection:
        connection.execute(
            "INSERT INTO opentour_identity.sessions "
            "(issuer,session_id,subject,created_at,expires_at,deny_until) "
            "VALUES(%s,%s,%s,%s,%s,%s)",
            (
                creator.issuer,
                creator.session_id,
                creator.subject,
                now - timedelta(seconds=1),
                now + timedelta(hours=48),
                now + timedelta(hours=48),
            ),
        )
    return creator


def controlled_admission_time(durable):
    clock = [durable.clock()]
    durable.repository.clock = lambda: clock[0]
    with durable.connection("c03_owner") as connection:
        connection.execute(
            "UPDATE opentour_identity.sessions SET expires_at=%s,deny_until=%s "
            "WHERE issuer=%s AND session_id=%s",
            (
                clock[0] + timedelta(hours=48),
                clock[0] + timedelta(hours=48),
                durable.creator.issuer,
                durable.creator.session_id,
            ),
        )
    return clock


def test_default_policy_admits_five_and_refuses_sixth_before_durable_effect(durable):
    receipts = [admit_new(durable) for _ in range(5)]
    assert len({receipt.organization_id for receipt in receipts}) == 5
    assert admission_counts(durable) == (5, 5, 5, 5)
    with pytest.raises(SetupError, match="ADMISSION_LIMIT") as error:
        admit_new(durable)
    assert error.value.status == 429 and error.value.retryable
    assert 1 <= error.value.retry_after <= 86_400
    assert admission_counts(durable) == (5, 5, 5, 5)
    assert durable.provider.creates == durable.provider.member_creates == 0


def test_exact_replay_at_limit_is_free_and_changed_body_stays_conflict(durable):
    first = admit_new(durable, key=durable.key)
    for _ in range(4):
        admit_new(durable)
    assert admit_new(durable, key=durable.key) == first
    changed = replace_body(durable.body, "Generated changed input")
    with pytest.raises(SetupError, match="IDEMPOTENCY_CONFLICT") as error:
        admit_new(durable, key=durable.key, body=changed)
    assert error.value.status == 409
    with pytest.raises(SetupError, match="ADMISSION_LIMIT"):
        admit_new(durable)
    assert admission_counts(durable) == (5, 5, 5, 5)


@pytest.mark.parametrize(
    "outcome", ["active", "cancelled", "action_required", "provider_outcome_unknown"]
)
def test_completed_cancelled_failed_and_unknown_attempts_keep_their_slots(durable, outcome):
    first = admit_new(durable)
    for _ in range(4):
        admit_new(durable)
    if outcome == "active":
        durable.worker.step(first.operation_id)
        assert durable.worker.step(first.operation_id).state == outcome
    elif outcome == "cancelled":
        value = durable.repository.change(
            durable.creator,
            first.operation_id,
            "cancel",
            "1",
            key_digest(uuid4().hex),
            "generated_cancel",
            durable.clock(),
        )
        assert value.state == outcome
    elif outcome == "action_required":

        def rejected(operation):
            raise ProviderRejected()

        durable.provider.create_organization = rejected
        assert durable.worker.step(first.operation_id).state == outcome
    else:
        durable.provider.lose_organization_response = True
        assert durable.worker.step(first.operation_id).state == outcome
    before = admission_counts(durable)
    with pytest.raises(SetupError, match="ADMISSION_LIMIT"):
        admit_new(durable)
    assert admission_counts(durable) == before


def test_retry_reconciliation_and_cancellation_do_not_create_another_attempt(durable):
    first = admit_new(durable)
    durable.provider.lose_organization_response = True
    unknown = durable.worker.step(first.operation_id)
    retried = durable.repository.change(
        durable.creator,
        first.operation_id,
        "retry",
        str(unknown.revision),
        key_digest(uuid4().hex),
        "generated_retry",
        durable.clock(),
    )
    durable.repository.change(
        durable.creator,
        first.operation_id,
        "cancel",
        retried.revision,
        key_digest(uuid4().hex),
        "generated_cancel",
        durable.clock(),
    )
    durable.worker.step(first.operation_id)
    for _ in range(4):
        admit_new(durable)
    assert admission_counts(durable)[:3] == (5, 5, 5)
    with pytest.raises(SetupError, match="ADMISSION_LIMIT"):
        admit_new(durable)


def test_rolling_window_excludes_exact_24_hours_but_counts_one_microsecond_inside(durable):
    clock = controlled_admission_time(durable)
    start = clock[0]
    admit_new(durable)
    clock[0] = start + timedelta(hours=1)
    for _ in range(4):
        admit_new(durable)
    clock[0] = start + timedelta(hours=24) - timedelta(microseconds=1)
    with pytest.raises(SetupError, match="ADMISSION_LIMIT") as error:
        admit_new(durable)
    assert error.value.retry_after == 1
    clock[0] = start + timedelta(hours=24)
    admit_new(durable)
    with pytest.raises(SetupError, match="ADMISSION_LIMIT") as error:
        admit_new(durable)
    assert error.value.retry_after == 3600
    assert admission_counts(durable) == (6, 6, 6, 6)


def test_existing_over_limit_history_waits_for_fifth_newest_admission(durable):
    clock = controlled_admission_time(durable)
    start = clock[0]
    # Generated legacy history is explicit fixture setup, not a product policy option.
    durable.repository.policy = GeneratedAdmission()
    for index in range(7):
        clock[0] = start + timedelta(seconds=index)
        admit_new(durable)
    durable.repository = PostgresOrganizationRepository(durable.connection, clock=lambda: clock[0])
    clock[0] = start + timedelta(hours=12)
    with pytest.raises(SetupError, match="ADMISSION_LIMIT") as error:
        admit_new(durable)
    assert error.value.retry_after == 43_202
    assert admission_counts(durable) == (7, 7, 7, 7)


@pytest.mark.parametrize("other_identity", ["another_subject", "another_issuer", "same_person"])
def test_limit_binds_verified_issuer_subject_not_session_or_display_name(durable, other_identity):
    for _ in range(5):
        admit_new(durable)
    other = another_verified_session(
        durable,
        issuer="https://other-identity.example.invalid"
        if other_identity == "another_issuer"
        else None,
        subject="user_" + uuid4().hex if other_identity == "another_subject" else None,
    )
    if other_identity == "same_person":
        with pytest.raises(SetupError, match="ADMISSION_LIMIT"):
            admit_new(durable, creator=other)
    else:
        assert admit_new(durable, creator=other).operation_id
    assert admission_counts(durable) == (5, 5, 5, 5)


def test_twenty_distinct_concurrent_arrivals_across_sessions_admit_exactly_five(durable):
    other = another_verified_session(durable)
    barrier = threading.Barrier(20)
    pool_limit = threading.BoundedSemaphore(5)

    def submit(index):
        barrier.wait(timeout=5)
        with pool_limit:
            try:
                return admit_new(durable, creator=other if index % 2 else durable.creator)
            except SetupError as error:
                assert error.status == 429 and error.code == "ADMISSION_LIMIT"
                return None

    with ThreadPoolExecutor(max_workers=20) as pool:
        receipts = list(pool.map(submit, range(20)))
    accepted = [receipt for receipt in receipts if receipt is not None]
    assert len(accepted) == len({receipt.operation_id for receipt in accepted}) == 5
    assert receipts.count(None) == 15
    assert admission_counts(durable) == (5, 5, 5, 5)
    assert durable.provider.creates == 0


def test_pre_admission_terms_identity_and_configuration_refusals_consume_no_slots(durable):
    wrong = CreateRequest(display_name="Generated buyer", accepted_terms_revision="unpublished")
    with pytest.raises(SetupError, match="TERMS_REVISION_STALE"):
        admit_new(durable, body=wrong)
    foreign = replace(durable.creator, session_id="missing_generated_session")
    with pytest.raises(SetupError, match="SESSION_INVALID"):
        admit_new(durable, creator=foreign)
    durable.repository.policy = None
    with pytest.raises(SetupError, match="ADMISSION_POLICY_UNAVAILABLE"):
        admit_new(durable)
    assert admission_counts(durable) == (0, 0, 0, 0)
    durable.repository = PostgresOrganizationRepository(durable.connection, clock=durable.clock)
    for _ in range(5):
        admit_new(durable)
    assert admission_counts(durable) == (5, 5, 5, 5)


def test_rolling_window_is_sampled_after_actual_terms_lock_wait(durable):
    clock = controlled_admission_time(durable)
    start = clock[0]
    for _ in range(5):
        admit_new(durable)
    clock[0] = start + timedelta(hours=24) - timedelta(microseconds=1)
    connecting = threading.Event()
    pids = {}

    @contextmanager
    def admission_connection():
        with durable.connection() as connection:
            connection.execute("SET LOCAL statement_timeout='5s'")
            pids["admission"] = connection.info.backend_pid
            connecting.set()
            yield connection

    repository = PostgresOrganizationRepository(admission_connection, clock=lambda: clock[0])
    with ThreadPoolExecutor(max_workers=1) as pool:
        with durable.connection("c03_owner") as holder:
            holder.execute(
                "SELECT revision FROM opentour_tenants.terms_versions "
                "WHERE revision=%s FOR UPDATE",
                (durable.body.accepted_terms_revision,),
            )
            admission = pool.submit(
                repository.create,
                durable.creator,
                durable.body,
                key_digest(uuid4().hex),
                "generated_window_after_terms_lock",
                durable.provider.environment,
                clock[0],
            )
            assert connecting.wait(2)
            wait_for_database_block(durable, pids["admission"], holder.info.backend_pid)
            assert not admission.done()
            clock[0] = start + timedelta(hours=24)
        receipt = admission.result(timeout=6)
    assert admission_counts(durable) == (6, 6, 6, 6)
    assert durable.repository.get(durable.creator, receipt.operation_id).created_at == clock[0]


def test_clock_rollback_after_terms_wait_cannot_hide_the_fifth_admission(durable):
    clock = controlled_admission_time(durable)
    start = clock[0]
    for _ in range(4):
        admit_new(durable)
    connecting = threading.Event()
    pids = {}

    @contextmanager
    def admission_connection():
        with durable.connection() as connection:
            connection.execute("SET LOCAL statement_timeout='5s'")
            pids["admission"] = connection.info.backend_pid
            connecting.set()
            yield connection

    repository = PostgresOrganizationRepository(admission_connection, clock=lambda: clock[0])
    with ThreadPoolExecutor(max_workers=1) as pool:
        with durable.connection("c03_owner") as holder:
            holder.execute(
                "SELECT revision FROM opentour_tenants.terms_versions "
                "WHERE revision=%s FOR UPDATE",
                (durable.body.accepted_terms_revision,),
            )
            admission = pool.submit(
                repository.create,
                durable.creator,
                durable.body,
                key_digest(uuid4().hex),
                "generated_waited_fifth_admission",
                durable.provider.environment,
                start,
            )
            assert connecting.wait(2)
            wait_for_database_block(durable, pids["admission"], holder.info.backend_pid)
            clock[0] = start + timedelta(seconds=10)
        fifth = admission.result(timeout=6)
    assert durable.repository.get(durable.creator, fifth.operation_id).created_at == clock[0]
    with durable.connection("c03_owner") as connection:
        assert connection.execute(
            "SELECT last_checked_at FROM opentour_tenants.admission_locks "
            "WHERE issuer=%s AND subject=%s",
            (durable.creator.issuer, durable.creator.subject),
        ).fetchone()["last_checked_at"] == clock[0]
    clock[0] = start + timedelta(seconds=5)
    with pytest.raises(SetupError, match="CLOCK_UNAVAILABLE"):
        admit_new(durable)
    clock[0] = start + timedelta(seconds=10)
    with pytest.raises(SetupError, match="ADMISSION_LIMIT"):
        admit_new(durable)
    assert admission_counts(durable) == (5, 5, 5, 5)


def test_backward_clock_cannot_reopen_rolling_capacity(durable):
    clock = controlled_admission_time(durable)
    for _ in range(5):
        admit_new(durable)
    clock[0] -= timedelta(microseconds=1)
    with pytest.raises(SetupError, match="CLOCK_UNAVAILABLE"):
        admit_new(durable)
    assert admission_counts(durable) == (5, 5, 5, 5)


@pytest.mark.parametrize("table", ["organizations", "provisioning_operations"])
def test_request_role_cannot_rewrite_original_admission_time(durable, table):
    receipt = admit_new(durable)
    identity = receipt.organization_id if table == "organizations" else receipt.operation_id
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with durable.repository._connection(durable.creator) as connection:
            connection.execute(
                f"UPDATE opentour_tenants.{table} SET created_at=created_at-interval '1 day' "
                "WHERE id=%s",
                (identity,),
            )
    for _ in range(4):
        admit_new(durable)
    with pytest.raises(SetupError, match="ADMISSION_LIMIT"):
        admit_new(durable)
