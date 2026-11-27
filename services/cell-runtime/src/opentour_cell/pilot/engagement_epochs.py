"""Owner-local engagement-epoch ledger for withdrawn organizations that return (OPE-543).

One organization identifier may hold several engagements over time, each under its own
epoch, lease, budget, approvals and effect counters. Re-entry never reopens an earlier
epoch: it needs fresh authority, a durably recorded trusted source high-water checkpoint,
and a persisted terminal state (with revocation epoch) for every earlier epoch.

The withdrawal fence is also kept in an append-only, hash-chained journal outside the
SQLite file, so restoring an older database backup re-applies every fence before work
restarts. Unknown or unreliable lineage is a hold, never a guess. Fixtures are generated;
this ledger makes no cross-CRM deduplication or counting claim (OPE-452/455 own that).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.training import _check_directory, _check_file

SCHEMA = "opentour.pilot.engagement-epochs.v1"
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
"""


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
        try:
            _check_directory(self.path)
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
    def _db(self) -> Iterator[sqlite3.Connection]:
        try:
            _check_file(self.path)
        except (TrainingError, OSError):
            raise EngagementError("private_store_required") from None
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    def _create_schema(self) -> None:
        with self._db() as db:
            for statement in _SCHEMA_SQL.split(";"):
                if statement.strip():
                    db.execute(statement)

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
        fd = os.open(self.journal, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def reconcile_fences(self) -> None:
        """Re-apply every journaled fence; a restored older database cannot lift one."""
        entries = self._journal_entries()
        with self._db() as db:
            for entry in entries:
                org, epoch, revocation = entry["org"], entry["epoch"], entry["revocation_epoch"]
                db.execute("INSERT OR IGNORE INTO fence VALUES (?,?,?)", (org, epoch, revocation))
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
        with self._db() as db:
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
            for row in prior:
                if row["state"] != "terminal" or row["revocation_epoch"] is None:
                    raise EngagementHold("prior_engagement_not_terminal")
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
            return self._row(
                db.execute(
                    "SELECT * FROM engagement WHERE org=? AND epoch=?", (org, epoch)
                ).fetchone()
            )

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
        with self._db() as db:
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
        """Persist terminal state and revocation epoch (fence first). Idempotent."""
        org, epoch = _ref(org), _ref(epoch)
        _int(revocation_epoch, "invalid_revocation_epoch", minimum=1)
        with self.pin(exclusive=True), self._db() as db:
            row = self._require_row(db, org, epoch)
            if row["revocation_epoch"] is not None and row["revocation_epoch"] != revocation_epoch:
                raise EngagementError("revocation_epoch_conflict")
            peak = db.execute(
                "SELECT max(revocation_epoch) FROM engagement WHERE org=? AND epoch!=?",
                (org, epoch),
            ).fetchone()[0]
            if row["revocation_epoch"] is None and peak is not None and revocation_epoch <= peak:
                raise EngagementError("revocation_epoch_not_monotonic")
            self._append_fence(org, epoch, revocation_epoch)
            db.execute("INSERT OR IGNORE INTO fence VALUES (?,?,?)", (org, epoch, revocation_epoch))
            db.execute(
                "UPDATE engagement SET state='terminal', terminal_kind='withdrawal', "
                "revocation_epoch=? WHERE org=? AND epoch=?",
                (revocation_epoch, org, epoch),
            )
            return self._row(self._require_row(db, org, epoch))

    def release_slot(self, org: str, epoch: str) -> Engagement:
        """Free the live slot only after the terminal state and revocation epoch persist."""
        org, epoch = _ref(org), _ref(epoch)
        with self._db() as db:
            row = self._require_row(db, org, epoch)
            fenced = db.execute(
                "SELECT revocation_epoch FROM fence WHERE org=? AND epoch=?", (org, epoch)
            ).fetchone()
            if (
                row["state"] != "terminal"
                or row["revocation_epoch"] is None
                or fenced is None
                or fenced[0] != row["revocation_epoch"]
            ):
                raise EngagementHold("terminal_state_not_persisted")
            db.execute(
                "UPDATE engagement SET slot_released=1 WHERE org=? AND epoch=?", (org, epoch)
            )
            return self._row(self._require_row(db, org, epoch))

    def restore_backup(self, backup: Path) -> None:
        """Replace the database with an older backup, then re-apply every journaled fence."""
        with self.pin(exclusive=True):
            _check_file(backup)
            temporary = self.path.with_name(self.path.name + ".restore")
            shutil.copyfile(backup, temporary)
            os.chmod(temporary, 0o600)
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
        with self._db() as db:
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
        with self._db() as db:
            if not self._live(db, org, epoch):
                return False
            row = db.execute(
                "SELECT epoch FROM binding WHERE org=? AND kind=? AND ref=?", (org, kind, ref)
            ).fetchone()
            return row is not None and row[0] == epoch

    def grant_approval(self, org: str, epoch: str, ref: str) -> None:
        with self._db() as db:
            if not self._live(db, org, epoch):
                raise EngagementError("epoch_not_live")
            db.execute("INSERT OR IGNORE INTO approval VALUES (?,?,?)", (org, epoch, _ref(ref)))

    def has_approval(self, org: str, epoch: str, ref: str) -> bool:
        """Approvals never carry over: the lookup is by exact live epoch."""
        with self._db() as db:
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
        with self._db() as db:
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
        with self._db() as db:
            if not self._live(db, org, epoch):
                return False
            cursor = db.execute(
                "INSERT OR IGNORE INTO effect VALUES (?,?,?,?)", (org, epoch, kind, key)
            )
            return cursor.rowcount == 1

    def counts(self, org: str, epoch: str) -> dict[str, int]:
        """Epoch-bound counts only; prior epochs never leak in. See `historical`."""
        with self._db() as db:
            self._require_row(db, _ref(org), _ref(epoch))
            rows = db.execute(
                "SELECT kind, count(*) FROM effect WHERE org=? AND epoch=? GROUP BY kind",
                (org, epoch),
            ).fetchall()
        found = {r[0]: r[1] for r in rows}
        return {kind: found.get(kind, 0) for kind in EFFECT_KINDS}

    def historical(self, org: str, *, before: str) -> tuple[dict[str, Any], ...]:
        """Allowed prior-epoch evidence, labelled historical and never part of current counts."""
        with self._db() as db:
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
        with self._db() as db:
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
        with self._db() as db:
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
        with self._db() as db:
            db.execute(
                "UPDATE engagement SET observed_high=max(observed_high,?) "
                "WHERE org=? AND epoch=? AND state='active'",
                (high, org, epoch),
            )

    # -- reads -------------------------------------------------------------------------------

    def engagements(self, org: str) -> tuple[Engagement, ...]:
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM engagement WHERE org=? ORDER BY ordinal", (_ref(org),)
            ).fetchall()
        return tuple(self._row(r) for r in rows)

    def get(self, org: str, epoch: str) -> Engagement | None:
        with self._db() as db:
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
