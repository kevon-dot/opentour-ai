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

from opentour_cell.pilot.engagement_epochs import (
    EngagementError,
    EngagementLedger,
    ledger_marker_present,
)
from opentour_cell.pilot.slack_adapter import withdraw_for_custody
from opentour_cell.pilot.text_classifier import TrainingError
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
# Names only: withdrawal must not reopen policy, source or verifier payloads.
# Arbitrary customer export filenames remain part of the custodian's inventory review.
_PRIVATE_STATE = {
    "intake-records": (
        "receipt",
        ("intake.sqlite3*", "benchmark-inspection-selection.json"),
        "Account for admitted workflow and run records, private selections and SQLite sidecars.",
    ),
    "benchmark-records": (
        "receipt",
        ("workflow-benchmarks.sqlite3*", "benchmark-worker.lock", "benchmark-intake-*.json"),
        "Account for benchmark versions, frozen source inputs, protected answers, "
        "materialization receipts and worker recovery state.",
    ),
    "source-intake-copies": (
        "source",
        ("source-intake-*.json",),
        "Reconcile trace and correction exports, protected answers, "
        "development projections and source grants.",
    ),
    "starter-permissions": (
        "grant",
        ("starter-intake-*.json",),
        "Reconcile policy grants, private signing material and withdrawal tombstones.",
    ),
    "live-source-records": (
        "source",
        ("live-evidence.sqlite3*", "live-export-*.json"),
        "Reconcile committed decisions, independent outcomes, quarantine, read receipts "
        "and export manifests; inventory the separately named raw exports and backups.",
    ),
    "live-permissions": (
        "grant",
        ("live-authority-*.json",),
        "Reconcile live resource grants, lease bindings and private signing material.",
    ),
    "readiness-records": (
        "receipt",
        (
            "training-readiness.sqlite3*",
            "live-repair-assessments.sqlite3*",
            "readiness-selection.json",
        ),
        "Account for readiness and repair assessments and their separately named private policy.",
    ),
}
_ENGAGEMENT_LEDGER = ("engagement-epochs.sqlite3", "engagement-fences.jsonl")
# Files that outlive a deleted ledger and show it was in use: every pinned ledger operation
# (each live write since OPE-543's #199, every withdrawal and restore) creates the epoch lock,
# and live evidence is only written under a ledger-required grant.
_LEDGER_TRACES = ("engagement-epochs.lock", "live-evidence.sqlite3")
_LEDGER_BUSY_BACKOFF = (0.05, 0.1, 0.2, 0.4, 0.8)
# What the closeout has seen of the engagement ledger. It only ever strengthens:
# NULL (closeout predates this record) < absent < present (the ledger, or one of its traces,
# was there when the closeout opened) < row_seen (this engagement's row was in the ledger when
# the closeout opened) < bound (this closeout retired that row).
_LEDGER_SEEN = (None, "absent", "present", "row_seen", "bound")
_ROW_REQUIRED = {"row_seen", "bound"}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS closeout (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), organization TEXT NOT NULL,
 engagement TEXT NOT NULL, agreement TEXT NOT NULL, custodian TEXT NOT NULL,
 support TEXT NOT NULL, due TEXT NOT NULL, revision TEXT, trigger TEXT NOT NULL,
 withdrawn_at INTEGER NOT NULL, reconciled INTEGER NOT NULL DEFAULT 0, finished_ref TEXT,
 transport TEXT NOT NULL DEFAULT '{}', minimized INTEGER NOT NULL DEFAULT 0,
 engagement_ledger TEXT
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
                columns = {r[1] for r in db.execute("PRAGMA table_info(closeout)")}
                if columns and "engagement_ledger" not in columns:
                    db.execute("ALTER TABLE closeout ADD COLUMN engagement_ledger TEXT")
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
            appeared_again = (kind in {"local", "receipt"} or item in _PRIVATE_STATE) and existing[
                "disposition"
            ] in {
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

    def _private_state_present(self, item: str) -> bool:
        # glob includes broken symlinks and never follows them to read a target.
        return any(any(self.directory.glob(pattern)) for pattern in _PRIVATE_STATE[item][1])

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
        minimal: dict[str, Any] | None = None
        with self._db(create=True) as db:
            old = db.execute("SELECT * FROM closeout WHERE singleton=1").fetchone()
            binding = (self.organization, agreement, custodian, support, due, revision, trigger)
            if old and old["minimized"]:
                self._row(db)
                self._store(old)
                minimal = self._minimal(old)
            elif old:
                self._row(db)
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
                    "revision,trigger,engagement,withdrawn_at,engagement_ledger)"
                    " VALUES(1,?,?,?,?,?,?,?,?,?,?)",
                    (
                        *binding,
                        store.engagement_id,
                        stopped["withdrawn_at"],
                        self._ledger_at_open(store.engagement_id),
                    ),
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
        if minimal is not None:
            self._retire_engagement_epoch(old)
            return minimal
        return self.reconcile()

    def _ledger_present(self) -> bool:
        return any(os.path.lexists(self.directory / name) for name in _ENGAGEMENT_LEDGER)

    def _ledger_traces_present(self) -> bool:
        return any(os.path.lexists(self.directory / name) for name in _LEDGER_TRACES)

    def _ledger_at_open(self, engagement: str) -> str:
        """Record what the ledger holds for this engagement, without creating anything.

        Opened read-write only so SQLite can recover an interrupted transaction; `mode=rw`
        never creates a missing file, and only a SELECT runs. A busy ledger is retried with
        bounded backoff and then refused (`engagement_ledger_busy`, retryable): the closeout
        does not open on a guess. Any other unreadable ledger is recorded as present, and
        reconcile then holds until it can be read.
        """
        if not self._ledger_present():
            traced = self._ledger_traces_present() or ledger_marker_present(self.directory)
            return "present" if traced else "absent"
        path = self.directory / _ENGAGEMENT_LEDGER[0]
        for pause in (*_LEDGER_BUSY_BACKOFF, None):
            try:
                _check_file(path)
                with closing(
                    sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=0.25)
                ) as db:
                    db.execute("PRAGMA trusted_schema=OFF")
                    found = db.execute(
                        "SELECT 1 FROM engagement WHERE org=? AND epoch=?",
                        (self.organization, engagement),
                    ).fetchone()
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    return "present"
                if pause is None:
                    raise CustodyError("engagement_ledger_busy") from None
                time.sleep(pause)
                continue
            except (TrainingError, OSError, sqlite3.Error):
                return "present"
            return "row_seen" if found else "present"
        raise CustodyError("engagement_ledger_busy")  # pragma: no cover - loop always returns

    def _grant_requires_ledger(self) -> bool:
        """Names only: a live grant here means live work that needs the engagement ledger.

        Every grant the live authority accepts requires the ledger, so custody does not
        reopen grant payloads to decide; any grant file, readable or not, requires it.
        """
        return any(
            path.name != "live-authority-key.json"
            for path in self.directory.glob("live-authority-*.json")
        )

    def _retire_engagement_epoch(self, row: sqlite3.Row) -> None:
        """Bridge withdrawal to the engagement ledger after the closeout hold has committed.

        The epoch is this closeout's committed engagement and the revocation epoch is the
        ledger's own, so no operator argument selects either. The ledger persists the terminal
        state before it frees the slot; repeating after any interruption completes the release.

        Enforced, else `engagement_ledger_unavailable` and the closeout stays unreconciled:
        - the ledger database must exist; it is opened `existing_only`, so a journal without
          its database holds instead of being recreated empty;
        - a ledger present when the closeout opened, or a trace of one (`present`), must stay
          present;
        - once this engagement's row has been seen, at open (`row_seen`) or by retiring it
          (`bound`), the row must stay in the ledger, so restoring an older backup that lacks
          it holds instead of skipping retirement;
        - a live grant file, the epoch lock or the live-evidence database, now or at open,
          requires a present ledger holding this engagement's row;
        - the anti-rollback marker kept outside the snapshot (written at the directory's first
          grant ingest; unreadable counts as present) requires a present ledger, so deleting
          the ledger, with or without its traces, holds. It is worker-attested at most: it
          covers a snapshot restore or accidental rollback, not a hostile same-uid process.
        Not enforced (fails open): the ledger is present but this engagement's row is not in it
        when the closeout opens, and the directory has no live grant file, epoch lock or
        live-evidence database. Retirement is then skipped as before, because nothing
        distinguishes that directory from one where this engagement never used the ledger.
        """
        engagement = row["engagement"]
        recorded = row["engagement_ledger"] if "engagement_ledger" in row.keys() else None
        if recorded not in _LEDGER_SEEN:
            raise CustodyError("engagement_ledger_unavailable")
        required = self._grant_requires_ledger() or self._ledger_traces_present()
        seen = recorded
        if not self._ledger_present():
            if (
                required
                or recorded in {"present", *_ROW_REQUIRED}
                or ledger_marker_present(self.directory)
            ):
                raise CustodyError("engagement_ledger_unavailable")
            seen = "absent"
        else:
            try:
                # Never create a ledger here: a journal without its database is a hold.
                ledger = EngagementLedger(self.directory, existing_only=True)
                ledger.reconcile_fences()
                if ledger.get(self.organization, engagement) is None:
                    if required or recorded in _ROW_REQUIRED:
                        raise CustodyError("engagement_ledger_unavailable")
                    seen = "present"
                else:
                    ledger.retire(self.organization, engagement)
                    seen = "bound"
            except (EngagementError, TrainingError, OSError, sqlite3.Error):
                raise CustodyError("engagement_ledger_unavailable") from None
        if _LEDGER_SEEN.index(seen) > _LEDGER_SEEN.index(recorded):
            with self._db() as db:
                db.execute("UPDATE closeout SET engagement_ledger=? WHERE singleton=1", (seen,))

    def reconcile(self) -> dict[str, Any]:
        # Persist the hold on closeout if a journal, training file or inventory cannot be verified.
        with self._db() as db:
            row = self._row(db)
            if row["minimized"]:
                self._store(row)
            else:
                db.execute("UPDATE closeout SET reconciled=0 WHERE singleton=1")
        # Outside any closeout transaction: the ledger takes its own withdrawal lock.
        self._retire_engagement_epoch(row)
        if row["minimized"]:
            return self._minimal(row)
        with self._db() as db:
            row = self._row(db)
            store = self._store(row)
            local = store.custody_inventory()
            outbound = withdraw_for_custody(self.directory, self.organization)
            custodian, due = row["custodian"], row["due"]
            for item, (kind, _, detail) in _PRIVATE_STATE.items():
                if self._private_state_present(item):
                    self._observe(db, item, kind, custodian, due, detail=detail)
            for key, kind, present, detail in (
                (
                    "approval-requests",
                    "destination",
                    any(self.directory.glob("approval-ready-*.json"))
                    or any(self.directory.glob("approval-held-*.json")),
                    "Account for Slack or Zapier messages already admitted, any "
                    "pending decision and every recipient copy.",
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
                item in _PRIVATE_STATE
                and disposition in {"confirmed_deleted", "confirmed_expiry", "confirmed_returned"}
                and self._private_state_present(item)
            ):
                raise CustodyError("private_state_still_present")
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
