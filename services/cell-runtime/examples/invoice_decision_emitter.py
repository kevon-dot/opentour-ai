"""Keep invoice application commits and provider attempts in separate local records."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Never

from opentour_cell.pilot import repair_bundle as bundle
from opentour_cell.pilot.intake_mcp import IntakeError, require_ref
from opentour_cell.pilot.live_evidence import _canonical, _digest
from opentour_cell.pilot.live_reader import PAGE
from opentour_cell.pilot.onboarding_mcp import _private_parent, _read_private
from opentour_cell.pilot.readout import parse_manifest
from opentour_cell.pilot.repair_workflow import _record
from opentour_cell.pilot.training import _check_file

SCHEMA = "opentour.example.invoice-decision-emitter.v1"
OBSERVATION = "opentour.example.invoice-observation.v1"
OBSERVATION_SELECTION = "opentour.example.invoice-observation-selection.v1"
INVOICE_ACTIONS = ("freight", "review", "defer", "ask", "supplies")
Json = dict[str, Any]
_BASENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
_HASH = re.compile(r"[0-9a-f]{64}")
_OBSERVED_SCOPE = ("tenant", "workflow", "engagement_epoch", "namespace", "policy_ref")


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


class _Observation:
    """Verify an independently signed observation; no raw inputs or private keys."""

    def __init__(self, directory: Path, filename: str, scope: Scope, commit: Json):
        require(
            isinstance(filename, str)
            and _BASENAME.fullmatch(filename)
            and filename.endswith(".json"),
            "invalid_observation_file",
        )
        self.directory, self.scope, self.commit = directory, scope, commit
        self.selection_path = directory / "invoice-observation-selection.json"
        self.selection_bytes = bundle._read(self.selection_path, 8192)
        self.selection = parse_manifest(self.selection_bytes)
        fields = {
            "schema_version",
            "grant_ref",
            *_OBSERVED_SCOPE,
            "producer_ref",
            "model_ref",
            "model_revision",
            "public_key_file",
            "public_key_sha256",
            "expires_at",
        }
        require(
            set(self.selection) == fields
            and self.selection["schema_version"] == OBSERVATION_SELECTION,
            "invalid_observation_selection",
        )
        for name, value in self.selection.items():
            if name == "expires_at":
                require(type(value) is int and 0 < value < 2**63, "invalid_observation_expiry")
            else:
                require_ref(value)
        key_file = self.selection["public_key_file"]
        require(_BASENAME.fullmatch(key_file), "invalid_observation_key_file")
        require(_HASH.fullmatch(self.selection["public_key_sha256"]), "invalid_observation_key")
        self.key_path = directory / key_file
        self.key = bundle._read(self.key_path, 16384)
        require(b"PRIVATE KEY" not in self.key and b"PUBLIC KEY" in self.key, "public_key_required")
        self._permission()
        self.receipt_path = directory / filename
        self.receipt_bytes = bundle._read(self.receipt_path, 16384)
        self.receipt = parse_manifest(self.receipt_bytes)
        require(set(self.receipt) == {"claim", "signature"}, "invalid_observation_receipt")
        claim = self.receipt["claim"]
        require(isinstance(claim, dict), "invalid_observation_receipt")
        self.claim: Json = claim
        self.revalidate()

    def _permission(self) -> None:
        selected = self.selection
        require(
            bundle._read(self.selection_path, 8192) == self.selection_bytes,
            "observation_selection_changed",
        )
        require(bundle._read(self.key_path, 16384) == self.key, "observation_key_changed")
        require(bundle._sha(self.key) == selected["public_key_sha256"], "observation_key_mismatch")
        require(time.time() < selected["expires_at"], "observation_permission_expired")
        require(
            not os.path.lexists(
                self.directory / f"invoice-observation-withdrawn-{selected['grant_ref']}.json"
            ),
            "observation_permission_withdrawn",
        )
        require(
            all(selected[name] == getattr(self.scope, name) for name in _OBSERVED_SCOPE),
            "observation_scope_mismatch",
        )

    def revalidate(self) -> None:
        selected, claim = self.selection, self.claim
        self._permission()
        require(
            bundle._read(self.receipt_path, 16384) == self.receipt_bytes,
            "observation_receipt_changed",
        )
        fields = {
            "schema_version",
            "grant_ref",
            "producer_ref",
            *_OBSERVED_SCOPE,
            *self.commit,
            "model_ref",
            "model_revision",
            "input_ref",
            "receipt_ref",
        }
        require(
            set(claim) == fields and claim["schema_version"] == OBSERVATION,
            "invalid_observation_claim",
        )
        for name, value in claim.items():
            if name == "decision_at":
                require(type(value) is int and 0 <= value < 2**63, "invalid_decision_time")
            else:
                require_ref(value)
        require(
            all(claim[name] == value for name, value in self.commit.items()),
            "observation_commit_mismatch",
        )
        require(
            all(
                claim[name] == selected[name]
                for name in (
                    *_OBSERVED_SCOPE,
                    "grant_ref",
                    "producer_ref",
                    "model_ref",
                    "model_revision",
                )
            ),
            "observation_producer_mismatch",
        )
        encoded = self.receipt["signature"]
        require(isinstance(encoded, str) and len(encoded) <= 8192, "invalid_observation_signature")
        signature = base64.b64decode(encoded, validate=True)
        require(0 < len(signature) <= 4096, "invalid_observation_signature")
        with tempfile.TemporaryDirectory() as temp:
            key, sig = Path(temp) / "public", Path(temp) / "signature"
            bundle._write_private(key, self.key)
            bundle._write_private(sig, signature)
            bundle._crypto(
                ["dgst", "-sha256", "-verify", str(key), "-signature", str(sig)],
                bundle._bytes(claim),
                "invalid_observation_signature",
            )
        # Signature verification is an independent process boundary. Check the
        # current authority and pinned files again before returning to a writer.
        self._permission()
        require(
            bundle._read(self.receipt_path, 16384) == self.receipt_bytes,
            "observation_receipt_changed",
        )

    def metadata(self) -> Json:
        return dict(
            input_ref=self.claim["input_ref"],
            input_fidelity="observed",
            actor_ref=self.claim["producer_ref"],
            model_ref=self.claim["model_ref"],
            # The immutable receipt includes its opaque ref and exact model revision.
            # A later re-signed/rebound receipt cannot reinterpret a prior commit.
            source_provenance="invoice-observed-"
            + _digest([self.selection, bundle._sha(self.receipt_bytes)])[:40],
        )


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
        observation: str | None = None,
    ) -> Json:
        """This sample commits the effective invoice action and outbox event atomically."""
        for reference in (operation, attempt, decision, case, family):
            require_ref(reference)
        require(action in INVOICE_ACTIONS, "invalid_invoice_action")
        require(type(decision_at) is int and 0 <= decision_at < 2**63, "invalid_decision_time")
        observed = (
            None
            if observation is None
            else _Observation(
                self.path.parent,
                observation,
                self.scope,
                dict(
                    operation=operation,
                    attempt=attempt,
                    decision=decision,
                    case=case,
                    family=family,
                    action=action,
                    decision_at=decision_at,
                ),
            )
        )
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
        if observed is not None:
            event.update(observed.metadata())
        encoded = _canonical(event)
        with self._connection(write=True) as db:
            self._require_scope(db)
            if observed is not None:
                observed.revalidate()
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
            if observed is not None:
                observed.revalidate()
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
    commit.add_argument("--observation", help="Basename of an independently signed private receipt")
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
                observation=parsed.observation,
            )
        else:
            result = emitter.export_page(parsed.page_ref, parsed.after, parsed.through)
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, sqlite3.Error, TypeError, subprocess.SubprocessError):
        cli.exit(2, "invoice_decision_emitter_unavailable\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
