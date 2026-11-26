"""Versioned benchmark drafts anchored to an already-authorized durable intake.

Admission creates identity, not a score. Live evidence does not materialize or
supersede benchmark records. Frozen/scored results retain their producer-owned
authority and are adopted only through a separately configured worker port.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from opentour_cell.pilot.intake_admission import DurableIntake, IntakeWork
from opentour_cell.pilot.intake_mcp import (
    AdmissionHold,
    AdmissionReceipt,
    BenchmarkView,
    CatalogPage,
    IntakeError,
    IntakePrincipal,
    IntakeRequest,
    RunStatus,
    require_ref,
)
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.training import _check_file

Json = dict[str, Any]


def _require(condition: object, reason: str = "benchmark_unavailable") -> None:
    if not condition:
        raise IntakeError(reason)


@dataclass(frozen=True)
class BenchmarkScope:
    tenant: str
    engagement_epoch: str
    workflow_ref: str


class BenchmarkInspection(Protocol):
    def guard(
        self, actor: IntakePrincipal, workflow_ref: str
    ) -> AbstractContextManager[BenchmarkScope]:
        """Pin explicit current benchmark inspection, separately from live-feed rights."""
        ...


class LiveBenchmarkReadback(Protocol):
    def read(self, actor: IntakePrincipal, work: IntakeWork) -> BenchmarkView | None:
        """Resolve an explicitly selected benchmark under its own current intake authority."""
        ...


@dataclass(frozen=True)
class MaterializedBenchmark:
    """Verified worker producer output; unavailable producers never mint this record."""

    state: str
    definition_ref: str
    definition_digest: str
    source_revision: str
    result_ref: str | None = None
    result_digest: str | None = None


class BenchmarkMaterializer(Protocol):
    def materialize(
        self, actor: IntakePrincipal, work: IntakeWork, *, operation_ref: str
    ) -> MaterializedBenchmark | None:
        """Run the actual authorized owner/adjudication or historical freeze producer.

        The implementation owns source reads, grouped split, independent truth,
        standing evaluation permission and any scoring; it may return a hold.
        This port receives no protected answers from the draft ledger.
        """
        ...

    def readback(
        self, actor: IntakePrincipal, work: IntakeWork, *, operation_ref: str
    ) -> MaterializedBenchmark | None:
        """Read an actual durable producer receipt, with zero new work.

        None is an unresolved operation, not permission to run it again.
        """
        ...


class WorkflowBenchmarks:
    def __init__(self, directory: Path):
        self.path = directory.absolute() / "workflow-benchmarks.sqlite3"

    @contextmanager
    def _db(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if write:
            parent = self.path.parent
            _require(parent.resolve(strict=True) == parent and parent.is_dir())
            info = parent.stat()
            _require(info.st_uid == os.getuid() and not info.st_mode & 0o077)
            try:
                os.close(
                    os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                )
            except FileExistsError:
                pass
        _check_file(self.path)
        db = sqlite3.connect(
            self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
            uri=True,
            timeout=10,
            isolation_level=None,
        )
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if write:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS benchmarks (tenant TEXT, epoch TEXT, "
                    "workflow TEXT, run TEXT, reference TEXT UNIQUE, version INTEGER, "
                    "parent TEXT, definition TEXT, state TEXT, materialized TEXT, "
                    "superseded INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(tenant,run), "
                    "UNIQUE(tenant,epoch,workflow,version))"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS benchmark_operations (tenant TEXT, run TEXT, "
                    "target TEXT, operation_ref TEXT UNIQUE, completed INTEGER NOT NULL DEFAULT 0, "
                    "PRIMARY KEY(tenant,run,target))"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS benchmark_receipts (tenant TEXT, run TEXT, "
                    "revision TEXT NOT NULL, PRIMARY KEY(tenant,run))"
                )
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _definition(work: IntakeWork) -> str:
        return canonical(
            {
                "workflow_ref": work.workflow_ref,
                "mode": work.mode,
                "resource": asdict(work.resource),
                "revisions": work.revisions,
            }
        )

    @staticmethod
    def _view(row: sqlite3.Row) -> BenchmarkView:
        return BenchmarkView(
            row["reference"], row["version"], "superseded" if row["superseded"] else row["state"]
        )

    def admit(self, tenant: str, work: IntakeWork) -> BenchmarkView:
        """Called only inside DurableIntake.admitted_work, before any worker dispatch."""
        _require(work.mode in {"starter", "historical"}, "live_cannot_materialize_benchmark")
        _require(work.benchmark_version is not None, "benchmark_admission_order_unavailable")
        assert work.benchmark_version is not None
        scope = tenant, work.resource.engagement_epoch, work.workflow_ref
        version = work.benchmark_version
        parent = (
            None
            if work.benchmark_parent_run is None
            else "benchmark-" + digest([*scope, work.benchmark_parent_run])[:40]
        )
        definition = self._definition(work)
        with self._db(write=True) as db:
            existing = db.execute(
                "SELECT * FROM benchmarks WHERE tenant=? AND run=?", (tenant, work.run_ref)
            ).fetchone()
            if existing is not None:
                _require(
                    existing["definition"] == definition
                    and existing["version"] == version
                    and existing["parent"] == parent,
                    "benchmark_intent_conflict",
                )
                self._validated_materialized(db, existing, work)
                if not work.benchmark_current:
                    db.execute(
                        "UPDATE benchmarks SET superseded=1 WHERE reference=?",
                        (existing["reference"],),
                    )
                    return replace(self._view(existing), benchmark_state="superseded")
                return self._view(existing)
            prior = db.execute(
                "SELECT * FROM benchmarks WHERE tenant=? AND epoch=? AND workflow=? "
                "ORDER BY version DESC LIMIT 1",
                scope,
            ).fetchone()
            superseded = not work.benchmark_current
            reference = "benchmark-" + digest([*scope, work.run_ref])[:40]
            db.execute(
                "INSERT INTO benchmarks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    *scope,
                    work.run_ref,
                    reference,
                    version,
                    parent,
                    definition,
                    "draft",
                    None,
                    int(superseded),
                ),
            )
            if prior is not None:
                db.execute(
                    "UPDATE benchmarks SET superseded=1 WHERE tenant=? AND epoch=? "
                    "AND workflow=? AND version < ?",
                    (*scope, version),
                )
            return BenchmarkView(reference, version, "superseded" if superseded else "draft")

    @contextmanager
    def worker_guard(self) -> Iterator[None]:
        """Serialize the producer operation across threads and worker processes."""
        path = self.path.with_name("benchmark-worker.lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            _require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and info.st_nlink == 1
                and not info.st_mode & 0o077
                and os.path.samestat(info, path.lstat())
            )
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _transitions(state: str) -> set[str]:
        return {
            "draft": {"needs_owner_review", "frozen_unscored", "scored"},
            "needs_owner_review": {"frozen_unscored", "scored"},
            "frozen_unscored": {"scored"},
            "scored": set(),
        }[state]

    @staticmethod
    def _validate_receipt(receipt: MaterializedBenchmark, revisions: tuple[str, ...]) -> None:
        _require(receipt.state in {"needs_owner_review", "frozen_unscored", "scored"})
        require_ref(receipt.definition_ref)
        _require(
            isinstance(receipt.definition_digest, str)
            and re.fullmatch("[a-f0-9]{64}", receipt.definition_digest)
            and receipt.source_revision in revisions
        )
        if receipt.state == "scored":
            require_ref(receipt.result_ref)
            _require(
                isinstance(receipt.result_digest, str)
                and re.fullmatch("[a-f0-9]{64}", receipt.result_digest)
            )
        else:
            _require(receipt.result_ref is None and receipt.result_digest is None)

    @staticmethod
    def _receipt_revision(row: sqlite3.Row, encoded: str) -> str:
        return digest(
            [
                row["tenant"],
                row["epoch"],
                row["workflow"],
                row["run"],
                row["reference"],
                row["version"],
                row["parent"],
                row["definition"],
                encoded,
            ]
        )

    def _validated_materialized(
        self, db: sqlite3.Connection, row: sqlite3.Row, work: IntakeWork | None = None
    ) -> MaterializedBenchmark | None:
        """Replay and every disclosure verify the same current receipt binding."""
        try:
            definition = json.loads(row["definition"])
            _require(
                definition["workflow_ref"] == row["workflow"]
                and definition["resource"]["engagement_epoch"] == row["epoch"]
                and row["reference"]
                == "benchmark-"
                + digest([row["tenant"], row["epoch"], row["workflow"], row["run"]])[:40]
                and type(row["version"]) is int
                and row["version"] > 0
            )
            if work is not None:
                scope = row["tenant"], work.resource.engagement_epoch, work.workflow_ref
                parent = (
                    None
                    if work.benchmark_parent_run is None
                    else "benchmark-" + digest([*scope, work.benchmark_parent_run])[:40]
                )
                _require(
                    row["definition"] == self._definition(work)
                    and row["version"] == work.benchmark_version
                    and row["parent"] == parent
                )
            _require(row["state"] in {"draft", "needs_owner_review", "frozen_unscored", "scored"})
            tables = {
                entry[0]
                for entry in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            sealed = (
                db.execute(
                    "SELECT revision FROM benchmark_receipts WHERE tenant=? AND run=?",
                    (row["tenant"], row["run"]),
                ).fetchone()
                if "benchmark_receipts" in tables
                else None
            )
            completed = (
                [
                    entry[0]
                    for entry in db.execute(
                        "SELECT target FROM benchmark_operations "
                        "WHERE tenant=? AND run=? AND completed=1",
                        (row["tenant"], row["run"]),
                    )
                ]
                if "benchmark_operations" in tables
                else []
            )
            progression = {"draft": 0, "needs_owner_review": 1, "frozen_unscored": 2, "scored": 3}
            _require(
                all(
                    target in progression and progression[target] <= progression[row["state"]]
                    for target in completed
                )
            )
            if row["materialized"] is None:
                _require(row["state"] == "draft" and sealed is None and not completed)
                return None
            data = json.loads(row["materialized"])
            _require(
                isinstance(data, dict)
                and set(data) == set(MaterializedBenchmark.__dataclass_fields__)
            )
            receipt = MaterializedBenchmark(**data)
            self._validate_receipt(receipt, tuple(definition["revisions"]))
            _require(receipt.state == row["state"])
            # An unsealed legacy record is not silently upgraded into evidence.
            _require(
                sealed is not None
                and sealed["revision"] == self._receipt_revision(row, canonical(data)),
                "benchmark_receipt_binding_unavailable",
            )
            return receipt
        except (TypeError, ValueError, KeyError) as error:
            if isinstance(error, IntakeError):
                raise
            raise IntakeError("benchmark_unavailable") from error

    def admit_materialization(
        self, tenant: str, work: IntakeWork, target: str
    ) -> tuple[str | None, bool, MaterializedBenchmark | None]:
        """Admit one explicit transition before any producer invocation."""
        _require(target in {"needs_owner_review", "frozen_unscored", "scored"})
        with self._db(write=True) as db:
            row = db.execute(
                "SELECT * FROM benchmarks WHERE tenant=? AND run=?", (tenant, work.run_ref)
            ).fetchone()
            _require(row is not None and row["definition"] == self._definition(work))
            _require(not row["superseded"], "immutable_benchmark_version")
            receipt = self._validated_materialized(db, row, work)
            if row["state"] == target:
                _require(receipt is not None)
                return None, False, receipt
            _require(target in self._transitions(row["state"]), "immutable_benchmark_version")
            operation = (
                "benchmark-operation-"
                + digest([tenant, work.run_ref, target, row["definition"]])[:40]
            )
            previous = db.execute(
                "SELECT * FROM benchmark_operations WHERE tenant=? AND run=? AND target=?",
                (tenant, work.run_ref, target),
            ).fetchone()
            if previous is not None:
                _require(previous["operation_ref"] == operation and not previous["completed"])
                return operation, False, None
            # An uncertain earlier transition must resolve before another one runs.
            pending = db.execute(
                "SELECT 1 FROM benchmark_operations WHERE tenant=? AND run=? AND completed=0",
                (tenant, work.run_ref),
            ).fetchone()
            _require(pending is None, "benchmark_materialization_unresolved")
            db.execute(
                "INSERT INTO benchmark_operations VALUES(?,?,?,?,0)",
                (tenant, work.run_ref, target, operation),
            )
            return operation, True, None

    def for_run(self, tenant: str, work: IntakeWork) -> BenchmarkView | None:
        if not self.path.exists():
            return None
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM benchmarks WHERE tenant=? AND run=?", (tenant, work.run_ref)
            ).fetchone()
            if row is None:
                return None
            _require(row["definition"] == self._definition(work))
            self._validated_materialized(db, row, work)
            return (
                self._view(row)
                if work.benchmark_current
                else replace(self._view(row), benchmark_state="superseded")
            )

    def current(
        self, scope: BenchmarkScope, *, expected_run: str | None = None
    ) -> BenchmarkView | None:
        if not self.path.exists():
            return None
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM benchmarks WHERE tenant=? AND epoch=? AND workflow=? "
                "ORDER BY version DESC LIMIT 1",
                (scope.tenant, scope.engagement_epoch, scope.workflow_ref),
            ).fetchone()
            if row is None or (expected_run is not None and row["run"] != expected_run):
                return None
            self._validated_materialized(db, row)
            return self._view(row)

    def observed_workflow(self, scope: BenchmarkScope) -> bool:
        """Prove an admitted identity under a separately held current authority guard.

        The caller supplies the current engagement epoch from its trusted store,
        never from the wire. This is only an identity check: it grants neither
        feed access nor benchmark inspection and never creates a workflow.
        """
        if not self.path.exists():
            return False
        with self._db() as db:
            return (
                db.execute(
                    "SELECT 1 FROM benchmarks WHERE tenant=? AND epoch=? AND workflow=? LIMIT 1",
                    (scope.tenant, scope.engagement_epoch, scope.workflow_ref),
                ).fetchone()
                is not None
            )

    def inspect(
        self, scope: BenchmarkScope, reference: str, *, current_run: str | None = None
    ) -> Json | None:
        """Only under the caller's explicit benchmark inspection guard, including old versions."""
        if not self.path.exists():
            return None
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM benchmarks WHERE tenant=? AND epoch=? AND workflow=? "
                "AND reference=?",
                (scope.tenant, scope.engagement_epoch, scope.workflow_ref, reference),
            ).fetchone()
            if row is None:
                return None
            receipt = self._validated_materialized(db, row)
            return {
                "benchmark_ref": row["reference"],
                "version": row["version"],
                "parent_ref": row["parent"],
                "benchmark_state": (
                    "superseded"
                    if current_run is not None and row["run"] != current_run
                    else self._view(row).benchmark_state
                ),
                "as_of_state": row["state"],
                "materialized": None if receipt is None else asdict(receipt),
            }

    def publish(
        self,
        tenant: str,
        work: IntakeWork,
        receipt: MaterializedBenchmark,
        *,
        operation_ref: str | None = None,
    ) -> None:
        _require(work.mode != "live", "live_cannot_materialize_benchmark")
        self._validate_receipt(receipt, work.revisions)
        encoded = canonical(asdict(receipt))
        with self._db(write=True) as db:
            row = db.execute(
                "SELECT * FROM benchmarks WHERE tenant=? AND run=?", (tenant, work.run_ref)
            ).fetchone()
            _require(
                row is not None
                and row["definition"] == self._definition(work)
                and not row["superseded"]
            )
            self._validated_materialized(db, row, work)
            if row["materialized"] == encoded:
                return
            _require(
                receipt.state in self._transitions(row["state"]), "immutable_benchmark_version"
            )
            if row["state"] == "frozen_unscored":
                previous = json.loads(row["materialized"])
                _require(
                    all(
                        previous[k] == getattr(receipt, k)
                        for k in ("definition_ref", "definition_digest", "source_revision")
                    ),
                    "frozen_benchmark_changed",
                )
            db.execute(
                "UPDATE benchmarks SET state=?,materialized=? WHERE tenant=? AND run=?",
                (receipt.state, encoded, tenant, work.run_ref),
            )
            db.execute(
                "INSERT INTO benchmark_receipts VALUES(?,?,?) "
                "ON CONFLICT(tenant,run) DO UPDATE SET revision=excluded.revision",
                (tenant, work.run_ref, self._receipt_revision(row, encoded)),
            )
            if operation_ref is not None:
                changed = db.execute(
                    "UPDATE benchmark_operations SET completed=1 "
                    "WHERE tenant=? AND run=? AND target=? AND operation_ref=? AND completed=0",
                    (tenant, work.run_ref, receipt.state, operation_ref),
                ).rowcount
                _require(changed == 1)


class BenchmarkIntake:
    """Composition for stdio/HTTPS/setup; keeps the deployed intake wire schemas."""

    def __init__(
        self,
        intake: DurableIntake,
        *,
        inspection: BenchmarkInspection | None = None,
        live_readback: LiveBenchmarkReadback | None = None,
    ):
        self.intake = intake
        self.benchmarks = WorkflowBenchmarks(intake.path.parent)
        self.inspection = inspection
        self.live_readback = live_readback

    def discover_sources(
        self, actor: IntakePrincipal, *, limit: int, cursor: str | None
    ) -> CatalogPage:
        return self.intake.discover_sources(actor, limit=limit, cursor=cursor)

    def submit_intake(
        self, actor: IntakePrincipal, request: IntakeRequest
    ) -> AdmissionReceipt | AdmissionHold:
        receipt = self.intake.submit_intake(actor, request)
        if isinstance(receipt, AdmissionReceipt):
            with self.intake.admitted_work(actor, receipt.run_ref) as work:
                if work.mode != "live":
                    self.benchmarks.admit(actor.tenant, work)
        return receipt

    def get_run(self, actor: IntakePrincipal, run_ref: str) -> RunStatus:
        status = self.intake.get_run(actor, run_ref)
        with self.intake.admitted_work(actor, run_ref) as work:
            if work.mode != "live":
                view = self.benchmarks.for_run(actor.tenant, work)
                return replace(status, benchmark=view or status.benchmark)
            if self.live_readback is not None:
                # The requesting live grant is pinned throughout the independent readback.
                return replace(status, benchmark=self.live_readback.read(actor, work))
        # Live feed authorization alone confers no benchmark inspection rights.
        if self.inspection is not None:
            with self.inspection.guard(actor, status.workflow_ref) as scope:
                _require(
                    scope.tenant == actor.tenant
                    and scope.workflow_ref == status.workflow_ref
                    and scope.engagement_epoch == work.resource.engagement_epoch
                )
                current_run = self.intake.current_benchmark_run(
                    scope.tenant, scope.workflow_ref, scope.engagement_epoch
                )
                return replace(
                    status,
                    benchmark=self.benchmarks.current(scope, expected_run=current_run)
                    or status.benchmark,
                )
        return status

    def inspect_benchmark(
        self, actor: IntakePrincipal, workflow: str, reference: str
    ) -> Json | None:
        if self.inspection is None:
            return None
        with self.inspection.guard(actor, workflow) as scope:
            _require(scope.tenant == actor.tenant and scope.workflow_ref == workflow)
            current_run = self.intake.current_benchmark_run(
                scope.tenant, workflow, scope.engagement_epoch
            )
            return self.benchmarks.inspect(scope, require_ref(reference), current_run=current_run)

    def materialize(
        self,
        actor: IntakePrincipal,
        run_ref: str,
        producer: BenchmarkMaterializer,
        *,
        target_state: str = "scored",
    ) -> bool:
        # Denied actors/runs do not even create the worker lock file.
        with self.intake.admitted_work(actor, run_ref) as admitted:
            _require(admitted.mode != "live", "live_cannot_materialize_benchmark")
            _require(admitted.benchmark_current, "immutable_benchmark_version")
        with self.benchmarks.worker_guard():
            with self.intake.admitted_work(actor, run_ref) as work:
                _require(work.mode != "live", "live_cannot_materialize_benchmark")
                _require(work.benchmark_current, "immutable_benchmark_version")
                self.benchmarks.admit(actor.tenant, work)
                operation, dispatch, receipt = self.benchmarks.admit_materialization(
                    actor.tenant, work, target_state
                )
            # Producer locks are independent of the intake authority writer.
            # A crash leaves one pending operation; retries only read its receipt.
            if operation is not None:
                method = producer.materialize if dispatch else producer.readback
                receipt = method(actor, work, operation_ref=operation)
                if receipt is None:
                    return False
                _require(receipt.state == target_state, "benchmark_transition_mismatch")
                with self.intake.admitted_work(actor, run_ref) as current:
                    _require(current == work)
                    self.benchmarks.publish(actor.tenant, current, receipt, operation_ref=operation)
            assert receipt is not None
            self.intake.materialize(actor, run_ref, lambda _: receipt.definition_ref)
            return True
