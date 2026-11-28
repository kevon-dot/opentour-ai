"""Test-only doubles for ``fit_dispatch``. Nothing in the source tree imports this module.

* ``SqliteFitLedger`` is a durable double of the narrow OPE-442 fit-ledger port. It is NOT the
  OPE-442 ledger: it exists so the dispatcher's restart, retry and concurrency behaviour can be
  exercised against real SQLite writer transactions. OPE-442 owns the production ledger.
* ``SimulatedBackend`` is a generated in-memory training backend behind the OPE-506 connector
  protocol, with optional progress, early-stop, usage and absence capabilities and fault
  injection (lost acknowledgement, worker crash after acceptance, hidden lookups, over-cap
  usage). Every receipt, job, checkpoint and artifact reference is a ``simulated:*`` sentinel.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from opentour_cell.pilot import fit_dispatch as fd
from opentour_cell.pilot import model_connector as mc
from opentour_cell.pilot.connector_ext import Usage, UsageSource
from opentour_cell.pilot.operating_point import canonical


class WorkerCrash(BaseException):
    """The worker process dies here. Not an ``Exception``: nothing in the dispatcher catches it."""


class FakeClock:
    def __init__(self, start: float | None = None) -> None:
        self.now = time.time() if start is None else start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


# --- Durable ledger double --------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries(
  execution_id TEXT PRIMARY KEY, intent_key TEXT NOT NULL UNIQUE, intent_digest TEXT NOT NULL,
  scope TEXT NOT NULL, tenant TEXT NOT NULL, actor TEXT NOT NULL, record TEXT NOT NULL,
  execution TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS aliases(
  tenant TEXT NOT NULL, actor TEXT NOT NULL, client TEXT NOT NULL, operation_key TEXT NOT NULL,
  execution_id TEXT NOT NULL, intent_digest TEXT NOT NULL,
  PRIMARY KEY(tenant, actor, client, operation_key));
CREATE TABLE IF NOT EXISTS events(
  execution_id TEXT NOT NULL, version INTEGER NOT NULL, event TEXT NOT NULL, stage TEXT NOT NULL,
  at REAL NOT NULL, PRIMARY KEY(execution_id, version));
"""


def _dump(execution: fd.Execution) -> str:
    value = asdict(execution)
    value["stage"] = str(execution.stage)
    return canonical(value)


def _load(text: str) -> fd.Execution:
    value = json.loads(text)
    value["stage"] = fd.Stage(value["stage"])
    return fd.Execution(**value)


class SqliteFitLedger:
    """Durable double of ``fit_dispatch.FitLedger``; see the port's contract for each method."""

    def __init__(self, path: Path, clock: Callable[[], float]) -> None:
        self.path = path
        self.clock = clock
        self.in_reserve = threading.local()
        # Fault hooks: called with (method, execution) inside the transaction, before commit.
        self.before_commit: Callable[[str, fd.Execution], None] | None = None
        self.before_recheck: Callable[[], None] | None = None
        with self._db() as db:
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=30, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def _entry(self, db: sqlite3.Connection, execution_id: str, **extra: Any) -> fd.LedgerEntry:
        row = db.execute("SELECT * FROM entries WHERE execution_id=?", (execution_id,)).fetchone()
        if row is None:
            raise fd.DispatchError("execution_unavailable")
        aliases = db.execute(
            "SELECT count(*) FROM aliases WHERE execution_id=?", (execution_id,)
        ).fetchone()[0]
        return fd.LedgerEntry(
            execution_id=row["execution_id"],
            record=json.loads(row["record"]),
            execution=_load(row["execution"]),
            created_at=row["created"],
            updated_at=row["updated"],
            aliases=aliases,
            **extra,
        )

    def _usage(self, db: sqlite3.Connection, scope: str) -> fd.LedgerUsage:
        rows = db.execute("SELECT record, execution FROM entries WHERE scope=?", (scope,))
        open_fits = reserved = seconds = money = 0
        for row in rows:
            record, execution = json.loads(row["record"]), _load(row["execution"])
            reserved += 1
            open_fits += execution.stage in fd.OPEN_STAGES
            seconds += record["bound"]["maximum_seconds"]
            money += execution.charged_minor_units
        return fd.LedgerUsage(scope, open_fits, reserved, seconds, money)

    def _event(
        self, db: sqlite3.Connection, execution_id: str, ex: fd.Execution, event: str
    ) -> None:
        db.execute(
            "INSERT INTO events VALUES(?,?,?,?,?)",
            (execution_id, ex.version, event, str(ex.stage), self.clock()),
        )

    def reserve(
        self,
        record: fd.Json,
        *,
        client: str,
        operation_key: str,
        recheck: Callable[[fd.LedgerUsage], None],
    ) -> fd.LedgerEntry:
        alias_key = (record["tenant"], record["actor"], client, operation_key)
        with self._db() as db:
            alias = db.execute(
                "SELECT * FROM aliases WHERE (tenant,actor,client,operation_key)=(?,?,?,?)",
                alias_key,
            ).fetchone()
            if alias is not None:
                if alias["intent_digest"] != record["intent_digest"]:
                    raise fd.DispatchError("fit_operation_key_conflict")
                return self._entry(db, alias["execution_id"], replayed=True)
            row = db.execute(
                "SELECT execution_id, intent_digest FROM entries WHERE intent_key=?",
                (record["intent_key"],),
            ).fetchone()
            if row is not None:
                if row["intent_digest"] != record["intent_digest"]:
                    raise fd.DispatchError("fit_intent_conflict")
                db.execute(
                    "INSERT INTO aliases VALUES(?,?,?,?,?,?)",
                    (*alias_key, row["execution_id"], record["intent_digest"]),
                )
                return self._entry(db, row["execution_id"], replayed=True)
            if self.before_recheck is not None:
                self.before_recheck()
            self.in_reserve.active = True
            try:
                recheck(self._usage(db, record["budget_scope"]))
            finally:
                self.in_reserve.active = False
            execution = fd.Execution(
                stage=fd.Stage.RESERVED,
                version=1,
                charged_minor_units=record["bound"]["maximum_minor_units"],
            )
            now = self.clock()
            db.execute(
                "INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    record["execution_id"],
                    record["intent_key"],
                    record["intent_digest"],
                    record["budget_scope"],
                    record["tenant"],
                    record["actor"],
                    canonical(record),
                    _dump(execution),
                    now,
                    now,
                ),
            )
            db.execute(
                "INSERT INTO aliases VALUES(?,?,?,?,?,?)",
                (*alias_key, record["execution_id"], record["intent_digest"]),
            )
            self._event(db, record["execution_id"], execution, "reserved")
            return self._entry(db, record["execution_id"])

    def get(self, execution_id: str) -> fd.LedgerEntry | None:
        with self._db() as db:
            try:
                return self._entry(db, execution_id)
            except fd.DispatchError:
                return None

    def transition(
        self, execution_id: str, *, version: int, execution: fd.Execution, event: str
    ) -> fd.LedgerEntry:
        with self._db() as db:
            current = self._entry(db, execution_id).execution
            if current.version != version:
                raise fd.DispatchError("ledger_version_conflict")
            allowed = fd.TRANSITIONS[current.stage]
            if (
                execution.stage not in allowed
                or execution.charged_minor_units < current.charged_minor_units
                or execution.submissions < current.submissions
                or (current.job_ref and execution.job_ref != current.job_ref)
                or execution.settlement is not None
            ):
                raise fd.DispatchError("ledger_transition_refused")
            stored = replace(execution, version=version + 1)
            if self.before_commit is not None:
                self.before_commit("transition", stored)
            db.execute(
                "UPDATE entries SET execution=?, updated=? WHERE execution_id=?",
                (_dump(stored), self.clock(), execution_id),
            )
            self._event(db, execution_id, stored, event)
            return self._entry(db, execution_id)

    def settle(self, execution_id: str, *, version: int, settlement: fd.Json) -> fd.LedgerEntry:
        with self._db() as db:
            current = self._entry(db, execution_id).execution
            if current.stage is fd.Stage.SETTLED:
                if current.settlement == settlement:
                    return self._entry(db, execution_id)
                raise fd.DispatchError("settlement_conflict")
            if current.version != version:
                raise fd.DispatchError("ledger_version_conflict")
            if current.stage not in fd.OPEN_STAGES:
                raise fd.DispatchError("ledger_transition_refused")
            stored = replace(
                current,
                stage=fd.Stage.SETTLED,
                version=version + 1,
                settlement=json.loads(canonical(settlement)),
                lease_owner="",
                lease_until=0.0,
            )
            if self.before_commit is not None:
                self.before_commit("settle", stored)
            db.execute(
                "UPDATE entries SET execution=?, updated=? WHERE execution_id=?",
                (_dump(stored), self.clock(), execution_id),
            )
            self._event(db, execution_id, stored, "settled")
            return self._entry(db, execution_id)

    # Inspection helpers for assertions only.

    def count(self, table: str) -> int:
        assert table in ("entries", "aliases", "events")
        with self._db() as db:
            return int(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])

    def stages(self) -> list[str]:
        with self._db() as db:
            return [_load(r[0]).stage.value for r in db.execute("SELECT execution FROM entries")]

    def settlements(self) -> int:
        return self.stages().count("settled")

    def history(self, execution_id: str) -> list[str]:
        with self._db() as db:
            return [
                r[0]
                for r in db.execute(
                    "SELECT event FROM events WHERE execution_id=? ORDER BY version",
                    (execution_id,),
                )
            ]


# --- Simulated backend ------------------------------------------------------------------------


@dataclass
class SimJob:
    intent: mc.FitIntent
    job_ref: str
    evidence_ref: str
    state: mc.JobState = mc.JobState.QUEUED
    steps: int = 0
    evaluations: list[fd.Evaluation] = field(default_factory=list)
    selected: str = ""
    rows: int = 0


@dataclass
class BackendConfig:
    training_supported: bool = True
    hard_cost_cap: bool = True
    cancellation: bool = True
    progress: bool = True
    checkpoint_recovery: bool = True
    exact_revision: bool = True
    estimate_known: bool = True
    metrics: tuple[float, ...] = ()
    cadence: int = 10
    run_steps: int = 100
    seconds_per_step: int = 2
    minor_units_per_step: int = 0
    lose_ack_on_submit: int = 0
    crash_after_accept: int = 0
    fail_before_accept: int = 0
    hidden_lookups: int = 0
    final_usage: Callable[[SimJob], Usage | None] | None = None
    live_usage: bool = False
    step_offset: int = 0
    invalid_output: bool = False


class SimulatedBackend:
    """Implements the OPE-506 connector plus usage and progress reporting."""

    def __init__(self, clock: Callable[[], float], config: BackendConfig | None = None) -> None:
        self.clock = clock
        self.config = config or BackendConfig()
        self.jobs: dict[str, SimJob] = {}
        self.lock = threading.RLock()
        self.calls: dict[str, int] = {}
        self.log: list[str] = []
        self.on_submit: Callable[[mc.FitIntent], None] | None = None

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1
        self.log.append(name)

    def jobs_created(self) -> int:
        return len(self.jobs)

    def observe(self, binding: mc.ModelBinding, purpose: mc.Purpose) -> mc.Observation:
        with self.lock:
            self._count("observe")
            cfg = self.config
            now = self.clock()
            return mc.Observation(
                binding=binding,
                source_ref="simulated:observation-" + binding.model_ref,
                observed_at=now,
                expires_at=now + 600,
                resolved_revision=binding.immutable_revision if cfg.exact_revision else "moved",
                inference_supported=True,
                training_supported=cfg.training_supported,
                hard_cost_cap=cfg.hard_cost_cap,
                cancellation=cfg.cancellation,
                progress=cfg.progress,
                checkpoint_recovery=cfg.checkpoint_recovery,
                capture={},
            )

    def serialize_development(
        self, intent: mc.FitIntent, rows: tuple[Mapping[str, object], ...]
    ) -> bytes:
        self._count("serialize")
        return canonical([dict(row) for row in rows]).encode()

    def estimate(self, intent: mc.FitIntent) -> mc.FitBound | None:
        self._count("estimate")
        return intent.bound if self.config.estimate_known else None

    def _receipt(self, job: SimJob) -> mc.JobReceipt:
        return mc.JobReceipt(job.intent, job.state, job.job_ref, job.evidence_ref)

    def submit(self, intent: mc.FitIntent, development: bytes) -> mc.JobReceipt:
        with self.lock:
            self._count("submit")
            cfg = self.config
            if self.on_submit is not None:
                self.on_submit(intent)
            if cfg.fail_before_accept > 0:
                cfg.fail_before_accept -= 1
                raise OSError("simulated transport failure before acceptance")
            key = intent.idempotency_key
            job = self.jobs.get(key)
            if job is None:
                digest = hashlib.sha256(key.encode()).hexdigest()[:16]
                job = SimJob(
                    intent,
                    "simulated:job-" + digest,
                    "simulated:evidence-" + digest,
                    rows=len(json.loads(development)),
                )
                self.jobs[key] = job
            if cfg.crash_after_accept > 0:
                cfg.crash_after_accept -= 1
                raise WorkerCrash("simulated worker death after acceptance")
            if cfg.lose_ack_on_submit > 0:
                cfg.lose_ack_on_submit -= 1
                raise OSError("simulated lost acknowledgement")
            return self._receipt(job)

    def lookup(self, intent: mc.FitIntent) -> mc.JobReceipt | None:
        with self.lock:
            self._count("lookup")
            if self.config.hidden_lookups > 0:
                self.config.hidden_lookups -= 1
                return None
            job = self.jobs.get(intent.idempotency_key)
            return None if job is None else self._receipt(job)

    def confirm_absent(self, intent: mc.FitIntent) -> bool:
        with self.lock:
            self._count("confirm_absent")
            return intent.idempotency_key not in self.jobs

    def _job(self, receipt: mc.JobReceipt) -> SimJob:
        job = self.jobs.get(receipt.intent.idempotency_key)
        if job is None or job.job_ref != receipt.job_ref:
            raise LookupError("simulated job unavailable")
        return job

    def status(self, job: mc.JobReceipt) -> mc.JobReceipt:
        with self.lock:
            self._count("status")
            sim = self._job(job)
            if sim.state is mc.JobState.QUEUED:
                sim.state = mc.JobState.RUNNING
            return self._receipt(sim)

    def cancel(self, job: mc.JobReceipt) -> mc.JobReceipt:
        with self.lock:
            self._count("cancel")
            sim = self._job(job)
            if sim.state in (mc.JobState.QUEUED, mc.JobState.RUNNING):
                sim.state = mc.JobState.CANCELED
            return self._receipt(sim)

    def collect(self, job: mc.JobReceipt) -> mc.Candidate:
        with self.lock:
            self._count("collect")
            sim = self._job(job)
            if sim.state is not mc.JobState.COMPLETED:
                raise ValueError("job_not_collectable")
            checkpoint = sim.selected or f"simulated:ckpt-{sim.steps}"
            parent = (
                "moved" if self.config.invalid_output else job.intent.binding.immutable_revision
            )
            return mc.Candidate(
                job.intent,
                sim.job_ref,
                parent,
                "simulated:artifact-" + checkpoint.removeprefix("simulated:"),
                "simulated:replay-" + sim.job_ref.removeprefix("simulated:"),
            )

    def predict_for_replay(
        self, candidate: mc.Candidate, inputs: tuple[Mapping[str, object], ...]
    ) -> tuple[Mapping[str, object], ...]:
        return tuple({"label": "a"} for _ in inputs)

    # Capabilities beyond the OPE-506 protocol.

    def progress(self, job: mc.JobReceipt) -> fd.ProgressReport | None:
        """Each call advances the simulated fit by one evaluation interval."""
        with self.lock:
            self._count("progress")
            sim = self._job(job)
            cfg = self.config
            if sim.state is mc.JobState.RUNNING:
                sim.steps += cfg.cadence
                index = len(sim.evaluations)
                value = cfg.metrics[index] if index < len(cfg.metrics) else 0.5 + 0.02 * index
                sim.evaluations.append(
                    fd.Evaluation(sim.steps + cfg.step_offset, value, f"simulated:ckpt-{sim.steps}")
                )
                if sim.steps >= cfg.run_steps:
                    sim.state = mc.JobState.COMPLETED
            return fd.ProgressReport(
                sim.steps, sim.steps * cfg.seconds_per_step, tuple(sim.evaluations)
            )

    def stop_early(self, job: mc.JobReceipt, checkpoint_ref: str | None) -> fd.EarlyStopAck:
        with self.lock:
            self._count("stop_early")
            sim = self._job(job)
            applied = sim.state in (mc.JobState.QUEUED, mc.JobState.RUNNING)
            restored = ""
            if applied:
                sim.state = mc.JobState.COMPLETED
                if checkpoint_ref and self.config.checkpoint_recovery:
                    restored = sim.selected = checkpoint_ref
            return fd.EarlyStopAck(self._receipt(sim), applied, restored)

    def usage(self, job: mc.JobReceipt) -> Usage | None:
        with self.lock:
            self._count("usage")
            sim = self._job(job)
            cfg = self.config
            terminal = sim.state in (mc.JobState.COMPLETED, mc.JobState.CANCELED)
            if not terminal and not cfg.live_usage:
                return None
            if terminal and cfg.final_usage is not None:
                return cfg.final_usage(sim)
            return Usage(
                sim.job_ref,
                job.intent.bound.currency,
                sim.steps * cfg.minor_units_per_step,
                sim.steps * cfg.seconds_per_step,
                sim.rows,
                sim.steps,
                UsageSource.MEASURED,
            )


class WithoutEarlyStop(SimulatedBackend):
    """Progress reporting but no early-stop control (``stop_early`` absent)."""

    stop_early = None  # type: ignore[assignment]


class WithoutAbsence(SimulatedBackend):
    """A backend that can never confirm that no job exists (like the OPE-506 connectors)."""

    confirm_absent = None  # type: ignore[assignment]


class FailingEarlyStop(SimulatedBackend):
    def stop_early(self, job: mc.JobReceipt, checkpoint_ref: str | None) -> fd.EarlyStopAck:
        self._count("stop_early")
        raise OSError("simulated early-stop callback failure")


class OwnHold(SimulatedBackend):
    """A connector whose own journal holds the result (OPE-508 ``hold_reason``)."""

    def hold_reason(self, job: mc.JobReceipt) -> str | None:
        return "over_cap"


def nonfinite() -> float:
    return math.nan
