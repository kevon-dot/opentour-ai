"""Fenced fit-dispatch reservations in the existing customer-local mandate database."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, replace

from opentour_cell.pilot import fit_dispatch as fd
from opentour_cell.pilot.learning_mandate import LearningMandates
from opentour_cell.pilot.operating_point import canonical, digest


class MandateFitLedger:
    def __init__(self, mandates: LearningMandates) -> None:
        self.mandates = mandates
        with mandates._db() as db:
            for statement in (
                """CREATE TABLE IF NOT EXISTS dispatch_fits(
                    scope TEXT NOT NULL, id TEXT NOT NULL, mandate TEXT NOT NULL,
                    run TEXT NOT NULL, budget_scope TEXT NOT NULL, record TEXT NOT NULL,
                    execution TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY(scope,id))""",
                """CREATE INDEX IF NOT EXISTS dispatch_budget
                   ON dispatch_fits(scope,budget_scope)""",
                """CREATE TABLE IF NOT EXISTS dispatch_aliases(scope TEXT NOT NULL,
                   actor TEXT NOT NULL, client TEXT NOT NULL, operation_key TEXT NOT NULL,
                   id TEXT NOT NULL, PRIMARY KEY(scope,actor,client,operation_key))""",
                """CREATE INDEX IF NOT EXISTS dispatch_alias_target
                   ON dispatch_aliases(scope,id)""",
                """CREATE TABLE IF NOT EXISTS dispatch_events(scope TEXT NOT NULL, id TEXT NOT NULL,
                   version INTEGER NOT NULL, event TEXT NOT NULL, stage TEXT NOT NULL,
                   at REAL NOT NULL, PRIMARY KEY(scope,id,version))""",
            ):
                db.execute(statement)

    def _entry(self, db: sqlite3.Connection, key: str) -> fd.LedgerEntry | None:
        row = db.execute(
            "SELECT * FROM dispatch_fits WHERE scope=? AND id=?", (self.mandates.scope, key)
        ).fetchone()
        if row is None:
            return None
        data = json.loads(row["execution"])
        data["stage"] = fd.Stage(data["stage"])
        aliases = db.execute(
            "SELECT count(*) FROM dispatch_aliases WHERE scope=? AND id=?",
            (self.mandates.scope, key),
        ).fetchone()[0]
        return fd.LedgerEntry(
            key,
            json.loads(row["record"]),
            fd.Execution(**data),
            row["created"],
            row["updated"],
            aliases=aliases,
        )

    def get(self, execution_id: str) -> fd.LedgerEntry | None:
        with self.mandates._db(write=False) as db:
            return self._entry(db, execution_id)

    def _required(self, db: sqlite3.Connection, key: str) -> fd.LedgerEntry:
        entry = self._entry(db, key)
        if entry is None:
            raise fd.DispatchError("execution_unavailable")
        return entry

    def reserve(
        self,
        record: fd.Json,
        *,
        client: str,
        operation_key: str,
        recheck: Callable[[fd.LedgerUsage], None],
    ) -> fd.LedgerEntry:
        m = self.mandates
        key = record["execution_id"]
        if (record["tenant"], record["engagement"]) != (m.organization, m.engagement):
            raise fd.DispatchError("budget_scope_mismatch")
        with m._authorized_db() as db:
            row, spec = m._mandate(db, record["mandate_id"])
            m._active(row, spec, record["actor"])
            scope = "budget-" + digest([m.scope, m._lineage_root(db, row["id"])])[:40]
            if record["mandate_revision"] != row["revision"] or record["budget_scope"] != scope:
                raise fd.DispatchError("mandate_revision_changed")
            aliases = (m.scope, record["actor"], client, operation_key)
            alias = db.execute(
                "SELECT id FROM dispatch_aliases WHERE "
                "(scope,actor,client,operation_key)=(?,?,?,?)",
                aliases,
            ).fetchone()
            if (
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='fit_operation_aliases'"
                ).fetchone()
                and db.execute(
                    "SELECT 1 FROM fit_operation_aliases WHERE (scope,actor,client,operation_key)"
                    "=(?,?,?,?)",
                    aliases,
                ).fetchone()
            ):
                raise fd.DispatchError("fit_operation_key_conflict")
            entry = self._entry(db, key)
            if alias and (
                alias["id"] != key
                or not entry
                or entry.record["intent_digest"] != record["intent_digest"]
            ):
                raise fd.DispatchError("fit_operation_key_conflict")
            if entry and entry.record["intent_digest"] != record["intent_digest"]:
                raise fd.DispatchError("fit_intent_conflict")
            if not entry:
                if db.execute(
                    "SELECT 1 FROM reservations WHERE scope=? AND mandate=? AND "
                    "(run=? OR json_extract(request,'$.plan_revision')=?) ",
                    (
                        m.scope,
                        row["id"],
                        record["run_id"],
                        record["plan_revision"],
                    ),
                ).fetchone():
                    raise fd.DispatchError("fit_intent_conflict")
                usage = db.execute(
                    """SELECT count(*),
                    coalesce(sum(json_extract(execution,'$.stage') NOT IN ('settled','held')),0),
                    coalesce(sum(json_extract(record,'$.bound.maximum_seconds')),0),
                    coalesce(sum(json_extract(execution,'$.charged_minor_units')),0)
                    FROM dispatch_fits WHERE scope=? AND budget_scope=?""",
                    (m.scope, scope),
                ).fetchone()
                recheck(fd.LedgerUsage(scope, usage[1], usage[0], usage[2], usage[3]))
                now = time.time()
                execution = fd.Execution(
                    fd.Stage.RESERVED, 1, charged_minor_units=record["bound"]["maximum_minor_units"]
                )
                db.execute(
                    "INSERT INTO dispatch_fits VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        m.scope,
                        key,
                        row["id"],
                        record["run_id"],
                        scope,
                        canonical(record),
                        canonical(asdict(execution)),
                        now,
                        now,
                    ),
                )
                db.execute(
                    "INSERT INTO dispatch_events VALUES(?,?,?,?,?,?)",
                    (m.scope, key, 1, "reserved", "reserved", now),
                )
            if not alias:
                db.execute("INSERT INTO dispatch_aliases VALUES(?,?,?,?,?)", (*aliases, key))
            return replace(self._required(db, key), replayed=entry is not None)

    def _save(
        self, db: sqlite3.Connection, key: str, execution: fd.Execution, event: str
    ) -> fd.LedgerEntry:
        now = time.time()
        db.execute(
            "UPDATE dispatch_fits SET execution=?,updated=? WHERE scope=? AND id=?",
            (canonical(asdict(execution)), now, self.mandates.scope, key),
        )
        db.execute(
            "INSERT INTO dispatch_events VALUES(?,?,?,?,?,?)",
            (self.mandates.scope, key, execution.version, event, execution.stage, now),
        )
        return self._required(db, key)

    def transition(
        self, execution_id: str, *, version: int, execution: fd.Execution, event: str
    ) -> fd.LedgerEntry:
        with self.mandates._db() as db:
            prior = self._required(db, execution_id).execution
            if version != prior.version:
                raise fd.DispatchError("ledger_version_conflict")
            if (
                execution.stage not in fd.TRANSITIONS[prior.stage]
                or execution.charged_minor_units < prior.charged_minor_units
                or execution.submissions < prior.submissions
                or (prior.job_ref and execution.job_ref != prior.job_ref)
                or execution.settlement is not None
            ):
                raise fd.DispatchError("ledger_transition_refused")
            return self._save(db, execution_id, replace(execution, version=version + 1), event)

    def settle(self, execution_id: str, *, version: int, settlement: fd.Json) -> fd.LedgerEntry:
        with self.mandates._db() as db:
            entry = self._required(db, execution_id)
            prior = entry.execution
            if prior.stage == fd.Stage.SETTLED:
                if prior.settlement != settlement:
                    raise fd.DispatchError("settlement_conflict")
                return entry
            if version != prior.version:
                raise fd.DispatchError("ledger_version_conflict")
            if prior.stage not in fd.OPEN_STAGES:
                raise fd.DispatchError("ledger_transition_refused")
            settled = replace(
                prior,
                stage=fd.Stage.SETTLED,
                version=version + 1,
                settlement=settlement,
                lease_owner="",
                lease_until=0,
            )
            return self._save(db, execution_id, settled, "settled")
