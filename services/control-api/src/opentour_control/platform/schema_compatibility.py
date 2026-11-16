"""Transactional expand/contract guard for approved migration implementations.

OPE-215 owns migration plans and rollout orchestration; OPE-142 owns identity DDL.
Callbacks below are reviewed in-process migration code, never request-supplied SQL.
Every participating reader takes the shared lock for its complete DB transaction.
Unintegrated callers are not certified compatible by this module.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from psycopg import Connection
from psycopg.pq import TransactionStatus

LOCK_ID = 213_032_002
SCHEMA = """
CREATE SCHEMA IF NOT EXISTS opentour_platform;
CREATE TABLE IF NOT EXISTS opentour_platform.schema_state (
 singleton boolean PRIMARY KEY CHECK (singleton),
 schema_version integer NOT NULL CHECK (schema_version > 0),
 minimum_reader integer NOT NULL CHECK (minimum_reader > 0),
 software_sha256 text NOT NULL CHECK (software_sha256 ~ '^[a-f0-9]{64}$'),
 plan_id text,
 target_version integer,
 target_software_sha256 text,
 phase text NOT NULL CHECK (phase IN
  ('stable','expanded','backfilling','backfill_failed','verified','switched','contracted')),
 read_path text NOT NULL CHECK (read_path IN ('legacy','dual','new')),
 progress_cursor text,
 backfill_complete boolean NOT NULL DEFAULT false,
 backup_checkpoint_sha256 text,
 revision bigint NOT NULL DEFAULT 1 CHECK(revision > 0),
 CHECK (minimum_reader <= schema_version)
);
REVOKE ALL ON SCHEMA opentour_platform FROM PUBLIC;
REVOKE ALL ON opentour_platform.schema_state FROM PUBLIC;
"""


class CompatibilityError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _digest(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise CompatibilityError("INVALID_MANIFEST_DIGEST")


def _version(value: int) -> None:
    if type(value) is not int or not 1 <= value <= 2_147_483_647:
        raise CompatibilityError("INVALID_SCHEMA_VERSION")


@dataclass(frozen=True)
class MigrationPlan:
    plan_id: str
    from_version: int
    to_version: int
    software_sha256: str

    def validate(self) -> None:
        if not isinstance(self.plan_id, str) or not re.fullmatch(
            r"[a-z0-9_-]{1,128}", self.plan_id
        ):
            raise CompatibilityError("INVALID_PLAN_ID")
        _version(self.from_version)
        _version(self.to_version)
        _digest(self.software_sha256)
        if self.to_version != self.from_version + 1:
            raise CompatibilityError("NONSEQUENTIAL_MIGRATION")


@dataclass(frozen=True)
class Progress:
    cursor: str
    complete: bool

    def validate(self) -> None:
        if (
            not isinstance(self.cursor, str)
            or not 1 <= len(self.cursor) <= 256
            or any(ord(c) < 32 or ord(c) == 127 for c in self.cursor)
            or type(self.complete) is not bool
        ):
            raise CompatibilityError("INVALID_BACKFILL_PROGRESS")


@dataclass(frozen=True)
class SchemaState:
    schema_version: int
    minimum_reader: int
    software_sha256: str
    plan_id: str | None
    target_version: int | None
    target_software_sha256: str | None
    phase: str
    read_path: str
    progress_cursor: str | None
    backfill_complete: bool
    backup_checkpoint_sha256: str | None
    revision: int


def _state(connection: Connection[Any], *, lock: bool = False) -> SchemaState:
    row = connection.execute(
        "SELECT schema_version,minimum_reader,software_sha256,plan_id,target_version,"
        "target_software_sha256,phase,read_path,progress_cursor,backfill_complete,"
        "backup_checkpoint_sha256,revision FROM opentour_platform.schema_state "
        "WHERE singleton" + (" FOR UPDATE" if lock else "")
    ).fetchone()
    if row is None:
        raise CompatibilityError("SCHEMA_NOT_INITIALIZED")
    return SchemaState(*row)


@contextmanager
def _transaction(connection: Connection[Any], *, shared: bool = False) -> Iterator[None]:
    if not connection.autocommit or connection.info.transaction_status != TransactionStatus.IDLE:
        raise CompatibilityError("DEDICATED_IDLE_AUTOCOMMIT_CONNECTION_REQUIRED")
    with connection.transaction():
        function = "pg_try_advisory_xact_lock_shared" if shared else "pg_try_advisory_xact_lock"
        row = connection.execute(f"SELECT {function}(%s)", (LOCK_ID,)).fetchone()
        if not row or row[0] is not True:
            raise CompatibilityError("MIGRATION_OR_READER_ACTIVE")
        yield


def initialize(connection: Connection[Any], version: int, software_sha256: str) -> None:
    """Explicit privileged bootstrap of this module's metadata only; no domain DDL."""
    _version(version)
    _digest(software_sha256)
    with _transaction(connection):
        connection.execute(SCHEMA)
        connection.execute(
            "INSERT INTO opentour_platform.schema_state "
            "(singleton,schema_version,minimum_reader,software_sha256,phase,read_path) "
            "VALUES(true,%s,%s,%s,'stable','legacy') ON CONFLICT(singleton) DO NOTHING",
            (version, version, software_sha256),
        )
        state = _state(connection)
        if (
            state.schema_version != version
            or state.software_sha256 != software_sha256
            or state.phase != "stable"
        ):
            raise CompatibilityError("INITIALIZATION_CONFLICT")


def readiness(connection: Connection[Any], reader_version: int) -> SchemaState:
    """A readiness check is an observation; use reader_transaction for actual queries."""
    _version(reader_version)
    with _transaction(connection, shared=True):
        state = _state(connection)
        if not state.minimum_reader <= reader_version <= state.schema_version:
            raise CompatibilityError("UNSUPPORTED_SCHEMA_VERSION")
        return state


@contextmanager
def reader_transaction(connection: Connection[Any], reader_version: int) -> Iterator[SchemaState]:
    _version(reader_version)
    with _transaction(connection, shared=True):
        state = _state(connection)
        if not state.minimum_reader <= reader_version <= state.schema_version:
            raise CompatibilityError("UNSUPPORTED_SCHEMA_VERSION")
        yield state


def _matches(state: SchemaState, plan: MigrationPlan) -> None:
    if (
        state.plan_id != plan.plan_id
        or state.target_version != plan.to_version
        or state.target_software_sha256 != plan.software_sha256
    ):
        raise CompatibilityError("MIGRATION_PLAN_CONFLICT")
    if state.revision == 9_223_372_036_854_775_807:
        raise CompatibilityError("REVISION_EXHAUSTED")


def expand(
    connection: Connection[Any],
    plan: MigrationPlan,
    approved_ddl: Callable[[Connection[Any]], None],
) -> SchemaState:
    plan.validate()
    with _transaction(connection):
        state = _state(connection, lock=True)
        if state.plan_id == plan.plan_id:
            _matches(state, plan)
            return state  # Identical retry never executes DDL twice.
        if state.phase not in {"stable", "contracted"} or state.schema_version != plan.from_version:
            raise CompatibilityError("MIGRATION_PLAN_CONFLICT")
        if state.revision == 9_223_372_036_854_775_807:
            raise CompatibilityError("REVISION_EXHAUSTED")
        approved_ddl(connection)
        connection.execute(
            "UPDATE opentour_platform.schema_state SET schema_version=%s,plan_id=%s,"
            "target_version=%s,target_software_sha256=%s,phase='expanded',read_path='legacy',"
            "progress_cursor=NULL,backfill_complete=false,backup_checkpoint_sha256=NULL,"
            "revision=revision+1 WHERE singleton",
            (plan.to_version, plan.plan_id, plan.to_version, plan.software_sha256),
        )
        return _state(connection)


def backfill(
    connection: Connection[Any],
    plan: MigrationPlan,
    approved_batch: Callable[[Connection[Any], str | None], Progress],
) -> SchemaState:
    plan.validate()
    attempted_revision: int | None = None
    try:
        with _transaction(connection):
            state = _state(connection, lock=True)
            _matches(state, plan)
            if state.backfill_complete:
                return state
            if state.phase not in {"expanded", "backfilling", "backfill_failed"}:
                raise CompatibilityError("INVALID_MIGRATION_PHASE")
            attempted_revision = state.revision
            progress = approved_batch(connection, state.progress_cursor)
            progress.validate()
            connection.execute(
                "UPDATE opentour_platform.schema_state SET phase='backfilling',"
                "progress_cursor=%s,backfill_complete=%s,revision=revision+1 WHERE singleton",
                (progress.cursor, progress.complete),
            )
            return _state(connection)
    except CompatibilityError:
        raise
    except Exception:
        # The failed batch and its effects rolled back. Preserve prior committed cursor.
        with _transaction(connection):
            state = _state(connection, lock=True)
            _matches(state, plan)
            if state.revision == attempted_revision and state.phase in {
                "expanded",
                "backfilling",
                "backfill_failed",
            }:
                connection.execute(
                    "UPDATE opentour_platform.schema_state SET phase='backfill_failed',"
                    "read_path='legacy',revision=revision+1 WHERE singleton"
                )
        raise CompatibilityError("BACKFILL_FAILED") from None


def verify(
    connection: Connection[Any],
    plan: MigrationPlan,
    approved_verify: Callable[[Connection[Any]], bool],
) -> SchemaState:
    plan.validate()
    with _transaction(connection):
        state = _state(connection, lock=True)
        _matches(state, plan)
        if state.phase == "verified":
            return state
        if state.phase != "backfilling" or not state.backfill_complete:
            raise CompatibilityError("BACKFILL_NOT_COMPLETE")
        if approved_verify(connection) is not True:
            raise CompatibilityError("BACKFILL_NOT_VERIFIED")
        connection.execute(
            "UPDATE opentour_platform.schema_state SET phase='verified',read_path='dual',"
            "revision=revision+1 WHERE singleton"
        )
        return _state(connection)


def switch(connection: Connection[Any], plan: MigrationPlan) -> SchemaState:
    plan.validate()
    with _transaction(connection):
        state = _state(connection, lock=True)
        _matches(state, plan)
        if state.phase == "switched":
            return state
        if state.phase != "verified":
            raise CompatibilityError("BACKFILL_NOT_VERIFIED")
        connection.execute(
            "UPDATE opentour_platform.schema_state SET phase='switched',read_path='new',"
            "software_sha256=target_software_sha256,revision=revision+1 WHERE singleton"
        )
        return _state(connection)


def contract(
    connection: Connection[Any],
    plan: MigrationPlan,
    backup_checkpoint_sha256: str,
    verify_checkpoint: Callable[[str], bool],
    approved_ddl: Callable[[Connection[Any]], None],
) -> SchemaState:
    """The recovery owner verifies the checkpoint; a caller's digest alone is insufficient."""
    plan.validate()
    _digest(backup_checkpoint_sha256)
    with _transaction(connection):
        state = _state(connection, lock=True)
        _matches(state, plan)
        if state.phase == "contracted":
            if state.backup_checkpoint_sha256 != backup_checkpoint_sha256:
                raise CompatibilityError("CHECKPOINT_CONFLICT")
            return state
        if state.phase != "switched" or not state.backfill_complete:
            raise CompatibilityError("CONTRACT_PREREQUISITE_MISSING")
        if verify_checkpoint(backup_checkpoint_sha256) is not True:
            raise CompatibilityError("BACKUP_CHECKPOINT_UNVERIFIED")
        # Exclusive transaction lock excludes every participating old-reader transaction.
        approved_ddl(connection)
        connection.execute(
            "UPDATE opentour_platform.schema_state SET phase='contracted',minimum_reader=%s,"
            "backup_checkpoint_sha256=%s,revision=revision+1 WHERE singleton",
            (plan.to_version, backup_checkpoint_sha256),
        )
        return _state(connection)
