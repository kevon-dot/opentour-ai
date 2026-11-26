"""Stop one owner-local engagement and track the custodian's private closeout evidence."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from opentour_cell.pilot.slack_adapter import withdraw_for_custody
from opentour_cell.pilot.training import LocalTrainingStore, _check_directory, _check_file
from opentour_cell.pilot.training_data import identifier

TRIGGERS = ("withdrawal", "revocation", "retention_due", "completed_readout")
KINDS = ("inventory", "support", "local", "receipt", "destination", "source", "backup", "grant")
OPEN = {"unresolved", "recipient_followup", "agreed_retention"}
RESOLVED = {
    "confirmed_deleted",
    "confirmed_returned",
    "confirmed_expiry",
    "custody_transferred",
    "inventory_verified",
    "handoff_confirmed",
    "live_rows_removed",
}
LOCAL = {"inputs", "heldout", "models", "outputs"}
_EXPORT_OBSERVATIONS = (
    "slack_exposed",
    "slack_possible",
    "zapier_accepted",
    "zapier_uncertain",
    "legacy_unknown",
)
_ZERO_EXPORT_RECEIPT = "zero_exports"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS closeout (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), organization TEXT NOT NULL,
 engagement TEXT NOT NULL, agreement TEXT NOT NULL, custodian TEXT NOT NULL,
 support TEXT NOT NULL, due TEXT NOT NULL, revision TEXT, trigger TEXT NOT NULL,
 withdrawn_at INTEGER NOT NULL, reconciled INTEGER NOT NULL DEFAULT 0, finished_ref TEXT,
 transport TEXT NOT NULL DEFAULT '{}', minimized INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS items (
 item TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL, due TEXT NOT NULL,
 disposition TEXT NOT NULL, evidence TEXT, updated_at INTEGER NOT NULL,
 observed INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT ''
);
"""


class CustodyError(ValueError):
    """A fixed local operator diagnostic without customer content."""


def _date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise CustodyError("expected_iso_date") from None
    if parsed.isoformat() != value:
        raise CustodyError("expected_iso_date")
    return value


class Closeout:
    """One private directory and bound training engagement. No external requests."""

    def __init__(self, directory: Path, organization: str):
        if not directory.is_absolute():
            raise CustodyError("absolute_private_directory_required")
        self.organization = identifier(organization)
        self.directory = directory
        self.path = directory / "custody.sqlite3"
        self.training = directory / "training.sqlite3"
        _check_directory(self.path)

    @contextmanager
    def _db(self, *, create: bool = False) -> Iterator[sqlite3.Connection]:
        if create:
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            except FileExistsError:
                pass
        if not self.path.exists():
            raise CustodyError("closeout_not_open")
        _check_file(self.path)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            if create:
                db.executescript(_SCHEMA)
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db

    def _row(self, db: sqlite3.Connection) -> sqlite3.Row:
        row: sqlite3.Row | None = db.execute("SELECT * FROM closeout WHERE singleton=1").fetchone()
        if row is None:
            raise CustodyError("closeout_not_open")
        if row["organization"] != self.organization:
            raise CustodyError("organization_mismatch")
        return row

    @staticmethod
    def _minimal(row: sqlite3.Row) -> dict[str, Any]:
        receipt = {
            "organization": row["organization"],
            "engagement": row["engagement"],
            "state": "closed",
            "retained": "receipt_only",
            "closure_evidence": row["finished_ref"],
        }
        if json.loads(row["transport"]).get("export_disposition") == _ZERO_EXPORT_RECEIPT:
            receipt["export_disposition"] = _ZERO_EXPORT_RECEIPT
        return receipt

    @staticmethod
    def _zero_exports(db: sqlite3.Connection, delivery: dict[str, Any]) -> bool:
        # A zero in the worker journal alone cannot account for copies outside the worker.
        return all(
            type(delivery.get(key)) is int and delivery[key] == 0 for key in _EXPORT_OBSERVATIONS
        ) and bool(
            db.execute(
                "SELECT 1 FROM items WHERE item='inventory-review' AND kind='inventory'"
                " AND disposition='inventory_verified' AND evidence IS NOT NULL"
                " AND NOT EXISTS(SELECT 1 FROM items WHERE kind='destination')"
            ).fetchone()
        )

    def _store(self, row: sqlite3.Row) -> LocalTrainingStore:
        if not self.training.exists():
            raise CustodyError("training_hold_unverified")
        store = LocalTrainingStore.for_custody(
            self.training, self.organization, expected_engagement=row["engagement"]
        )
        # A custodian may have restored an older copy of the same bound store.
        store.withdraw()
        return store

    @staticmethod
    def _observe(
        db: sqlite3.Connection,
        item: str,
        kind: str,
        owner: str,
        due: str,
        observed: int = 1,
        detail: str = "",
    ) -> None:
        if type(observed) is not int or observed < 0:
            raise CustodyError("invalid_inventory")
        if not observed:
            return
        existing = db.execute(
            "SELECT observed,detail,disposition FROM items WHERE item=?", (item,)
        ).fetchone()
        if existing:
            appeared_again = kind in {"local", "receipt"} and existing["disposition"] in {
                "confirmed_deleted",
                "confirmed_returned",
                "confirmed_expiry",
                "live_rows_removed",
            }
            if (
                not appeared_again
                and observed <= existing["observed"]
                and detail == existing["detail"]
            ):
                return
        db.execute(
            "INSERT INTO items(item,kind,owner,due,disposition,updated_at,observed,detail)"
            " VALUES(?,?,?,?,'unresolved',?,?,?) ON CONFLICT(item) DO UPDATE SET"
            " disposition='unresolved',evidence=NULL,updated_at=excluded.updated_at,"
            " observed=excluded.observed,detail=excluded.detail",
            (item, kind, owner, due, int(time.time()), observed, detail),
        )
        db.execute("UPDATE closeout SET finished_ref=NULL WHERE singleton=1")

    def withdraw(
        self,
        *,
        agreement: str,
        custodian: str,
        support: str,
        due: str,
        trigger: str,
        revision: str | None = None,
    ) -> dict[str, Any]:
        for value in (agreement, custodian, support):
            identifier(value)
        if revision is not None:
            identifier(revision)
        if trigger not in TRIGGERS:
            raise CustodyError("invalid_trigger")
        _date(due)
        with self._db(create=True) as db:
            old = db.execute("SELECT * FROM closeout WHERE singleton=1").fetchone()
            binding = (self.organization, agreement, custodian, support, due, revision, trigger)
            if old:
                self._row(db)
                if old["minimized"]:
                    self._store(old)
                    return self._minimal(old)
                columns = (
                    "organization",
                    "agreement",
                    "custodian",
                    "support",
                    "due",
                    "revision",
                    "trigger",
                )
                if tuple(old[name] for name in columns) != binding:
                    raise CustodyError("closeout_scope_conflict")
                self._store(old)
            else:
                store = LocalTrainingStore.for_custody(self.training, self.organization)
                # Commit the domain admission hold first. Repeating after any interruption is safe.
                stopped = store.withdraw()
                db.execute(
                    "INSERT INTO closeout(singleton,organization,agreement,custodian,support,due,"
                    "revision,trigger,engagement,withdrawn_at) VALUES(1,?,?,?,?,?,?,?,?,?)",
                    (*binding, store.engagement_id, stopped["withdrawn_at"]),
                )
                self._observe(
                    db,
                    "inventory-review",
                    "inventory",
                    custodian,
                    due,
                    detail="Verify source files, exports, backups, recipients, grants, invitations "
                    "and separately running readers against the agreement.",
                )
                self._observe(
                    db,
                    "support-handoff",
                    "support",
                    support,
                    due,
                    detail="Confirm support contact, handoff date and outstanding duties.",
                )
        return self.reconcile()

    def reconcile(self) -> dict[str, Any]:
        # Persist the hold on closeout if a journal, training file or inventory cannot be verified.
        with self._db() as db:
            row = self._row(db)
            if row["minimized"]:
                self._store(row)
                return self._minimal(row)
            db.execute("UPDATE closeout SET reconciled=0 WHERE singleton=1")
        with self._db() as db:
            row = self._row(db)
            store = self._store(row)
            local = store.custody_inventory()
            outbound = withdraw_for_custody(self.directory, self.organization)
            custodian, due = row["custodian"], row["due"]
            for key, kind, present, detail in (
                (
                    "approval-requests",
                    "destination",
                    any(self.directory.glob("approval-ready-*.json")),
                    "Account for Slack or Zapier messages already admitted, any "
                    "pending decision and every recipient copy.",
                ),
                (
                    "source-intake-copies",
                    "source",
                    any(self.directory.glob("source-intake-*.json")),
                    "Reconcile trace and correction exports, protected answers, "
                    "development projections and source grants.",
                ),
                (
                    "workplace-snapshots",
                    "source",
                    (self.directory / "workplace-snapshots").exists(),
                    "Reconcile paired source versions, verified corrections, private "
                    "snapshots and their retention.",
                ),
                (
                    "workplace-permissions",
                    "grant",
                    any(self.directory.glob("workplace-source-*.json")),
                    "Revoke the workplace connection and account for evaluation, "
                    "training and delegated grants.",
                ),
                (
                    "autonomous-cycle-state",
                    "source",
                    any(self.directory.glob("cycle-*.json"))
                    or any(self.directory.glob("cycle-*.lock")),
                    "Account for scheduled cycles, privately staged plans, source "
                    "decisions and verifications.",
                ),
                (
                    "plugin-decisions",
                    "destination",
                    (self.directory / "zapier-decisions").exists()
                    or (self.directory / "zapier-reconciliation.sqlite3").exists()
                    or any(self.directory.glob("zapier-approval-za-*.json")),
                    "Reconcile signed decision returns, admitted challenges, "
                    "uncertain Zap runs and approved recipient copies.",
                ),
                (
                    "standing-mandate",
                    "grant",
                    (self.directory / "learning-mandates.sqlite3").exists(),
                    "Withdraw delegated training authority, reconcile outstanding fit "
                    "reservations, and retain the required audit tombstone.",
                ),
                (
                    "managed-repair-copies",
                    "source",
                    (self.directory / "managed-outputs").exists(),
                    "Reconcile run evidence, trained adapters, repair bundles, "
                    "retrieval keys and every approved recipient copy.",
                ),
                (
                    "model-permissions",
                    "grant",
                    any(self.directory.glob("model-grant-*.json")),
                    "Withdraw scoped fit permissions and confirm the disposition of "
                    "any previously copied artifacts.",
                ),
            ):
                if present:
                    self._observe(db, key, kind, custodian, due, detail=detail)
            for name in sorted(LOCAL):
                self._observe(db, "local-" + name, "local", custodian, due, local[name])
            journal_path = self.directory / "slack-pilot.sqlite3"
            if journal_path.exists():
                operations = outbound["local_journal_records"]
                self._observe(
                    db,
                    "local-handoff-journal",
                    "receipt",
                    custodian,
                    due,
                    max(1, operations),
                    f"historical operational records={operations}",
                )
            if outbound["legacy_unknown"]:
                self._observe(db, "historical-destinations", "destination", custodian, due)
            for kind, definite, possible in (
                ("slack", "slack_exposed", "slack_possible"),
                ("zapier", "zapier_accepted", "zapier_uncertain"),
            ):
                confirmed, uncertain = outbound[definite], outbound[possible]
                self._observe(
                    db,
                    kind + "-copies",
                    "destination",
                    custodian,
                    due,
                    confirmed + uncertain,
                    f"observed={confirmed},possible={uncertain}",
                )
            db.execute(
                "UPDATE closeout SET reconciled=1,transport=? WHERE singleton=1",
                (json.dumps(outbound, sort_keys=True),),
            )
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._db() as db:
            row = self._row(db)
            if row["minimized"]:
                return self._minimal(row)
            items = [dict(v) for v in db.execute("SELECT * FROM items ORDER BY item")]
            outstanding = sum(item["disposition"] not in RESOLVED for item in items)
            delivery = json.loads(row["transport"])
            closed = bool(row["finished_ref"] and row["reconciled"])
            report = {
                "organization": self.organization,
                "engagement": row["engagement"],
                "agreement": row["agreement"],
                "custodian": row["custodian"],
                "support_owner": row["support"],
                "due": row["due"],
                "revision": row["revision"],
                "trigger": row["trigger"],
                "withdrawn_at": row["withdrawn_at"],
                "reconciled": bool(row["reconciled"]),
                "delivery_inventory": delivery,
                "outstanding": outstanding,
                "state": "closed" if closed else "open",
                "closure_evidence": row["finished_ref"],
                "items": items,
            }
            if closed and self._zero_exports(db, delivery):
                report["export_disposition"] = _ZERO_EXPORT_RECEIPT
            return report

    def record(
        self,
        *,
        item: str,
        kind: str,
        disposition: str,
        owner: str,
        due: str,
        evidence: str | None = None,
    ) -> dict[str, Any]:
        identifier(item)
        identifier(owner)
        _date(due)
        if evidence is not None:
            identifier(evidence)
        allowed = OPEN | {
            "confirmed_deleted",
            "confirmed_returned",
            "confirmed_expiry",
            "custody_transferred",
        }
        if kind == "inventory":
            allowed = OPEN | {"inventory_verified"}
        elif kind == "support":
            allowed = OPEN | {"handoff_confirmed"}
        if kind not in KINDS or disposition not in allowed:
            raise CustodyError("invalid_disposition")
        if disposition not in {"unresolved", "recipient_followup"} and not evidence:
            raise CustodyError("custodian_evidence_required")
        if disposition == "confirmed_expiry" and date.fromisoformat(due) > datetime.now(UTC).date():
            raise CustodyError("expiry_not_due")
        with self._db() as db:
            row = self._row(db)
            if row["minimized"]:
                raise CustodyError("closed_receipt_only")
            prior = db.execute("SELECT * FROM items WHERE item=?", (item,)).fetchone()
            if prior and prior["kind"] != kind:
                raise CustodyError("item_kind_conflict")
            if not prior and (
                item.startswith("local-")
                or item in {"slack-copies", "zapier-copies", "historical-destinations"}
                or kind in {"inventory", "support", "local"}
            ):
                raise CustodyError("reserved_inventory_item")
            if kind == "local" and disposition in {
                "confirmed_deleted",
                "confirmed_expiry",
                "confirmed_returned",
            }:
                if self._store(row).custody_inventory()[item.removeprefix("local-")]:
                    raise CustodyError("live_data_still_present")
            if (
                item == "local-handoff-journal"
                and disposition in {"confirmed_deleted", "confirmed_expiry", "confirmed_returned"}
                and (self.directory / "slack-pilot.sqlite3").exists()
            ):
                raise CustodyError("journal_still_present")
            fact = (kind, owner, due, disposition, evidence)
            if (
                not prior
                or tuple(prior[k] for k in ("kind", "owner", "due", "disposition", "evidence"))
                != fact
            ):
                db.execute(
                    "INSERT INTO items(item,kind,owner,due,disposition,evidence,updated_at)"
                    " VALUES(?,?,?,?,?,?,?) ON CONFLICT(item) DO UPDATE SET owner=excluded.owner,"
                    " due=excluded.due,disposition=excluded.disposition,evidence=excluded.evidence,"
                    " updated_at=excluded.updated_at",
                    (item, *fact, int(time.time())),
                )
                db.execute("UPDATE closeout SET finished_ref=NULL WHERE singleton=1")
        return self.status()

    def purge_local(
        self, *, engagement: str, agreement: str, custodian: str, evidence: str
    ) -> dict[str, Any]:
        identifier(evidence)
        with self._db() as db:
            row = self._row(db)
            if (engagement, agreement, custodian) != (
                row["engagement"],
                row["agreement"],
                row["custodian"],
            ):
                raise CustodyError("purge_authority_mismatch")
            if not row["reconciled"]:
                raise CustodyError("reconcile_before_purge")
            store = self._store(row)
            remaining = store.purge_withdrawn()
            if any(remaining[name] != 0 for name in LOCAL):
                raise CustodyError("local_disposition_uncertain")
            result = db.execute(
                "UPDATE items SET disposition='live_rows_removed',evidence=?,owner=?,updated_at=?"
                " WHERE kind='local' AND disposition!='live_rows_removed'",
                (evidence, custodian, int(time.time())),
            )
            if result.rowcount:
                db.execute("UPDATE closeout SET finished_ref=NULL WHERE singleton=1")
        return self.status()

    def finish(
        self, *, custodian: str, evidence: str, receipt_only: bool = False
    ) -> dict[str, Any]:
        identifier(evidence)
        self.reconcile()
        with self._db() as db:
            row = self._row(db)
            if row["minimized"]:
                if row["finished_ref"] != evidence:
                    raise CustodyError("closure_receipt_conflict")
                return self._minimal(row)
            if row["custodian"] != custodian:
                raise CustodyError("custodian_mismatch")
            if row["finished_ref"] and row["finished_ref"] != evidence:
                raise CustodyError("closure_receipt_conflict")
            pending = db.execute("SELECT disposition FROM items").fetchall()
            if not row["reconciled"] or any(item[0] not in RESOLVED for item in pending):
                raise CustodyError("closeout_obligations_open")
            db.execute("UPDATE closeout SET finished_ref=? WHERE singleton=1", (evidence,))
            if receipt_only:
                # The agreement controls whether even opaque working records may be retained.
                transport = (
                    {"export_disposition": _ZERO_EXPORT_RECEIPT}
                    if self._zero_exports(db, json.loads(row["transport"]))
                    else {}
                )
                db.execute("PRAGMA secure_delete=ON")
                db.execute("DELETE FROM items")
                db.execute(
                    "UPDATE closeout SET agreement='',custodian='',support='',due='',revision=NULL,"
                    " trigger='',withdrawn_at=0,transport=?,minimized=1 WHERE singleton=1",
                    (json.dumps(transport),),
                )
        return self.status()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory", type=Path, required=True, help="absolute private worker directory"
    )
    parser.add_argument("--org", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    for action in ("status", "reconcile"):
        commands.add_parser(action)
    stop = commands.add_parser("withdraw", help="stop admission before reconciling any destination")
    for name in ("agreement", "custodian", "support", "due"):
        stop.add_argument("--" + name, required=True)
    stop.add_argument("--trigger", choices=TRIGGERS, required=True)
    stop.add_argument("--revision")
    record = commands.add_parser("record", help="record one operator-confirmed inventory fact")
    for name in ("item", "owner", "due"):
        record.add_argument("--" + name, required=True)
    record.add_argument("--kind", choices=KINDS, required=True)
    record.add_argument(
        "--disposition", choices=sorted((OPEN | RESOLVED) - {"live_rows_removed"}), required=True
    )
    record.add_argument("--evidence", help="opaque private evidence reference")
    purge = commands.add_parser("purge-local", help="remove live training-store rows only")
    for name in ("engagement", "agreement", "custodian", "evidence"):
        purge.add_argument("--" + name, required=True)
    finish = commands.add_parser("finish", help="close only after all custody and support evidence")
    finish.add_argument("--custodian", required=True)
    finish.add_argument("--evidence", required=True)
    finish.add_argument(
        "--receipt-only",
        action="store_true",
        help="discard detailed working records once all obligations are resolved",
    )
    args = vars(parser.parse_args())
    try:
        closeout = Closeout(args.pop("directory"), args.pop("org"))
        action = args.pop("command")
        result = getattr(closeout, action.replace("-", "_"))(**args)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(
            str(exc) if isinstance(exc, CustodyError) else "private_closeout_unavailable",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
