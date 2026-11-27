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

Work partitioning: files remain the unit of READING. Source partitions are read in
source-then-partition order, because a customer partition is an immutable file whose rows span
many cases and re-reading it per bucket would multiply I/O. Every unit of work AFTER staging is
keyed by a work unit, (source, bucket): normalization, outcome joins, checkpoints and cursors,
failure isolation and scheduling. A bucket comes from a stable SHA-256 hash of a
length-prefixed (tenant, workflow, case) encoding (`work_partition_key`), where the case is
the ONE case field the mapping declares for the run (`case_field`), with no per-row fallback.
Known gap: an UNDECLARED case field still falls back per row (case key, else decision key)
because existing tests pin that default; see `case_of`.
Each work unit has its own cursor, count, digest and failure boundary (`normalize`), so a
failure in one unit never stops another, and outcome joins run per bucket. The bucket never
uses Python's per-process `hash()`, never uses the evidence-family identity, and is frozen
into the run with the case field, so a resumed process always assigns identically. Stores
written before this checkpoint format are refused explicitly (`checkpoint_format_unsupported`)
and left byte-for-byte untouched; they are never migrated or silently re-read.

Failure isolation for files: when a partition's bytes change, vanish or are replaced by a
link while it is being read, the reason is recorded on it, every row already staged from it is
quarantined (never cited, joined, normalized or published), and every other partition keeps
advancing within the same `step`. It refuses publication until a successor re-reads it from
its first row. Known gap: it stays `pending` and `step` still raises after the others have
advanced, instead of being marked `failed`, because an existing test pins both behaviours.

Source history: the store of record for what a manifest source has shown before, the
first-seen file digest of every partition ID, the digest of every manifest revision and the
append-only chain of the change log, lives IN this register, scoped by tenant and source
root, and the change chain by the change feed's declared identity rather than its file path.
The engine binds that history into the source itself; a caller cannot supply, swap or reset
it short of replacing the whole register (and with it every run and published snapshot). A
register that has seen a source but has lost its history refuses it (`source_history_missing`)
rather than starting fresh. Nothing here is signed: protecting the register against its own
writer needs the signing and key-custody contract that does not exist yet (OPE-566).

What is NOT proven: no real Parquet row reader, MCP provider, customer export, installed
connector, provider rate limit, disk exhaustion or multi-host concurrency is exercised. The
manifest-file route and its immutability ledger live in `bulk_manifest`; its Parquet row
decoder is not built (no Parquet library is a locked dependency).
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
# A store is opened only when it carries exactly this format; older stores lack the work
# partition columns and the frozen partitioning scheme, so they are refused, never re-read.
# v3 is deliberately never issued: the format-refusal test uses it as an unknown format.
CHECKPOINT_FORMAT = "opentour.ope519.checkpoint.v4"
WORK_PARTITION_SCHEME = "opentour.ope519.work-partition.sha256-lp.v1"
DEFAULT_WORK_BUCKETS = 64
MAX_WORK_BUCKETS = 65_536
# The one field per run that names a row's case, and the kind tag it carries in the hash.
CASE_FIELDS = {"decision_key": "decision", "case_key": "case"}
# None = undeclared: the earlier per-row fallback (case key, else decision key). It stays the
# default only because existing tests pin it; see `case_of`. A mapping should declare one.
DEFAULT_CASE_FIELD: str | None = None
UNKEYED = -1  # the work-unit key of a source's unkeyed rows (no value in the case field)
# Per-file integrity failures: they fail that partition and quarantine its staged rows, while
# every other partition continues. Anything else a source raises still stops the step.
INTEGRITY_FAILURES = frozenset(
    {
        "partition_mutated",
        "source_file_missing",
        "partition_path_invalid",
        "source_file_linked",
        "source_root_changed",
    }
)
# A normalizer raising this is retried up to the run's retry limit; any other code fails it.
NORMALIZER_UNAVAILABLE = "normalizer_unavailable"


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
    case_key: str | None = None


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
            # The case decides the work partition, so a different case at the same revision is
            # a contradiction (disputed), never a silent move between work partitions.
            row.case_key,
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


def _text_ok(value: object) -> bool:
    if type(value) is not str or not value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _length_prefixed(fields: tuple[str | None, ...]) -> bytes:
    """Unambiguous encoding: a field count, then per field a presence tag and a byte length.

    No separator character exists, so ("a|b", "c") and ("a", "b|c") encode differently, and an
    absent field (None) can never equal any present string, including the text "None".
    """
    out = [len(fields).to_bytes(4, "big")]
    for value in fields:
        if value is None:
            out.append(b"\x00")
            continue
        data = value.encode("utf-8")
        out.append(b"\x01" + len(data).to_bytes(8, "big") + data)
    return b"".join(out)


def work_partition_key(tenant: str, workflow: str | None, case_kind: str, case: str) -> str:
    """SHA-256 over the domain-separated, length-prefixed (tenant, workflow, case) encoding.

    Deterministic across processes, machines and Python versions: no `hash()`, no JSON
    formatting choices, no locale. Its inputs never include the evidence-family identity or
    any split or benchmark-family assignment, so data partitioning cannot leak into them.
    """
    if not _text_ok(tenant) or not (workflow is None or _text_ok(workflow)):
        raise ExtractionError("work_partition_key_invalid")
    if case_kind not in ("case", "decision") or not _text_ok(case):
        raise ExtractionError("work_partition_key_invalid")
    encoded = _length_prefixed((WORK_PARTITION_SCHEME, tenant, workflow, case_kind, case))
    return hashlib.sha256(encoded).hexdigest()


def work_bucket(tenant: str, workflow: str | None, case_kind: str, case: str, buckets: int) -> int:
    if type(buckets) is not int or not 1 <= buckets <= MAX_WORK_BUCKETS:
        raise ExtractionError("work_buckets_invalid")
    return int(work_partition_key(tenant, workflow, case_kind, case), 16) % buckets


def case_of(row: SourceRow, field: str | None = DEFAULT_CASE_FIELD) -> tuple[str, str] | None:
    """The row's case from the ONE case field the mapping declares for the run; None = unkeyed.

    With a declared field there is no per-row fallback: a decision row and its outcome row
    must both carry the declared field to land in the same bucket, and a row lacking it goes
    to its source's unkeyed work partition. The kind is part of the hashed encoding, so the
    same text under the two fields hashes differently.

    KNOWN DEFECT, kept only while existing tests pin it: an undeclared field (None) falls back
    per row, case key else decision key, so one case can land in two buckets when its rows
    carry different fields. Declaring the field removes this; making a declaration mandatory
    needs the owner to approve changing the tests that pin the fallback.
    """
    if field is None:
        if row.case_key is not None:
            return ("case", row.case_key)
        if row.decision_key:
            return ("decision", row.decision_key)
        return None
    if field not in CASE_FIELDS:
        raise ExtractionError("case_field_invalid")
    value = getattr(row, field)
    if not value:
        return None
    return (CASE_FIELDS[field], value)


def _case_fields_ok(row: SourceRow) -> bool:
    if row.case_key is not None and not _text_ok(row.case_key):
        return False
    if row.decision_key is None or row.decision_key == "":
        return True
    return _text_ok(row.decision_key)


@dataclass(frozen=True)
class PartitionRef:
    partition_id: str
    resource_id: str
    feed: str
    digest: str
    row_count: int
    # The SHA-256 the source itself computed over the file's bytes, when it reads files.
    file_sha256: str | None = None


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
    # cited | excluded_unknown_visibility | excluded_not_yet_available | unknown_object
    # | excluded_quarantined (staged only from a failed partition) | excluded_not_sealed
    status: str
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
    # Decisions whose bucket has a failed work unit: still counted (as pending), never joined.
    held_decisions: int = 0


@dataclass(frozen=True)
class StagedEvent:
    """What a normalizer sees of one staged admission: identities and digests, no content."""

    resource_id: str
    bucket: int | None
    family_id: str
    revision: int
    kind: str
    payload_digest: str


@dataclass(frozen=True)
class WorkStep:
    pages: int
    normalized: int
    failed_units: int
    waiting_units: int


@dataclass(frozen=True)
class ChangeLogState:
    """How many committed lines of a change feed were verified, and their rolling digest."""

    lines: int
    chain: str


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
        excluded_unauthorized INTEGER NOT NULL DEFAULT 0, work_scheme TEXT NOT NULL,
        work_buckets INTEGER NOT NULL, workflow TEXT, case_field TEXT,
        tenant TEXT NOT NULL, source_scope TEXT)""",
    """CREATE TABLE IF NOT EXISTS partitions (
        run_id TEXT NOT NULL, partition_id TEXT NOT NULL, resource_id TEXT NOT NULL,
        feed TEXT NOT NULL, declared_digest TEXT NOT NULL, declared_count INTEGER NOT NULL,
        grant_revision TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL,
        cursor INTEGER NOT NULL, count INTEGER NOT NULL, digest TEXT NOT NULL,
        mapping_revision TEXT NOT NULL, failures INTEGER NOT NULL DEFAULT 0, file_sha256 TEXT,
        PRIMARY KEY(run_id, partition_id))""",
    """CREATE TABLE IF NOT EXISTS admissions (
        run_id TEXT NOT NULL, family_id TEXT NOT NULL, revision INTEGER NOT NULL,
        resource_id TEXT NOT NULL, partition_id TEXT NOT NULL, kind TEXT NOT NULL,
        native_id TEXT NOT NULL, path TEXT NOT NULL, content_digest TEXT NOT NULL,
        payload_digest TEXT NOT NULL, size_bytes INTEGER NOT NULL, available_at INTEGER,
        occurred_at INTEGER, decision_key TEXT, authority TEXT, label TEXT,
        state TEXT NOT NULL, case_key TEXT, work_bucket INTEGER, seq INTEGER NOT NULL,
        origin TEXT NOT NULL, normalized INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(run_id, family_id, revision))""",
    """CREATE INDEX IF NOT EXISTS admissions_work ON admissions(
        run_id, resource_id, work_bucket, family_id, revision)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS admissions_seq ON admissions(run_id, seq)""",
    """CREATE INDEX IF NOT EXISTS admissions_unit ON admissions(
        run_id, resource_id, work_bucket, seq)""",
    """CREATE TABLE IF NOT EXISTS work_units (
        run_id TEXT NOT NULL, resource_id TEXT NOT NULL, bucket INTEGER NOT NULL,
        state TEXT NOT NULL, reason TEXT NOT NULL, cursor INTEGER NOT NULL,
        count INTEGER NOT NULL, digest TEXT NOT NULL, excluded INTEGER NOT NULL,
        failures INTEGER NOT NULL, PRIMARY KEY(run_id, resource_id, bucket))""",
    """CREATE TABLE IF NOT EXISTS source_scopes (
        tenant TEXT NOT NULL, scope TEXT NOT NULL, feed_id TEXT,
        manifests INTEGER NOT NULL, partitions INTEGER NOT NULL, PRIMARY KEY(tenant, scope))""",
    """CREATE TABLE IF NOT EXISTS source_manifests (
        tenant TEXT NOT NULL, scope TEXT NOT NULL, revision TEXT NOT NULL,
        sha256 TEXT NOT NULL, PRIMARY KEY(tenant, scope, revision))""",
    """CREATE TABLE IF NOT EXISTS source_partitions (
        tenant TEXT NOT NULL, scope TEXT NOT NULL, partition_id TEXT NOT NULL,
        sha256 TEXT NOT NULL, byte_size INTEGER NOT NULL, first_revision TEXT NOT NULL,
        PRIMARY KEY(tenant, scope, partition_id))""",
    """CREATE TABLE IF NOT EXISTS source_feeds (
        tenant TEXT NOT NULL, scope TEXT NOT NULL, feed_id TEXT NOT NULL, predecessor TEXT,
        state TEXT NOT NULL, lines INTEGER NOT NULL, chain TEXT NOT NULL,
        PRIMARY KEY(tenant, scope, feed_id))""",
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
    """CREATE TABLE IF NOT EXISTS change_holds (
        run_id TEXT NOT NULL, feed TEXT NOT NULL, resource_id TEXT NOT NULL,
        feed_revision INTEGER NOT NULL, PRIMARY KEY(run_id, feed, resource_id))""",
    """CREATE TABLE IF NOT EXISTS snapshots (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_id TEXT NOT NULL UNIQUE,
        run_id TEXT NOT NULL, manifest_digest TEXT NOT NULL, completeness TEXT NOT NULL,
        counts TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
)


# An admission feeds work after staging only once what staged it is sealed and trusted: a
# snapshot row once its file partition is complete, a change row once no partition of its
# source is still pending, and never a quarantined row or one in a failed work unit. Rows of
# a source partition still being read wait; rows of a failed, withdrawn, drifted or
# mismatched partition are excluded.
_SEALED = (
    "a.state != 'quarantined' AND ((a.origin = 'snapshot' AND (SELECT p.state FROM partitions p"
    " WHERE p.run_id = a.run_id AND p.partition_id = a.partition_id) = 'complete') OR"
    " (a.origin = 'change' AND NOT EXISTS (SELECT 1 FROM partitions p WHERE p.run_id = a.run_id"
    " AND p.resource_id = a.resource_id AND p.state != 'complete')))"
)
_UNIT_OK = (
    "NOT EXISTS (SELECT 1 FROM work_units u WHERE u.run_id = a.run_id AND u.resource_id ="
    " a.resource_id AND u.bucket = COALESCE(a.work_bucket, -1) AND u.state = 'failed')"
)


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    return db


@contextmanager
def _transaction(path: Path) -> Iterator[sqlite3.Connection]:
    db = _connect(path)
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


class SourceHistory:
    """A source's history kept IN the extraction register, scoped by tenant and source root.

    Only digests the reader itself computed from bytes it read are recorded; a digest a
    manifest declares is never taken as fact. The engine creates this and binds it into its
    source (`BulkExtraction.__init__`); a source refuses to be re-bound to any other register,
    tenant or scope, so the store of record cannot be swapped or reset beneath a run.
    """

    def __init__(self, register: Path, tenant: str, scope: str) -> None:
        info = os.stat(register)
        self._path = register
        self._tenant = tenant
        self._scope = scope
        self.binding = (info.st_dev, info.st_ino, tenant, scope)

    def _audit(self, db: sqlite3.Connection) -> sqlite3.Row | None:
        """Refuse a source this register has seen whose recorded history has gone missing."""
        key = (self._tenant, self._scope)
        row: sqlite3.Row | None = db.execute(
            "SELECT * FROM source_scopes WHERE tenant=? AND scope=?", key
        ).fetchone()
        counts = {
            table: db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE tenant=? AND scope=?", key
            ).fetchone()[0]
            for table in ("source_manifests", "source_partitions", "source_feeds")
        }
        runs = db.execute(
            "SELECT COUNT(*) FROM runs WHERE tenant=? AND source_scope=?", key
        ).fetchone()[0]
        if row is None:
            if runs or any(counts.values()):
                raise ExtractionError("source_history_missing")
            return None
        if (
            row["manifests"] < 1
            or counts["source_manifests"] != row["manifests"]
            or counts["source_partitions"] != row["partitions"]
        ):
            raise ExtractionError("source_history_missing")
        if row["feed_id"] is not None and self._feed(db, row["feed_id"]) is None:
            raise ExtractionError("source_history_missing")
        unrecorded = db.execute(
            "SELECT COUNT(*) FROM partitions p JOIN runs r ON r.run_id=p.run_id"
            " WHERE r.tenant=? AND r.source_scope=? AND NOT EXISTS (SELECT 1 FROM"
            " source_partitions s WHERE s.tenant=r.tenant AND s.scope=r.source_scope"
            " AND s.partition_id=p.partition_id)",
            key,
        ).fetchone()[0]
        if unrecorded:
            raise ExtractionError("source_history_missing")
        return row

    def _feed(self, db: sqlite3.Connection, feed_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = db.execute(
            "SELECT * FROM source_feeds WHERE tenant=? AND scope=? AND feed_id=?",
            (self._tenant, self._scope, feed_id),
        ).fetchone()
        return row

    def partition(self, partition_id: str) -> tuple[str, int] | None:
        with _transaction(self._path) as db:
            self._audit(db)
            row = db.execute(
                "SELECT sha256, byte_size FROM source_partitions WHERE tenant=? AND scope=?"
                " AND partition_id=?",
                (self._tenant, self._scope, partition_id),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def manifest(self, revision: str) -> str | None:
        with _transaction(self._path) as db:
            self._audit(db)
            row = db.execute(
                "SELECT sha256 FROM source_manifests WHERE tenant=? AND scope=? AND revision=?",
                (self._tenant, self._scope, revision),
            ).fetchone()
        return row[0] if row else None

    def record_manifest(
        self,
        revision: str,
        digest: str,
        partitions: list[tuple[str, str, int]],
        feed: tuple[str, str | None] | None,
    ) -> None:
        """Record a verified manifest all at once, re-checking everything inside the commit.

        `feed` is the change feed's declared (identity, predecessor), or None for a
        snapshot-only manifest. The chain is keyed by that identity, never by the log's path:
        renaming the log continues the same chain. A different identity for a source that
        already has one is refused unless it explicitly succeeds the current identity.
        """
        key = (self._tenant, self._scope)
        with _transaction(self._path) as db:
            scope = self._audit(db)
            known = db.execute(
                "SELECT sha256 FROM source_manifests WHERE tenant=? AND scope=? AND revision=?",
                (*key, revision),
            ).fetchone()
            if known is not None and known[0] != digest:
                raise ExtractionError("manifest_revision_rewritten")
            added = 0
            for partition_id, sha, size in partitions:
                row = db.execute(
                    "SELECT sha256, byte_size FROM source_partitions WHERE tenant=? AND scope=?"
                    " AND partition_id=?",
                    (*key, partition_id),
                ).fetchone()
                if row is not None and (row[0], row[1]) != (sha, size):
                    raise ExtractionError("partition_mutated")
                if row is None:
                    db.execute(
                        "INSERT INTO source_partitions VALUES (?,?,?,?,?,?)",
                        (*key, partition_id, sha, size, revision),
                    )
                    added += 1
            current = scope["feed_id"] if scope is not None else None
            feed_id = self._record_feed(db, current, feed)
            if known is None:
                db.execute(
                    "INSERT INTO source_manifests VALUES (?,?,?,?)", (*key, revision, digest)
                )
            new = 1 if known is None else 0
            db.execute(
                "INSERT INTO source_scopes VALUES (?,?,?,?,?) ON CONFLICT(tenant, scope) DO"
                " UPDATE SET feed_id=excluded.feed_id, manifests=manifests+?,"
                " partitions=partitions+?",
                (*key, feed_id, new, added, new, added),
            )

    def _record_feed(
        self, db: sqlite3.Connection, current: str | None, feed: tuple[str, str | None] | None
    ) -> str | None:
        if feed is None:
            # A source that had an append-only change feed cannot quietly drop it.
            if current is not None:
                raise ExtractionError("change_feed_identity_changed")
            return None
        feed_id, succeeds = feed
        existing = self._feed(db, feed_id)
        if existing is not None and existing["state"] != "current":
            raise ExtractionError("change_feed_identity_changed")  # a retired identity
        if current is None:
            # First sight: nothing recorded can contradict the declared predecessor.
            db.execute(
                "INSERT INTO source_feeds VALUES (?,?,?,?,'current',0,?)",
                (self._tenant, self._scope, feed_id, succeeds, _ZERO),
            )
        elif feed_id == current:
            assert existing is not None
            if succeeds is not None and succeeds != existing["predecessor"]:
                raise ExtractionError("change_feed_identity_changed")
        else:
            if succeeds != current:
                raise ExtractionError("change_feed_identity_changed")
            db.execute(
                "UPDATE source_feeds SET state='retired' WHERE tenant=? AND scope=? AND feed_id=?",
                (self._tenant, self._scope, current),
            )
            db.execute(
                "INSERT INTO source_feeds VALUES (?,?,?,?,'current',0,?)",
                (self._tenant, self._scope, feed_id, current, _ZERO),
            )
        return feed_id

    def change_feed(self, feed_id: str) -> ChangeLogState:
        """The verified prefix of the CURRENT change feed with this declared identity."""
        with _transaction(self._path) as db:
            scope = self._audit(db)
            row = self._feed(db, feed_id)
            if scope is None or row is None or scope["feed_id"] != feed_id:
                raise ExtractionError("change_feed_identity_changed")
        return ChangeLogState(row["lines"], row["chain"])

    def advance_change_feed(self, feed_id: str, seen: ChangeLogState, new: ChangeLogState) -> None:
        """Compare-and-set: the history only moves forward from exactly the state verified."""
        with _transaction(self._path) as db:
            scope = self._audit(db)
            row = self._feed(db, feed_id)
            if scope is None or row is None or scope["feed_id"] != feed_id:
                raise ExtractionError("change_feed_identity_changed")
            if (row["lines"], row["chain"]) != (seen.lines, seen.chain):
                raise ExtractionError("ledger_conflict")
            if new.lines < seen.lines:
                raise ExtractionError("change_manifest_truncated")
            db.execute(
                "UPDATE source_feeds SET lines=?, chain=? WHERE tenant=? AND scope=? AND feed_id=?",
                (new.lines, new.chain, self._tenant, self._scope, feed_id),
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
        workflow: str | None = None,
        work_buckets: int = DEFAULT_WORK_BUCKETS,
        case_field: str | None = DEFAULT_CASE_FIELD,
        normalizer: Callable[[StagedEvent], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        if not tenant:
            raise ExtractionError("tenant_required")
        if not _text_ok(tenant) or not (workflow is None or _text_ok(workflow)):
            raise ExtractionError("work_partition_key_invalid")
        if type(work_buckets) is not int or not 1 <= work_buckets <= MAX_WORK_BUCKETS:
            raise ExtractionError("work_buckets_invalid")
        if case_field is not None and case_field not in CASE_FIELDS:
            raise ExtractionError("case_field_invalid")
        directory = path.absolute().parent
        info = directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ExtractionError("private_store_required")
        os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600))
        self._path = path
        self._source = source
        self._grants = grants
        self._tenant = tenant
        self._workflow = workflow
        self._work_buckets = work_buckets
        self._case_field = case_field
        self._normalizer = normalizer or (lambda event: event.payload_digest)
        self._clock = clock
        self._fault = fault or (lambda _point: None)
        self._step_started = clock()
        with self._txn() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables:
                # Refuse anything that is not exactly this checkpoint format, before writing a
                # byte: an older store has no work partition columns and must not be re-read.
                found = None
                if "meta" in tables:
                    found = db.execute(
                        "SELECT value FROM meta WHERE key='checkpoint_format'"
                    ).fetchone()
                if found is None or found[0] != CHECKPOINT_FORMAT:
                    raise ExtractionError("checkpoint_format_unsupported")
            else:
                for statement in _SCHEMA:
                    db.execute(statement)
                db.execute("INSERT INTO meta VALUES ('checkpoint_format', ?)", (CHECKPOINT_FORMAT,))
        # A source that reads files keeps its history in THIS register: bind it here, so the
        # caller never supplies the store of record. Re-binding elsewhere is refused.
        self._source_scope: str | None = None
        bind = getattr(source, "bind_history", None)
        if callable(bind):
            scope = source.history_scope()  # type: ignore[attr-defined]
            if not _text_ok(scope):
                raise ExtractionError("source_scope_invalid")
            bind(SourceHistory(path, tenant, scope))
            self._source_scope = scope

    # ---- storage ---------------------------------------------------------------------

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        with _transaction(self._path) as db:
            yield db

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
            existing = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if existing is not None:
                self._check_scheme(existing)
                self._check_bound(db, existing, manifest)
                return run_id
            db.execute(
                "INSERT INTO runs(run_id, manifest_revision, mapping_revision, limits,"
                " capability, state, reason, work_scheme, work_buckets, workflow, case_field,"
                " tenant, source_scope) VALUES (?,?,?,?,?,'active','',?,?,?,?,?,?)",
                (
                    run_id,
                    manifest.revision,
                    manifest.mapping_revision,
                    _canon(asdict(limits)),
                    _canon(asdict(manifest.capability)),
                    WORK_PARTITION_SCHEME,
                    self._work_buckets,
                    self._workflow,
                    self._case_field,
                    self._tenant,
                    self._source_scope,
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
            "INSERT INTO partitions(run_id, partition_id, resource_id, feed, declared_digest,"
            " declared_count, grant_revision, state, reason, cursor, count, digest,"
            " mapping_revision, failures, file_sha256) VALUES (?,?,?,?,?,?,?,?,?,0,0,?,?,0,?)",
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
                ref.file_sha256,
            ),
        )

    def _check_scheme(self, run: sqlite3.Row) -> None:
        """A run's partitioning is frozen at start; a differently configured opener is refused."""
        frozen = (run["work_scheme"], run["work_buckets"], run["workflow"], run["case_field"])
        if frozen != (
            WORK_PARTITION_SCHEME,
            self._work_buckets,
            self._workflow,
            self._case_field,
        ):
            raise ExtractionError("work_partitioning_changed")

    def _check_bound(
        self, db: sqlite3.Connection, run: sqlite3.Row, manifest: SourceManifest
    ) -> None:
        """A resumed run is bound to its source scope and to the file bytes it first froze."""
        if run["source_scope"] != self._source_scope or run["tenant"] != self._tenant:
            raise ExtractionError("source_scope_changed")
        frozen = {
            r["partition_id"]: r["file_sha256"]
            for r in db.execute(
                "SELECT partition_id, file_sha256 FROM partitions WHERE run_id=?",
                (run["run_id"],),
            )
        }
        for ref in manifest.partitions:
            if frozen.get(ref.partition_id) != ref.file_sha256:
                raise ExtractionError("partition_mutated")

    def _bucket(self, run: sqlite3.Row, row: SourceRow) -> int | None:
        """The row's work bucket from the run's FROZEN scheme, never the opener's settings."""
        case = case_of(row, run["case_field"])
        if case is None:
            return None
        return work_bucket(self._tenant, run["workflow"], case[0], case[1], run["work_buckets"])

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
        integrity: ExtractionError | None = None
        for part in self._all(
            "SELECT partition_id FROM partitions WHERE run_id=? ORDER BY resource_id, partition_id",
            run_id,
        ):
            while pages < max_pages:
                try:
                    outcome, rows = self._advance(run_id, part["partition_id"])
                except ExtractionError as error:
                    if error.code not in INTEGRITY_FAILURES:
                        raise
                    # This file changed under the read. What it staged is already quarantined;
                    # every other partition still advances in this step before it is reported.
                    integrity = integrity or error
                    break
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
        if integrity is not None:
            raise integrity
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
            if error.code in INTEGRITY_FAILURES:
                # This file's bytes or identity changed under it: quarantine what it already
                # staged and record why, then let `step` carry on with the other partitions.
                self._quarantine_partition(run_id, partition_id, error.code)
                raise
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

    def _quarantine_partition(self, run_id: str, partition_id: str, reason: str) -> None:
        """Record why a partition's file failed and quarantine every row it staged, at once.

        Quarantined rows stay visible to the store (and counted in status) but are excluded
        from citation, normalization, joins and publication; a successor re-reads the file
        from its first row instead of trusting anything staged before the failure.

        KNOWN GAP, kept only while an existing test pins it: the partition stays `pending`
        (with the reason recorded) and `step` still raises once the other partitions have
        advanced, rather than marking it `failed` and returning normally. A pending partition
        refuses publication just as a failed one does.
        """
        with self._txn() as db:
            db.execute(
                "UPDATE partitions SET reason=? WHERE run_id=? AND partition_id=?"
                " AND state='pending'",
                (reason, run_id, partition_id),
            )
            db.execute(
                "UPDATE admissions SET state='quarantined' WHERE run_id=? AND partition_id=?"
                " AND origin='snapshot'",
                (run_id, partition_id),
            )

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
            self._check_scheme(run)
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
            terminal_empty = page.exhausted and not page.rows and page.start == cursor
            if end <= cursor and not terminal_empty:
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
            result = self._admit_row(db, run, part, row, offset, limits, allow_new=True)
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
        run: sqlite3.Row,
        part: sqlite3.Row,
        row: SourceRow,
        offset: int,
        limits: RunLimits,
        *,
        allow_new: bool,
    ) -> str:
        run_id = run["run_id"]
        resource = part["resource_id"]
        valid = (
            row.kind in KINDS
            and bool(row.native_id)
            and type(row.revision) is int
            and row.revision >= 1
            and bool(row.content_digest)
            and type(row.size_bytes) is int
            and row.size_bytes >= 0
            and _case_fields_ok(row)
        )
        if not valid:
            family = f"offset-{part['partition_id']}-{offset}"
            self._reject(db, run_id, family, max(offset, 0), "malformed_row", resource)
            return "rejected"
        family = family_id(self._tenant, resource, row.native_id)
        if row.size_bytes > limits.max_object_bytes:
            self._reject(db, run_id, family, row.revision, "object_over_limit", resource)
            return "rejected"
        bucket = self._bucket(run, row)
        return self._admit(
            db, run_id, part["partition_id"], resource, family, row, allow_new, bucket, "snapshot"
        )

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
        bucket: int | None,
        origin: str,
    ) -> str:
        digest = payload_digest(row)
        existing = db.execute(
            "SELECT payload_digest FROM admissions WHERE run_id=? AND family_id=? AND revision=?",
            (run_id, family, row.revision),
        ).fetchone()
        if existing is not None:
            if existing["payload_digest"] == digest:
                return "duplicate"
            # A quarantined row stays quarantined: a later contradiction cannot revive it.
            db.execute(
                "UPDATE admissions SET state='disputed' WHERE run_id=? AND family_id=?"
                " AND revision=? AND state!='quarantined'",
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
        seq = db.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM admissions WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        db.execute(
            "INSERT INTO admissions(run_id, family_id, revision, resource_id, partition_id, kind,"
            " native_id, path, content_digest, payload_digest, size_bytes, available_at,"
            " occurred_at, decision_key, authority, label, state, case_key, work_bucket, seq,"
            " origin, normalized) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'admitted',?,?,?,?,0)",
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
                row.case_key,
                bucket,
                seq,
                origin,
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
            self._check_scheme(run)
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
            rows_seen, input_bytes = run["rows_seen"], run["input_bytes"]
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
                size = max(entry.row.size_bytes, 0) if entry.row is not None else 0
                if not replay and input_bytes + size > limits.max_run_bytes:
                    limit = "run_limit_bytes"
                    break
                tally[self._apply_change(db, run, partitions, entry, replay, limits)] += 1
                rows_seen += 0 if replay else 1
                input_bytes += 0 if replay else size
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
                "UPDATE runs SET rows_seen=?, input_bytes=?,"
                " duplicate_deliveries=duplicate_deliveries+? WHERE run_id=?",
                (rows_seen, input_bytes, tally["duplicate"], run_id),
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
        run: sqlite3.Row,
        partitions: Mapping[str, sqlite3.Row],
        entry: ChangeEntry,
        replay: bool,
        limits: RunLimits,
    ) -> str:
        run_id = run["run_id"]
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
            if not replay:
                # The shared feed cursor still advances for other resources, so remember the
                # earliest entry that was NOT applied: a successor must re-read from there.
                db.execute(
                    "INSERT INTO change_holds VALUES (?,?,?,?) ON CONFLICT(run_id, feed,"
                    " resource_id) DO UPDATE SET feed_revision=MIN(feed_revision,"
                    " excluded.feed_revision)",
                    (run_id, entry.feed, entry.resource_id, entry.feed_revision),
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
            or not _case_fields_ok(row)
        ):
            self._reject(db, run_id, family, entry.revision, "malformed_change", entry.resource_id)
            return "rejected"
        bucket = self._bucket(run, row)
        return self._admit(
            db,
            run_id,
            part["partition_id"],
            entry.resource_id,
            family,
            row,
            not replay,
            bucket,
            "change",
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
            "work_partitioning": [run["work_scheme"], run["work_buckets"], run["workflow"]],
            "partitions": [
                [r["partition_id"], r["state"], r["cursor"], r["count"], r["digest"]]
                for r in self._all(
                    "SELECT * FROM partitions WHERE run_id=? ORDER BY partition_id", run_id
                )
            ],
            "admissions": [
                [
                    r["family_id"],
                    r["revision"],
                    r["payload_digest"],
                    r["state"],
                    r["resource_id"],
                    r["work_bucket"],
                ]
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
        quarantined = self._one(
            "SELECT COUNT(*) AS n FROM admissions WHERE run_id=? AND state='quarantined'", run_id
        )
        complete = run["state"] == "active" and all(p["state"] == "complete" for p in parts)
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
            "work_partitioning": {
                "scheme": run["work_scheme"],
                "buckets": run["work_buckets"],
                "workflow_registered": run["workflow"] is not None,
            },
            "case_field": run["case_field"],
            "quarantined_rows": quarantined["n"] if quarantined else 0,
            "work_partitions": self.work_partitions(run_id),
            "work_units": self.work_units(run_id),
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

    # ---- work partitions ---------------------------------------------------------------

    def work_partitions(self, run_id: str) -> list[dict[str, Any]]:
        """Admission counts per work partition: by source, then by stable hash bucket.

        A bucket of None is the source's unkeyed partition (rows with no case or decision
        key). Counts only: no case key, native ID or path reaches this view.
        """
        return [
            {"resource_id": r["resource_id"], "bucket": r["work_bucket"], "admissions": r["n"]}
            for r in self._all(
                "SELECT resource_id, work_bucket, COUNT(*) AS n FROM admissions WHERE run_id=?"
                " GROUP BY resource_id, work_bucket"
                " ORDER BY resource_id, work_bucket IS NOT NULL, work_bucket",
                run_id,
            )
        ]

    def work_partition_rows(
        self,
        run_id: str,
        resource_id: str,
        bucket: int | None,
        *,
        after: tuple[str, int] | None = None,
        limit: int = 250,
    ) -> list[dict[str, Any]]:
        """One bounded page of a work partition's admissions, keyset-paged by family/revision."""
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
            raise ExtractionError("invalid_limits")
        family, revision = after if after is not None else ("", 0)
        where = "a.work_bucket IS NULL" if bucket is None else "a.work_bucket=?"
        scope: list[object] = [run_id, resource_id] + ([] if bucket is None else [bucket])
        # Only sealed rows of a unit that has not failed: nothing from a partition still being
        # read, failed, withdrawn or quarantined is handed to downstream work.
        rows = self._all(
            "SELECT a.family_id, a.revision, a.kind, a.payload_digest, a.state, a.work_bucket"
            f" FROM admissions a WHERE a.run_id=? AND a.resource_id=? AND {where}"
            f" AND {_SEALED} AND {_UNIT_OK}"
            " AND (a.family_id > ? OR (a.family_id = ? AND a.revision > ?))"
            " ORDER BY a.family_id, a.revision LIMIT ?",
            *scope,
            family,
            family,
            revision,
            limit,
        )
        return [dict(r) for r in rows]

    # ---- work units: everything after staging, keyed by (source, bucket) -------------

    def normalize(self, run_id: str, *, max_pages: int = 1_000_000) -> WorkStep:
        """Advance every work unit's own cursor over its sealed staged rows, round-robin.

        Each page of one unit commits its normalized rows and its checkpoint (cursor, count,
        digest) atomically. A unit whose rows are not sealed yet waits; a unit whose
        normalizer fails is marked failed with that reason; neither stops any other unit.
        """
        run = self._run(run_id)
        self._check_scheme(run)
        limits = self.limits_of(run)
        with self._txn() as db:
            db.execute(
                "INSERT OR IGNORE INTO work_units SELECT DISTINCT run_id, resource_id,"
                " COALESCE(work_bucket, -1), 'pending', '', 0, 0, ?, 0, 0 FROM admissions"
                " WHERE run_id=?",
                (_ZERO, run_id),
            )
        pages = normalized = 0
        while pages < max_pages:
            moved = False
            for unit in self._all(
                "SELECT resource_id, bucket FROM work_units WHERE run_id=? AND state='pending'"
                " ORDER BY resource_id, bucket",
                run_id,
            ):
                if pages >= max_pages:
                    break
                done = self._normalize_page(run, unit["resource_id"], unit["bucket"], limits)
                if done > 0:
                    pages += 1
                    normalized += done
                    moved = True
            if not moved:
                break
        units = self.work_units(run_id)
        return WorkStep(
            pages,
            normalized,
            sum(1 for u in units if u["state"] == "failed"),
            sum(1 for u in units if u["state"] == "pending" and u["behind"]),
        )

    def _normalize_page(
        self, run: sqlite3.Row, resource_id: str, bucket: int, limits: RunLimits
    ) -> int:
        """One bounded page of one work unit; returns how many rows its cursor moved over."""
        run_id = run["run_id"]
        with self._txn() as db:
            unit = db.execute(
                "SELECT * FROM work_units WHERE run_id=? AND resource_id=? AND bucket=?",
                (run_id, resource_id, bucket),
            ).fetchone()
            if unit is None or unit["state"] != "pending":
                return 0
            rows = db.execute(
                "SELECT a.seq, a.family_id, a.revision, a.kind, a.payload_digest, a.state,"
                " a.origin, a.partition_id, (SELECT p.state FROM partitions p WHERE"
                " p.run_id=a.run_id AND p.partition_id=a.partition_id) AS part_state,"
                " (SELECT COUNT(*) FROM partitions p WHERE p.run_id=a.run_id AND"
                " p.resource_id=a.resource_id AND p.state='pending') AS open_parts,"
                " (SELECT COUNT(*) FROM partitions p WHERE p.run_id=a.run_id AND"
                " p.resource_id=a.resource_id AND p.state!='complete') AS unsealed"
                " FROM admissions a WHERE a.run_id=? AND a.resource_id=?"
                " AND COALESCE(a.work_bucket, -1)=? AND a.seq>? ORDER BY a.seq LIMIT ?",
                (run_id, resource_id, bucket, unit["cursor"], limits.page_size),
            ).fetchall()
            cursor, count, digest = unit["cursor"], unit["count"], unit["digest"]
            excluded, failures = unit["excluded"], unit["failures"]
            state, reason = "pending", ""
            done: list[int] = []
            moved = 0
            for r in rows:
                if r["origin"] == "change":
                    waiting, usable = r["open_parts"] > 0, r["unsealed"] == 0
                else:
                    waiting, usable = r["part_state"] == "pending", r["part_state"] == "complete"
                if waiting and r["state"] != "quarantined":
                    break  # its file is still being read: this unit waits, others go on
                if not usable or r["state"] == "quarantined":
                    excluded += 1
                    cursor = r["seq"]
                    moved += 1
                    continue
                event = StagedEvent(
                    resource_id,
                    None if bucket == UNKEYED else bucket,
                    r["family_id"],
                    r["revision"],
                    r["kind"],
                    r["payload_digest"],
                )
                try:
                    value = self._normalizer(event)
                    if not _text_ok(value):
                        raise ExtractionError("normalizer_output_invalid")
                except ExtractionError as error:
                    if error.code == NORMALIZER_UNAVAILABLE:
                        failures += 1
                        if failures > limits.max_page_retries:
                            state, reason = "failed", "retry_limit"
                    else:
                        state, reason = "failed", error.code
                    break
                digest = hashlib.sha256(
                    _canon([digest, r["family_id"], r["revision"], value]).encode()
                ).hexdigest()
                count += 1
                cursor = r["seq"]
                done.append(r["seq"])
                moved += 1
            if not moved and (state, failures) == ("pending", unit["failures"]):
                return 0  # nothing sealed to do yet: no checkpoint write
            for seq in done:
                db.execute(
                    "UPDATE admissions SET normalized=1 WHERE run_id=? AND seq=?", (run_id, seq)
                )
            db.execute(
                "UPDATE work_units SET state=?, reason=?, cursor=?, count=?, digest=?,"
                " excluded=?, failures=? WHERE run_id=? AND resource_id=? AND bucket=?",
                (
                    state,
                    reason,
                    cursor,
                    count,
                    digest,
                    excluded,
                    failures,
                    run_id,
                    resource_id,
                    bucket,
                ),
            )
            self._fault("normalize_before_commit")
        return moved

    def work_units(self, run_id: str) -> list[dict[str, Any]]:
        """Each work unit's own checkpoint and failure boundary. Counts and digests only."""
        out = []
        for u in self._all(
            "SELECT u.*, (SELECT COUNT(*) FROM admissions a WHERE a.run_id=u.run_id AND"
            " a.resource_id=u.resource_id AND COALESCE(a.work_bucket, -1)=u.bucket AND"
            " a.seq>u.cursor) AS behind FROM work_units u WHERE u.run_id=?"
            " ORDER BY u.resource_id, u.bucket",
            run_id,
        ):
            out.append(
                {
                    "resource_id": u["resource_id"],
                    "bucket": None if u["bucket"] == UNKEYED else u["bucket"],
                    "state": u["state"],
                    "reason": u["reason"],
                    "cursor": u["cursor"],
                    "count": u["count"],
                    "digest": u["digest"],
                    "excluded": u["excluded"],
                    "failures": u["failures"],
                    "behind": u["behind"],
                }
            )
        return out

    # ---- publication -----------------------------------------------------------------

    def publish(self, run_id: str) -> PublishResult:
        """Publish a paired snapshot only when every partition reconciles and is authorized.

        Work after staging is brought current first; a failed work unit, or one still behind,
        refuses publication just as a failed source partition does.
        """
        self.normalize(run_id)
        parts = self._all("SELECT * FROM partitions WHERE run_id=?", run_id)
        reasons: list[str] = []
        for unit in self.work_units(run_id):
            if unit["state"] == "failed":
                reasons.append(f"work_unit_failed:{unit['resource_id']}:{unit['bucket']}")
            elif unit["behind"]:
                reasons.append(f"work_unit_behind:{unit['resource_id']}:{unit['bucket']}")
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
            # Authority is re-read inside the commit scope, as for pages: a grant withdrawn
            # after the checks above publishes nothing.
            for p in parts:
                if self._grants.current(p["resource_id"]) != p["grant_revision"]:
                    return PublishResult(False, (f"partition_withdrawn:{p['partition_id']}",))
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
        self._check_scheme(prior)
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
                " capability, state, reason, predecessor, excluded_unauthorized, work_scheme,"
                " work_buckets, workflow, case_field, tenant, source_scope)"
                " VALUES (?,?,?,?,?,'active','',?,?,?,?,?,?,?,?)",
                (
                    new_id,
                    prior["manifest_revision"],
                    prior["mapping_revision"],
                    prior["limits"],
                    prior["capability"],
                    run_id,
                    excluded,
                    prior["work_scheme"],
                    prior["work_buckets"],
                    prior["workflow"],
                    prior["case_field"],
                    prior["tenant"],
                    prior["source_scope"],
                ),
            )
            for p in parts:
                if p["resource_id"] not in keep:
                    continue
                state = "pending" if p["state"] in ("withdrawn", "failed") else p["state"]
                # A partition that failed its integrity checks is re-read from its first row:
                # nothing staged from it before the failure is trusted or carried forward.
                reread = p["reason"] in INTEGRITY_FAILURES
                db.execute(
                    "INSERT INTO partitions(run_id, partition_id, resource_id, feed,"
                    " declared_digest, declared_count, grant_revision, state, reason, cursor,"
                    " count, digest, mapping_revision, failures, file_sha256)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
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
                        0 if reread else p["cursor"],
                        0 if reread else p["count"],
                        _ZERO if reread else p["digest"],
                        p["mapping_revision"],
                        p["file_sha256"],
                    ),
                )
            for table in ("admissions", "renames", "tombstones", "rejections"):
                for r in db.execute(f"SELECT * FROM {table} WHERE run_id=?", (run_id,)).fetchall():
                    if r["resource_id"] not in keep:
                        continue
                    record = dict(r)
                    record["run_id"] = new_id
                    if table == "admissions":
                        if record["state"] == "quarantined":
                            continue
                        record["normalized"] = 0  # the successor's work units start afresh
                    names = ",".join(record)
                    marks = ",".join("?" * len(record))
                    db.execute(
                        f"INSERT OR IGNORE INTO {table}({names}) VALUES ({marks})",
                        tuple(record.values()),
                    )
            holds = {
                (h["feed"], h["resource_id"]): h["feed_revision"]
                for h in db.execute("SELECT * FROM change_holds WHERE run_id=?", (run_id,))
            }
            for f in db.execute("SELECT * FROM feeds WHERE run_id=?", (run_id,)).fetchall():
                # Re-read from just before the earliest unapplied entry of any resource that
                # is authorized again; re-applying the rest is idempotent, never a skip.
                owed = [
                    rev for (feed, res), rev in holds.items() if feed == f["feed"] and res in keep
                ]
                cursor = min([f["cursor"], *(rev - 1 for rev in owed)])
                db.execute(
                    "INSERT INTO feeds VALUES (?,?,?,?)",
                    (new_id, f["feed"], f["cut"], max(cursor, f["cut"])),
                )
        return new_id

    # ---- as-of features and outcome reconciliation ------------------------------------

    def feature_citation(
        self, run_id: str, *, resource_id: str, native_id: str, decision_time: int
    ) -> FeatureCitation:
        """The newest revision the model could already see at decision time, else an exclusion."""
        family = family_id(self._tenant, resource_id, native_id)
        staged = self._all(
            f"SELECT a.revision, a.content_digest, a.available_at, a.state, {_SEALED} AS sealed"
            " FROM admissions a WHERE a.run_id=? AND a.family_id=? ORDER BY a.revision DESC",
            run_id,
            family,
        )
        # Only sealed, admitted rows can be cited: never a row staged from a partition that
        # failed (quarantined) or one whose partition is still being read.
        rows = [r for r in staged if r["state"] == "admitted" and r["sealed"]]
        if not rows:
            if any(r["state"] == "quarantined" for r in staged):
                return FeatureCitation("excluded_quarantined")
            if any(r["state"] == "admitted" for r in staged):
                return FeatureCitation("excluded_not_sealed")
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
        """Distinct decisions into pending/disputed/verified; unmatched outcomes separately.

        The join is a unit of work after staging, so it runs per bucket over normalized rows:
        a decision joins only outcomes in its own bucket. A bucket with a failed work unit is
        held: its decisions are still counted (as pending and as held), never joined, and
        every other bucket is joined as usual.
        """
        self.normalize(run_id)
        live = [f for f in self.families(run_id) if f["state"] in ("live", "disputed")]
        ids = {f["family_id"] for f in live}
        rows = self._all(
            "SELECT a.*, u.state AS unit_state FROM admissions a JOIN work_units u ON"
            " u.run_id=a.run_id AND u.resource_id=a.resource_id AND"
            " u.bucket=COALESCE(a.work_bucket, -1) WHERE a.run_id=? AND"
            " a.state IN ('admitted','disputed') AND"
            " a.revision=(SELECT MAX(revision) FROM admissions b WHERE b.run_id=a.run_id"
            f" AND b.family_id=a.family_id) AND {_SEALED}",
            run_id,
        )
        rows = [r for r in rows if r["family_id"] in ids]
        held = {r["work_bucket"] for r in rows if r["unit_state"] == "failed"}
        total = Reconciliation(0, 0, 0, 0, 0, 0)
        for bucket in sorted({r["work_bucket"] for r in rows}, key=lambda b: (b is not None, b)):
            group = [r for r in rows if r["work_bucket"] == bucket]
            if bucket in held:
                keys = {
                    r["decision_key"]
                    for r in group
                    if r["kind"] == "decision" and r["decision_key"]
                }
                part = Reconciliation(len(keys), 0, len(keys), 0, 0, 0, len(keys))
            else:
                part = self._join([r for r in group if r["normalized"]])
            total = Reconciliation(
                *(a + b for a, b in zip(asdict(total).values(), asdict(part).values(), strict=True))
            )
        return total

    @staticmethod
    def _join(rows: list[sqlite3.Row]) -> Reconciliation:
        """One bucket's outcome join over its normalized decision and outcome rows."""
        decisions = {
            r["decision_key"] for r in rows if r["kind"] == "decision" and r["decision_key"]
        }
        finals: dict[str, set[str]] = {}
        # A contradictory row never leaves the count: its decision stays an input decision and
        # is reported disputed, because nothing about it can be trusted as verified.
        contested = {r["decision_key"] for r in rows if r["state"] == "disputed"} & decisions
        unmatched = non_authoritative = 0
        for r in rows:
            if r["kind"] != "outcome" or r["state"] == "disputed":
                continue
            if r["decision_key"] not in decisions:
                unmatched += 1
            elif r["authority"] != "independent" or not r["label"]:
                non_authoritative += 1
            else:
                finals.setdefault(r["decision_key"], set()).add(r["label"])
        conflicting = {key for key, labels in finals.items() if len(labels) > 1} | contested
        disputed = len(conflicting)
        verified = sum(1 for key, labels in finals.items() if len(labels) == 1) - len(
            {key for key, labels in finals.items() if len(labels) == 1} & contested
        )
        return Reconciliation(
            len(decisions),
            verified,
            len(decisions) - verified - disputed,
            disputed,
            unmatched,
            non_authoritative,
        )
