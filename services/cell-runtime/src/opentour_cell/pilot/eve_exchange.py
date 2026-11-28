"""Owner-local handoff between the installed worker and the isolated EVE trainer."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

TERMINAL = frozenset({"completed", "stopped", "expired", "held", "failed"})
CONTROL_FROM = {
    "pause": {"running"},
    "resume": {"paused"},
    "stop": {"queued", "running", "pausing", "paused", "resuming", "stopping", "held"},
}
PENDING = {"pause": "pausing", "resume": "resuming", "stop": "stopping"}
LEASE_SECONDS = 30
STALE_SECONDS = 45


def digest(value: Any) -> str:
    wire = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(wire.encode()).hexdigest()


def run_ref(reservation_ref: str, spec: dict[str, Any]) -> str:
    return "eve-" + digest([reservation_ref, digest(spec)])[:32]


class Exchange:
    """One approved reservation admits one run. The trainer alone acknowledges optimizer state."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        if not directory.is_absolute() or directory.resolve() != directory:
            raise ValueError("private_eve_exchange_required")
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.stat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("private_eve_exchange_required")
        self.path = directory / "controls.sqlite3"
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise ValueError("private_eve_exchange_required")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("private_eve_exchange_required")
        finally:
            os.close(fd)
        with self.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS eve_jobs(
                reservation_ref TEXT PRIMARY KEY, run_ref TEXT UNIQUE NOT NULL, spec TEXT NOT NULL,
                spec_digest TEXT NOT NULL, approver TEXT NOT NULL, approval_key TEXT NOT NULL,
                state TEXT NOT NULL, version INTEGER NOT NULL, launched INTEGER NOT NULL DEFAULT -1,
                requested TEXT, completed_steps INTEGER, total_steps INTEGER NOT NULL,
                checkpoint_step INTEGER, active_seconds INTEGER, heartbeat REAL NOT NULL DEFAULT 0,
                permit_until REAL NOT NULL, deadline INTEGER NOT NULL, trainer_id TEXT,
                launched_at REAL, reported INTEGER NOT NULL DEFAULT 0)""")
            columns = {r[1] for r in db.execute("PRAGMA table_info(eve_jobs)")}
            if "reported" not in columns:
                db.execute("ALTER TABLE eve_jobs ADD COLUMN reported INTEGER NOT NULL DEFAULT 0")
            db.execute("""CREATE TABLE IF NOT EXISTS eve_controls(
                reservation_ref TEXT NOT NULL, key TEXT NOT NULL, action TEXT NOT NULL,
                revision TEXT NOT NULL, PRIMARY KEY(reservation_ref,key))""")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    @staticmethod
    def _row(db: sqlite3.Connection, ref: str) -> dict[str, Any] | None:
        row = db.execute("SELECT * FROM eve_jobs WHERE reservation_ref=?", (ref,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["revision"] = digest([row["run_ref"], row["spec_digest"], row["version"]])
        result["spec"] = json.loads(row["spec"])
        return result

    def get(self, reservation_ref: str) -> dict[str, Any] | None:
        with self.transaction() as db:
            return self._row(db, reservation_ref)

    def admit(
        self, reservation_ref: str, spec: dict[str, Any], *, actor: str, decision_key: str
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[\w.-]{1,160}", decision_key, flags=re.ASCII):
            raise ValueError("invalid_eve_decision")
        now = time.time()
        if spec["reviewer"] != actor or spec["reservation_ref"] != reservation_ref:
            raise ValueError("wrong_eve_approval")
        if not spec["starts_at"] <= now < spec["deadline"]:
            raise ValueError("eve_window_closed")
        identity = digest(spec)
        with self.transaction() as db:
            prior = self._row(db, reservation_ref)
            if prior is not None:
                if prior["spec_digest"] != identity or prior["approver"] != actor:
                    raise ValueError("eve_reservation_conflict")
                return prior
            if db.execute(
                "SELECT 1 FROM eve_jobs WHERE state NOT IN ('completed','stopped','expired',"
                "'failed') LIMIT 1"
            ).fetchone():
                raise ValueError("eve_host_busy")
            db.execute(
                """INSERT INTO eve_jobs(reservation_ref,run_ref,spec,spec_digest,approver,
                   approval_key,state,version,total_steps,permit_until,deadline)
                   VALUES(?,?,?,?,?,?,'queued',0,?,?,?)""",
                (
                    reservation_ref,
                    run_ref(reservation_ref, spec),
                    json.dumps(spec, sort_keys=True),
                    identity,
                    actor,
                    decision_key,
                    spec["steps"],
                    min(now + LEASE_SECONDS, spec["deadline"]),
                    spec["deadline"],
                ),
            )
            row = self._row(db, reservation_ref)
            assert row is not None
            return row

    def renew(self, reservation_ref: str, spec_digest: str) -> None:
        now = time.time()
        with self.transaction() as db:
            db.execute(
                "UPDATE eve_jobs SET permit_until=min(deadline,?) WHERE reservation_ref=? "
                "AND spec_digest=? AND permit_until>? AND deadline>?",
                (now + LEASE_SECONDS, reservation_ref, spec_digest, now, now),
            )

    def control(self, reservation_ref: str, action: str, revision: str, key: str) -> dict[str, Any]:
        with self.transaction() as db:
            row = self._row(db, reservation_ref)
            if row is None or action not in CONTROL_FROM or not key or len(key) > 160:
                raise ValueError("invalid_eve_control")
            previous = db.execute(
                "SELECT action,revision FROM eve_controls WHERE reservation_ref=? AND key=?",
                (reservation_ref, key),
            ).fetchone()
            if previous:
                if tuple(previous) != (action, revision):
                    raise ValueError("eve_control_conflict")
                return row
            now = time.time()
            if (
                row["revision"] != revision
                or row["state"] not in CONTROL_FROM[action]
                or now >= row["permit_until"]
                or now - row["heartbeat"] > STALE_SECONDS
                or (action == "resume" and row["checkpoint_step"] is None)
            ):
                raise ValueError("stale_eve_control")
            db.execute(
                "INSERT INTO eve_controls VALUES(?,?,?,?)", (reservation_ref, key, action, revision)
            )
            db.execute(
                "UPDATE eve_jobs SET requested=?,state=?,version=version+1 WHERE reservation_ref=?",
                (action, PENDING[action], reservation_ref),
            )
            out = self._row(db, reservation_ref)
            assert out is not None
            return out

    def active(self) -> dict[str, Any] | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT reservation_ref FROM eve_jobs WHERE state NOT IN "
                "('completed','stopped','expired','failed') LIMIT 1"
            ).fetchone()
            return self._row(db, row[0]) if row else None

    def unreported(self) -> list[dict[str, Any]]:
        with self.transaction() as db:
            refs = db.execute(
                "SELECT reservation_ref FROM eve_jobs WHERE reported=0 AND state IN "
                "('completed','stopped','expired','failed') ORDER BY rowid"
            ).fetchall()
            return [row for ref in refs if (row := self._row(db, ref[0])) is not None]

    def mark_reported(self, row: dict[str, Any]) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE eve_jobs SET reported=1 WHERE reservation_ref=? AND version=?",
                (row["reservation_ref"], row["version"]),
            )

    def begin_launch(self, row: dict[str, Any]) -> bool:
        now = time.time()
        with self.transaction() as db:
            changed = db.execute(
                "UPDATE eve_jobs SET launched=version,launched_at=?,heartbeat=? "
                "WHERE reservation_ref=? "
                "AND version=? AND launched!=version AND permit_until>? AND deadline>? "
                "AND state IN ('queued','resuming')",
                (now, now, row["reservation_ref"], row["version"], now, now),
            )
            return changed.rowcount == 1

    def observe(
        self,
        row: dict[str, Any],
        state: str,
        *,
        requested: str | None = None,
        step: int | None = None,
        checkpoint: int | None = None,
        seconds: int | None = None,
        trainer_id: str | None = None,
    ) -> None:
        # A Slack click may land between observation and acknowledgement. Its revision wins.
        if state not in TERMINAL | {
            "running",
            "queued",
            "pausing",
            "paused",
            "resuming",
            "stopping",
        }:
            raise ValueError("unknown_eve_state")
        with self.transaction() as db:
            db.execute(
                """UPDATE eve_jobs SET state=?,version=version+(state!=?),requested=?,
                   completed_steps=coalesce(?,completed_steps),checkpoint_step=coalesce(?,checkpoint_step),
                   active_seconds=coalesce(?,active_seconds),trainer_id=coalesce(?,trainer_id),
                   heartbeat=? WHERE reservation_ref=? AND version=?""",
                (
                    state,
                    state,
                    requested,
                    step,
                    checkpoint,
                    seconds,
                    trainer_id,
                    time.time(),
                    row["reservation_ref"],
                    row["version"],
                ),
            )
