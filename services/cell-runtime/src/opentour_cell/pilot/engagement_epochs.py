"""Owner-local engagement-epoch ledger for withdrawn organizations that return (OPE-543).

One organization identifier may hold several engagements over time, each under its own
epoch, lease, budget, approvals and effect counters. Re-entry never reopens an earlier
epoch: it needs fresh authority, a durably recorded trusted source high-water checkpoint,
and a persisted terminal state (with revocation epoch) for every earlier epoch.

The withdrawal fence is also kept in an append-only, hash-chained journal outside the
SQLite file, so restoring an older database backup re-applies every fence before work
restarts. Unknown or unreliable lineage is a hold, never a guess. Fixtures are generated;
this ledger makes no cross-CRM deduplication or counting claim (OPE-452/455 own that).

Anti-rollback records (OPE-543). The ledger, its journal and every grant live in the
snapshotted worker volume (`/var/lib/opentour-worker`), so restoring an older snapshot could
bring back an engagement that was withdrawn after the snapshot, and deleting the ledger left
nothing that showed one had existed. Two kinds of record are therefore kept outside the
snapshot, in the anti-rollback location (`ANTI_ROLLBACK_DIRECTORY`, the worker-host state
directory `/var/lib/opentour-antirollback`; `OPENTOUR_ANTI_ROLLBACK_DIRECTORY` overrides it):

* the ledger marker, written atomically at sequence 1 when the directory's first grant is
  ingested (`record_engagement` on a ledger with no history) and before that ingest commits,
  then advanced to sequence 2 once the ingest is durable. It binds the store identity. The
  real binding is the random store id kept in the ledger database; the directory's real path
  is the key, and its device and inode are also checked, but a filesystem may reuse an inode
  for a recreated directory, so they are not relied on alone. A marker with no ledger, or one
  that does not match the ledger beside it, is a hold;
* per organization, the highest revocation epoch and the engagement epoch it terminated, as a
  forward-only record. It is written under the record lock after the fence journal entry and
  before the database fence commits; a slot is released only once it is durable. A volume
  whose journal is behind the record is a hold, at startup and before any live write.

Both use the `opentour.pilot.trust-state.v1` record format of OPE-566's `TrustStateStore`
(one canonical JSON file per key under `records/`, named by the key's SHA-256), with their
own namespaces, so no format change is needed. They are written here with the standard
library only, which keeps the packaged worker's import closure unchanged. Writers take the
lock file `pilot-trust-state.lock` in the location. They serialize with `TrustStateStore`
only when both use the same directory: at the default location they do, since both modules
default to `/var/lib/opentour-antirollback`. `TrustStateStore` has no override, so under
`OPENTOUR_ANTI_ROLLBACK_DIRECTORY` these records are kept apart from its records and lock.

The location must be on a different filesystem device from the ledger directory
(`anti_rollback_same_volume` otherwise), because one snapshot or backup of a volume carries
everything on it, and restoring it would roll these records back with the ledger. On the
worker host the volume is its own mount, so the default location qualifies. A run elsewhere
must point the override at a separate volume that no snapshot or backup of the ledger's
volume includes. If there is none, the run fails closed.

What these records protect against, and what they do not. They defend against a snapshot
restore or an accidental rollback of `/var/lib/opentour-worker`: they are worker-attested at
most, labelled `worker_attested` and `snapshot_restore_only`. They are not tamper-proof. Both
worker units run as the same uid, which can ptrace the other, rewrite the shared volume and
wipe or rewrite the anti-rollback directory; such a process can undo all of this.
Defending against a hostile same-uid process needs a separate uid for the anti-rollback
writer or an external anchor (OPE-566/OPE-583), which this module does not provide.

Fail-closed rules:
* an unusable location (unset, missing, a link, not a private directory owned by this uid,
  inside the ledger directory, or on the ledger's filesystem device) holds every ledger
  operation and refuses the first-grant ingest before any state changes;
* a marker that cannot be parsed exactly holds (`anti_rollback_marker_corrupt`);
* a marker with the ledger database missing holds and creates nothing;
* a ledger with history but no marker holds (`anti_rollback_marker_missing`). The marker is
  never re-derived from the volume it protects: it is written before the first ingest
  commits, so no crash produces this state; it means the anti-rollback state was lost or
  reset, and re-baselining it needs a supervised decision this module does not make;
* a sequence-1 marker with an empty ledger of the same store is the crash window between
  the marker write and the first commit, and the ingest may proceed. A sequence-2 marker
  with an empty ledger is a restore from before the first grant and holds
  (`anti_rollback_ledger_rewound`). A sequence-1 marker beside a ledger with history is
  promoted to 2 by the next ledger operation, or that operation holds.
Times recorded are the worker's own clock, never a caller's.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.training import _check_directory, _check_file

SCHEMA = "opentour.pilot.engagement-epochs.v1"
# Worker-host layout (OPE-577): a StateDirectory owned by the worker uid, mode 0700, outside
# the snapshot volume. The environment variable exists for tests and non-host runs.
ANTI_ROLLBACK_DIRECTORY: Path | None = Path("/var/lib/opentour-antirollback")
ANTI_ROLLBACK_ENV = "OPENTOUR_ANTI_ROLLBACK_DIRECTORY"
ANTI_ROLLBACK_ASSURANCE = "worker_attested"
ANTI_ROLLBACK_SCOPE = "snapshot_restore_only"
TRUST_STATE_FORMAT = "opentour.pilot.trust-state.v1"
MARKER_NAMESPACE = "engagement_ledger_marker"
REVOCATION_NAMESPACE = "engagement_revocation"
_RECORDS = "records"
_RECORD_LOCK = "pilot-trust-state.lock"  # shared with OPE-566's TrustStateStore writers
_MAX_RECORD_BYTES = 1048576
_RECORD_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}(?::[^\x00-\x1f]{1,512})?")
_SEQUENCE = re.compile(r"[1-9][0-9]{0,18}")
_STORE_ID = re.compile(r"[0-9a-f]{32}")
_DIGITS = re.compile(r"[0-9]{1,20}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
_MARKER_FIELDS = frozenset(
    {"kind", "store_id", "directory", "device", "inode", "written_at", "assurance"}
    | {"protects_against"}
)
_REVOCATION_FIELDS = frozenset(
    {"kind", "store_id", "org", "epoch", "revocation_epoch", "recorded_at", "assurance"}
    | {"protects_against"}
)
_MARKER_CORRUPT = "anti_rollback_marker_corrupt"
# Marker sequence: 1 is written before the first ingest commits (the crash window); 2 once
# that ingest is durable. An empty ledger beside a sequence-2 marker is a rewound volume.
_MARKER_WRITTEN = 1
_MARKER_COMMITTED = 2
_RECORD_CORRUPT = "anti_rollback_record_corrupt"
BINDING_KINDS = ("queue", "session", "grant", "callback", "receipt", "storage")
EFFECT_KINDS = ("decision", "read", "fit", "export")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_MAX = 2**63 - 1
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS engagement (
 org TEXT NOT NULL, epoch TEXT NOT NULL, ordinal INTEGER NOT NULL, state TEXT NOT NULL,
 lease_ref TEXT NOT NULL, authority_digest TEXT NOT NULL, lineage_ref TEXT NOT NULL,
 resource TEXT NOT NULL, checkpoint INTEGER NOT NULL, observed_high INTEGER NOT NULL,
 budget_limit INTEGER NOT NULL, budget_spent INTEGER NOT NULL DEFAULT 0,
 label TEXT NOT NULL DEFAULT '', revocation_epoch INTEGER, terminal_kind TEXT,
 slot_released INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY (org, epoch), UNIQUE (org, ordinal), UNIQUE (lease_ref), UNIQUE (authority_digest)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_slot ON engagement (org) WHERE slot_released = 0;
CREATE TABLE IF NOT EXISTS binding (
 org TEXT NOT NULL, epoch TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL,
 PRIMARY KEY (org, kind, ref)
);
CREATE TABLE IF NOT EXISTS approval (
 org TEXT NOT NULL, epoch TEXT NOT NULL, ref TEXT NOT NULL, PRIMARY KEY (org, epoch, ref)
);
CREATE TABLE IF NOT EXISTS effect (
 org TEXT NOT NULL, epoch TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,
 PRIMARY KEY (org, epoch, kind, key)
);
CREATE TABLE IF NOT EXISTS budget_reservation (
 org TEXT NOT NULL, epoch TEXT NOT NULL, key TEXT NOT NULL, amount INTEGER NOT NULL,
 PRIMARY KEY (org, epoch, key)
);
CREATE TABLE IF NOT EXISTS fence (
 org TEXT NOT NULL, epoch TEXT NOT NULL, revocation_epoch INTEGER NOT NULL,
 PRIMARY KEY (org, epoch)
);
CREATE TABLE IF NOT EXISTS ledger_store (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), store_id TEXT NOT NULL,
 created_at INTEGER NOT NULL
);
"""
# Defence in depth against code bugs, not a security boundary: the worker or custodian uid
# that owns these 0700/0600 files can drop the triggers or rewrite both files. Within that
# limit no write path, including a direct SQL insert, update or REPLACE, can mark a live slot
# released unless the same row is terminal and its revocation epoch is fenced, and a fence
# row is never deleted or changed. The authority for a termination is the fsynced journal
# entry (worker-attested; it still has no external anchor): `record_engagement` admits a
# successor and `release_slot` frees a slot only when the journal carries the same epoch.
_RELEASE_REQUIRES_FENCE = (
    "NEW.slot_released != 0 AND (NEW.state != 'terminal' OR NEW.revocation_epoch IS NULL "
    "OR NOT EXISTS (SELECT 1 FROM fence WHERE fence.org = NEW.org AND fence.epoch = NEW.epoch "
    "AND fence.revocation_epoch = NEW.revocation_epoch))"
)
_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS slot_release_requires_fence_insert BEFORE INSERT ON engagement "
    f"WHEN {_RELEASE_REQUIRES_FENCE} "
    "BEGIN SELECT RAISE(ABORT, 'terminal_state_not_persisted'); END",
    "CREATE TRIGGER IF NOT EXISTS slot_release_requires_fence_update BEFORE UPDATE ON engagement "
    f"WHEN {_RELEASE_REQUIRES_FENCE} "
    "BEGIN SELECT RAISE(ABORT, 'terminal_state_not_persisted'); END",
    # REPLACE deletes without firing delete triggers (recursive_triggers is off), so refuse
    # a differing row for an existing key before the insert happens.
    "CREATE TRIGGER IF NOT EXISTS fence_insert_is_immutable BEFORE INSERT ON fence "
    "WHEN EXISTS (SELECT 1 FROM fence WHERE fence.org = NEW.org AND fence.epoch = NEW.epoch "
    "AND fence.revocation_epoch IS NOT NEW.revocation_epoch) "
    "BEGIN SELECT RAISE(ABORT, 'fence_is_permanent'); END",
    "CREATE TRIGGER IF NOT EXISTS fence_is_permanent BEFORE DELETE ON fence "
    "BEGIN SELECT RAISE(ABORT, 'fence_is_permanent'); END",
    "CREATE TRIGGER IF NOT EXISTS fence_is_immutable BEFORE UPDATE ON fence "
    "BEGIN SELECT RAISE(ABORT, 'fence_is_permanent'); END",
)


class EngagementError(ValueError):
    """A fixed local diagnostic; never contains customer content."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class EngagementHold(EngagementError):
    """Reliable lineage, checkpoint or fence state is missing; do no dependent work."""


def _ref(value: object, reason: str = "invalid_reference") -> str:
    if not isinstance(value, str) or _REF.fullmatch(value) is None:
        raise EngagementError(reason)
    return value


def _int(value: object, reason: str, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= _MAX:
        raise EngagementError(reason)
    return value


def _digest(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()


# -- anti-rollback records (worker-attested; snapshot_restore_only; see module docstring) ----


def _canonical(value: object) -> bytes:
    """RFC 8785 bytes for the string-valued records kept here (objects and strings only)."""
    try:
        text = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        return text.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise EngagementHold("anti_rollback_record_invalid") from None


def _worker_time() -> str:
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _private_directory(path: Path) -> bool:
    """A real directory (never a link) owned by this uid, not writable by group or others."""
    try:
        status = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISDIR(status.st_mode)
        and status.st_uid == os.geteuid()
        and not status.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    )


def marker_key(directory: Path) -> str:
    """The marker's key: bound to the ledger directory's real path."""
    return f"{MARKER_NAMESPACE}:{os.path.realpath(directory)}"


def revocation_key(store_id: str, org: str) -> str:
    """The revocation record's key: one per ledger store and organization."""
    return f"{REVOCATION_NAMESPACE}:{store_id}:{org}"


def record_path(location: Path, key: str) -> Path:
    if _RECORD_KEY.fullmatch(key) is None:
        raise EngagementHold("anti_rollback_key_invalid")
    try:
        name = hashlib.sha256(key.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        raise EngagementHold("anti_rollback_key_invalid") from None
    return location / _RECORDS / (name + ".json")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate key")
    return result


def _not_a_string(_value: str) -> Any:
    raise ValueError("only strings are recorded")


def _write_record(records: Path, name: str, body: bytes) -> None:
    """Atomic: temporary file, fsync, rename, directory fsync. Leaves no partial record."""
    temporary = records / (name + ".tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            view = memoryview(body)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, records / name)
        directory = os.open(records, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise EngagementHold("anti_rollback_unavailable") from None


def _device_of(path: str) -> int:
    """The filesystem device holding `path`. Private; no flag or caller can replace it."""
    return os.stat(path).st_dev


class _AntiRollback:
    """Records kept outside the worker snapshot. Worker-attested at most; not tamper-proof."""

    def __init__(self, ledger_directory: Path):
        configured = os.environ.get(ANTI_ROLLBACK_ENV)
        location = Path(configured) if configured else ANTI_ROLLBACK_DIRECTORY
        if location is None or not location.is_absolute() or not _private_directory(location):
            raise EngagementHold("anti_rollback_unavailable")
        real, ledger = os.path.realpath(location), os.path.realpath(ledger_directory)
        if os.path.commonpath([real, ledger]) in {real, ledger}:
            # Inside the ledger directory (or containing it), a snapshot would carry it along.
            raise EngagementHold("anti_rollback_inside_snapshot")
        try:
            shared = _device_of(real) == _device_of(ledger)
        except OSError:
            raise EngagementHold("anti_rollback_unavailable") from None
        if shared:
            # On the ledger's own filesystem, one volume snapshot or backup can carry both,
            # and restoring it would roll the records back with the ledger.
            raise EngagementHold("anti_rollback_same_volume")
        self.location = location

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Exclusive writer lock, acquired before any state is written."""
        try:
            fd = os.open(
                self.location / _RECORD_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
            )
        except OSError:
            raise EngagementHold("anti_rollback_unavailable") from None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                raise EngagementHold("anti_rollback_unavailable") from None
            yield
        finally:
            os.close(fd)

    def _records(self, *, create: bool) -> Path | None:
        records = self.location / _RECORDS
        if create:
            try:
                records.mkdir(mode=0o700)
            except FileExistsError:
                pass
            except OSError:
                raise EngagementHold("anti_rollback_unavailable") from None
        elif not os.path.lexists(records):
            return None
        if not _private_directory(records):
            raise EngagementHold("anti_rollback_unavailable")
        return records

    def read(self, key: str, corrupt: str) -> tuple[int, dict[str, str]] | None:
        """The record's sequence and value, None when absent, or a hold when not exact."""
        path = record_path(self.location, key)
        if self._records(create=False) is None:
            return None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError:
            raise EngagementHold(corrupt) from None
        try:
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise EngagementHold(corrupt)
                data = handle.read(_MAX_RECORD_BYTES + 1)
        except OSError:
            raise EngagementHold(corrupt) from None
        try:
            state = json.loads(
                data.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_int=_not_a_string,
                parse_float=_not_a_string,
                parse_constant=_not_a_string,
            )
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise EngagementHold(corrupt) from None
        if (
            len(data) > _MAX_RECORD_BYTES
            or not isinstance(state, dict)
            or set(state) != {"format", "key", "sequence", "value"}
            or state["format"] != TRUST_STATE_FORMAT
            or state["key"] != key
            or not isinstance(state["sequence"], str)
            or _SEQUENCE.fullmatch(state["sequence"]) is None
            or not isinstance(state["value"], dict)
            or not all(isinstance(item, str) for item in state["value"].values())
        ):
            raise EngagementHold(corrupt)
        try:
            exact = _canonical(state) == data
        except EngagementHold:
            exact = False
        if not exact:
            raise EngagementHold(corrupt)
        return int(state["sequence"]), state["value"]

    def write(self, key: str, sequence: int, value: dict[str, str]) -> None:
        """Requires `locked`. Atomic replacement in the trust-state record format."""
        path = record_path(self.location, key)
        body = _canonical(
            {"format": TRUST_STATE_FORMAT, "key": key, "sequence": str(sequence), "value": value}
        )
        records = self._records(create=True)
        assert records is not None
        _write_record(records, path.name, body)


def _identity(directory: Path) -> tuple[str, str, str]:
    real = os.path.realpath(directory)
    try:
        status = os.stat(real)
    except OSError:
        raise EngagementHold("anti_rollback_store_mismatch") from None
    return real, str(status.st_dev), str(status.st_ino)


def _marker(value: dict[str, str]) -> dict[str, str]:
    if (
        set(value) != _MARKER_FIELDS
        or value["kind"] != MARKER_NAMESPACE
        or value["assurance"] != ANTI_ROLLBACK_ASSURANCE
        or value["protects_against"] != ANTI_ROLLBACK_SCOPE
        or _STORE_ID.fullmatch(value["store_id"]) is None
        or _DIGITS.fullmatch(value["device"]) is None
        or _DIGITS.fullmatch(value["inode"]) is None
        or _TIMESTAMP.fullmatch(value["written_at"]) is None
    ):
        raise EngagementHold(_MARKER_CORRUPT)
    return value


def _bound(value: dict[str, str]) -> tuple[str, str, str, str]:
    """What a valid marker binds: real path, device, inode and the ledger's store id."""
    marker = _marker(value)
    return marker["directory"], marker["device"], marker["inode"], marker["store_id"]


def _revocation(record: tuple[int, dict[str, str]], store_id: str, org: str) -> dict[str, str]:
    sequence, value = record
    if (
        set(value) != _REVOCATION_FIELDS
        or value["kind"] != REVOCATION_NAMESPACE
        or value["assurance"] != ANTI_ROLLBACK_ASSURANCE
        or value["protects_against"] != ANTI_ROLLBACK_SCOPE
        or value["store_id"] != store_id
        or value["org"] != org
        or value["revocation_epoch"] != str(sequence)
        or _REF.fullmatch(value["epoch"]) is None
        or _TIMESTAMP.fullmatch(value["recorded_at"]) is None
    ):
        raise EngagementHold(_RECORD_CORRUPT)
    return value


def ledger_marker_present(directory: Path) -> bool:
    """Whether a ledger must exist in `directory`, as the anti-rollback marker shows.

    Fails closed: True when a marker exists, or when the location or the marker cannot be
    read. Worker-attested at most (see the module docstring); it shows that a first grant
    was ingested here, not which engagements the ledger held.
    """
    try:
        return _AntiRollback(directory).read(marker_key(directory), _MARKER_CORRUPT) is not None
    except EngagementError:
        return True


@dataclass(frozen=True)
class Engagement:
    org: str
    epoch: str
    ordinal: int
    state: str
    lease_ref: str
    resources: tuple[str, ...]
    checkpoint: int
    observed_high: int
    budget_limit: int
    budget_spent: int
    revocation_epoch: int | None
    terminal_kind: str | None
    slot_released: bool
    label: str


@dataclass(frozen=True)
class Admission:
    """Verdict for one source row: accepted, denied (excluded), or held."""

    verdict: str
    reason: str
    checkpoint: int | None = None


class EngagementLedger:
    """SQLite ledger plus an out-of-database withdrawal-fence journal."""

    def __init__(self, directory: Path, *, existing_only: bool = False):
        if not directory.is_absolute():
            raise EngagementError("absolute_private_directory_required")
        self.path = directory / "engagement-epochs.sqlite3"
        self.journal = directory / "engagement-fences.jsonl"
        self.guard_path = directory / "engagement-epochs.lock"
        self.directory = directory
        try:
            _check_directory(self.path)
        except (TrainingError, OSError):
            raise EngagementError("private_store_required") from None
        if not existing_only and not os.path.lexists(self.path):
            # A marker shows this directory's ledger existed: never recreate it empty.
            if _AntiRollback(directory).read(marker_key(directory), _MARKER_CORRUPT) is not None:
                raise EngagementHold("anti_rollback_ledger_missing")
        try:
            if not existing_only and not self.path.exists():
                os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600))
            _check_file(self.path)
        except (TrainingError, OSError):
            raise EngagementError("private_store_required") from None
        if not existing_only:
            self._create_schema()
            self.reconcile_fences()

    # -- storage ---------------------------------------------------------------------

    @contextmanager
    def pin(self, *, exclusive: bool = False) -> Iterator[None]:
        """Order a live operation's commit against withdrawal and database restore."""
        fd = os.open(self.guard_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            _check_file(self.guard_path)
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            os.close(fd)

    @contextmanager
    def _db(self, org: str | None = None, *, verify: bool = True) -> Iterator[sqlite3.Connection]:
        """One immediate transaction, verified against the anti-rollback records first."""
        try:
            _check_file(self.path)
        except (TrainingError, OSError):
            raise EngagementError("private_store_required") from None
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("BEGIN IMMEDIATE")
            try:
                if verify:
                    self._verify_anti_rollback(db, org)
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    def _create_schema(self) -> None:
        with self._db(verify=False) as db:
            for statement in _SCHEMA_SQL.split(";"):
                if statement.strip():
                    db.execute(statement)
            for trigger in _TRIGGERS:
                db.execute(trigger)
            db.execute(
                "INSERT OR IGNORE INTO ledger_store VALUES (1,?,?)",
                (secrets.token_hex(16), int(time.time())),
            )
            # Inside the same transaction: a refused open leaves the schema untouched.
            self._verify_anti_rollback(db)

    def _history(self, db: sqlite3.Connection) -> bool:
        """Whether this volume shows any ingest: an engagement, a fence or a journal entry."""
        tables = self._tables(db)
        return bool(self._journal_entries()) or any(
            db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
            for table in ("engagement", "fence")
            if table in tables
        )

    @staticmethod
    def _tables(db: sqlite3.Connection) -> set[str]:
        return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def _store_id(self, db: sqlite3.Connection) -> str | None:
        if "ledger_store" not in self._tables(db):
            return None
        row = db.execute("SELECT store_id FROM ledger_store WHERE singleton=1").fetchone()
        return None if row is None else str(row[0])

    def _verify_anti_rollback(self, db: sqlite3.Connection, org: str | None = None) -> None:
        """Hold unless this volume matches the records kept outside its snapshot.

        Runs at startup and before every ledger operation, live writes included. The marker
        must match this store when the ledger has history; every organization's journal must
        reach its recorded highest revocation epoch, naming the same engagement epoch.
        """
        entries = self._journal_entries()
        tables = self._tables(db)
        orgs = {entry["org"] for entry in entries}
        for table in ("engagement", "fence"):
            if table in tables:
                orgs |= {r[0] for r in db.execute(f"SELECT DISTINCT org FROM {table}")}
        if isinstance(org, str) and _REF.fullmatch(org) is not None:
            orgs.add(org)
        store = _AntiRollback(self.directory)
        marker = store.read(marker_key(self.directory), _MARKER_CORRUPT)
        if marker is None:
            if self._history(db):
                raise EngagementHold("anti_rollback_marker_missing")
            return
        if marker[0] not in {_MARKER_WRITTEN, _MARKER_COMMITTED}:
            raise EngagementHold(_MARKER_CORRUPT)
        store_id = self._store_id(db)
        if store_id is None or _bound(marker[1]) != (*_identity(self.directory), store_id):
            raise EngagementHold("anti_rollback_store_mismatch")
        if not self._history(db):
            if marker[0] == _MARKER_COMMITTED:
                # The first ingest committed, yet the ledger is empty: a restored snapshot or
                # backup from before the first grant. Only sequence 1 is the crash window.
                raise EngagementHold("anti_rollback_ledger_rewound")
        elif marker[0] == _MARKER_WRITTEN and not self._promote_marker(store, store_id):
            # History proves the first ingest is durable; its promotion could not be written.
            raise EngagementHold("anti_rollback_unavailable")
        journaled: dict[str, dict[int, str]] = {}
        for entry in entries:
            journaled.setdefault(entry["org"], {})[entry["revocation_epoch"]] = entry["epoch"]
        for name in sorted(orgs):
            record = store.read(revocation_key(store_id, name), _RECORD_CORRUPT)
            if record is None:
                continue
            recorded = _revocation(record, store_id, name)
            seen = journaled.get(name, {})
            if max(seen, default=0) < record[0]:
                raise EngagementHold("anti_rollback_volume_behind")
            if seen.get(record[0]) != recorded["epoch"]:
                raise EngagementHold("anti_rollback_revocation_conflict")

    def _mark_first_grant(self, db: sqlite3.Connection) -> None:
        """Write the marker durably before the directory's first ingest commits.

        Only reached once `_verify_anti_rollback` found no history in this transaction, so
        this is the first grant. Any failure refuses the ingest; the transaction rolls back.
        """
        store_id = self._store_id(db)
        if store_id is None:
            raise EngagementHold("anti_rollback_baseline_missing")
        store = _AntiRollback(self.directory)
        key = marker_key(self.directory)
        with store.locked():
            current = store.read(key, _MARKER_CORRUPT)
            directory, device, inode = _identity(self.directory)
            if current is not None:
                # The crash window between a marker write and the first commit: same store.
                if current[0] != _MARKER_WRITTEN or _bound(current[1]) != (
                    directory,
                    device,
                    inode,
                    store_id,
                ):
                    raise EngagementHold("anti_rollback_store_mismatch")
                return
            store.write(
                key,
                _MARKER_WRITTEN,
                {
                    "kind": MARKER_NAMESPACE,
                    "store_id": store_id,
                    "directory": directory,
                    "device": device,
                    "inode": inode,
                    "written_at": _worker_time(),
                    "assurance": ANTI_ROLLBACK_ASSURANCE,
                    "protects_against": ANTI_ROLLBACK_SCOPE,
                },
            )

    def _promote_marker(self, store: _AntiRollback, store_id: str) -> bool:
        """Advance the marker to sequence 2 once the first ingest is durable in the ledger.

        Same value, higher sequence (forward only). False when it cannot be written: the
        verifier then holds, and `record_engagement` leaves it to the next ledger operation.
        """
        key = marker_key(self.directory)
        try:
            with store.locked():
                current = store.read(key, _MARKER_CORRUPT)
                if current is None or _bound(current[1]) != (*_identity(self.directory), store_id):
                    return False
                if current[0] == _MARKER_WRITTEN:
                    store.write(key, _MARKER_COMMITTED, current[1])
                return True
        except EngagementHold:
            return False

    def _record_revocation(
        self, store: _AntiRollback, store_id: str, org: str, epoch: str, revocation: int
    ) -> None:
        """Requires `store.locked()`. Forward only; equal epoch must name the same epoch."""
        key = revocation_key(store_id, org)
        current = store.read(key, _RECORD_CORRUPT)
        if current is not None:
            recorded = _revocation(current, store_id, org)
            if current[0] > revocation:
                return
            if current[0] == revocation:
                if recorded["epoch"] != epoch:
                    raise EngagementHold("anti_rollback_revocation_conflict")
                return
        store.write(
            key,
            revocation,
            {
                "kind": REVOCATION_NAMESPACE,
                "store_id": store_id,
                "org": org,
                "epoch": epoch,
                "revocation_epoch": str(revocation),
                "recorded_at": _worker_time(),
                "assurance": ANTI_ROLLBACK_ASSURANCE,
                "protects_against": ANTI_ROLLBACK_SCOPE,
            },
        )

    def _revocation_durable(
        self, db: sqlite3.Connection, org: str, epoch: str, revocation: int
    ) -> bool:
        store_id = self._store_id(db)
        if store_id is None:
            return False
        record = _AntiRollback(self.directory).read(revocation_key(store_id, org), _RECORD_CORRUPT)
        if record is None:
            return False
        recorded = _revocation(record, store_id, org)
        return record[0] > revocation or (record[0] == revocation and recorded["epoch"] == epoch)

    def _journal_entries(self) -> list[dict[str, Any]]:
        if not self.journal.exists():
            return []
        try:
            _check_file(self.journal)
            lines = self.journal.read_text(encoding="utf-8").splitlines()
        except (TrainingError, OSError, UnicodeDecodeError):
            raise EngagementHold("fence_journal_unreadable") from None
        entries, previous = [], "genesis"
        for line in lines:
            try:
                entry = json.loads(line)
                body = {k: entry[k] for k in ("org", "epoch", "revocation_epoch", "previous")}
                ok = (
                    set(entry) == {*body, "digest"}
                    and entry["previous"] == previous
                    and entry["digest"] == _digest(body)
                )
            except (ValueError, KeyError, TypeError):
                ok = False
            if not ok:
                raise EngagementHold("fence_journal_broken")
            entries.append(entry)
            previous = entry["digest"]
        return entries

    def _append_fence(self, org: str, epoch: str, revocation_epoch: int) -> None:
        entries = self._journal_entries()
        for entry in entries:
            if (entry["org"], entry["epoch"]) == (org, epoch):
                if entry["revocation_epoch"] != revocation_epoch:
                    raise EngagementError("revocation_epoch_conflict")
                return
        previous = entries[-1]["digest"] if entries else "genesis"
        body = {
            "org": org,
            "epoch": epoch,
            "revocation_epoch": revocation_epoch,
            "previous": previous,
        }
        line = json.dumps({**body, "digest": _digest(body)}, sort_keys=True) + "\n"
        created = not os.path.lexists(self.journal)
        fd = os.open(self.journal, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        if created:
            # A new journal's directory entry must survive a crash, not only its bytes.
            directory = os.open(self.journal.parent, os.O_DIRECTORY | os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def _journaled_revocation(self, org: str, epoch: str) -> int | None:
        for entry in self._journal_entries():
            if (entry["org"], entry["epoch"]) == (org, epoch):
                return int(entry["revocation_epoch"])
        return None

    def reconcile_fences(self) -> None:
        """Re-apply every journaled fence; a restored older database cannot lift one."""
        entries = self._journal_entries()
        with self._db() as db:
            for entry in entries:
                org, epoch, revocation = entry["org"], entry["epoch"], entry["revocation_epoch"]
                try:
                    db.execute(
                        "INSERT OR IGNORE INTO fence VALUES (?,?,?)", (org, epoch, revocation)
                    )
                except sqlite3.IntegrityError:
                    # The database fence disagrees with the journal: hold, never pick one.
                    raise EngagementHold("fence_conflict") from None
                db.execute(
                    "UPDATE engagement SET state='terminal', "
                    "terminal_kind=coalesce(terminal_kind,'withdrawal'), "
                    "revocation_epoch=coalesce(revocation_epoch,?) WHERE org=? AND epoch=?",
                    (revocation, org, epoch),
                )

    # -- enrollment ------------------------------------------------------------------

    def record_engagement(
        self,
        org: str,
        epoch: str,
        *,
        lease_ref: str,
        authority_digest: str,
        resources: tuple[str, ...],
        lineage_ref: str | None,
        checkpoint: int | None,
        budget_limit: int,
        label: str = "",
    ) -> Engagement:
        """Durably record a fresh epoch with its checkpoint and lease. Idempotent.

        Recording does not activate the live binding. A display label is not identity.
        """
        org, epoch = _ref(org), _ref(epoch)
        lease_ref = _ref(lease_ref)
        if not isinstance(resources, tuple) or not resources:
            raise EngagementError("resources_required")
        resource = json.dumps(sorted({_ref(item) for item in resources}))
        if (
            not isinstance(authority_digest, str)
            or re.fullmatch("[0-9a-f]{64}", authority_digest) is None
        ):
            raise EngagementError("invalid_authority_digest")
        if lineage_ref is None or checkpoint is None:
            raise EngagementHold("lineage_or_checkpoint_missing")
        _ref(lineage_ref)
        _int(checkpoint, "invalid_checkpoint")
        _int(budget_limit, "invalid_budget")
        identity = (lease_ref, authority_digest, resource, lineage_ref, checkpoint, budget_limit)
        with self._db(org) as db:
            existing = db.execute(
                "SELECT * FROM engagement WHERE org=? AND epoch=?", (org, epoch)
            ).fetchone()
            if existing is not None:
                same = identity == (
                    existing["lease_ref"],
                    existing["authority_digest"],
                    existing["resource"],
                    existing["lineage_ref"],
                    existing["checkpoint"],
                    existing["budget_limit"],
                )
                if not same:
                    raise EngagementError("engagement_conflict")
                return self._row(existing)
            if db.execute("SELECT 1 FROM fence WHERE org=? AND epoch=?", (org, epoch)).fetchone():
                # Journaled withdrawal whose row a backup lost: never recreated as live.
                raise EngagementError("epoch_fenced")
            prior = db.execute(
                "SELECT * FROM engagement WHERE org=? ORDER BY ordinal", (org,)
            ).fetchall()
            known = {r["epoch"] for r in prior}
            for fenced in db.execute("SELECT epoch FROM fence WHERE org=?", (org,)):
                if fenced["epoch"] not in known:
                    # A fenced epoch lost to an older backup leaves no reliable checkpoint.
                    raise EngagementHold("prior_lineage_missing")
            high = 0
            journaled = {
                entry["epoch"]: entry["revocation_epoch"]
                for entry in self._journal_entries()
                if entry["org"] == org
            }
            for row in prior:
                if row["state"] != "terminal" or row["revocation_epoch"] is None:
                    raise EngagementHold("prior_engagement_not_terminal")
                if journaled.get(row["epoch"]) != row["revocation_epoch"]:
                    # Only a journaled termination admits a successor, not database rows alone.
                    raise EngagementHold("prior_termination_not_journaled")
                if not row["slot_released"]:
                    raise EngagementHold("live_slot_not_released")
                if row["lease_ref"] == lease_ref or row["authority_digest"] == authority_digest:
                    raise EngagementError("fresh_authority_required")
                high = max(high, row["checkpoint"], row["observed_high"])
            if checkpoint < high:
                raise EngagementHold("checkpoint_before_prior_high_water")
            if (
                prior
                and set(json.loads(resource)) & set(json.loads(prior[-1]["resource"]))
                and checkpoint <= high
            ):
                # Re-binding the same resource must start strictly after what was seen.
                raise EngagementHold("checkpoint_not_after_prior_rows")
            ordinal = len(prior) + 1
            first = not self._history(db)
            if first:
                # The directory's first grant: the marker is durable before this commits.
                self._mark_first_grant(db)
            db.execute(
                "INSERT INTO engagement(org,epoch,ordinal,state,lease_ref,authority_digest,"
                "lineage_ref,resource,checkpoint,observed_high,budget_limit,label) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    org,
                    epoch,
                    ordinal,
                    "recorded",
                    lease_ref,
                    authority_digest,
                    lineage_ref,
                    resource,
                    checkpoint,
                    checkpoint,
                    budget_limit,
                    label[:64],
                ),
            )
            recorded = self._row(
                db.execute(
                    "SELECT * FROM engagement WHERE org=? AND epoch=?", (org, epoch)
                ).fetchone()
            )
            store_id = self._store_id(db)
        if first and store_id is not None:
            # Committed: advance the marker to sequence 2. Not a refusal if this write fails,
            # since the ingest is already durable; the next ledger operation promotes the
            # marker or holds (`anti_rollback_unavailable`) until it can.
            try:
                self._promote_marker(_AntiRollback(self.directory), store_id)
            except EngagementHold:
                pass
        return recorded

    def migrate_single_engagement(
        self,
        org: str,
        epoch: str,
        *,
        lease_ref: str,
        authority_digest: str,
        resources: tuple[str, ...],
        lineage_ref: str | None,
        checkpoint: int | None,
        budget_limit: int,
        records: tuple[tuple[str, str], ...] = (),
    ) -> Engagement:
        """Adopt pre-epoch records under their original scope; idempotent, never rebinding."""
        if any(known.epoch != epoch for known in self.engagements(_ref(org))):
            raise EngagementError("migration_requires_first_engagement")
        engagement = self.record_engagement(
            org,
            epoch,
            lease_ref=lease_ref,
            authority_digest=authority_digest,
            resources=resources,
            lineage_ref=lineage_ref,
            checkpoint=checkpoint,
            budget_limit=budget_limit,
        )
        if engagement.ordinal != 1:
            raise EngagementError("migration_requires_first_engagement")
        if engagement.state == "recorded":
            engagement = self.activate(org, epoch)
        for kind, ref in records:
            self.bind(org, epoch, kind, ref)
        return engagement

    def activate(self, org: str, epoch: str) -> Engagement:
        """Open the live binding. Requires the recorded checkpoint, unfenced, slot free."""
        org, epoch = _ref(org), _ref(epoch)
        with self._db(org) as db:
            row = self._require_row(db, org, epoch)
            if db.execute("SELECT 1 FROM fence WHERE org=? AND epoch=?", (org, epoch)).fetchone():
                raise EngagementError("epoch_fenced")
            if row["state"] == "active":
                return self._row(row)
            if row["state"] != "recorded":
                raise EngagementError("engagement_not_activatable")
            db.execute("UPDATE engagement SET state='active' WHERE org=? AND epoch=?", (org, epoch))
            return self._row(self._require_row(db, org, epoch))

    # -- withdrawal ------------------------------------------------------------------

    def withdraw(self, org: str, epoch: str, *, revocation_epoch: int) -> Engagement:
        """Persist terminal state and revocation epoch (fence first). Idempotent.

        The supplied epoch is only a confirmation: it must equal the one `retire` would use
        (already recorded, else journaled, else the organization's next), or it is refused.
        """
        org, epoch = _ref(org), _ref(epoch)
        _int(revocation_epoch, "invalid_revocation_epoch", minimum=1)
        with self.pin(exclusive=True), self._db(org) as db:
            row = self._require_row(db, org, epoch)
            recorded = row["revocation_epoch"]
            if recorded is None:
                recorded = self._journaled_revocation(org, epoch)
            assigned = recorded if recorded is not None else self._next_revocation(db, org)
            if revocation_epoch != assigned:
                if recorded is not None:
                    raise EngagementError("revocation_epoch_conflict")
                if revocation_epoch < assigned:
                    raise EngagementError("revocation_epoch_not_monotonic")
                raise EngagementError("revocation_epoch_not_assigned")
            return self._terminate(db, org, epoch, revocation_epoch)

    def _terminate(
        self, db: sqlite3.Connection, org: str, epoch: str, revocation_epoch: int
    ) -> Engagement:
        row = self._require_row(db, org, epoch)
        if row["revocation_epoch"] is not None and row["revocation_epoch"] != revocation_epoch:
            raise EngagementError("revocation_epoch_conflict")
        peak = db.execute(
            "SELECT max(revocation_epoch) FROM engagement WHERE org=? AND epoch!=?",
            (org, epoch),
        ).fetchone()[0]
        if row["revocation_epoch"] is None and peak is not None and revocation_epoch <= peak:
            raise EngagementError("revocation_epoch_not_monotonic")
        store_id = self._store_id(db)
        if store_id is None:
            raise EngagementHold("anti_rollback_baseline_missing")
        store = _AntiRollback(self.directory)
        # The record lock is taken before any write, so an unusable anti-rollback location
        # refuses here with nothing journaled. Then journal (fsync), then the revocation record
        # outside the snapshot, then the database row; all of them before any release.
        with store.locked():
            self._append_fence(org, epoch, revocation_epoch)
            self._record_revocation(store, store_id, org, epoch, revocation_epoch)
        db.execute("INSERT OR IGNORE INTO fence VALUES (?,?,?)", (org, epoch, revocation_epoch))
        if row["state"] != "terminal" or row["revocation_epoch"] != revocation_epoch:
            db.execute(
                "UPDATE engagement SET state='terminal', terminal_kind='withdrawal', "
                "revocation_epoch=? WHERE org=? AND epoch=?",
                (revocation_epoch, org, epoch),
            )
        return self._row(self._require_row(db, org, epoch))

    def retire(self, org: str, epoch: str) -> Engagement:
        """Custody withdrawal: persist terminal state and revocation epoch, then free the slot.

        The revocation epoch is read from this ledger (row, then journal) or assigned by it as
        the organization's next one; a caller never supplies it. Termination commits in its own
        transaction before the release transaction begins, so an interruption between them
        leaves a terminal, fenced epoch with its slot held, which a retry releases. Repeating
        a completed retirement writes nothing.
        """
        org, epoch = _ref(org), _ref(epoch)
        with self.pin(exclusive=True):
            with self._db(org) as db:
                row = self._require_row(db, org, epoch)
                revocation = row["revocation_epoch"]
                if revocation is None:
                    revocation = self._journaled_revocation(org, epoch)
                if revocation is None:
                    revocation = self._next_revocation(db, org)
                self._terminate(db, org, epoch, revocation)
            return self.release_slot(org, epoch)

    def _next_revocation(self, db: sqlite3.Connection, org: str) -> int:
        known = [
            row[0]
            for row in db.execute(
                "SELECT revocation_epoch FROM engagement WHERE org=? AND revocation_epoch "
                "IS NOT NULL UNION ALL SELECT revocation_epoch FROM fence WHERE org=?",
                (org, org),
            )
        ]
        known += [e["revocation_epoch"] for e in self._journal_entries() if e["org"] == org]
        return _int(max(known, default=0) + 1, "invalid_revocation_epoch", minimum=1)

    def release_slot(self, org: str, epoch: str) -> Engagement:
        """Free the live slot only after the terminal state and revocation epoch persist.

        Persisted means the committed database row, its fence row and the fsynced journal
        entry all carry the same revocation epoch, and the organization's anti-rollback
        revocation record outside the snapshot has reached it. Releasing an already released
        slot is a no-op.
        """
        org, epoch = _ref(org), _ref(epoch)
        with self._db(org) as db:
            row = self._require_row(db, org, epoch)
            fenced = db.execute(
                "SELECT revocation_epoch FROM fence WHERE org=? AND epoch=?", (org, epoch)
            ).fetchone()
            if (
                row["state"] != "terminal"
                or row["revocation_epoch"] is None
                or fenced is None
                or fenced[0] != row["revocation_epoch"]
                or self._journaled_revocation(org, epoch) != row["revocation_epoch"]
            ):
                raise EngagementHold("terminal_state_not_persisted")
            if not row["slot_released"]:
                # A withdrawal is complete only once its anti-rollback record is durable.
                if not self._revocation_durable(db, org, epoch, row["revocation_epoch"]):
                    raise EngagementHold("anti_rollback_revocation_not_durable")
                db.execute(
                    "UPDATE engagement SET slot_released=1 WHERE org=? AND epoch=?", (org, epoch)
                )
            return self._row(self._require_row(db, org, epoch))

    def restore_backup(self, backup: Path) -> None:
        """Replace the database with an older backup, then re-apply every journaled fence.

        The backup is verified against the anti-rollback records before it replaces anything:
        a backup of another store, or one the records show is behind, is refused unchanged.
        """
        with self.pin(exclusive=True):
            _check_file(backup)
            temporary = self.path.with_name(self.path.name + ".restore")
            shutil.copyfile(backup, temporary)
            os.chmod(temporary, 0o600)
            try:
                with closing(sqlite3.connect(temporary, isolation_level=None)) as candidate:
                    candidate.execute("PRAGMA trusted_schema=OFF")
                    self._verify_anti_rollback(candidate)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            os.replace(temporary, self.path)
            self._create_schema()
            self.reconcile_fences()

    def backup(self, destination: Path) -> None:
        shutil.copyfile(self.path, destination)
        os.chmod(destination, 0o600)

    # -- epoch-bound binding, approvals, budget and effects ------------------------------

    def bind(self, org: str, epoch: str, kind: str, ref: str) -> None:
        """Bind a queue, session, grant, callback, receipt or storage ref to one epoch."""
        org, epoch, ref = _ref(org), _ref(epoch), _ref(ref)
        if kind not in BINDING_KINDS:
            raise EngagementError("unknown_binding_kind")
        with self._db(org) as db:
            row = self._require_row(db, org, epoch)
            if row["state"] == "terminal" or self._fenced(db, org, epoch):
                raise EngagementError("epoch_terminal")
            db.execute("INSERT OR IGNORE INTO binding VALUES (?,?,?,?)", (org, epoch, kind, ref))
            bound = db.execute(
                "SELECT epoch FROM binding WHERE org=? AND kind=? AND ref=?", (org, kind, ref)
            ).fetchone()[0]
            if bound != epoch:
                raise EngagementError("binding_belongs_to_other_epoch")

    def authorize(self, org: str, epoch: str, kind: str, ref: str) -> bool:
        """True only for a ref bound to this exact, live, unfenced epoch."""
        with self._db(org) as db:
            if not self._live(db, org, epoch):
                return False
            row = db.execute(
                "SELECT epoch FROM binding WHERE org=? AND kind=? AND ref=?", (org, kind, ref)
            ).fetchone()
            return row is not None and row[0] == epoch

    def grant_approval(self, org: str, epoch: str, ref: str) -> None:
        with self._db(org) as db:
            if not self._live(db, org, epoch):
                raise EngagementError("epoch_not_live")
            db.execute("INSERT OR IGNORE INTO approval VALUES (?,?,?)", (org, epoch, _ref(ref)))

    def has_approval(self, org: str, epoch: str, ref: str) -> bool:
        """Approvals never carry over: the lookup is by exact live epoch."""
        with self._db(org) as db:
            return (
                self._live(db, org, epoch)
                and db.execute(
                    "SELECT 1 FROM approval WHERE org=? AND epoch=? AND ref=?", (org, epoch, ref)
                ).fetchone()
                is not None
            )

    def reserve_budget(self, org: str, epoch: str, key: str, amount: int) -> bool:
        """Reserve against this epoch's own ledger. Idempotent by key; False when over limit."""
        _int(amount, "invalid_amount", minimum=1)
        with self._db(org) as db:
            if not self._live(db, org, epoch):
                raise EngagementError("epoch_not_live")
            row = self._require_row(db, org, epoch)
            old = db.execute(
                "SELECT amount FROM budget_reservation WHERE org=? AND epoch=? AND key=?",
                (org, epoch, _ref(key)),
            ).fetchone()
            if old is not None:
                if old[0] != amount:
                    raise EngagementError("reservation_conflict")
                return True
            if row["budget_spent"] + amount > row["budget_limit"]:
                return False
            db.execute("INSERT INTO budget_reservation VALUES (?,?,?,?)", (org, epoch, key, amount))
            db.execute(
                "UPDATE engagement SET budget_spent=budget_spent+? WHERE org=? AND epoch=?",
                (amount, org, epoch),
            )
            return True

    def record_effect(self, org: str, epoch: str, kind: str, key: str) -> bool:
        """Count one decision/read/fit/export for this epoch. Late old-epoch effects change
        nothing and return False; a repeat of the same key is counted once."""
        if kind not in EFFECT_KINDS:
            raise EngagementError("unknown_effect_kind")
        key = _ref(key)
        with self._db(org) as db:
            if not self._live(db, org, epoch):
                return False
            cursor = db.execute(
                "INSERT OR IGNORE INTO effect VALUES (?,?,?,?)", (org, epoch, kind, key)
            )
            return cursor.rowcount == 1

    def counts(self, org: str, epoch: str) -> dict[str, int]:
        """Epoch-bound counts only; prior epochs never leak in. See `historical`."""
        with self._db(org) as db:
            self._require_row(db, _ref(org), _ref(epoch))
            rows = db.execute(
                "SELECT kind, count(*) FROM effect WHERE org=? AND epoch=? GROUP BY kind",
                (org, epoch),
            ).fetchall()
        found = {r[0]: r[1] for r in rows}
        return {kind: found.get(kind, 0) for kind in EFFECT_KINDS}

    def historical(self, org: str, *, before: str) -> tuple[dict[str, Any], ...]:
        """Allowed prior-epoch evidence, labelled historical and never part of current counts."""
        with self._db(org) as db:
            current = self._require_row(db, _ref(org), _ref(before))
            rows = db.execute(
                "SELECT * FROM engagement WHERE org=? AND ordinal<? ORDER BY ordinal",
                (org, current["ordinal"]),
            ).fetchall()
            result = []
            for row in rows:
                counted = {
                    r[0]: r[1]
                    for r in db.execute(
                        "SELECT kind, count(*) FROM effect WHERE org=? AND epoch=? GROUP BY kind",
                        (org, row["epoch"]),
                    )
                }
                result.append(
                    {
                        "epoch": row["epoch"],
                        "historical": True,
                        "terminal_kind": row["terminal_kind"],
                        "revocation_epoch": row["revocation_epoch"],
                        "counts": {k: counted.get(k, 0) for k in EFFECT_KINDS},
                    }
                )
            return tuple(result)

    # -- source rows ---------------------------------------------------------------------

    def live_gate(self, org: str, epoch: str, resource: str, lease_ref: str) -> int:
        """Return the recorded checkpoint for an active epoch bound to this lease and resource.

        Anything else is a hold; no source row may be admitted without it.
        """
        with self._db(org) as db:
            row = db.execute(
                "SELECT * FROM engagement WHERE org=? AND epoch=?", (org, epoch)
            ).fetchone()
            if row is None:
                raise EngagementHold("engagement_not_recorded")
            if self._fenced(db, org, epoch) or row["state"] == "terminal":
                raise EngagementHold("epoch_terminal")
            if self._journal_fenced(org, epoch):
                raise EngagementHold("epoch_terminal")
            if row["state"] != "active":
                raise EngagementHold("engagement_not_active")
            if resource not in json.loads(row["resource"]) or row["lease_ref"] != lease_ref:
                raise EngagementHold("lease_or_resource_mismatch")
            return int(row["checkpoint"])

    def known_prior_epoch(self, org: str, epoch: str) -> bool:
        """True when `epoch` is a different, already-terminal or fenced epoch of this org."""
        with self._db(org) as db:
            row = db.execute(
                "SELECT state FROM engagement WHERE org=? AND epoch=?", (org, epoch)
            ).fetchone()
            return (
                self._fenced(db, org, epoch)
                or (row is not None and row[0] == "terminal")
                or self._journal_fenced(org, epoch)
            )

    def _journal_fenced(self, org: str, epoch: str) -> bool:
        return any(
            (entry["org"], entry["epoch"]) == (org, epoch) for entry in self._journal_entries()
        )

    def admit_row(
        self, org: str, epoch: str, *, resource: str, lease_ref: str, row_epoch: str, position: int
    ) -> Admission:
        """Decide one source row. Never accepts a prior-epoch row, at any source position."""
        try:
            checkpoint = self.live_gate(org, epoch, resource, lease_ref)
        except EngagementHold as hold:
            return Admission("hold", hold.reason)
        if row_epoch != epoch:
            reason = "prior_epoch" if self.known_prior_epoch(org, row_epoch) else "epoch_mismatch"
            return Admission("deny", reason, checkpoint)
        if type(position) is not int or position <= checkpoint:
            return Admission("deny", "at_or_before_checkpoint", checkpoint)
        return Admission("accept", "admitted", checkpoint)

    def observe_positions(self, org: str, epoch: str, high: int) -> None:
        """Remember the highest admitted source position; later epochs must start after it."""
        _int(high, "invalid_position")
        with self._db(org) as db:
            db.execute(
                "UPDATE engagement SET observed_high=max(observed_high,?) "
                "WHERE org=? AND epoch=? AND state='active'",
                (high, org, epoch),
            )

    # -- reads -------------------------------------------------------------------------------

    def engagements(self, org: str) -> tuple[Engagement, ...]:
        with self._db(org) as db:
            rows = db.execute(
                "SELECT * FROM engagement WHERE org=? ORDER BY ordinal", (_ref(org),)
            ).fetchall()
        return tuple(self._row(r) for r in rows)

    def get(self, org: str, epoch: str) -> Engagement | None:
        with self._db(org) as db:
            row = db.execute(
                "SELECT * FROM engagement WHERE org=? AND epoch=?", (org, epoch)
            ).fetchone()
            return None if row is None else self._row(row)

    def organizations(self) -> tuple[str, ...]:
        with self._db() as db:
            return tuple(r[0] for r in db.execute("SELECT DISTINCT org FROM engagement ORDER BY 1"))

    # -- helpers -----------------------------------------------------------------------------

    @staticmethod
    def _fenced(db: sqlite3.Connection, org: str, epoch: str) -> bool:
        return (
            db.execute("SELECT 1 FROM fence WHERE org=? AND epoch=?", (org, epoch)).fetchone()
            is not None
        )

    def _live(self, db: sqlite3.Connection, org: str, epoch: str) -> bool:
        row = db.execute(
            "SELECT state FROM engagement WHERE org=? AND epoch=?", (org, epoch)
        ).fetchone()
        return row is not None and row[0] == "active" and not self._fenced(db, org, epoch)

    @staticmethod
    def _require_row(db: sqlite3.Connection, org: str, epoch: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM engagement WHERE org=? AND epoch=?", (org, epoch)
        ).fetchone()
        if row is None:
            raise EngagementError("engagement_not_recorded")
        assert isinstance(row, sqlite3.Row)
        return row

    @staticmethod
    def _row(row: sqlite3.Row) -> Engagement:
        return Engagement(
            row["org"],
            row["epoch"],
            row["ordinal"],
            row["state"],
            row["lease_ref"],
            tuple(json.loads(row["resource"])),
            row["checkpoint"],
            row["observed_high"],
            row["budget_limit"],
            row["budget_spent"],
            row["revocation_epoch"],
            row["terminal_kind"],
            bool(row["slot_released"]),
            row["label"],
        )
