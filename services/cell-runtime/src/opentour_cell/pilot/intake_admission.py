"""Durable customer-local admission shared by authorized intake transports."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from opentour_cell.pilot.intake_mcp import (
    MAX_PAGE,
    AdmissionHold,
    AdmissionReceipt,
    BenchmarkView,
    CatalogFeed,
    CatalogPage,
    HistoricalPair,
    IntakeError,
    IntakePrincipal,
    IntakeRequest,
    LiveBinding,
    LiveBindingResolver,
    RunStatus,
    Starter,
    parse_intake,
    require_ref,
)

MAX_RUNS = 1_000
MAX_REQUEST_KEYS = 10_000


@dataclass(frozen=True)
class AuthorizedSource:
    workflow_ref: str | None
    engagement_epoch: str
    purpose: str
    resource_id: str
    grant_revision: str
    binding_id: str | None = None
    creation_ref: str | None = None


class IntakeAuthority(LiveBindingResolver, Protocol):
    def guard(self, actor: IntakePrincipal) -> AbstractContextManager[None]:
        """Pin agent authority and source bindings until the scoped operation commits."""

    def discover(
        self, actor: IntakePrincipal, *, limit: int, cursor: str | None
    ) -> CatalogPage: ...

    def historical(
        self, actor: IntakePrincipal, request: HistoricalPair
    ) -> AuthorizedSource | None: ...

    def starter(self, actor: IntakePrincipal, request: Starter) -> AuthorizedSource | None: ...

    def live_reader_available(self, actor: IntakePrincipal, workflow_ref: str) -> bool: ...

    def permits_live_revision(
        self, actor: IntakePrincipal, binding: LiveBinding, source_revision: str
    ) -> bool: ...

    def benchmark(self, actor: IntakePrincipal, workflow_ref: str) -> BenchmarkView | None: ...


@dataclass(frozen=True)
class _Resolved:
    resource: AuthorizedSource
    revisions: tuple[str, ...]
    mode: str
    reader_available: bool = True


@dataclass(frozen=True)
class IntakeWork:
    run_ref: str
    workflow_ref: str
    mode: str
    resource: AuthorizedSource
    revisions: tuple[str, ...]
    benchmark_version: int | None = None
    benchmark_parent_run: str | None = None
    benchmark_current: bool = False


def _encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(value: object) -> str:
    return hashlib.sha256(_encode(value).encode()).hexdigest()


def _require(check: object, code: str = "intake_unavailable") -> None:
    if not check:
        raise IntakeError(code)


def _identity(actor: IntakePrincipal) -> None:
    _require(actor.role == "intake", "intake_role_required")
    require_ref(actor.tenant)
    require_ref(actor.actor)


def _serial(request: IntakeRequest, mode: str) -> str:
    copied = {field: value for field, value in asdict(request).items() if value is not None}
    copied.pop("idempotency_key")
    return _encode({**copied, "mode": mode})


def _run_ref(label: str) -> str:
    return f"{label}-{secrets.token_hex(16)}"


def _live_source(binding: LiveBinding) -> AuthorizedSource:
    return AuthorizedSource(
        binding.workflow_ref,
        binding.engagement_epoch,
        binding.feed_kind,
        binding.resource,
        binding.grant_revision,
        binding_id=binding.binding_id,
    )


class DurableIntake:
    """An admission queue with one operation per authorized canonical intent."""

    def __init__(self, directory: Path, authority: IntakeAuthority) -> None:
        try:
            directory = directory.absolute()
            _require(directory.resolve(strict=True) == directory, "private_intake_required")
            info = directory.stat()
            _require(
                stat.S_ISDIR(info.st_mode)
                and info.st_uid == os.getuid()
                and not info.st_mode & 0o077,
                "private_intake_required",
            )
            self.path = directory / "intake.sqlite3"
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                _require(
                    stat.S_ISREG(info.st_mode)
                    and info.st_uid == os.getuid()
                    and info.st_nlink == 1
                    and not info.st_mode & 0o077,
                    "private_intake_required",
                )
            finally:
                os.close(fd)
            self.authority = authority
            with self._db() as db:
                db.execute("""
                    CREATE TABLE IF NOT EXISTS intake_workflows (
                        tenant TEXT NOT NULL, creation_ref TEXT NOT NULL,
                        workflow_ref TEXT NOT NULL, resource TEXT NOT NULL,
                        epoch TEXT NOT NULL, PRIMARY KEY(tenant, creation_ref),
                        UNIQUE(tenant, workflow_ref))
                """)
                db.execute("""
                    CREATE TABLE IF NOT EXISTS intake_runs (
                        tenant TEXT NOT NULL, run_ref TEXT NOT NULL, intent TEXT NOT NULL,
                        workflow_ref TEXT NOT NULL, correlation_ref TEXT NOT NULL,
                        request TEXT NOT NULL, grant_revision TEXT NOT NULL,
                        PRIMARY KEY(tenant, run_ref), UNIQUE(tenant, intent))
                """)
                db.execute("""
                    CREATE TABLE IF NOT EXISTS intake_keys (
                        tenant TEXT NOT NULL, key_hash TEXT NOT NULL, intent TEXT NOT NULL,
                        PRIMARY KEY(tenant, key_hash))
                """)
                db.execute("""
                    CREATE TABLE IF NOT EXISTS intake_benchmark_admissions (
                        tenant TEXT NOT NULL, run_ref TEXT NOT NULL, workflow_ref TEXT NOT NULL,
                        epoch TEXT NOT NULL, version INTEGER NOT NULL, parent_run TEXT,
                        PRIMARY KEY(tenant,run_ref), UNIQUE(tenant,epoch,workflow_ref,version))
                """)
                db.execute("""
                    CREATE TABLE IF NOT EXISTS intake_materialization (
                        tenant TEXT NOT NULL, run_ref TEXT NOT NULL, receipt_ref TEXT NOT NULL,
                        PRIMARY KEY(tenant, run_ref))
                """)
                db.execute("""
                    CREATE TABLE IF NOT EXISTS intake_live_resources (
                        tenant TEXT NOT NULL, binding_version TEXT NOT NULL, resource TEXT NOT NULL,
                        PRIMARY KEY(tenant, binding_version))
                """)
        except (OSError, RuntimeError) as error:
            raise IntakeError("private_intake_required") from error

    @contextmanager
    def _db(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            if not write:
                db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def discover_sources(
        self, actor: IntakePrincipal, *, limit: int, cursor: str | None
    ) -> CatalogPage:
        _identity(actor)
        _require(type(limit) is int and 1 <= limit <= MAX_PAGE, "invalid_arguments")
        if cursor is not None:
            require_ref(cursor)
        with self.authority.guard(actor), self._db(write=False) as db:
            page = self.authority.discover(actor, limit=limit, cursor=cursor)
            _require(len(page.sources) <= limit)
            sources = tuple(
                entry
                for entry in page.sources
                if not isinstance(entry, CatalogFeed) or self._visible_live_feed(db, actor, entry)
            )
            return CatalogPage(sources, page.next_cursor)

    def _visible_live_feed(
        self, db: sqlite3.Connection, actor: IntakePrincipal, entry: CatalogFeed
    ) -> bool:
        binding = self.authority.resolve(entry.feed_binding_ref, actor)
        if (
            binding is None
            or binding.tenant != actor.tenant
            or binding.state != "current"
            or binding.binding_version != entry.binding_version
            or binding.feed_kind != entry.feed_kind
            or binding.source_revision != entry.source_revision
            or not self.authority.permits_live_revision(actor, binding, entry.source_revision)
        ):
            return False
        _, resource, pinned = self._live_resource_pin(
            db, actor.tenant, _live_source(binding), binding.binding_version
        )
        return pinned is None or pinned == resource

    def _resolve(self, actor: IntakePrincipal, request: IntakeRequest) -> _Resolved | AdmissionHold:
        if isinstance(request, HistoricalPair):
            pair = self.authority.historical(actor, request)
            if pair is None:
                raise IntakeError("intake_unavailable")
            return _Resolved(
                pair,
                (request.source_revision, request.correction_revision, request.policy_revision),
                "historical",
            )
        if isinstance(request, Starter):
            policy = self.authority.starter(actor, request)
            if policy is None:
                raise IntakeError("intake_unavailable")
            return _Resolved(
                policy, (request.source_revision, request.setup_policy_revision), "starter"
            )
        binding = self.authority.resolve(request.feed_binding_ref, actor)
        if binding is None or binding.tenant != actor.tenant:
            raise IntakeError("intake_unavailable")
        if binding.state == "superseded":
            successor = None
            if binding.successor_ref is not None:
                candidate = self.authority.resolve(binding.successor_ref, actor)
                if (
                    candidate is not None
                    and candidate.state == "current"
                    and candidate.tenant == binding.tenant
                    and candidate.engagement_epoch == binding.engagement_epoch
                    and candidate.workflow_ref == binding.workflow_ref
                    and candidate.binding_id == binding.binding_id
                    and candidate.resource == binding.resource
                    and candidate.feed_kind == binding.feed_kind
                ):
                    successor = binding.successor_ref
            return AdmissionHold("binding_superseded", successor)
        _require(
            binding.state == "current"
            and self.authority.permits_live_revision(actor, binding, request.source_revision)
        )
        return _Resolved(
            _live_source(binding),
            (binding.binding_version, request.source_revision),
            "live",
            self.authority.live_reader_available(actor, binding.workflow_ref),
        )

    def _workflow(
        self,
        db: sqlite3.Connection,
        actor: IntakePrincipal,
        request: IntakeRequest,
        scope: AuthorizedSource,
        *,
        create: bool,
    ) -> str:
        if not isinstance(request, Starter):
            if scope.workflow_ref is None:
                raise IntakeError("intake_unavailable")
            return scope.workflow_ref
        if request.creation_ref is not None:
            _require(scope.creation_ref is None or scope.creation_ref == request.creation_ref)
            found = db.execute(
                "SELECT * FROM intake_workflows WHERE tenant=? AND creation_ref=?",
                (actor.tenant, request.creation_ref),
            ).fetchone()
            if found is None and create:
                workflow_ref = _run_ref("workflow")
                db.execute(
                    "INSERT INTO intake_workflows VALUES (?,?,?,?,?)",
                    (
                        actor.tenant,
                        request.creation_ref,
                        workflow_ref,
                        scope.resource_id,
                        scope.engagement_epoch,
                    ),
                )
                return workflow_ref
        else:
            found = db.execute(
                "SELECT * FROM intake_workflows WHERE tenant=? AND workflow_ref=?",
                (actor.tenant, request.workflow_ref),
            ).fetchone()
        if (
            found is None
            or found["resource"] != scope.resource_id
            or found["epoch"] != scope.engagement_epoch
            or (scope.creation_ref is not None and found["creation_ref"] != scope.creation_ref)
            or (scope.workflow_ref is not None and found["workflow_ref"] != scope.workflow_ref)
        ):
            raise IntakeError("intake_unavailable")
        return str(found["workflow_ref"])

    @staticmethod
    def _intent(workflow_ref: str, resolved: _Resolved) -> str:
        scope = resolved.resource
        return _hash(
            (
                workflow_ref,
                resolved.mode,
                scope.purpose,
                scope.engagement_epoch,
                scope.binding_id or scope.resource_id,
                resolved.revisions,
            )
        )

    @staticmethod
    def _live_resource_pin(
        db: sqlite3.Connection,
        tenant: str,
        scope: AuthorizedSource,
        binding_version: str,
    ) -> tuple[str, str, str | None]:
        version = _hash(
            (scope.workflow_ref, scope.engagement_epoch, scope.binding_id, binding_version)
        )
        resource = _hash((scope.resource_id, scope.purpose))
        row = db.execute(
            "SELECT resource FROM intake_live_resources WHERE tenant=? AND binding_version=?",
            (tenant, version),
        ).fetchone()
        return version, resource, None if row is None else str(row["resource"])

    def _check_live_resource(
        self,
        db: sqlite3.Connection,
        tenant: str,
        resolved: _Resolved,
        *,
        create: bool,
    ) -> None:
        if resolved.mode != "live":
            return
        version, resource, pinned = self._live_resource_pin(
            db, tenant, resolved.resource, resolved.revisions[0]
        )
        if pinned is not None:
            _require(pinned == resource)
        else:
            _require(create)
            db.execute(
                "INSERT INTO intake_live_resources VALUES (?,?,?)", (tenant, version, resource)
            )

    @staticmethod
    def _capacity(db: sqlite3.Connection, tenant: str, table: str, maximum: int) -> None:
        query = {
            "runs": "SELECT count(*) FROM intake_runs WHERE tenant=?",
            "keys": "SELECT count(*) FROM intake_keys WHERE tenant=?",
        }[table]
        _require(db.execute(query, (tenant,)).fetchone()[0] < maximum, "intake_capacity_reached")

    def submit_intake(
        self, actor: IntakePrincipal, request: IntakeRequest
    ) -> AdmissionReceipt | AdmissionHold:
        _identity(actor)
        with self.authority.guard(actor), self._db() as db:
            resolved = self._resolve(actor, request)
            if isinstance(resolved, AdmissionHold):
                return resolved
            scope = resolved.resource
            workflow = self._workflow(db, actor, request, scope, create=True)
            self._check_live_resource(db, actor.tenant, resolved, create=True)
            intent = self._intent(workflow, resolved)
            key_hash = _hash(require_ref(request.idempotency_key))
            matching_key = db.execute(
                "SELECT intent FROM intake_keys WHERE tenant=? AND key_hash=?",
                (actor.tenant, key_hash),
            ).fetchone()
            if matching_key is not None and matching_key["intent"] != intent:
                previous = db.execute(
                    "SELECT * FROM intake_runs WHERE tenant=? AND intent=?",
                    (actor.tenant, matching_key["intent"]),
                ).fetchone()
                self._current_run(db, actor, previous)
                raise IntakeError("idempotency_conflict")
            run = db.execute(
                "SELECT * FROM intake_runs WHERE tenant=? AND intent=?",
                (actor.tenant, intent),
            ).fetchone()
            if run is None:
                self._capacity(db, actor.tenant, "runs", MAX_RUNS)
                run_ref, correlation_ref = _run_ref("run"), _run_ref("correlation")
                db.execute(
                    "INSERT INTO intake_runs VALUES (?,?,?,?,?,?,?)",
                    (
                        actor.tenant,
                        run_ref,
                        intent,
                        workflow,
                        correlation_ref,
                        _serial(request, resolved.mode),
                        scope.grant_revision,
                    ),
                )
            else:
                _require(run["grant_revision"] == scope.grant_revision)
                run_ref, correlation_ref = str(run["run_ref"]), str(run["correlation_ref"])
            if matching_key is None:
                self._capacity(db, actor.tenant, "keys", MAX_REQUEST_KEYS)
                db.execute(
                    "INSERT INTO intake_keys VALUES (?,?,?)", (actor.tenant, key_hash, intent)
                )
            if resolved.mode != "live":
                self._benchmark_order(db, actor, run_ref, workflow, scope, create=True)
            if self._materialized(db, actor, run_ref):
                state = "materialized"
            else:
                state = "queued" if resolved.reader_available else "pending"
            return AdmissionReceipt(run_ref, state, correlation_ref)

    @staticmethod
    def _benchmark_order(
        db: sqlite3.Connection,
        actor: IntakePrincipal,
        run_ref: str,
        workflow: str,
        scope: AuthorizedSource,
        *,
        create: bool = False,
    ) -> tuple[int | None, str | None]:
        row = db.execute(
            "SELECT * FROM intake_benchmark_admissions WHERE tenant=? AND run_ref=?",
            (actor.tenant, run_ref),
        ).fetchone()
        if row is None and create:
            # Existing deployments can have admissions predating this ledger. Do
            # not guess their epoch or jump over their actual durable order. Each
            # must first be re-authorized in order; old wire clients remain readable.
            unknown_prior = db.execute(
                "SELECT 1 FROM intake_runs r LEFT JOIN intake_benchmark_admissions b "
                "ON b.tenant=r.tenant AND b.run_ref=r.run_ref "
                "WHERE r.tenant=? AND r.workflow_ref=? AND b.run_ref IS NULL "
                "AND json_extract(r.request,'$.mode') != 'live' "
                "AND r.rowid < (SELECT rowid FROM intake_runs WHERE tenant=? AND run_ref=?) "
                "LIMIT 1",
                (actor.tenant, workflow, actor.tenant, run_ref),
            ).fetchone()
            if unknown_prior is not None:
                return None, None
            prior = db.execute(
                "SELECT version,run_ref FROM intake_benchmark_admissions "
                "WHERE tenant=? AND epoch=? AND workflow_ref=? ORDER BY version DESC LIMIT 1",
                (actor.tenant, scope.engagement_epoch, workflow),
            ).fetchone()
            version = 1 if prior is None else prior["version"] + 1
            parent = None if prior is None else prior["run_ref"]
            db.execute(
                "INSERT INTO intake_benchmark_admissions VALUES(?,?,?,?,?,?)",
                (actor.tenant, run_ref, workflow, scope.engagement_epoch, version, parent),
            )
            return version, parent
        if row is None:
            return None, None
        _require(row["workflow_ref"] == workflow and row["epoch"] == scope.engagement_epoch)
        return row["version"], row["parent_run"]

    def _current_run(
        self, db: sqlite3.Connection, actor: IntakePrincipal, row: sqlite3.Row | None
    ) -> tuple[str, _Resolved]:
        if row is None:
            raise IntakeError("intake_unavailable")
        request = parse_intake({**json.loads(row["request"]), "idempotency_key": "readback"})
        resolved = self._resolve(actor, request)
        if isinstance(resolved, AdmissionHold):
            raise IntakeError("intake_unavailable")
        workflow = self._workflow(db, actor, request, resolved.resource, create=False)
        self._check_live_resource(db, actor.tenant, resolved, create=False)
        _require(
            workflow == row["workflow_ref"]
            and self._intent(workflow, resolved) == row["intent"]
            and resolved.resource.grant_revision == row["grant_revision"]
        )
        return workflow, resolved

    def get_run(self, actor: IntakePrincipal, run_ref: str) -> RunStatus:
        _identity(actor)
        require_ref(run_ref)
        with self.authority.guard(actor), self._db(write=False) as db:
            row = db.execute(
                "SELECT * FROM intake_runs WHERE tenant=? AND run_ref=?",
                (actor.tenant, run_ref),
            ).fetchone()
            workflow, resolved = self._current_run(db, actor, row)
            benchmark = self.authority.benchmark(actor, workflow)
            if self._materialized(db, actor, run_ref):
                return RunStatus(
                    run_ref, "materialized", "inspect_workflow", None, workflow, benchmark
                )
            if not resolved.reader_available:
                return RunStatus(
                    run_ref,
                    "pending",
                    "connect_live_reader",
                    "live_reader_unavailable",
                    workflow,
                    benchmark,
                )
            return RunStatus(run_ref, "queued", "wait_for_worker", None, workflow, benchmark)

    @contextmanager
    def admitted_work(self, actor: IntakePrincipal, run_ref: str) -> Iterator[IntakeWork]:
        """Pin current admitted scope for a worker-owned metadata operation.

        This read-only accessor neither dispatches nor marks a run materialized.
        Consumers must not reacquire the authority's writer lock inside this guard.
        """
        _identity(actor)
        require_ref(run_ref)
        with self.authority.guard(actor), self._db(write=False) as db:
            row = db.execute(
                "SELECT * FROM intake_runs WHERE tenant=? AND run_ref=?", (actor.tenant, run_ref)
            ).fetchone()
            workflow, resolved = self._current_run(db, actor, row)
            version, parent = self._benchmark_order(db, actor, run_ref, workflow, resolved.resource)
            latest = db.execute(
                "SELECT run_ref FROM intake_benchmark_admissions WHERE tenant=? AND epoch=? "
                "AND workflow_ref=? ORDER BY version DESC LIMIT 1",
                (actor.tenant, resolved.resource.engagement_epoch, workflow),
            ).fetchone()
            yield IntakeWork(
                run_ref,
                workflow,
                resolved.mode,
                resolved.resource,
                resolved.revisions,
                version,
                parent,
                latest is not None and latest["run_ref"] == run_ref,
            )

    def current_benchmark_run(self, tenant: str, workflow: str, epoch: str) -> str | None:
        """Metadata only; caller must hold a current exact-scope inspection guard."""
        with self._db(write=False) as db:
            row = db.execute(
                "SELECT run_ref FROM intake_benchmark_admissions WHERE tenant=? AND epoch=? "
                "AND workflow_ref=? ORDER BY version DESC LIMIT 1",
                (tenant, epoch, workflow),
            ).fetchone()
            return None if row is None else str(row["run_ref"])

    @staticmethod
    def _materialized(db: sqlite3.Connection, actor: IntakePrincipal, run_ref: str) -> bool:
        return (
            db.execute(
                "SELECT 1 FROM intake_materialization WHERE tenant=? AND run_ref=?",
                (actor.tenant, run_ref),
            ).fetchone()
            is not None
        )

    @contextmanager
    def _worker_lease(self) -> Iterator[None]:
        path = self.path.with_name("intake-worker.lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            _require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and info.st_nlink == 1
                and not info.st_mode & 0o077,
                "private_intake_required",
            )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise IntakeError("intake_worker_busy") from error
            _require(os.path.samestat(info, path.lstat()), "private_intake_required")
            yield
        finally:
            os.close(fd)

    def materialize(
        self,
        actor: IntakePrincipal,
        run_ref: str,
        read: Callable[[IntakeWork], str],
    ) -> bool:
        """Record an opaque receipt from the trusted worker's read-only callback."""
        _identity(actor)
        require_ref(run_ref)

        def current(db: sqlite3.Connection) -> IntakeWork | None:
            row = db.execute(
                "SELECT * FROM intake_runs WHERE tenant=? AND run_ref=?", (actor.tenant, run_ref)
            ).fetchone()
            workflow, resolved = self._current_run(db, actor, row)
            if self._materialized(db, actor, run_ref) or not resolved.reader_available:
                return None
            return IntakeWork(
                run_ref, workflow, resolved.mode, resolved.resource, resolved.revisions
            )

        with self._worker_lease():
            with self.authority.guard(actor), self._db(write=False) as db:
                work = current(db)
                if work is None:
                    return False
                receipt = require_ref(read(work))
            with self.authority.guard(actor), self._db() as db:
                _require(current(db) == work)
                db.execute(
                    "INSERT INTO intake_materialization VALUES (?,?,?)",
                    (actor.tenant, run_ref, receipt),
                )
            return True
