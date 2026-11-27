"""Restartable snapshot and change-feed extraction of a large generated workplace history.

OPE-519. A customer-controlled manifest of immutable partitions is read page by page into a
private, owner-local SQLite register. Rows and the partition checkpoint (count, rolling
digest, end cursor, mapping revision) commit in one transaction, so an interruption either
leaves the previous checkpoint or the next one, never a half page. A companion change feed
is consumed strictly after the frozen high-water cut, with explicit tombstones.

What is simulated: every source, partition, change feed and grant in the tests is a generated
sentinel double behind the three narrow ports below (`SnapshotSource`, `GrantPort`, and the
store). The process-crash test kills a child Python process mid-transaction on a local
SQLite file. Volume is synthesized at test time; nothing is committed or imported.

What is NOT proven: no real Parquet reader, MCP provider, customer export, installed
connector, provider rate limit, disk exhaustion or multi-host concurrency is exercised.
Mixed-document format readers belong to OPE-531 and the source-pair reader to OPE-434; this
module defines only the narrow ports it needs and must be reconciled with their adapter
interfaces. These tests prove software behavior only and establish no provider or customer
evidence. A generated result cannot satisfy a scale-qualification gate (OPE-521).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

MAX_PAGE = 500
PAIRED_FEEDS = ("decision", "outcome")
KINDS = ("decision", "outcome", "document")
_ZERO = "0" * 64


class ExtractionError(Exception):
    """A fixed, public-safe reason code that carries no source content."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _canon(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha(value: object) -> str:
    return hashlib.sha256(_canon(value).encode()).hexdigest()


@dataclass(frozen=True)
class SourceRow:
    kind: str
    native_id: str
    revision: int
    path: str
    content_digest: str
    size_bytes: int
    available_at: int | None = None
    occurred_at: int | None = None
    decision_key: str | None = None
    authority: str | None = None
    label: str | None = None


def row_fingerprint(row: SourceRow) -> str:
    return _sha(asdict(row))


def payload_digest(row: SourceRow) -> str:
    """Identity of what a revision says; a path or size change alone is not a content change."""
    return _sha(
        [
            row.kind,
            row.content_digest,
            row.decision_key,
            row.authority,
            row.label,
            row.available_at,
            row.occurred_at,
        ]
    )


def chain(previous: str, row: SourceRow) -> str:
    return hashlib.sha256((previous + row_fingerprint(row)).encode()).hexdigest()


def partition_digest(rows: Iterable[SourceRow]) -> str:
    digest = _ZERO
    for row in rows:
        digest = chain(digest, row)
    return digest


def family_id(tenant: str, resource_id: str, native_id: str) -> str:
    """Stable evidence-family identity: tenant-scoped native ID, never a path or a label."""
    return "family-" + _sha([tenant, resource_id, native_id])[:32]


@dataclass(frozen=True)
class PartitionRef:
    partition_id: str
    resource_id: str
    feed: str
    digest: str
    row_count: int


@dataclass(frozen=True)
class SourceCapability:
    change_log: bool
    deletion_feed: bool
    timestamp_only: bool = False
    tie_breaker: bool = False
    overlap: bool = False
    full_reconciliation_done: bool = False


@dataclass(frozen=True)
class SourceManifest:
    revision: str
    mapping_revision: str
    partitions: tuple[PartitionRef, ...]
    high_water: tuple[tuple[str, int], ...]
    capability: SourceCapability


@dataclass(frozen=True)
class PartitionPage:
    partition_id: str
    start: int
    rows: tuple[SourceRow, ...]
    mapping_revision: str
    exhausted: bool


@dataclass(frozen=True)
class ChangeEntry:
    feed: str
    feed_revision: int
    op: str  # upsert | reopen | tombstone
    resource_id: str
    native_id: str
    revision: int
    row: SourceRow | None = None


@dataclass(frozen=True)
class ChangePage:
    feed: str
    after: int
    through: int
    entries: tuple[ChangeEntry, ...]


class SnapshotSource(Protocol):
    def manifest(self) -> SourceManifest: ...

    def read_partition(self, partition_id: str, *, start: int, limit: int) -> PartitionPage: ...

    def read_changes(self, feed: str, *, after: int, limit: int) -> ChangePage: ...


class GrantPort(Protocol):
    def current(self, resource_id: str) -> str | None:
        """The current grant revision, or None once withdrawn."""


@dataclass(frozen=True)
class RunLimits:
    page_size: int = 250
    max_object_bytes: int = 1_000_000
    max_run_rows: int = 5_000_000
    max_run_bytes: int = 50_000_000_000
    max_run_seconds: float = 3600.0
    max_page_retries: int = 3

    def validate(self) -> None:
        if not 1 <= self.page_size <= MAX_PAGE:
            raise ExtractionError("invalid_limits")
        values = (self.max_object_bytes, self.max_run_rows, self.max_run_bytes)
        if any(type(v) is not int or v < 1 for v in values) or self.max_run_seconds <= 0:
            raise ExtractionError("invalid_limits")
        if self.max_page_retries < 0:
            raise ExtractionError("invalid_limits")


@dataclass(frozen=True)
class PageOutcome:
    status: str  # applied | replayed | cursor_gap | withdrawn | paused_drift | not_pending
    admitted: int = 0
    duplicates: int = 0
    rejected: int = 0
    disputed: int = 0
    limit: str = ""


@dataclass(frozen=True)
class StepResult:
    pages: int
    peak_page_rows: int
    retries: int
    stopped: str


@dataclass(frozen=True)
class PublishResult:
    published: bool
    reasons: tuple[str, ...]
    snapshot_id: str | None = None


@dataclass(frozen=True)
class SnapshotRecord:
    snapshot_id: str
    run_id: str
    manifest_digest: str
    completeness: str
    counts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class FeatureCitation:
    status: str  # cited | excluded_unknown_visibility | excluded_not_yet_available | unknown_object
    revision: int | None = None
    content_digest: str | None = None


@dataclass(frozen=True)
class Reconciliation:
    decisions: int
    verified: int
    pending: int
    disputed: int
    unmatched_outcomes: int
    non_authoritative_outcomes: int


@dataclass(frozen=True)
class DeletionReconciliation:
    applied: bool
    reason: str
    deleted: int = 0


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY, manifest_revision TEXT NOT NULL, mapping_revision TEXT NOT NULL,
        limits TEXT NOT NULL, capability TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL,
        predecessor TEXT, rows_seen INTEGER NOT NULL DEFAULT 0,
        input_bytes INTEGER NOT NULL DEFAULT 0, duplicate_deliveries INTEGER NOT NULL DEFAULT 0,
        pages INTEGER NOT NULL DEFAULT 0, retries INTEGER NOT NULL DEFAULT 0,
        discarded_pages INTEGER NOT NULL DEFAULT 0, elapsed REAL NOT NULL DEFAULT 0,
        excluded_unauthorized INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS partitions (
        run_id TEXT NOT NULL, partition_id TEXT NOT NULL, resource_id TEXT NOT NULL,
        feed TEXT NOT NULL, declared_digest TEXT NOT NULL, declared_count INTEGER NOT NULL,
        grant_revision TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL,
        cursor INTEGER NOT NULL, count INTEGER NOT NULL, digest TEXT NOT NULL,
        mapping_revision TEXT NOT NULL, failures INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(run_id, partition_id))""",
    """CREATE TABLE IF NOT EXISTS admissions (
        run_id TEXT NOT NULL, family_id TEXT NOT NULL, revision INTEGER NOT NULL,
        resource_id TEXT NOT NULL, partition_id TEXT NOT NULL, kind TEXT NOT NULL,
        native_id TEXT NOT NULL, path TEXT NOT NULL, content_digest TEXT NOT NULL,
        payload_digest TEXT NOT NULL, size_bytes INTEGER NOT NULL, available_at INTEGER,
        occurred_at INTEGER, decision_key TEXT, authority TEXT, label TEXT,
        state TEXT NOT NULL, PRIMARY KEY(run_id, family_id, revision))""",
    """CREATE TABLE IF NOT EXISTS renames (
        run_id TEXT NOT NULL, family_id TEXT NOT NULL, revision INTEGER NOT NULL,
        resource_id TEXT NOT NULL, PRIMARY KEY(run_id, family_id, revision))""",
    """CREATE TABLE IF NOT EXISTS tombstones (
        run_id TEXT NOT NULL, family_id TEXT NOT NULL, revision INTEGER NOT NULL,
        resource_id TEXT NOT NULL, origin TEXT NOT NULL,
        PRIMARY KEY(run_id, family_id, revision))""",
    """CREATE TABLE IF NOT EXISTS rejections (
        run_id TEXT NOT NULL, family_id TEXT NOT NULL, revision INTEGER NOT NULL,
        reason TEXT NOT NULL, resource_id TEXT NOT NULL,
        PRIMARY KEY(run_id, family_id, revision, reason))""",
    """CREATE TABLE IF NOT EXISTS feeds (
        run_id TEXT NOT NULL, feed TEXT NOT NULL, cut INTEGER NOT NULL, cursor INTEGER NOT NULL,
        PRIMARY KEY(run_id, feed))""",
    """CREATE TABLE IF NOT EXISTS receipts (
        run_id TEXT NOT NULL, feed TEXT NOT NULL, after_cursor INTEGER NOT NULL,
        through INTEGER NOT NULL, entries INTEGER NOT NULL,
        PRIMARY KEY(run_id, feed, after_cursor, through))""",
    """CREATE TABLE IF NOT EXISTS snapshots (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_id TEXT NOT NULL UNIQUE,
        run_id TEXT NOT NULL, manifest_digest TEXT NOT NULL, completeness TEXT NOT NULL,
        counts TEXT NOT NULL)""",
)


class BulkExtraction:
    """One tenant's restartable extraction register over a `SnapshotSource`."""

    def __init__(
        self,
        path: Path,
        source: SnapshotSource,
        grants: GrantPort,
        *,
        tenant: str,
        clock: Callable[[], float] = time.monotonic,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        if not tenant:
            raise ExtractionError("tenant_required")
        directory = path.absolute().parent
        info = directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ExtractionError("private_store_required")
        os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600))
        self._path = path
        self._source = source
        self._grants = grants
        self._tenant = tenant
        self._clock = clock
        self._fault = fault or (lambda _point: None)
        self._step_started = clock()
        with self._txn() as db:
            for statement in _SCHEMA:
                db.execute(statement)

    # ---- storage ---------------------------------------------------------------------

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _all(self, sql: str, *args: object) -> list[sqlite3.Row]:
        with self._txn() as db:
            return db.execute(sql, args).fetchall()

    def _one(self, sql: str, *args: object) -> sqlite3.Row | None:
        rows = self._all(sql, *args)
        return rows[0] if rows else None

    # ---- run lifecycle ---------------------------------------------------------------

    def start(self, limits: RunLimits | None = None) -> str:
        """Freeze the source manifest. Starting the same frozen manifest again resumes it."""
        limits = limits or RunLimits()
        limits.validate()
        manifest = self._source.manifest()
        if not manifest.partitions or not manifest.mapping_revision:
            raise ExtractionError("manifest_invalid")
        ids = [p.partition_id for p in manifest.partitions]
        if len(set(ids)) != len(ids):
            raise ExtractionError("manifest_invalid")
        run_id = "run-" + _sha([self._tenant, manifest.revision, manifest.mapping_revision])[:24]
        with self._txn() as db:
            if db.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
                return run_id
            db.execute(
                "INSERT INTO runs(run_id, manifest_revision, mapping_revision, limits,"
                " capability, state, reason) VALUES (?,?,?,?,?,'active','')",
                (
                    run_id,
                    manifest.revision,
                    manifest.mapping_revision,
                    _canon(asdict(limits)),
                    _canon(asdict(manifest.capability)),
                ),
            )
            for ref in manifest.partitions:
                self._insert_partition(db, run_id, ref, manifest.mapping_revision)
            for feed, cut in manifest.high_water:
                db.execute("INSERT INTO feeds VALUES (?,?,?,?)", (run_id, feed, cut, cut))
        return run_id

    def _insert_partition(
        self, db: sqlite3.Connection, run_id: str, ref: PartitionRef, mapping: str
    ) -> None:
        grant = self._grants.current(ref.resource_id)
        state, reason = ("pending", "") if grant else ("withdrawn", "grant_withdrawn")
        db.execute(
            "INSERT INTO partitions VALUES (?,?,?,?,?,?,?,?,?,0,0,?,?,0)",
            (
                run_id,
                ref.partition_id,
                ref.resource_id,
                ref.feed,
                ref.digest,
                ref.row_count,
                grant or "",
                state,
                reason,
                _ZERO,
                mapping,
            ),
        )

    def _run(self, run_id: str) -> sqlite3.Row:
        row = self._one("SELECT * FROM runs WHERE run_id=?", run_id)
        if row is None:
            raise ExtractionError("run_unknown")
        return row

    @staticmethod
    def limits_of(run: sqlite3.Row) -> RunLimits:
        return RunLimits(**json.loads(run["limits"]))

    def step(self, run_id: str, *, max_pages: int = 1_000_000) -> StepResult:
        """Advance partitions one bounded page at a time until done, blocked or out of budget."""
        self._step_started = self._clock()
        self._run(run_id)
        pages = peak = retries = 0
        stopped = ""
        for part in self._all(
            "SELECT partition_id FROM partitions WHERE run_id=? ORDER BY partition_id", run_id
        ):
            while pages < max_pages:
                outcome, rows = self._advance(run_id, part["partition_id"])
                if outcome == "page":
                    pages += 1
                    peak = max(peak, rows)
                    continue
                if outcome == "retry":
                    retries += 1
                if outcome == "limit":
                    stopped = self._run(run_id)["reason"]
                break
            if stopped:
                break
        if not stopped and pages >= max_pages:
            stopped = "page_budget"
        spent = self._clock() - self._step_started
        with self._txn() as db:
            db.execute(
                "UPDATE runs SET elapsed=elapsed+? WHERE run_id=?", (max(spent, 0.0), run_id)
            )
        return StepResult(pages, peak, retries, stopped)

    def _set_partition(self, run_id: str, partition_id: str, state: str, reason: str) -> None:
        with self._txn() as db:
            db.execute(
                "UPDATE partitions SET state=?, reason=? WHERE run_id=? AND partition_id=?"
                " AND state='pending'",
                (state, reason, run_id, partition_id),
            )

    def _limit_hit(self, run_id: str, reason: str) -> None:
        with self._txn() as db:
            db.execute(
                "UPDATE runs SET state='incomplete', reason=? WHERE run_id=? AND state='active'",
                (reason, run_id),
            )

    def _advance(self, run_id: str, partition_id: str) -> tuple[str, int]:
        run = self._run(run_id)
        part = self._one(
            "SELECT * FROM partitions WHERE run_id=? AND partition_id=?", run_id, partition_id
        )
        assert part is not None
        if part["state"] != "pending":
            return ("done" if part["state"] == "complete" else "blocked"), 0
        if run["state"] != "active":
            return "limit", 0
        limits = self.limits_of(run)
        if self._grants.current(part["resource_id"]) != part["grant_revision"]:
            self._set_partition(run_id, partition_id, "withdrawn", "grant_withdrawn")
            return "blocked", 0
        elapsed = run["elapsed"] + (self._clock() - self._step_started)
        if elapsed > limits.max_run_seconds:
            self._limit_hit(run_id, "run_limit_seconds")
            return "limit", 0
        remaining = limits.max_run_rows - run["rows_seen"]
        if remaining <= 0:
            self._limit_hit(run_id, "run_limit_rows")
            return "limit", 0
        try:
            page = self._source.read_partition(
                partition_id, start=part["cursor"], limit=min(limits.page_size, remaining)
            )
        except ExtractionError as error:
            if error.code != "source_unavailable":
                raise
            return self._record_failure(run_id, partition_id, limits), 0
        if len(page.rows) > min(limits.page_size, remaining):
            raise ExtractionError("page_over_limit")
        outcome = self.accept_partition_page(run_id, page)
        if outcome.limit:
            self._limit_hit(run_id, outcome.limit)
            return "limit", len(page.rows)
        if outcome.status == "applied":
            return "page", len(page.rows)
        return "blocked", 0

    def _record_failure(self, run_id: str, partition_id: str, limits: RunLimits) -> str:
        with self._txn() as db:
            db.execute("UPDATE runs SET retries=retries+1 WHERE run_id=?", (run_id,))
            db.execute(
                "UPDATE partitions SET failures=failures+1 WHERE run_id=? AND partition_id=?",
                (run_id, partition_id),
            )
            failures = db.execute(
                "SELECT failures FROM partitions WHERE run_id=? AND partition_id=?",
                (run_id, partition_id),
            ).fetchone()[0]
            if failures > limits.max_page_retries:
                db.execute(
                    "UPDATE partitions SET state='failed', reason='retry_limit'"
                    " WHERE run_id=? AND partition_id=?",
                    (run_id, partition_id),
                )
                return "blocked"
        return "retry"

    # ---- partition pages -------------------------------------------------------------

    def accept_partition_page(self, run_id: str, page: PartitionPage) -> PageOutcome:
        """Apply one page atomically with its checkpoint. Safe to call twice or out of order."""
        outcome = self._accept_partition_page(run_id, page)
        self._fault("after_commit")
        return outcome

    def _accept_partition_page(self, run_id: str, page: PartitionPage) -> PageOutcome:
        with self._txn() as db:
            run = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            part = db.execute(
                "SELECT * FROM partitions WHERE run_id=? AND partition_id=?",
                (run_id, page.partition_id),
            ).fetchone()
            if run is None or part is None:
                raise ExtractionError("run_unknown")
            if (
                part["state"] == "complete"
                and run["state"] == "active"
                and page.start + len(page.rows) <= part["cursor"]
            ):
                db.execute(
                    "UPDATE runs SET duplicate_deliveries=duplicate_deliveries+? WHERE run_id=?",
                    (len(page.rows), run_id),
                )
                return PageOutcome("replayed", duplicates=len(page.rows))
            if part["state"] != "pending" or run["state"] != "active":
                return PageOutcome("not_pending")
            if page.mapping_revision != part["mapping_revision"]:
                db.execute(
                    "UPDATE partitions SET state='paused_drift', reason='schema_drift'"
                    " WHERE run_id=? AND partition_id=?",
                    (run_id, page.partition_id),
                )
                return PageOutcome("paused_drift")
            cursor = part["cursor"]
            end = page.start + len(page.rows)
            if page.start > cursor:
                return PageOutcome("cursor_gap")
            if end <= cursor and not (page.exhausted and cursor == 0 and not page.rows):
                db.execute(
                    "UPDATE runs SET duplicate_deliveries=duplicate_deliveries+? WHERE run_id=?",
                    (len(page.rows), run_id),
                )
                return PageOutcome("replayed", duplicates=len(page.rows))
            # The grant is re-read inside the commit scope: a page staged before a withdrawal
            # is discarded whole, so nothing unauthorized is admitted or published.
            if self._grants.current(part["resource_id"]) != part["grant_revision"]:
                db.execute(
                    "UPDATE partitions SET state='withdrawn', reason='grant_withdrawn'"
                    " WHERE run_id=? AND partition_id=?",
                    (run_id, page.partition_id),
                )
                db.execute(
                    "UPDATE runs SET discarded_pages=discarded_pages+1 WHERE run_id=?", (run_id,)
                )
                return PageOutcome("withdrawn")
            return self._apply_rows(db, run, part, page)

    def _apply_rows(
        self, db: sqlite3.Connection, run: sqlite3.Row, part: sqlite3.Row, page: PartitionPage
    ) -> PageOutcome:
        limits = self.limits_of(run)
        run_id = run["run_id"]
        cursor, count, digest = part["cursor"], part["count"], part["digest"]
        rows_seen, input_bytes = run["rows_seen"], run["input_bytes"]
        tally: Counter[str] = Counter()
        limit = ""
        for index, row in enumerate(page.rows):
            offset = page.start + index
            if offset < cursor:
                tally["duplicates"] += 1
                continue
            if rows_seen >= limits.max_run_rows:
                limit = "run_limit_rows"
                break
            if input_bytes + max(row.size_bytes, 0) > limits.max_run_bytes:
                limit = "run_limit_bytes"
                break
            result = self._admit_row(db, run_id, part, row, offset, limits, allow_new=True)
            tally[result] += 1
            digest = chain(digest, row)
            cursor, count = offset + 1, count + 1
            rows_seen += 1
            input_bytes += max(row.size_bytes, 0)
        self._fault("after_rows")
        state, reason = "pending", ""
        if not limit and page.exhausted:
            matches = count == part["declared_count"] and digest == part["declared_digest"]
            state = "complete" if matches else "mismatch"
            reason = (
                ""
                if matches
                else ("count_mismatch" if count != part["declared_count"] else "digest_mismatch")
            )
        db.execute(
            "UPDATE partitions SET cursor=?, count=?, digest=?, state=?, reason=?"
            " WHERE run_id=? AND partition_id=?",
            (cursor, count, digest, state, reason, run_id, part["partition_id"]),
        )
        db.execute(
            "UPDATE runs SET rows_seen=?, input_bytes=?, pages=pages+1,"
            " duplicate_deliveries=duplicate_deliveries+? WHERE run_id=?",
            (rows_seen, input_bytes, tally["duplicates"] + tally["duplicate"], run_id),
        )
        if limit:
            db.execute(
                "UPDATE runs SET state='incomplete', reason=? WHERE run_id=?", (limit, run_id)
            )
        self._fault("before_commit")
        return PageOutcome(
            "applied",
            admitted=tally["admitted"],
            duplicates=tally["duplicates"] + tally["duplicate"],
            rejected=tally["rejected"],
            disputed=tally["disputed"],
            limit=limit,
        )

    def _admit_row(
        self,
        db: sqlite3.Connection,
        run_id: str,
        part: sqlite3.Row,
        row: SourceRow,
        offset: int,
        limits: RunLimits,
        *,
        allow_new: bool,
    ) -> str:
        resource = part["resource_id"]
        valid = (
            row.kind in KINDS
            and bool(row.native_id)
            and type(row.revision) is int
            and row.revision >= 1
            and bool(row.content_digest)
            and type(row.size_bytes) is int
            and row.size_bytes >= 0
        )
        if not valid:
            family = f"offset-{part['partition_id']}-{offset}"
            self._reject(db, run_id, family, max(offset, 0), "malformed_row", resource)
            return "rejected"
        family = family_id(self._tenant, resource, row.native_id)
        if row.size_bytes > limits.max_object_bytes:
            self._reject(db, run_id, family, row.revision, "object_over_limit", resource)
            return "rejected"
        return self._admit(db, run_id, part["partition_id"], resource, family, row, allow_new)

    @staticmethod
    def _reject(
        db: sqlite3.Connection, run_id: str, family: str, revision: int, reason: str, resource: str
    ) -> None:
        db.execute(
            "INSERT OR IGNORE INTO rejections VALUES (?,?,?,?,?)",
            (run_id, family, revision, reason, resource),
        )

    def _admit(
        self,
        db: sqlite3.Connection,
        run_id: str,
        partition_id: str,
        resource: str,
        family: str,
        row: SourceRow,
        allow_new: bool,
    ) -> str:
        digest = payload_digest(row)
        existing = db.execute(
            "SELECT payload_digest FROM admissions WHERE run_id=? AND family_id=? AND revision=?",
            (run_id, family, row.revision),
        ).fetchone()
        if existing is not None:
            if existing["payload_digest"] == digest:
                return "duplicate"
            db.execute(
                "UPDATE admissions SET state='disputed' WHERE run_id=? AND family_id=?"
                " AND revision=?",
                (run_id, family, row.revision),
            )
            return "disputed"
        if not allow_new:
            self._reject(db, run_id, family, row.revision, "before_cut_not_in_snapshot", resource)
            return "rejected"
        previous = db.execute(
            "SELECT path FROM admissions WHERE run_id=? AND family_id=? AND revision<?"
            " ORDER BY revision DESC LIMIT 1",
            (run_id, family, row.revision),
        ).fetchone()
        db.execute(
            "INSERT INTO admissions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'admitted')",
            (
                run_id,
                family,
                row.revision,
                resource,
                partition_id,
                row.kind,
                row.native_id,
                row.path,
                row.content_digest,
                digest,
                row.size_bytes,
                row.available_at,
                row.occurred_at,
                row.decision_key,
                row.authority,
                row.label,
            ),
        )
        if previous is not None and previous["path"] != row.path:
            db.execute(
                "INSERT OR IGNORE INTO renames VALUES (?,?,?,?)",
                (run_id, family, row.revision, resource),
            )
        return "admitted"

    # ---- change feed -----------------------------------------------------------------

    def consume_changes(self, run_id: str, feed: str, *, max_pages: int = 1_000_000) -> int:
        """Pull deltas strictly after the frozen cut (resuming from the feed cursor)."""
        run = self._run(run_id)
        if not json.loads(run["capability"])["change_log"]:
            raise ExtractionError("snapshot_only_source")
        limits = self.limits_of(run)
        pages = 0
        while pages < max_pages:
            feed_row = self._one("SELECT * FROM feeds WHERE run_id=? AND feed=?", run_id, feed)
            if feed_row is None:
                raise ExtractionError("feed_unknown")
            page = self._source.read_changes(feed, after=feed_row["cursor"], limit=limits.page_size)
            if len(page.entries) > limits.page_size:
                raise ExtractionError("page_over_limit")
            outcome = self.accept_change_page(run_id, page)
            pages += 1
            moved = page.through > feed_row["cursor"]
            if outcome.status != "applied" or not page.entries or outcome.limit or not moved:
                break
        return pages

    def accept_change_page(self, run_id: str, page: ChangePage) -> PageOutcome:
        with self._txn() as db:
            run = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            feed = db.execute(
                "SELECT * FROM feeds WHERE run_id=? AND feed=?", (run_id, page.feed)
            ).fetchone()
            if run is None or feed is None:
                raise ExtractionError("run_unknown")
            if run["state"] != "active":
                return PageOutcome("not_pending")
            if not json.loads(run["capability"])["change_log"]:
                raise ExtractionError("snapshot_only_source")
            cursor = feed["cursor"]
            if page.after > cursor:
                return PageOutcome("cursor_gap")
            top = max((e.feed_revision for e in page.entries), default=page.after)
            if page.through < top or page.through < page.after:
                raise ExtractionError("change_page_invalid")
            limits = self.limits_of(run)
            tally: Counter[str] = Counter()
            rows_seen = run["rows_seen"]
            partitions = {
                r["resource_id"]: r
                for r in db.execute(
                    "SELECT * FROM partitions WHERE run_id=? AND feed=?", (run_id, page.feed)
                ).fetchall()
            }
            limit = ""
            for entry in sorted(page.entries, key=lambda e: (e.feed_revision, e.native_id)):
                if rows_seen >= limits.max_run_rows:
                    limit = "run_limit_rows"
                    break
                replay = entry.feed_revision <= cursor
                tally[self._apply_change(db, run_id, partitions, entry, replay, limits)] += 1
                rows_seen += 0 if replay else 1
            if not limit:
                db.execute(
                    "UPDATE feeds SET cursor=MAX(cursor, ?) WHERE run_id=? AND feed=?",
                    (page.through, run_id, page.feed),
                )
                db.execute(
                    "INSERT OR IGNORE INTO receipts VALUES (?,?,?,?,?)",
                    (run_id, page.feed, page.after, page.through, len(page.entries)),
                )
            db.execute(
                "UPDATE runs SET rows_seen=?, duplicate_deliveries=duplicate_deliveries+?"
                " WHERE run_id=?",
                (rows_seen, tally["duplicate"], run_id),
            )
            if limit:
                db.execute(
                    "UPDATE runs SET state='incomplete', reason=? WHERE run_id=?", (limit, run_id)
                )
            self._fault("before_commit")
            return PageOutcome(
                "applied",
                admitted=tally["admitted"] + tally["tombstoned"],
                duplicates=tally["duplicate"],
                rejected=tally["rejected"],
                disputed=tally["disputed"],
                limit=limit,
            )

    def _apply_change(
        self,
        db: sqlite3.Connection,
        run_id: str,
        partitions: Mapping[str, sqlite3.Row],
        entry: ChangeEntry,
        replay: bool,
        limits: RunLimits,
    ) -> str:
        part = partitions.get(entry.resource_id)
        family = family_id(self._tenant, entry.resource_id, entry.native_id)
        if part is None:
            self._reject(db, run_id, family, entry.revision, "resource_out_of_scope", "")
            return "rejected"
        if self._grants.current(entry.resource_id) != part["grant_revision"]:
            db.execute(
                "UPDATE partitions SET state='withdrawn', reason='grant_withdrawn'"
                " WHERE run_id=? AND resource_id=? AND state IN ('pending','complete')",
                (run_id, entry.resource_id),
            )
            return "withdrawn"
        if entry.op == "tombstone":
            if replay:
                return "duplicate"
            db.execute(
                "INSERT OR IGNORE INTO tombstones VALUES (?,?,?,?,'feed')",
                (run_id, family, entry.revision, entry.resource_id),
            )
            return "tombstoned"
        row = entry.row
        if (
            entry.op not in ("upsert", "reopen")
            or row is None
            or row.native_id != entry.native_id
            or row.revision != entry.revision
            or row.size_bytes > limits.max_object_bytes
        ):
            self._reject(db, run_id, family, entry.revision, "malformed_change", entry.resource_id)
            return "rejected"
        return self._admit(
            db, run_id, part["partition_id"], entry.resource_id, family, row, not replay
        )

    # ---- deletion --------------------------------------------------------------------

    def deletion_semantics(self, run_id: str) -> str:
        capability = json.loads(self._run(run_id)["capability"])
        if capability["deletion_feed"] and capability["change_log"]:
            return "explicit_tombstone"
        return "reconciliation_only"

    def reconcile_deletions(
        self,
        run_id: str,
        listing: Iterable[tuple[str, str, int]],
        *,
        authorized: bool,
        complete: bool,
    ) -> DeletionReconciliation:
        """Declare disappearance only from a complete, authorized key/revision listing."""
        if not authorized:
            return DeletionReconciliation(False, "reconciliation_not_authorized")
        if not complete:
            return DeletionReconciliation(False, "reconciliation_incomplete")
        present = {family_id(self._tenant, r, n) for r, n, _ in listing}
        deleted = 0
        with self._txn() as db:
            resources = {
                r["resource_id"]
                for r in db.execute(
                    "SELECT resource_id FROM partitions WHERE run_id=? AND state='complete'",
                    (run_id,),
                )
            }
            for row in db.execute(
                "SELECT family_id, resource_id, MAX(revision) AS revision FROM admissions"
                " WHERE run_id=? GROUP BY family_id",
                (run_id,),
            ).fetchall():
                if row["resource_id"] in resources and row["family_id"] not in present:
                    cursor = db.execute(
                        "INSERT OR IGNORE INTO tombstones VALUES (?,?,?,?,'reconciliation')",
                        (run_id, row["family_id"], row["revision"], row["resource_id"]),
                    )
                    deleted += cursor.rowcount
        return DeletionReconciliation(True, "reconciled", deleted)

    # ---- views -----------------------------------------------------------------------

    def families(self, run_id: str) -> list[dict[str, Any]]:
        """Current state per evidence family, derived so arrival order cannot change it."""
        rows = self._all(
            "SELECT a.family_id, a.revision, a.state, a.payload_digest, a.kind,"
            " COALESCE((SELECT MAX(t.revision) FROM tombstones t WHERE t.run_id=a.run_id"
            " AND t.family_id=a.family_id), 0) AS tomb FROM admissions a WHERE a.run_id=? AND"
            " a.revision=(SELECT MAX(revision) FROM admissions b WHERE b.run_id=a.run_id"
            " AND b.family_id=a.family_id) ORDER BY a.family_id",
            run_id,
        )
        out = []
        for r in rows:
            state = "deleted" if r["tomb"] >= r["revision"] else r["state"]
            out.append(
                {
                    "family_id": r["family_id"],
                    "kind": r["kind"],
                    "revision": r["revision"],
                    "state": "live" if state == "admitted" else state,
                    "payload_digest": r["payload_digest"],
                }
            )
        return out

    def semantic_manifest(self, run_id: str) -> dict[str, Any]:
        """Order- and paging-independent content of the run; excludes delivery telemetry."""
        run = self._run(run_id)
        body = {
            "manifest_revision": run["manifest_revision"],
            "mapping_revision": run["mapping_revision"],
            "partitions": [
                [r["partition_id"], r["state"], r["cursor"], r["count"], r["digest"]]
                for r in self._all(
                    "SELECT * FROM partitions WHERE run_id=? ORDER BY partition_id", run_id
                )
            ],
            "admissions": [
                [r["family_id"], r["revision"], r["payload_digest"], r["state"]]
                for r in self._all(
                    "SELECT * FROM admissions WHERE run_id=? ORDER BY family_id, revision", run_id
                )
            ],
            "tombstones": [
                [r["family_id"], r["revision"], r["origin"]]
                for r in self._all(
                    "SELECT * FROM tombstones WHERE run_id=? ORDER BY family_id, revision", run_id
                )
            ],
            "rejections": [
                [r["family_id"], r["revision"], r["reason"]]
                for r in self._all(
                    "SELECT * FROM rejections WHERE run_id=? ORDER BY family_id, revision, reason",
                    run_id,
                )
            ],
            "renames": [
                [r["family_id"], r["revision"]]
                for r in self._all(
                    "SELECT * FROM renames WHERE run_id=? ORDER BY family_id, revision", run_id
                )
            ],
            "feeds": [
                [r["feed"], r["cut"], r["cursor"]]
                for r in self._all("SELECT * FROM feeds WHERE run_id=? ORDER BY feed", run_id)
            ],
        }
        return {**body, "digest": _sha(body)}

    def completeness(self, run_id: str) -> str:
        capability = json.loads(self._run(run_id)["capability"])
        if not capability["change_log"]:
            return "snapshot_only"
        if not capability["deletion_feed"]:
            return "unknown"
        if capability["timestamp_only"] and not (
            capability["tie_breaker"]
            and capability["overlap"]
            and capability["full_reconciliation_done"]
        ):
            return "unknown"
        return "complete"

    def status(self, run_id: str) -> dict[str, Any]:
        """Private counts and states only: no path, native ID or content reaches this view."""
        run = self._run(run_id)
        parts = self._all("SELECT * FROM partitions WHERE run_id=? ORDER BY partition_id", run_id)
        rejected = Counter(
            r["reason"] for r in self._all("SELECT reason FROM rejections WHERE run_id=?", run_id)
        )
        complete = all(p["state"] == "complete" for p in parts)
        latest = self.fit_snapshot()
        return {
            "run_id": run_id,
            "state": run["state"],
            "reason": run["reason"],
            "snapshot_state": "complete" if complete else "incomplete",
            "fit_usable": complete and latest is not None and latest.run_id == run_id,
            "limits": json.loads(run["limits"]),
            "rows_seen": run["rows_seen"],
            "input_bytes": run["input_bytes"],
            "duplicate_deliveries": run["duplicate_deliveries"],
            "discarded_pages": run["discarded_pages"],
            "retries": run["retries"],
            "rejected_rows": dict(sorted(rejected.items())),
            "excluded_unauthorized": run["excluded_unauthorized"],
            "completeness": self.completeness(run_id),
            "deletion_semantics": self.deletion_semantics(run_id),
            "partitions": [
                {
                    "partition_id": p["partition_id"],
                    "state": p["state"],
                    "reason": p["reason"],
                    "cursor": p["cursor"],
                    "count": p["count"],
                    "declared_count": p["declared_count"],
                }
                for p in parts
            ],
            "feeds": [
                {"feed": f["feed"], "cut": f["cut"], "cursor": f["cursor"]}
                for f in self._all("SELECT * FROM feeds WHERE run_id=? ORDER BY feed", run_id)
            ],
            "read_receipts": [
                [r["feed"], r["after_cursor"], r["through"], r["entries"]]
                for r in self._all(
                    "SELECT * FROM receipts WHERE run_id=? ORDER BY feed, after_cursor", run_id
                )
            ],
        }

    # ---- publication -----------------------------------------------------------------

    def publish(self, run_id: str) -> PublishResult:
        """Publish a paired snapshot only when every partition reconciles and is authorized."""
        parts = self._all("SELECT * FROM partitions WHERE run_id=?", run_id)
        reasons: list[str] = []
        feeds = {p["feed"] for p in parts}
        if not set(PAIRED_FEEDS) <= feeds:
            reasons.append("pair_incomplete")
        for p in parts:
            if p["state"] != "complete":
                reasons.append(f"partition_{p['state']}:{p['partition_id']}")
            elif self._grants.current(p["resource_id"]) != p["grant_revision"]:
                reasons.append(f"partition_withdrawn:{p['partition_id']}")
        for feed in {p["feed"] for p in parts}:
            done = [p for p in parts if p["feed"] == feed and p["state"] == "complete"]
            if sum(p["count"] for p in done) != sum(
                p["declared_count"] for p in parts if p["feed"] == feed
            ):
                reasons.append(f"counts_unreconciled:{feed}")
        if self._run(run_id)["state"] != "active":
            reasons.append("run_" + self._run(run_id)["state"])
        if reasons:
            return PublishResult(False, tuple(sorted(set(reasons))))
        manifest = self.semantic_manifest(run_id)
        counts = {
            "families": len(self.families(run_id)),
            "tombstones": len(manifest["tombstones"]),
            "rejections": len(manifest["rejections"]),
        }
        snapshot_id = "snapshot-" + manifest["digest"][:32]
        completeness = self.completeness(run_id)
        with self._txn() as db:
            db.execute(
                "INSERT OR IGNORE INTO snapshots(snapshot_id, run_id, manifest_digest,"
                " completeness, counts) VALUES (?,?,?,?,?)",
                (
                    snapshot_id,
                    run_id,
                    manifest["digest"],
                    completeness,
                    _canon(counts),
                ),
            )
        return PublishResult(True, (), snapshot_id)

    def fit_snapshot(self) -> SnapshotRecord | None:
        """The last verified snapshot; an incomplete or in-flight run never replaces it."""
        row = self._one("SELECT * FROM snapshots ORDER BY seq DESC LIMIT 1")
        if row is None:
            return None
        return SnapshotRecord(
            row["snapshot_id"],
            row["run_id"],
            row["manifest_digest"],
            row["completeness"],
            json.loads(row["counts"]),
        )

    def successor(self, run_id: str) -> str:
        """Resume over only still-authorized resources; withdrawn ones are excluded and counted."""
        prior = self._run(run_id)
        manifest = self._source.manifest()
        if manifest.revision != prior["manifest_revision"]:
            raise ExtractionError("manifest_changed")
        count = self._one("SELECT COUNT(*) AS n FROM runs WHERE predecessor=?", run_id)
        assert count is not None
        new_id = "run-" + _sha([run_id, "successor", count["n"] + 1])[:24]
        with self._txn() as db:
            parts = db.execute("SELECT * FROM partitions WHERE run_id=?", (run_id,)).fetchall()
            granted = {p["resource_id"]: self._grants.current(p["resource_id"]) for p in parts}
            keep = {r for r, g in granted.items() if g}
            excluded = sum(1 for p in parts if p["resource_id"] not in keep)
            db.execute(
                "INSERT INTO runs(run_id, manifest_revision, mapping_revision, limits,"
                " capability, state, reason, predecessor, excluded_unauthorized)"
                " VALUES (?,?,?,?,?,'active','',?,?)",
                (
                    new_id,
                    prior["manifest_revision"],
                    prior["mapping_revision"],
                    prior["limits"],
                    prior["capability"],
                    run_id,
                    excluded,
                ),
            )
            for p in parts:
                if p["resource_id"] not in keep:
                    continue
                state = "pending" if p["state"] in ("withdrawn", "failed") else p["state"]
                db.execute(
                    "INSERT INTO partitions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                    (
                        new_id,
                        p["partition_id"],
                        p["resource_id"],
                        p["feed"],
                        p["declared_digest"],
                        p["declared_count"],
                        granted[p["resource_id"]],
                        state,
                        "" if state == "pending" else p["reason"],
                        p["cursor"],
                        p["count"],
                        p["digest"],
                        p["mapping_revision"],
                    ),
                )
            for table in ("admissions", "renames", "tombstones", "rejections"):
                for r in db.execute(f"SELECT * FROM {table} WHERE run_id=?", (run_id,)).fetchall():
                    if r["resource_id"] in keep:
                        values = [new_id, *tuple(r)[1:]]
                        marks = ",".join("?" * len(values))
                        db.execute(f"INSERT OR IGNORE INTO {table} VALUES ({marks})", values)
            for f in db.execute("SELECT * FROM feeds WHERE run_id=?", (run_id,)).fetchall():
                db.execute("INSERT INTO feeds VALUES (?,?,?,?)", (new_id, *tuple(f)[1:]))
        return new_id

    # ---- as-of features and outcome reconciliation ------------------------------------

    def feature_citation(
        self, run_id: str, *, resource_id: str, native_id: str, decision_time: int
    ) -> FeatureCitation:
        """The newest revision the model could already see at decision time, else an exclusion."""
        family = family_id(self._tenant, resource_id, native_id)
        rows = self._all(
            "SELECT revision, content_digest, available_at FROM admissions WHERE run_id=? AND"
            " family_id=? AND state='admitted' ORDER BY revision DESC",
            run_id,
            family,
        )
        if not rows:
            return FeatureCitation("unknown_object")
        known = [r for r in rows if r["available_at"] is not None]
        if not known:
            return FeatureCitation("excluded_unknown_visibility")
        visible = [r for r in known if r["available_at"] <= decision_time]
        if not visible:
            return FeatureCitation("excluded_not_yet_available")
        best = max(visible, key=lambda r: r["revision"])
        return FeatureCitation("cited", best["revision"], best["content_digest"])

    def feature_coverage(
        self, run_id: str, requests: Iterable[tuple[str, str, int]]
    ) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for resource, native, when in requests:
            counts[
                self.feature_citation(
                    run_id, resource_id=resource, native_id=native, decision_time=when
                ).status
            ] += 1
        return dict(sorted(counts.items()))

    def reconcile_outcomes(self, run_id: str) -> Reconciliation:
        """Distinct decisions into pending/disputed/verified; unmatched outcomes separately."""
        live = [f for f in self.families(run_id) if f["state"] == "live"]
        ids = {f["family_id"] for f in live}
        rows = self._all(
            "SELECT a.* FROM admissions a WHERE a.run_id=? AND a.state='admitted' AND"
            " a.revision=(SELECT MAX(revision) FROM admissions b WHERE b.run_id=a.run_id"
            " AND b.family_id=a.family_id)",
            run_id,
        )
        rows = [r for r in rows if r["family_id"] in ids]
        decisions = {
            r["decision_key"] for r in rows if r["kind"] == "decision" and r["decision_key"]
        }
        finals: dict[str, set[str]] = {}
        unmatched = non_authoritative = 0
        for r in rows:
            if r["kind"] != "outcome":
                continue
            if r["decision_key"] not in decisions:
                unmatched += 1
            elif r["authority"] != "independent" or not r["label"]:
                non_authoritative += 1
            else:
                finals.setdefault(r["decision_key"], set()).add(r["label"])
        disputed = sum(1 for labels in finals.values() if len(labels) > 1)
        verified = sum(1 for labels in finals.values() if len(labels) == 1)
        return Reconciliation(
            len(decisions),
            verified,
            len(decisions) - verified - disputed,
            disputed,
            unmatched,
            non_authoritative,
        )
