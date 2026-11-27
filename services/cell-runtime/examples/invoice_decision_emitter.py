"""Keep invoice application commits and provider attempts in separate local records."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Never

from opentour_cell.pilot.intake_mcp import IntakeError, require_ref
from opentour_cell.pilot.live_evidence import _canonical, _digest
from opentour_cell.pilot.live_reader import PAGE
from opentour_cell.pilot.onboarding_mcp import _private_parent, _read_private
from opentour_cell.pilot.repair_workflow import _record
from opentour_cell.pilot.training import _check_file

SCHEMA = "opentour.example.invoice-decision-emitter.v1"
INVOICE_ACTIONS = ("freight", "review", "defer", "ask", "supplies")
Json = dict[str, Any]


def require(condition: object, reason: str) -> None:
    if not condition:
        raise IntakeError(reason)


@dataclass(frozen=True)
class Scope:
    tenant: str
    workflow: str
    engagement_epoch: str
    namespace: str
    policy_ref: str
    checkpoint: int

    def __post_init__(self) -> None:
        for value in (
            self.tenant,
            self.workflow,
            self.engagement_epoch,
            self.namespace,
            self.policy_ref,
        ):
            require_ref(value)
        require(type(self.checkpoint) is int and 0 <= self.checkpoint < 2**63, "invalid_checkpoint")

    @classmethod
    def load(cls, selected: Path) -> Scope:
        value = _read_private(selected, 4096)
        require(
            value.get("schema_version") == SCHEMA
            and set(value) == {"schema_version", *cls.__dataclass_fields__},
            "invalid_emitter_scope",
        )
        value.pop("schema_version")
        return cls(**value)


class InvoiceJournal:
    """Application-owned state. The separate custodian authorizes exported pages for intake."""

    def __init__(self, directory: Path, scope: Scope):
        require(
            directory.is_absolute() and directory.resolve() == directory,
            "private_directory_required",
        )
        self.path = directory / "invoice-decisions.sqlite3"
        self.scope = scope
        self.identity = _digest([SCHEMA, asdict(scope)])

    def initialize(self) -> None:
        _private_parent(self.path)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with self._connection(write=True) as db:
            db.execute("CREATE TABLE IF NOT EXISTS scope (singleton INTEGER PRIMARY KEY, id TEXT)")
            db.execute("INSERT OR IGNORE INTO scope VALUES (1, ?)", (self.identity,))
            self._require_scope(db)
            db.execute(
                "CREATE TABLE IF NOT EXISTS attempts ("
                "attempt TEXT PRIMARY KEY, operation TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS commits (position INTEGER PRIMARY KEY, "
                "operation TEXT UNIQUE NOT NULL, decision TEXT UNIQUE NOT NULL, "
                "event TEXT NOT NULL)"
            )

    @contextmanager
    def _connection(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        _check_file(self.path)
        mode = "rw" if write else "ro"
        with (
            closing(
                sqlite3.connect(f"{self.path.as_uri()}?mode={mode}", uri=True, timeout=5)
            ) as db,
            db,
        ):
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db

    def _require_scope(self, db: sqlite3.Connection) -> None:
        require(
            db.execute("SELECT id FROM scope WHERE singleton=1").fetchone() == (self.identity,),
            "emitter_scope_unavailable",
        )

    def record_attempt(self, operation: str, attempt: str) -> None:
        require_ref(operation)
        require_ref(attempt)
        with self._connection(write=True) as db:
            self._require_scope(db)
            db.execute("INSERT OR IGNORE INTO attempts VALUES (?, ?)", (attempt, operation))
            require(
                db.execute("SELECT operation FROM attempts WHERE attempt=?", (attempt,)).fetchone()
                == (operation,),
                "provider_attempt_conflict",
            )

    def commit(
        self,
        *,
        operation: str,
        attempt: str,
        decision: str,
        case: str,
        family: str,
        action: str,
        decision_at: int,
    ) -> Json:
        """This sample commits the effective invoice action and outbox event atomically."""
        for reference in (operation, attempt, decision, case, family):
            require_ref(reference)
        require(action in INVOICE_ACTIONS, "invalid_invoice_action")
        require(type(decision_at) is int and 0 <= decision_at < 2**63, "invalid_decision_time")
        effect = _digest([self.identity, operation, decision])
        event = {
            "kind": "DecisionRecorded",
            "event_id": "invoice-event-" + effect[:40],
            "event_revision": 1,
            "decision_id": decision,
            "decision_revision": 1,
            "case_id": case,
            "family_id": family,
            "decision_at": decision_at,
            "action_ref": action,
            "policy_ref": self.scope.policy_ref,
            "source_provenance": "invoice-commit-" + effect[:40],
            "input_ref": None,
            "input_fidelity": "unavailable",
            "actor_ref": None,
            "model_ref": None,
        }
        encoded = _canonical(event)
        with self._connection(write=True) as db:
            self._require_scope(db)
            require(
                db.execute("SELECT operation FROM attempts WHERE attempt=?", (attempt,)).fetchone()
                == (operation,),
                "provider_attempt_unavailable",
            )
            existing = db.execute(
                "SELECT position, operation, decision, event FROM commits "
                "WHERE operation=? OR decision=? ORDER BY position",
                (operation, decision),
            ).fetchall()
            if existing:
                require(
                    len(existing) == 1 and existing[0][1:] == (operation, decision, encoded),
                    "invoice_commit_conflict",
                )
                position = existing[0][0]
            else:
                previous = db.execute("SELECT max(position) FROM commits").fetchone()[0]
                position = max(self.scope.checkpoint, previous or 0) + 1
                require(position < 2**63, "emitter_position_exhausted")
                db.execute(
                    "INSERT INTO commits VALUES (?, ?, ?, ?)",
                    (position, operation, decision, encoded),
                )
        return {"event_ref": event["event_id"], "checkpoint": position}

    def export_page(self, page_ref: str, after: int, through: int, limit: int = 100) -> Json:
        require_ref(page_ref)
        require(
            type(after) is int
            and type(through) is int
            and self.scope.checkpoint <= after <= through,
            "invalid_checkpoint",
        )
        require(type(limit) is int and 1 <= limit <= 100, "invalid_page_limit")
        with self._connection(write=False) as db:
            self._require_scope(db)
            latest = db.execute("SELECT max(position) FROM commits").fetchone()[0]
            require(through <= max(self.scope.checkpoint, latest or 0), "checkpoint_unavailable")
            rows = db.execute(
                "SELECT position, event FROM commits WHERE position>? AND position<=? "
                "ORDER BY position LIMIT ?",
                (after, through, limit),
            ).fetchall()
        page = {
            "schema_version": PAGE,
            "page_ref": page_ref,
            "rows": [
                {
                    "position": position,
                    "epoch": self.scope.engagement_epoch,
                    "event": json.loads(raw),
                }
                for position, raw in rows
            ],
        }
        sha = _digest(page)
        identity = _digest([self.identity, page_ref])
        output = self.path.with_name(f"live-export-page-{identity}.json")
        _record(output, page)
        return {
            "file": output.name,
            "sha256": sha,
            "commits": len(rows),
            "checkpoint": rows[-1][0] if rows else after,
        }


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        self.exit(2, "invoice_decision_emitter_unavailable\n")


def main() -> int:
    cli = ArgumentParser(description=__doc__)
    cli.add_argument("--scope", type=Path, required=True)
    commands = cli.add_subparsers(dest="command", required=True)
    commands.add_parser("initialize")
    attempt = commands.add_parser("attempt")
    commit = commands.add_parser("commit")
    for command in (attempt, commit):
        command.add_argument("--operation", required=True)
        command.add_argument("--attempt", required=True)
    for field in ("decision", "case", "family"):
        commit.add_argument(f"--{field}", required=True)
    commit.add_argument("--action", choices=INVOICE_ACTIONS, required=True)
    commit.add_argument("--decision-at", type=int, required=True)
    page = commands.add_parser("export")
    page.add_argument("--page-ref", required=True)
    page.add_argument("--after", type=int, required=True)
    page.add_argument("--through", type=int, required=True)
    parsed = cli.parse_args()
    try:
        selected = parsed.scope.absolute()
        emitter = InvoiceJournal(selected.parent, Scope.load(selected))
        if parsed.command == "initialize":
            emitter.initialize()
            result: Json = {"state": "initialized"}
        elif parsed.command == "attempt":
            emitter.record_attempt(parsed.operation, parsed.attempt)
            result = {"state": "attempt_recorded"}
        elif parsed.command == "commit":
            result = emitter.commit(
                operation=parsed.operation,
                attempt=parsed.attempt,
                decision=parsed.decision,
                case=parsed.case,
                family=parsed.family,
                action=parsed.action,
                decision_at=parsed.decision_at,
            )
        else:
            result = emitter.export_page(parsed.page_ref, parsed.after, parsed.through)
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, sqlite3.Error, TypeError):
        cli.exit(2, "invoice_decision_emitter_unavailable\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
