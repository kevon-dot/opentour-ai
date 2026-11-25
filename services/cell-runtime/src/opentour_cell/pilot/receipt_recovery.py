"""Discover owner-local handoffs without initializing, migrating or dispatching a journal."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opentour_cell.pilot.custody import Closeout
from opentour_cell.pilot.slack_adapter import Journal, Settings, _check, _json
from opentour_cell.pilot.training import _check_file

# An admitted delivery remains outstanding until the owner independently confirms it.
_OUTSTANDING = ("pending", "sending", "uncertain", "failed", "accepted")
_CURSOR = re.compile(r"v1\.([0-9a-f]{1,16})\.([0-9a-f]{1,16})\.([0-9a-f]{64})")


@contextmanager
def _open_db(path: Path, *, reserve: bool = False) -> Iterator[sqlite3.Connection | None]:
    try:
        _check_file(path)
    except FileNotFoundError:
        _check(not any(os.path.lexists(str(path) + ext) for ext in ("-wal", "-shm", "-journal")))
        yield None
        return
    if not reserve:
        # Even mode=ro creates WAL/SHM files if a WAL-mode database was cleanly closed.
        # Hold that read until its normal worker has opened the existing live snapshot.
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as file:
            header = file.read(20)
        if header[18:20] == b"\x02\x02":
            for suffix in ("-wal", "-shm"):
                _check_file(Path(str(path) + suffix))
    mode = "rw" if reserve else "ro"
    with closing(sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True, timeout=2)) as db, db:
        db.row_factory = sqlite3.Row
        if not reserve:
            db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN IMMEDIATE" if reserve else "BEGIN")
        yield db


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    found = db.execute("SELECT type FROM sqlite_master WHERE name=?", (name,)).fetchone()
    if found is None:
        return False
    _check(found[0] == "table")
    return True


class _JournalSnapshot(Journal):
    """Use the existing receipt and integrity verifier in the current owner transaction."""

    def __init__(self, db: sqlite3.Connection, path: Path, binding: str):
        identity = db.execute("SELECT binding,key FROM identity WHERE id=1").fetchone()
        _check(identity and identity[0] == binding and isinstance(identity[1], bytes))
        _check(len(identity[1]) == 32)
        self.path = path
        self.binding = binding
        self.key = identity[1]
        self.connection = db

    @contextmanager
    def db(self, timeout: float = 4) -> Iterator[sqlite3.Connection]:
        del timeout
        yield self.connection


@dataclass(frozen=True)
class _Scope:
    engagement: str | None
    withdrawn: bool
    journal: _JournalSnapshot | None
    closure: dict[str, Any] | None = None


@contextmanager
def _scope(cfg: Settings, *, confirm: bool = False) -> Iterator[_Scope]:
    custody = Closeout(cfg.directory, cfg.organization)
    # Consistent with withdrawal: custody, then domain identity, then the handoff journal.
    # No mutating store constructor, new schema, or external call is needed for inspection.
    with ExitStack() as resources:
        ledger = resources.enter_context(_open_db(custody.path, reserve=confirm))
        withdrawn = ledger is not None
        engagement: str | None = None
        if ledger is not None:
            row = custody._row(ledger)
            engagement = row["engagement"]
            _check(isinstance(engagement, str) and re.fullmatch("[a-f0-9]{32}", engagement))
            _check(row["minimized"] in (0, 1))
            if row["minimized"]:
                yield _Scope(engagement, True, None, custody._minimal(row))
                return

        training = resources.enter_context(_open_db(custody.training, reserve=confirm))
        if training is not None:
            owner = training.execute("SELECT organization FROM owner WHERE singleton=1").fetchone()
            _check(owner and owner[0] == cfg.organization)
            if _has_table(training, "engagement"):
                current = training.execute(
                    "SELECT id,withdrawn_at FROM engagement WHERE singleton=1"
                ).fetchone()
                _check(
                    current
                    and isinstance(current[0], str)
                    and re.fullmatch("[a-f0-9]{32}", current[0])
                )
                _check(engagement in (None, current[0]))
                withdrawn = withdrawn or current[1] is not None
                engagement = current[0]
            else:
                _check(ledger is None and not _has_table(training, "dataset_scope"))

        path = cfg.directory / "slack-pilot.sqlite3"
        db = resources.enter_context(_open_db(path, reserve=confirm))
        _check(db is None or training is not None or ledger is not None)
        journal = _JournalSnapshot(db, path, cfg.binding) if db is not None else None
        if db is not None and _has_table(db, "journal_control"):
            held = db.execute("SELECT held FROM journal_control WHERE id=1").fetchone()
            _check(held and held[0] in (0, 1))
            withdrawn = withdrawn or bool(held[0])
        yield _Scope(engagement, withdrawn, journal)
        # A first withdrawal may have appeared while reading. Fail before returning any
        # metadata or committing confirmation, then a retry uses the new custody record.
        if ledger is None:
            _check(not os.path.lexists(custody.path))


def _unbound_legacy(db: sqlite3.Connection, scoped: bool) -> bool:
    query = (
        "SELECT 1 FROM decisions d WHERE d.exportable=1 "
        "AND d.delivery IN ('pending','sending','uncertain','failed','accepted','legacy_hold')"
    )
    if scoped:
        query += " AND NOT EXISTS (SELECT 1 FROM authorizations a WHERE a.receipt=d.receipt)"
    return db.execute(query + " LIMIT 1").fetchone() is not None


def _project(row: sqlite3.Row) -> dict[str, Any]:
    _check(row["delivery"] in (*_OUTSTANDING, "local_only", "suppressed", "downstream_confirmed"))
    _check(
        row["transmitted"] is None or (type(row["transmitted"]) is int and row["transmitted"] >= 0)
    )
    return {
        "receipt": row["receipt"],
        "state": row["delivery"],
        "attempted_at": row["transmitted"],
        "destination": row["destination"] or None,
    }


def _cursor(
    journal: _JournalSnapshot, scope: _Scope, destination: str, upper: int, last: int
) -> str:
    data = _json(
        [
            "recovery-v1",
            journal.binding,
            scope.engagement,
            scope.withdrawn,
            destination,
            upper,
            last,
        ]
    )
    seal = hmac.new(journal.key, data.encode(), hashlib.sha256).hexdigest()
    return f"v1.{upper:x}.{last:x}.{seal}"


def unresolved_receipts(
    cfg: Settings, *, limit: int = 25, cursor: str | None = None
) -> dict[str, Any]:
    """Return a bounded, minimized list for one private installation and destination."""
    _check(type(limit) is int and 1 <= limit <= 100)
    with _scope(cfg) as scope:
        if scope.closure is not None:
            return scope.closure
        result: dict[str, Any] = {"receipts": [], "next_cursor": None, "legacy_scope": None}
        journal = scope.journal
        if journal is None:
            _check(cursor is None)
            return result
        db = journal.connection
        scoped = _has_table(db, "authorizations")
        if _unbound_legacy(db, scoped):
            result["legacy_scope"] = "unknown"
        destination = cfg.zapier_destination_sha256
        if not scoped or scope.engagement is None or not destination:
            _check(cursor is None)
            return result

        maximum = int(db.execute("SELECT COALESCE(MAX(rowid),0) FROM decisions").fetchone()[0])
        upper, last = maximum, 0
        if cursor is not None:
            parsed = _CURSOR.fullmatch(cursor)
            _check(parsed is not None)
            assert parsed is not None
            upper, last = int(parsed[1], 16), int(parsed[2], 16)
            _check(0 < last <= upper <= maximum)
            _check(hmac.compare_digest(cursor, _cursor(journal, scope, destination, upper, last)))

        states = _OUTSTANDING[1:] if scope.withdrawn else _OUTSTANDING
        slots = ",".join("?" for _ in states)
        rows = db.execute(
            "SELECT d.rowid AS ordinal,d.receipt,d.delivery,a.transmitted,a.destination "
            "FROM decisions d JOIN authorizations a USING(receipt) WHERE d.rowid>? AND d.rowid<=? "
            "AND a.engagement=? AND a.destination=? AND d.exportable=1 AND a.confirmed IS NULL "
            f"AND d.delivery IN ({slots}) ORDER BY d.rowid LIMIT ?",
            (last, upper, scope.engagement, destination, *states, limit + 1),
        ).fetchall()
        for row in rows[:limit]:
            journal._seal(db, "decision", row["receipt"])
            result["receipts"].append(_project(row))
        if len(rows) > limit:
            result["next_cursor"] = _cursor(
                journal, scope, destination, upper, rows[limit - 1]["ordinal"]
            )
        return result


def _scoped_receipt(
    cfg: Settings, scope: _Scope, receipt: str
) -> tuple[_JournalSnapshot, sqlite3.Row]:
    journal = scope.journal
    _check(journal is not None)
    assert journal is not None
    db = journal.connection
    _check(_has_table(db, "authorizations"))
    row = db.execute(
        "SELECT d.receipt,d.delivery,a.transmitted,a.destination,a.engagement "
        "FROM decisions d JOIN authorizations a USING(receipt) WHERE d.receipt=?",
        (receipt,),
    ).fetchone()
    _check(row and scope.engagement and row["engagement"] == scope.engagement)
    _check(row["destination"] in ("", cfg.zapier_destination_sha256))
    journal._seal(db, "decision", receipt)
    return journal, row


def inspect_receipt(cfg: Settings, receipt: str) -> dict[str, Any]:
    """Preserve detailed owner inspection while honoring destination and closeout retention."""
    with _scope(cfg) as scope:
        if scope.closure is not None:
            return scope.closure
        journal, row = _scoped_receipt(cfg, scope, receipt)
        return _project(row) if scope.withdrawn else journal.receipt(receipt)


def confirm_receipt(cfg: Settings, receipt: str, reference: str) -> None:
    """Only an explicit owner confirmation may change the historical delivery state."""
    with _scope(cfg, confirm=True) as scope:
        _check(scope.closure is None)
        journal, row = _scoped_receipt(cfg, scope, receipt)
        _check(bool(row["destination"]))
        Journal.confirm_delivery_in(journal.connection, receipt, reference)
