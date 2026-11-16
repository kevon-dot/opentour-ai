"""Actual private PostgreSQL transactions with explicitly simulated AWS responses.

This fixture is not the OPE-143 production reservation/grant implementation, an
AWS deployment, RLS isolation proof or OPE-216 completed restore orchestration.
It exercises the real journal service/client against durable generated SQL result
records, so a caller's timeout assertion cannot invent a committed/aborted result.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from opentour_audit.journal import ReconciledTransaction
from opentour_journal.crypto import canonical, model_digest, sha256
from opentour_journal.models import ImmutableMetadataRef, JournalError

from tools.fixtures.postgres import DisposablePostgres

pytestmark = pytest.mark.local_integration
TARGET = "tests/integration/authority_journal_recovery.py"
MODES = ("customer_controlled", "opentour_managed")
ROOT = Path(__file__).resolve().parents[2]


def generated_helpers():
    # One fixed, owned fixture module; never an arbitrary plugin or request path.
    source = ROOT / "services/audit-journal/tests/test_append_protocol.py"
    spec = importlib.util.spec_from_file_location("generated_journal_fixtures", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cluster() -> Iterator[DisposablePostgres]:
    if os.environ.get("OPENTOUR_POSTGRES_TARGET") != TARGET:
        raise RuntimeError("Use the exact registered authority_journal_recovery command")
    fixture = DisposablePostgres()
    with fixture:
        fixture.sql(
            "CREATE ROLE c03_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOBYPASSRLS;"
            "CREATE SCHEMA generated_journal;"
            "CREATE TABLE generated_journal.transactions ("
            "organization_id text NOT NULL, operation_id text PRIMARY KEY,"
            "intent_digest text NOT NULL,"
            "result text NOT NULL CHECK(result IN ('committed','aborted')),"
            "epoch bigint NOT NULL, authority_revision text NOT NULL);"
            "CREATE TABLE generated_journal.outbox (operation_id text PRIMARY KEY);"
            "CREATE TABLE generated_journal.restored_state (epoch bigint NOT NULL,"
            "admission_enabled boolean NOT NULL DEFAULT false);"
            "GRANT USAGE ON SCHEMA generated_journal TO c03_runtime;"
            "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES "
            "IN SCHEMA generated_journal TO c03_runtime;",
            role="c03_owner",
        )
        yield fixture
    assert fixture.cleanup_complete


@pytest.fixture
def sql(cluster):
    assert cluster.socket is not None
    with psycopg.connect(
        host=str(cluster.socket),
        port=5432,
        dbname="postgres",
        user="c03_runtime",
        autocommit=True,
        connect_timeout=3,
        options="-c statement_timeout=3000",
    ) as connection:
        connection.execute("DELETE FROM generated_journal.transactions")
        connection.execute("DELETE FROM generated_journal.outbox")
        connection.execute("DELETE FROM generated_journal.restored_state")
        yield connection


@pytest.fixture
def journal(sql):
    helpers = generated_helpers()
    system = helpers.build_system()

    def reconcile(principal, body, intent):
        row = sql.execute(
            "SELECT organization_id,operation_id,intent_digest,result,epoch,authority_revision "
            "FROM generated_journal.transactions WHERE operation_id=%s",
            (body.operation_id,),
        ).fetchone()
        if row is None:
            raise JournalError("JOURNAL_RECONCILIATION_REQUIRED")
        expected_ref = transaction_reference(row)
        if body.control_transaction_ref != expected_ref:
            raise JournalError("JOURNAL_RECONCILIATION_REQUIRED")
        return ReconciledTransaction(row[0], row[1], row[2], expected_ref, row[3], row[4], row[5])

    system.authority.reconcile_transaction = reconcile
    return helpers, system


def transaction_reference(row):
    return ImmutableMetadataRef(
        record_id=row[1],
        revision="generated-transaction-v1",
        sha256=sha256(canonical(list(row))),
    )


def persist_result(sql, body, result, epoch, revision):
    row = (body.organization_id, body.operation_id, model_digest(body), result, epoch, revision)
    with sql.transaction():
        sql.execute("INSERT INTO generated_journal.transactions VALUES (%s,%s,%s,%s,%s,%s)", row)
        sql.execute("INSERT INTO generated_journal.outbox VALUES (%s)", (body.operation_id,))
    return transaction_reference(row)


@pytest.mark.parametrize("mode", MODES)
def test_actual_runtime_role_and_rollback_keep_intent_unresolved(sql, journal, mode):
    helpers, system = journal
    role = sql.execute(
        "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
    ).fetchone()
    assert role == (False, False)
    body = helpers.intent(system)
    system.client.append_intent(body)
    with pytest.raises(RuntimeError):
        with sql.transaction():
            sql.execute("INSERT INTO generated_journal.outbox VALUES (%s)", (body.operation_id,))
            raise RuntimeError("generated rollback")
    assert sql.execute("SELECT count(*) FROM generated_journal.outbox").fetchone()[0] == 0
    with pytest.raises(JournalError, match="JOURNAL_RECONCILIATION_REQUIRED"):
        system.client.append_outcome(helpers.outcome(system, body, "aborted"))
    frontier = system.client.verify_frontier("org-a", system.evidence)
    assert frontier.unresolved_kind == "restrict"
    with pytest.raises(JournalError, match="AUTHORITY_OPERATION_PENDING"):
        frontier.require_resolved()


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("kind", ["grant", "restrict"])
def test_actual_recorded_abort_is_bound_to_operation_and_prior_state(sql, journal, mode, kind):
    helpers, system = journal
    body = helpers.intent(system, kind=kind)
    system.client.append_intent(body)
    ref = persist_result(sql, body, "aborted", 6, "authority-six")
    aborted = helpers.outcome(system, body, "aborted").model_copy(
        update={"control_transaction_ref": ref}
    )
    system.client.append_outcome(aborted)
    restored = system.client.verify_frontier("org-a", system.evidence)
    restored.require_resolved()
    assert restored.head.latest_epoch == 6
    assert sql.execute("SELECT count(*) FROM generated_journal.outbox").fetchone()[0] == 1


@pytest.mark.parametrize("mode", MODES)
def test_actual_sql_commit_with_unknown_outcome_ack_replays_once(sql, journal, mode):
    helpers, system = journal
    body = helpers.intent(system)
    system.client.append_intent(body)
    ref = persist_result(
        sql, body, "committed", body.proposed_epoch, body.proposed_authority_revision
    )
    committed = helpers.outcome(system, body).model_copy(update={"control_transaction_ref": ref})
    system.ddb.lose_ack = True  # Explicit simulated AWS transport loss after durable fake commit.
    original = system.client.append_outcome(committed)
    assert system.client.append_outcome(committed) == original
    assert system.store.head("org-a").sequence == 2
    assert sql.execute("SELECT count(*) FROM generated_journal.transactions").fetchone()[0] == 1
    assert sql.execute("SELECT count(*) FROM generated_journal.outbox").fetchone()[0] == 1


@pytest.mark.parametrize("mode", MODES)
def test_durable_foreign_or_changed_reconciliation_record_cannot_resolve(sql, journal, mode):
    helpers, system = journal
    body = helpers.intent(system)
    system.client.append_intent(body)
    ref = persist_result(sql, body, "aborted", 6, "authority-six")
    sql.execute("UPDATE generated_journal.transactions SET organization_id='org-b'")
    with pytest.raises(JournalError, match="JOURNAL_RECONCILIATION_REQUIRED"):
        system.client.append_outcome(
            helpers.outcome(system, body, "aborted").model_copy(
                update={"control_transaction_ref": ref},
            )
        )
    assert system.store.head("org-a").sequence == 1


@pytest.mark.parametrize("mode", MODES)
def test_restored_sql_epoch_never_substitutes_for_independent_head(sql, journal, mode):
    helpers, system = journal
    for operation in ["operation-seven", "operation-eight"]:
        body = helpers.intent(system, operation)
        system.client.append_intent(body)
        ref = persist_result(
            sql, body, "committed", body.proposed_epoch, body.proposed_authority_revision
        )
        system.client.append_outcome(
            helpers.outcome(system, body).model_copy(
                update={"control_transaction_ref": ref},
            )
        )
    sql.execute("INSERT INTO generated_journal.restored_state(epoch) VALUES (6)")
    frontier = system.client.verify_frontier("org-a", system.evidence)
    assert frontier.head.latest_epoch == 8
    assert sql.execute(
        "SELECT epoch,admission_enabled FROM generated_journal.restored_state"
    ).fetchone() == (6, False)
    # OPE-216 still owns recovered generation, identity/runner reconciliation and admission.
    assert not hasattr(frontier, "issuance_generation")
