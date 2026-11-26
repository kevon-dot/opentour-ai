"""Versioned benchmark drafts anchored to an already-authorized durable intake.

Admission creates identity, not a score. Live evidence does not materialize or
supersede benchmark records. Frozen/scored results retain their producer-owned
authority and are adopted only through a separately configured worker port.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
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
    def materialize(self, actor: IntakePrincipal, work: IntakeWork) -> MaterializedBenchmark | None:
        """Run the actual authorized owner/adjudication or historical freeze producer.

        The implementation owns source reads, grouped split, independent truth,
        standing evaluation permission and any scoring; it may return a hold.
        This port receives no protected answers from the draft ledger.
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
        definition = self._definition(work)
        with self._db(write=True) as db:
            existing = db.execute(
                "SELECT * FROM benchmarks WHERE tenant=? AND run=?", (tenant, work.run_ref)
            ).fetchone()
            if existing is not None:
                _require(existing["definition"] == definition, "benchmark_intent_conflict")
                return self._view(existing)
            scope = tenant, work.resource.engagement_epoch, work.workflow_ref
            prior = db.execute(
                "SELECT * FROM benchmarks WHERE tenant=? AND epoch=? AND workflow=? "
                "ORDER BY version DESC LIMIT 1",
                scope,
            ).fetchone()
            version = 1 if prior is None else prior["version"] + 1
            reference = "benchmark-" + digest([*scope, work.run_ref])[:40]
            db.execute(
                "INSERT INTO benchmarks VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                (
                    *scope,
                    work.run_ref,
                    reference,
                    version,
                    None if prior is None else prior["reference"],
                    definition,
                    "draft",
                    None,
                ),
            )
            if prior is not None:
                db.execute(
                    "UPDATE benchmarks SET superseded=1 WHERE reference=?", (prior["reference"],)
                )
            return BenchmarkView(reference, version, "draft")

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
            return self._view(row)

    def current(self, scope: BenchmarkScope) -> BenchmarkView | None:
        if not self.path.exists():
            return None
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM benchmarks WHERE tenant=? AND epoch=? AND workflow=? "
                "ORDER BY version DESC LIMIT 1",
                (scope.tenant, scope.engagement_epoch, scope.workflow_ref),
            ).fetchone()
            return None if row is None else self._view(row)

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

    def inspect(self, scope: BenchmarkScope, reference: str) -> Json | None:
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
            return {
                "benchmark_ref": row["reference"],
                "version": row["version"],
                "parent_ref": row["parent"],
                "benchmark_state": self._view(row).benchmark_state,
                "as_of_state": row["state"],
                "materialized": json.loads(row["materialized"]) if row["materialized"] else None,
            }

    def publish(self, tenant: str, work: IntakeWork, receipt: MaterializedBenchmark) -> None:
        _require(work.mode != "live", "live_cannot_materialize_benchmark")
        _require(receipt.state in {"needs_owner_review", "frozen_unscored", "scored"})
        require_ref(receipt.definition_ref)
        _require(
            re.fullmatch("[a-f0-9]{64}", receipt.definition_digest)
            and receipt.source_revision in work.revisions
        )
        _require(
            (receipt.state == "scored")
            == (receipt.result_ref is not None and receipt.result_digest is not None)
        )
        if receipt.state != "scored":
            _require(receipt.result_ref is None and receipt.result_digest is None)
        if receipt.state == "scored":
            require_ref(receipt.result_ref)
            _require(
                isinstance(receipt.result_digest, str)
                and re.fullmatch("[a-f0-9]{64}", receipt.result_digest)
            )
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
            if row["materialized"] == encoded:
                return
            allowed = {
                "draft": {"needs_owner_review", "frozen_unscored", "scored"},
                "needs_owner_review": {"frozen_unscored", "scored"},
                "frozen_unscored": {"scored"},
                "scored": set(),
            }
            _require(receipt.state in allowed[row["state"]], "immutable_benchmark_version")
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


class BenchmarkIntake:
    """Composition for stdio/HTTPS/setup; keeps the deployed intake wire schemas."""

    def __init__(self, intake: DurableIntake, *, inspection: BenchmarkInspection | None = None):
        self.intake = intake
        self.benchmarks = WorkflowBenchmarks(intake.path.parent)
        self.inspection = inspection

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
        # Live feed authorization alone confers no benchmark inspection rights.
        if self.inspection is not None:
            with self.inspection.guard(actor, status.workflow_ref) as scope:
                _require(
                    scope.tenant == actor.tenant
                    and scope.workflow_ref == status.workflow_ref
                    and scope.engagement_epoch == work.resource.engagement_epoch
                )
                return replace(status, benchmark=self.benchmarks.current(scope) or status.benchmark)
        return status

    def inspect_benchmark(
        self, actor: IntakePrincipal, workflow: str, reference: str
    ) -> Json | None:
        if self.inspection is None:
            return None
        with self.inspection.guard(actor, workflow) as scope:
            _require(scope.tenant == actor.tenant and scope.workflow_ref == workflow)
            return self.benchmarks.inspect(scope, require_ref(reference))

    def materialize(
        self, actor: IntakePrincipal, run_ref: str, producer: BenchmarkMaterializer
    ) -> bool:
        with self.intake.admitted_work(actor, run_ref) as work:
            _require(work.mode != "live", "live_cannot_materialize_benchmark")
            self.benchmarks.admit(actor.tenant, work)
        # The actual producer acquires its own source/evaluation locks. It must
        # not run inside a nested intake authority writer transaction.
        receipt = producer.materialize(actor, work)
        if receipt is None:
            return False
        with self.intake.admitted_work(actor, run_ref) as current:
            _require(current == work)
            self.benchmarks.publish(actor.tenant, current, receipt)
        self.intake.materialize(actor, run_ref, lambda _: receipt.definition_ref)
        return True
