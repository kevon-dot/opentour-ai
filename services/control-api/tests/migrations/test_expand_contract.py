"""Actual disposable PostgreSQL proof for OPE-213; never qualifies RDS/AWS gates."""

from __future__ import annotations

import os
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from opentour_control.platform import schema_compatibility as guard
from psycopg import Connection

from tools.fixtures.postgres import DisposablePostgres

pytestmark = pytest.mark.local_integration
OLD, NEW, BACKUP = "a" * 64, "b" * 64, "c" * 64
PLAN = guard.MigrationPlan("generated-expand", 1, 2, NEW)


@pytest.fixture(scope="module")
def cluster() -> Iterator[DisposablePostgres]:
    if os.environ.get("OPENTOUR_POSTGRES_TARGET") != (
        "services/control-api/tests/migrations/test_expand_contract.py"
    ):
        raise RuntimeError("Use the exact registered PostgreSQL test-python target")
    # Missing binaries raise a real prerequisite error; never skip or pass.
    fixture = DisposablePostgres()
    with fixture:
        fixture.sql(
            "CREATE ROLE c03_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOBYPASSRLS;",
            role="c03_owner",
        )
        yield fixture
    assert fixture.cleanup_complete


def connect(cluster: DisposablePostgres, user: str = "c03_owner") -> Connection[Any]:
    assert cluster.socket is not None
    return psycopg.connect(
        host=str(cluster.socket),
        port=5432,
        dbname="postgres",
        user=user,
        autocommit=True,
        connect_timeout=3,
        options="-c statement_timeout=3000",
    )


@pytest.fixture
def owner(cluster: DisposablePostgres) -> Iterator[Connection[Any]]:
    with connect(cluster) as connection:
        connection.execute("DROP SCHEMA IF EXISTS opentour_platform CASCADE")
        connection.execute("DROP TABLE IF EXISTS public.generated_payload")
        connection.execute(
            "CREATE TABLE public.generated_payload (id integer PRIMARY KEY, legacy text NOT NULL)"
        )
        connection.execute(
            "INSERT INTO public.generated_payload VALUES (1,'generated-one'),(2,'generated-two')"
        )
        guard.initialize(connection, 1, OLD)
        yield connection


def add_column(connection: Connection[Any]) -> None:
    connection.execute("ALTER TABLE public.generated_payload ADD COLUMN replacement text")


def complete_batch(connection: Connection[Any], cursor: str | None) -> guard.Progress:
    connection.execute("UPDATE public.generated_payload SET replacement=legacy")
    return guard.Progress("2", True)


def verify_copy(connection: Connection[Any]) -> bool:
    row = connection.execute(
        "SELECT count(*) FROM public.generated_payload WHERE replacement IS DISTINCT FROM legacy"
    ).fetchone()
    return row is not None and row[0] == 0


def remove_legacy(connection: Connection[Any]) -> None:
    connection.execute("ALTER TABLE public.generated_payload DROP COLUMN legacy")


def switched(connection: Connection[Any]) -> None:
    guard.expand(connection, PLAN, add_column)
    guard.backfill(connection, PLAN, complete_batch)
    guard.verify(connection, PLAN, verify_copy)
    guard.switch(connection, PLAN)


def test_actual_expand_backfill_verify_switch_contract_and_retry(owner: Connection[Any]) -> None:
    first = guard.expand(owner, PLAN, add_column)
    assert (first.phase, first.read_path, first.schema_version) == ("expanded", "legacy", 2)
    assert guard.expand(owner, PLAN, add_column) == first
    assert guard.readiness(owner, 1).software_sha256 == OLD
    filled = guard.backfill(owner, PLAN, complete_batch)
    assert filled.progress_cursor == "2" and filled.backfill_complete
    assert guard.backfill(owner, PLAN, complete_batch) == filled
    verified = guard.verify(owner, PLAN, verify_copy)
    assert (verified.phase, verified.read_path) == ("verified", "dual")
    state = guard.switch(owner, PLAN)
    assert state.software_sha256 == NEW and state.read_path == "new"
    final = guard.contract(owner, PLAN, BACKUP, lambda digest: digest == BACKUP, remove_legacy)
    assert final.minimum_reader == final.schema_version == 2
    assert final.phase == "contracted" and final.backup_checkpoint_sha256 == BACKUP
    assert guard.contract(owner, PLAN, BACKUP, lambda _: False, remove_legacy) == final
    assert owner.execute(
        "SELECT replacement FROM public.generated_payload ORDER BY id"
    ).fetchall() == [("generated-one",), ("generated-two",)]


def test_runtime_role_can_read_but_cannot_drop_or_own_schema(
    owner: Connection[Any], cluster: DisposablePostgres
) -> None:
    owner.execute("GRANT USAGE ON SCHEMA opentour_platform TO c03_runtime")
    owner.execute(
        "GRANT SELECT ON opentour_platform.schema_state,public.generated_payload TO c03_runtime"
    )
    owner.execute("ALTER TABLE public.generated_payload ENABLE ROW LEVEL SECURITY")
    owner.execute("ALTER TABLE public.generated_payload FORCE ROW LEVEL SECURITY")
    owner.execute(
        "CREATE POLICY generated_scope ON public.generated_payload TO c03_runtime "
        "USING (id::text=current_setting('opentour.generated_scope',true))"
    )
    with connect(cluster, "c03_runtime") as runtime:
        row = runtime.execute(
            "SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole "
            "FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
        assert row == (False, False, False, False)
        assert runtime.execute(
            "SELECT tableowner=current_user FROM pg_tables "
            "WHERE schemaname='public' AND tablename='generated_payload'"
        ).fetchone() == (False,)
        with guard.reader_transaction(runtime, 1):
            runtime.execute("SELECT set_config('opentour.generated_scope','1',true)")
            assert runtime.execute("SELECT count(*) FROM public.generated_payload").fetchone() == (
                1,
            )
        assert runtime.execute("SELECT count(*) FROM public.generated_payload").fetchone() == (0,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runtime.execute("DROP TABLE public.generated_payload")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runtime.execute("UPDATE opentour_platform.schema_state SET minimum_reader=1")
    assert owner.execute("SELECT count(*) FROM public.generated_payload").fetchone() == (2,)


def test_concurrent_migration_has_one_winner(
    owner: Connection[Any], cluster: DisposablePostgres
) -> None:
    with connect(cluster) as contender:

        def expand_while_contender_tries(connection: Connection[Any]) -> None:
            with pytest.raises(guard.CompatibilityError, match="MIGRATION_OR_READER_ACTIVE"):
                guard.expand(contender, PLAN, add_column)
            add_column(connection)

        result = guard.expand(owner, PLAN, expand_while_contender_tries)
        assert result.revision == 2
        assert guard.expand(contender, PLAN, add_column) == result


def test_old_reader_blocks_contract_then_fails_readiness_after_contract(
    owner: Connection[Any], cluster: DisposablePostgres
) -> None:
    switched(owner)
    with connect(cluster) as reader:
        with guard.reader_transaction(reader, 1):
            with pytest.raises(guard.CompatibilityError, match="MIGRATION_OR_READER_ACTIVE"):
                guard.contract(owner, PLAN, BACKUP, lambda _: True, remove_legacy)
            assert reader.execute(
                "SELECT legacy FROM public.generated_payload WHERE id=1"
            ).fetchone() == ("generated-one",)
        guard.contract(owner, PLAN, BACKUP, lambda _: True, remove_legacy)
        with pytest.raises(guard.CompatibilityError, match="UNSUPPORTED_SCHEMA_VERSION"):
            guard.readiness(reader, 1)
        with pytest.raises(guard.CompatibilityError, match="UNSUPPORTED_SCHEMA_VERSION"):
            with guard.reader_transaction(reader, 1):
                pytest.fail("old reader entered contracted schema")
        assert guard.readiness(reader, 2).phase == "contracted"


def test_failed_backfill_rolls_back_batch_and_retains_explicit_progress(
    owner: Connection[Any],
) -> None:
    guard.expand(owner, PLAN, add_column)

    def first(connection: Connection[Any], cursor: str | None) -> guard.Progress:
        assert cursor is None
        connection.execute("UPDATE public.generated_payload SET replacement=legacy WHERE id=1")
        return guard.Progress("1", False)

    state = guard.backfill(owner, PLAN, first)

    def fail(connection: Connection[Any], cursor: str | None) -> guard.Progress:
        assert cursor == "1"
        connection.execute(
            "UPDATE public.generated_payload SET replacement='failed-sentinel' WHERE id=2"
        )
        raise RuntimeError("generated private failure")

    with pytest.raises(guard.CompatibilityError, match="^BACKFILL_FAILED$") as caught:
        guard.backfill(owner, PLAN, fail)
    rendered = "".join(traceback.format_exception(caught.value))
    assert "BACKFILL_FAILED" in rendered
    assert "generated private failure" not in rendered
    failed = guard.readiness(owner, 1)
    assert (failed.phase, failed.read_path, failed.progress_cursor) == (
        "backfill_failed",
        "legacy",
        "1",
    )
    assert failed.revision == state.revision + 1
    assert owner.execute(
        "SELECT replacement FROM public.generated_payload WHERE id=2"
    ).fetchone() == (None,)
    assert guard.backfill(owner, PLAN, complete_batch).backfill_complete


@pytest.mark.parametrize("stage", ["stable", "expanded", "backfilling", "verified"])
def test_destructive_contract_refuses_missing_phase_proof(
    owner: Connection[Any], stage: str
) -> None:
    if stage != "stable":
        guard.expand(owner, PLAN, add_column)
    if stage in {"backfilling", "verified"}:
        guard.backfill(owner, PLAN, complete_batch)
    if stage == "verified":
        guard.verify(owner, PLAN, verify_copy)
    with pytest.raises(guard.CompatibilityError):
        guard.contract(owner, PLAN, BACKUP, lambda _: True, remove_legacy)
    assert owner.execute("SELECT legacy FROM public.generated_payload WHERE id=1").fetchone() == (
        "generated-one",
    )


def test_checkpoint_digest_requires_independent_verification(owner: Connection[Any]) -> None:
    switched(owner)
    with pytest.raises(guard.CompatibilityError, match="BACKUP_CHECKPOINT_UNVERIFIED"):
        guard.contract(owner, PLAN, BACKUP, lambda _: False, remove_legacy)
    assert guard.readiness(owner, 1).phase == "switched"


def test_partial_or_false_verified_backfill_cannot_switch(owner: Connection[Any]) -> None:
    guard.expand(owner, PLAN, add_column)
    with pytest.raises(guard.CompatibilityError, match="BACKFILL_NOT_COMPLETE"):
        guard.verify(owner, PLAN, verify_copy)
    guard.backfill(owner, PLAN, complete_batch)
    with pytest.raises(guard.CompatibilityError, match="BACKFILL_NOT_VERIFIED"):
        guard.verify(owner, PLAN, lambda _: False)
    with pytest.raises(guard.CompatibilityError, match="BACKFILL_NOT_VERIFIED"):
        guard.switch(owner, PLAN)


def test_failed_ddl_transaction_has_no_schema_or_revision_effect(owner: Connection[Any]) -> None:
    def fail(connection: Connection[Any]) -> None:
        add_column(connection)
        raise RuntimeError("generated failure")

    with pytest.raises(RuntimeError):
        guard.expand(owner, PLAN, fail)
    assert guard.readiness(owner, 1).revision == 1
    assert owner.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name='generated_payload' AND column_name='replacement'"
    ).fetchone() == (0,)


def test_changed_plan_or_initialization_refused(owner: Connection[Any]) -> None:
    guard.initialize(owner, 1, OLD)
    with pytest.raises(guard.CompatibilityError, match="INITIALIZATION_CONFLICT"):
        guard.initialize(owner, 1, NEW)
    guard.expand(owner, PLAN, add_column)
    with pytest.raises(guard.CompatibilityError, match="MIGRATION_PLAN_CONFLICT"):
        guard.expand(owner, guard.MigrationPlan(PLAN.plan_id, 1, 2, "d" * 64), add_column)


def test_revision_overflow_refuses_effect_before_callback(owner: Connection[Any]) -> None:
    owner.execute("UPDATE opentour_platform.schema_state SET revision=9223372036854775807")
    with pytest.raises(guard.CompatibilityError, match="REVISION_EXHAUSTED"):
        guard.expand(owner, PLAN, add_column)
    assert guard.readiness(owner, 1).schema_version == 1


def test_non_autocommit_or_nested_transaction_refused(owner: Connection[Any]) -> None:
    with owner.transaction():
        with pytest.raises(guard.CompatibilityError, match="DEDICATED_IDLE"):
            guard.readiness(owner, 1)
    owner.autocommit = False
    try:
        with pytest.raises(guard.CompatibilityError, match="DEDICATED_IDLE"):
            guard.readiness(owner, 1)
    finally:
        owner.autocommit = True


@pytest.mark.parametrize("version", [0, -1, True, 1.0, 2_147_483_648])
def test_malformed_version_is_not_coerced(owner: Connection[Any], version: Any) -> None:
    with pytest.raises(guard.CompatibilityError, match="INVALID_SCHEMA_VERSION"):
        guard.readiness(owner, version)


def test_failure_record_does_not_overwrite_a_newer_successful_batch(
    owner: Connection[Any], cluster: DisposablePostgres, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard.expand(owner, PLAN, add_column)
    original = guard._transaction
    failed_attempt = False
    interleaved = False
    with connect(cluster) as contender:

        @contextmanager
        def transaction(connection: Connection[Any], *, shared: bool = False) -> Iterator[None]:
            nonlocal interleaved
            try:
                with original(connection, shared=shared):
                    yield
            finally:
                # The original transaction has released its DB lock before this retry.
                if connection is owner and failed_attempt and not interleaved:
                    interleaved = True
                    guard.backfill(contender, PLAN, complete_batch)

        monkeypatch.setattr(guard, "_transaction", transaction)

        def fail(connection: Connection[Any], cursor: str | None) -> guard.Progress:
            nonlocal failed_attempt
            failed_attempt = True
            raise RuntimeError("generated failed attempt")

        with pytest.raises(guard.CompatibilityError, match="BACKFILL_FAILED"):
            guard.backfill(owner, PLAN, fail)
        state = guard.readiness(owner, 1)
        assert state.phase == "backfilling" and state.backfill_complete
        assert state.progress_cursor == "2" and state.revision == 3
