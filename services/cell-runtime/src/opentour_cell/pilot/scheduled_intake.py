"""Bounded local admission and status reconciliation; no source reader dispatch."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from opentour_cell.pilot.intake_local import open_intake
from opentour_cell.pilot.intake_mcp import (
    AdmissionHold,
    AdmissionReceipt,
    CatalogFeed,
    CatalogSource,
    HistoricalPair,
    IntakeError,
    IntakePrincipal,
    parse_intake,
    require_ref,
)
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.readout import ManifestError
from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake

SCHEMA = "opentour.pilot.scheduled-intake.v1"
_FIELDS = {
    "schema_version",
    "installation_ref",
    "tenant",
    "actor",
    "selection_file",
    "selection_sha256",
    "enabled",
    "expires_at",
    "timezone",
    "frequency_seconds",
    "page_size",
    "pages_per_tick",
    "operations_per_tick",
    "monthly_operations",
    "tick_seconds",
    "historical_intent",
}
_STATES = {"enabled", "paused", "revoked"}


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _need(value: object, code: str = "schedule_unavailable") -> None:
    if not value:
        raise IntakeError(code)


def _config(path: Path) -> dict[str, Any]:
    body = _read_private(path, 8192)
    _need(set(body) == _FIELDS and body["schema_version"] == SCHEMA)
    for name in ("installation_ref", "tenant", "actor"):
        require_ref(body[name])
    _need(isinstance(body["selection_file"], str))
    _need(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,80}\.json", body["selection_file"]))
    _need(
        isinstance(body["selection_sha256"], str)
        and re.fullmatch(r"[0-9a-f]{64}", body["selection_sha256"])
    )
    _need(type(body["enabled"]) is bool)
    for name, minimum, maximum in (
        ("expires_at", 1, 2**63 - 1),
        ("frequency_seconds", 1, 2678400),
        ("page_size", 1, 50),
        ("pages_per_tick", 1, 32),
        ("operations_per_tick", 1, 1000),
        ("monthly_operations", 1, 1000000),
        ("tick_seconds", 1, 300),
    ):
        _need(type(body[name]) is int and minimum <= body[name] <= maximum)
    _need(isinstance(body["timezone"], str) and len(body["timezone"]) <= 128)
    ZoneInfo(body["timezone"])
    historical = body["historical_intent"]
    if historical is not None:
        _need(isinstance(historical, dict))
        _need(set(historical) == set(HistoricalPair.__dataclass_fields__) - {"idempotency_key"})
        _need(
            isinstance(parse_intake({**historical, "idempotency_key": "validate"}), HistoricalPair)
        )
    return body


def deterministic_key(tenant: str, installation: str, intent: dict[str, str]) -> str:
    """The installation and immutable intent identify a retry, never a tick/cursor."""
    require_ref(tenant)
    require_ref(installation)
    return "schedule-" + _digest([tenant, installation, intent])


class ScheduledIntake:
    """A local restriction layered over, never a replacement for, source authority.

    An immutable private configuration selects an existing intake factory. The
    existing source grant is checked by that factory and by each admission. Local
    controls serialize with ticks. Source-page cursors are owned by the reader;
    this journal tracks only discovery pages and acknowledged admission receipts.
    """

    def __init__(self, configuration: Path):
        self.configuration = configuration
        _need(
            configuration.is_absolute()
            and configuration.parent.resolve(strict=True) == configuration.parent
        )
        self.config = _config(configuration)
        self.revision = _digest(self.config)
        self.identity = _digest([self.config["tenant"], self.config["installation_ref"]])
        self.directory = configuration.parent
        self.path = self.directory / "scheduled-intake.sqlite3"

    def _private_fd(self, path: Path) -> int:
        info = self.directory.stat()
        _need(
            stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077
        )
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            _need(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and info.st_nlink == 1
                and not info.st_mode & 0o077
            )
            _need(os.path.samestat(info, path.lstat()))
        except BaseException:
            os.close(fd)
            raise
        return fd

    @contextmanager
    def _lease(self) -> Iterator[None]:
        # One installation per worker is deliberately conservative. It also
        # prevents local controls overtaking an in-flight admission acknowledgement.
        fd = self._private_fd(self.directory / "scheduled-intake.lock")
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise IntakeError("schedule_busy") from error
            self._current()
            yield
        finally:
            os.close(fd)

    def _current(self) -> None:
        _need(_config(self.configuration) == self.config, "schedule_configuration_changed")
        selected = _read_private(self.directory / self.config["selection_file"], 4096)
        _need(
            _digest(selected) == self.config["selection_sha256"], "schedule_configuration_changed"
        )

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        fd = self._private_fd(self.path)
        os.close(fd)
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
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

    def _initialize(self) -> None:
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS schedule_heads (
                identity TEXT PRIMARY KEY, revision TEXT NOT NULL, state TEXT NOT NULL,
                cursor TEXT, page_loaded INTEGER NOT NULL, next_cursor TEXT,
                pending TEXT NOT NULL, last_start REAL NOT NULL, rescan INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS schedule_usage (
                identity TEXT NOT NULL, month TEXT NOT NULL, operations INTEGER NOT NULL,
                PRIMARY KEY(identity, month))""")
            db.execute("""CREATE TABLE IF NOT EXISTS schedule_ticks (
                receipt_ref TEXT PRIMARY KEY, identity TEXT NOT NULL, state TEXT NOT NULL,
                operations INTEGER NOT NULL, pages INTEGER NOT NULL, runs TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS schedule_dispositions (
                identity TEXT NOT NULL, intent TEXT NOT NULL, request TEXT NOT NULL,
                reason TEXT NOT NULL, successor_ref TEXT, PRIMARY KEY(identity,intent))""")
            db.execute("""CREATE TABLE IF NOT EXISTS schedule_restarts (
                identity TEXT NOT NULL, cursor TEXT NOT NULL, attempts INTEGER NOT NULL,
                PRIMARY KEY(identity,cursor))""")
            db.execute("""CREATE TABLE IF NOT EXISTS schedule_work (
                identity TEXT NOT NULL, run_ref TEXT NOT NULL, request TEXT NOT NULL,
                admitted_state TEXT NOT NULL, observed_state TEXT NOT NULL,
                observed_at REAL NOT NULL, availability TEXT NOT NULL,
                checked_at REAL NOT NULL, checks INTEGER NOT NULL,
                PRIMARY KEY(identity,run_ref))""")
            row: sqlite3.Row | None = db.execute(
                "SELECT revision FROM schedule_heads WHERE identity=?", (self.identity,)
            ).fetchone()
            if row is None:
                if self.config["enabled"]:
                    # Do not bind a fresh installation irreversibly to a mistyped actor.
                    self._factory()
                db.execute(
                    "INSERT INTO schedule_heads VALUES (?,?,?,NULL,0,NULL,'[]',0,0)",
                    (
                        self.identity,
                        self.revision,
                        "enabled" if self.config["enabled"] else "paused",
                    ),
                )
            else:
                _need(row[0] == self.revision, "schedule_configuration_changed")

    def _head(self) -> sqlite3.Row:
        with self._db() as db:
            row: sqlite3.Row | None = db.execute(
                "SELECT * FROM schedule_heads WHERE identity=?", (self.identity,)
            ).fetchone()
            if row is None:
                raise IntakeError("schedule_unavailable")
            return row

    def _factory(self) -> tuple[IntakePrincipal, BenchmarkIntake]:
        actor, admission = open_intake(self.directory / self.config["selection_file"])
        _need(actor == IntakePrincipal(self.config["tenant"], self.config["actor"], "intake"))
        return actor, admission

    def control(self, state: str) -> dict[str, str]:
        """Local pause/revoke/resume; revocation is terminal for this installation."""
        _need(state in _STATES)
        with self._lease():
            self._initialize()
            previous = self._head()["state"]
            _need(previous != "revoked" or state == "revoked")
            if state == "enabled":
                _need(self.config["enabled"] and time.time() < self.config["expires_at"])
                self._factory()  # Resuming cannot resurrect withdrawn source authority.
            with self._db() as db:
                db.execute(
                    "UPDATE schedule_heads SET state=? WHERE identity=?", (state, self.identity)
                )
            return self._finish(state, 0, 0, [])

    def _finish(self, state: str, operations: int, pages: int, runs: list[str]) -> dict[str, str]:
        reference = "tick-" + secrets.token_hex(16)
        with self._db() as db:
            db.execute(
                "INSERT INTO schedule_ticks VALUES (?,?,?,?,?,?)",
                (reference, self.identity, state, operations, pages, _canonical(runs)),
            )
        # This is the entire transport-visible envelope. Opaque source metadata,
        # internal errors, file names, payloads and cursors are never emitted.
        return {"state": state, "receipt_ref": reference}

    def _operation_gate(self, deadline: float) -> str | None:
        self._current()
        if time.time() >= self.config["expires_at"]:
            return "expired"
        if time.monotonic() >= deadline:
            return "timeout"
        return None

    def _reserve(self, used: int, deadline: float) -> str | None:
        with self._db() as db:
            held = self._operation_gate(deadline)
            if held:
                return held
            if used >= self.config["operations_per_tick"]:
                return "quota_held"
            # Select the period after acquiring the budget writer, not before a wait.
            month = datetime.fromtimestamp(time.time(), ZoneInfo(self.config["timezone"])).strftime(
                "%Y-%m"
            )
            db.execute(
                "INSERT OR IGNORE INTO schedule_usage VALUES (?,?,0)", (self.identity, month)
            )
            previous = db.execute(
                "SELECT operations FROM schedule_usage WHERE identity=? AND month=?",
                (self.identity, month),
            ).fetchone()[0]
            if previous >= self.config["monthly_operations"]:
                return "quota_held"
            # Charge before attempting the local operation. A crash or ambiguous
            # acknowledgement cannot refund it and permit unbounded retries.
            db.execute(
                "UPDATE schedule_usage SET operations=operations+1 WHERE identity=? AND month=?",
                (self.identity, month),
            )
        return None

    def _envelope(self, entry: CatalogFeed | CatalogSource) -> dict[str, str]:
        if isinstance(entry, CatalogFeed):
            _need(self.config["historical_intent"] is None)
            intent = {
                "mode": "live",
                "feed_binding_ref": entry.feed_binding_ref,
                "binding_version": entry.binding_version,
                "source_revision": entry.source_revision,
            }
            key = deterministic_key(self.config["tenant"], self.config["installation_ref"], intent)
            return {key: value for key, value in intent.items() if key != "binding_version"} | {
                "idempotency_key": key
            }
        historical = self.config["historical_intent"]
        _need(entry.source_kind == "historical" and isinstance(historical, dict))
        _need(
            entry.source_ref == historical["pair_ref"]
            and entry.source_revision == historical["source_revision"]
        )
        intent = {"mode": "historical", **historical}
        return {
            **intent,
            "idempotency_key": deterministic_key(
                self.config["tenant"], self.config["installation_ref"], intent
            ),
        }

    def _ack(self, pending: list[dict[str, str]], result: AdmissionReceipt) -> None:
        with self._db() as db:
            request = _canonical(pending[0])
            prior = db.execute(
                "SELECT request FROM schedule_work WHERE identity=? AND run_ref=?",
                (self.identity, result.run_ref),
            ).fetchone()
            _need(prior is None or prior["request"] == request)
            db.execute(
                "INSERT OR IGNORE INTO schedule_work VALUES (?,?,?,?,?,?,?,?,0)",
                (
                    self.identity,
                    result.run_ref,
                    request,
                    result.state,
                    result.state,
                    time.time(),
                    "available",
                    time.time(),
                ),
            )
            # Handoff and admission ACK commit together. A lost tick receipt must
            # never lose the acknowledged run or imply that a reader completed it.
            db.execute(
                "UPDATE schedule_heads SET pending=? WHERE identity=?",
                (_canonical(pending[1:]), self.identity),
            )

    def _superseded(self, pending: list[dict[str, str]], hold: AdmissionHold) -> None:
        # An explicit trusted disposition is NOT an admission acknowledgement.
        # Retain the exact unadmitted request, and discover successors afresh;
        # the hint itself never becomes a new request or grants authority.
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO schedule_dispositions VALUES (?,?,?,?,?)",
                (
                    self.identity,
                    _digest(pending[0]),
                    _canonical(pending[0]),
                    "binding_superseded",
                    hold.successor_ref,
                ),
            )
            db.execute(
                "UPDATE schedule_heads SET pending=?,rescan=1 WHERE identity=?",
                (_canonical(pending[1:]), self.identity),
            )

    def _restart(self, cursor: str) -> None:
        with self._db() as db:
            db.execute(
                "INSERT INTO schedule_restarts VALUES (?,?,1) "
                "ON CONFLICT(identity,cursor) DO UPDATE SET attempts=attempts+1",
                (self.identity, cursor),
            )
            # Retain the cursor until a fresh authorized page succeeds. A generic
            # denial is not called cursor expiry, and cannot promote a successor.
            db.execute("UPDATE schedule_heads SET rescan=1 WHERE identity=?", (self.identity,))

    def tick(self) -> dict[str, str]:
        with self._lease():
            self._initialize()
            head = self._head()
            if head["state"] != "enabled":
                return self._finish(str(head["state"]), 0, 0, [])
            if time.time() >= self.config["expires_at"]:
                return self._finish("expired", 0, 0, [])
            if time.time() < head["last_start"] + self.config["frequency_seconds"]:
                return self._finish("cadence_held", 0, 0, [])
            deadline = time.monotonic() + self.config["tick_seconds"]
            with self._db() as db:
                db.execute(
                    "UPDATE schedule_heads SET last_start=? WHERE identity=?",
                    (time.time(), self.identity),
                )
            used, pages = 0, 0
            runs: list[str] = []
            states: list[str] = []
            disposed = False
            restarted = bool(head["rescan"])
            try:
                actor, admission = self._factory()
                while True:
                    if time.monotonic() >= deadline:
                        return self._finish("timeout", used, pages, runs)
                    head = self._head()
                    pending: list[dict[str, str]] = json.loads(head["pending"])
                    if pending:
                        held = self._reserve(used, deadline)
                        if held:
                            return self._finish(held, used, pages, runs)
                        used += 1
                        held = self._operation_gate(deadline)
                        if held:
                            return self._finish(held, used, pages, runs)
                        result = admission.submit_intake(actor, parse_intake(pending[0]))
                        if isinstance(result, AdmissionHold):
                            if result.reason_code == "binding_superseded":
                                self._superseded(pending, result)
                                disposed = True
                                continue
                            return self._finish("held", used, pages, runs)
                        _need(
                            isinstance(result, AdmissionReceipt)
                            and result.state in {"pending", "queued", "materialized"}
                        )
                        runs.append(require_ref(result.run_ref))
                        states.append(result.state)
                        self._ack(pending, result)  # A pre-commit crash replays the same intent.
                        continue
                    if head["page_loaded"]:
                        with self._db() as db:
                            db.execute(
                                "UPDATE schedule_heads SET cursor=next_cursor,page_loaded=0,"
                                "next_cursor=NULL WHERE identity=?",
                                (self.identity,),
                            )
                        if head["next_cursor"] is None and not head["rescan"]:
                            state = (
                                "held"
                                if disposed
                                else "pending"
                                if "pending" in states
                                else "admitted"
                                if runs
                                else "idle"
                            )
                            return self._finish(state, used, pages, runs)
                    if pages >= self.config["pages_per_tick"]:
                        return self._finish("page_limit", used, pages, runs)
                    held = self._reserve(used, deadline)
                    if held:
                        return self._finish(held, used, pages, runs)
                    used += 1
                    head = self._head()
                    cursor = None if head["rescan"] else head["cursor"]
                    held = self._operation_gate(deadline)
                    if held:
                        return self._finish(held, used, pages, runs)
                    pages += 1
                    try:
                        page = admission.discover_sources(
                            actor, limit=self.config["page_size"], cursor=cursor
                        )
                    except IntakeError:
                        if cursor is not None and not restarted:
                            self._restart(cursor)
                            restarted = True
                            continue
                        raise
                    _need(page.next_cursor is None or require_ref(page.next_cursor) != cursor)
                    envelopes = [self._envelope(entry) for entry in page.sources]
                    with self._db() as db:
                        db.execute(
                            "UPDATE schedule_heads SET pending=?,page_loaded=1,next_cursor=?,"
                            "cursor=CASE WHEN rescan=1 THEN NULL ELSE cursor END,rescan=0 "
                            "WHERE identity=?",
                            (_canonical(envelopes), page.next_cursor, self.identity),
                        )
            except IntakeError:
                # No pending envelope or cursor is discarded on scoped denial.
                return self._finish("unavailable", used, pages, runs)

    def reconcile(self) -> dict[str, str]:
        """Observe retained admissions through the existing authorized status API.

        This neither starts a reader nor accepts a caller's completion claim.
        Last successful observations remain historical on an unavailable readback.
        """
        with self._lease():
            self._initialize()
            head = self._head()
            if head["state"] != "enabled":
                return self._finish(str(head["state"]), 0, 0, [])
            if time.time() >= self.config["expires_at"]:
                return self._finish("expired", 0, 0, [])
            if time.time() < head["last_start"] + self.config["frequency_seconds"]:
                return self._finish("cadence_held", 0, 0, [])
            deadline = time.monotonic() + self.config["tick_seconds"]
            with self._db() as db:
                db.execute(
                    "UPDATE schedule_heads SET last_start=? WHERE identity=?",
                    (time.time(), self.identity),
                )
                work = db.execute(
                    "SELECT run_ref FROM schedule_work WHERE identity=? "
                    "AND observed_state!='materialized' ORDER BY checks,run_ref LIMIT ?",
                    (self.identity, self.config["operations_per_tick"]),
                ).fetchall()
            if not work:
                return self._finish("idle", 0, 0, [])
            used = 0
            runs: list[str] = []
            unavailable = False
            try:
                actor, admission = self._factory()
                for row in work:
                    held = self._reserve(used, deadline)
                    if held:
                        return self._finish(held, used, 0, runs)
                    used += 1
                    held = self._operation_gate(deadline)
                    if held:
                        return self._finish(held, used, 0, runs)
                    run_ref = str(row["run_ref"])
                    try:
                        status = admission.get_run(actor, run_ref)
                        _need(
                            status.run_ref == run_ref
                            and status.state in {"pending", "queued", "materialized"}
                        )
                    except (
                        IntakeError,
                        OSError,
                        RuntimeError,
                        sqlite3.Error,
                        ManifestError,
                        TrainingError,
                        KeyError,
                        ValueError,
                    ):
                        # Unknown is not completion or revocation. Retain the
                        # previous observation and give other current work a turn.
                        with self._db() as db:
                            db.execute(
                                "UPDATE schedule_work SET availability='unavailable',"
                                "checked_at=?,checks=checks+1 WHERE identity=? AND run_ref=?",
                                (time.time(), self.identity, run_ref),
                            )
                        unavailable = True
                        continue
                    with self._db() as db:
                        db.execute(
                            "UPDATE schedule_work SET observed_state=?,observed_at=?,"
                            "availability='available',checked_at=?,checks=checks+1 "
                            "WHERE identity=? AND run_ref=?",
                            (status.state, time.time(), time.time(), self.identity, run_ref),
                        )
                    runs.append(run_ref)
                with self._db() as db:
                    remaining = db.execute(
                        "SELECT 1 FROM schedule_work WHERE identity=? "
                        "AND observed_state!='materialized' LIMIT 1",
                        (self.identity,),
                    ).fetchone()
                held = self._operation_gate(deadline)
                return self._finish(
                    held
                    or ("unavailable" if unavailable else "pending" if remaining else "reconciled"),
                    used,
                    0,
                    runs,
                )
            except IntakeError:
                return self._finish("unavailable", used, 0, runs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument("action", choices=("tick", "reconcile", "pause", "resume", "revoke"))
    args = parser.parse_args()
    try:
        schedule = ScheduledIntake(args.configuration)
        result = (
            schedule.tick()
            if args.action == "tick"
            else schedule.reconcile()
            if args.action == "reconcile"
            else schedule.control(
                {"pause": "paused", "resume": "enabled", "revoke": "revoked"}[args.action]
            )
        )
    except (
        IntakeError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        ManifestError,
        TrainingError,
        KeyError,
        ValueError,
    ):
        # Paths, grants and provider exception strings never become task history.
        result = {"state": "unavailable"}
    print(_canonical(result))
    return 0 if result["state"] not in {"unavailable"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
