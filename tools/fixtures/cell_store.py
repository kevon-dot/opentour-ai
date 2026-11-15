"""Actual local persistence for test-only transaction conformance.

SQLite serializes these fixture operations. It does not emulate AWS IAM, network,
item/transaction limits, SDK retries, DynamoDB consistency or production authority.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from tools import schema_validate
from tools.fixtures.ports import EffectState, FixtureRefusal, Scope
from tools.fixtures.registry import ISSUER, digest


class InjectedCommitFailure(RuntimeError):
    pass


def generated_guard(scope: Scope) -> dict[str, Any]:
    snapshot = {
        "artifact_id": ISSUER + ":snapshot",
        "kind": "simulated_authority_snapshot",
        "revision": ISSUER + ":snapshot-version",
        "sha256": digest("simulated snapshot"),
        "manifest_id": ISSUER + ":manifest",
        "environment_id": scope.environment_id,
        "classification": "control_metadata",
        "purpose_grant_id": ISSUER + ":grant",
        "expires_at": "2026-01-01T00:02:00.000Z",
    }
    return {
        "organization_id": scope.organization_id,
        "environment_id": scope.environment_id,
        "cell_id": scope.cell_id,
        "authority_revision": ISSUER + ":authority-1",
        "revocation_epoch": 1,
        "grant_snapshot_sha256": digest("simulated grant"),
        "signed_snapshot_ref": snapshot,
        "valid_until": "2026-01-01T00:02:00.000Z",
        "installed_at": "2026-01-01T00:00:00.000Z",
        "mode": "active",
        "barrier_operation_id": None,
        "snapshot_sequence": 1,
        "issuance_generation": ISSUER + ":generation",
        "journal_frontier_sequence": 1,
        "journal_frontier_digest": digest("simulated journal"),
        "runner_identity_epoch": 1,
        "revision": "1",
    }


class ScriptedJournal:
    issuer = ISSUER

    def __init__(self, entries: tuple[str, ...] = ("restrictive-intent", "committed-outcome")):
        self.available = True
        self.entries = {i + 1: entry for i, entry in enumerate(entries)}
        self.head_sequence = len(entries)
        self.head_digest = digest(list(entries))

    def current_frontier(self) -> tuple[int, str] | None:
        return (self.head_sequence, self.head_digest) if self.available else None

    def contiguous(self, sequence: int, expected_digest: str) -> bool:
        if not self.available or set(self.entries) != set(range(1, sequence + 1)):
            return False
        return digest([self.entries[i] for i in range(1, sequence + 1)]) == expected_digest


class LocalCellStore:
    """One declared fixture cell. Trusted constructor selects scope, never caller input."""

    def __init__(self, path: Path, scope: Scope, clock: Callable[[], str]):
        self.path, self.scope, self.clock = path, scope, clock
        self.before_commit_failure = False
        self.after_commit_failure = False
        self.connection = sqlite3.connect(path, timeout=2, isolation_level=None)
        self.connection.execute("PRAGMA busy_timeout=2000")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS guard (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS effects (id TEXT PRIMARY KEY, scope TEXT NOT NULL,
                epoch INTEGER NOT NULL, revision TEXT NOT NULL, digest TEXT NOT NULL,
                state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                effect_id TEXT NOT NULL, state TEXT NOT NULL, UNIQUE(effect_id,state));
        """)
        row = self.connection.execute("SELECT payload FROM guard WHERE singleton=1").fetchone()
        if row is None:
            self.install_guard(generated_guard(scope))
        else:
            self._scope(scope, json.loads(row[0]))

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> LocalCellStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _scope(self, scope: Scope, guard: dict[str, Any]) -> None:
        if scope != self.scope or any(
            guard[k] != getattr(scope, k) for k in ("organization_id", "environment_id", "cell_id")
        ):
            raise FixtureRefusal("RESOURCE_NOT_FOUND")

    def install_guard(self, guard: dict[str, Any]) -> None:
        registry = schema_validate.schema_registry()
        if not schema_validate.valid_instance(
            "urn:opentour:authority:1.0#/$defs/LocalAuthorityGuard", guard, registry
        ):
            raise FixtureRefusal("SCHEMA_INVALID")
        self._scope(self.scope, guard)
        if not guard["signed_snapshot_ref"]["artifact_id"].startswith(ISSUER):
            raise FixtureRefusal("SIMULATED_NAMESPACE_REQUIRED")
        previous = self.connection.execute("SELECT payload FROM guard WHERE singleton=1").fetchone()
        if previous:
            old = json.loads(previous[0])
            old_tuple = (old["revocation_epoch"], old["snapshot_sequence"])
            new_tuple = (guard["revocation_epoch"], guard["snapshot_sequence"])
            if new_tuple < old_tuple or (new_tuple == old_tuple and guard != old):
                raise FixtureRefusal("AUTHORITY_STALE")
        self.connection.execute(
            "INSERT INTO guard(singleton,payload) VALUES(1,?) "
            "ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload",
            (json.dumps(guard, sort_keys=True),),
        )

    def check(self, scope: Scope, epoch: int, revision: str, now: str) -> None:
        row = self.connection.execute("SELECT payload FROM guard WHERE singleton=1").fetchone()
        guard = json.loads(row[0])
        self._scope(scope, guard)
        if (
            type(epoch) is not int
            or epoch != guard["revocation_epoch"]
            or revision != guard["revision"]
            or guard["mode"] != "active"
            or datetime.fromisoformat(now) >= datetime.fromisoformat(guard["valid_until"])
        ):
            raise FixtureRefusal("AUTHORITY_STALE")

    def prepare(
        self, effect_id: str, scope: Scope, epoch: int, revision: str, request_digest: str
    ) -> bool:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.check(scope, epoch, revision, self.clock())
            existing = self.connection.execute(
                "SELECT digest FROM effects WHERE id=?", (effect_id,)
            ).fetchone()
            if existing:
                if existing[0] != request_digest:
                    raise FixtureRefusal("IDEMPOTENCY_CONFLICT")
                self.connection.execute("COMMIT")
                return False
            self.connection.execute(
                "INSERT INTO effects VALUES(?,?,?,?,?,?)",
                (
                    effect_id,
                    json.dumps(scope.as_dict(), sort_keys=True),
                    epoch,
                    revision,
                    request_digest,
                    "prepared",
                ),
            )
            self.connection.execute(
                "INSERT INTO outbox(effect_id,state) VALUES(?,?)", (effect_id, "prepared")
            )
            if self.before_commit_failure:
                raise InjectedCommitFailure("before_local_commit")
            self.connection.execute("COMMIT")
        except Exception:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        if self.after_commit_failure:
            raise InjectedCommitFailure("after_local_commit")
        return True

    def state(self, effect_id: str, scope: Scope) -> EffectState | None:
        if scope != self.scope:
            raise FixtureRefusal("RESOURCE_NOT_FOUND")
        row = self.connection.execute(
            "SELECT state FROM effects WHERE id=?", (effect_id,)
        ).fetchone()
        return cast(EffectState, row[0]) if row else None

    def resolve(self, effect_id: str, scope: Scope, state: EffectState) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if scope != self.scope:
                raise FixtureRefusal("RESOURCE_NOT_FOUND")
            row = self.connection.execute(
                "SELECT state,epoch,revision FROM effects WHERE id=?", (effect_id,)
            ).fetchone()
            if row is None:
                raise FixtureRefusal("RESOURCE_NOT_FOUND")
            allowed = {
                "prepared": {"dispatching", "cancelled_before_dispatch", "refused"},
                "dispatching": {"applied", "outcome_unknown", "refused", "reconciling"},
                "outcome_unknown": {"applied", "reconciling", "outcome_unknown"},
                "reconciling": {"applied", "reconciling", "outcome_unknown", "compensated"},
            }
            if state != row[0] and state not in allowed.get(row[0], set()):
                raise FixtureRefusal("INVALID_EFFECT_TRANSITION")
            if state == "dispatching":
                self.check(scope, row[1], row[2], self.clock())
            self.connection.execute("UPDATE effects SET state=? WHERE id=?", (state, effect_id))
            self.connection.execute(
                "INSERT OR IGNORE INTO outbox(effect_id,state) VALUES(?,?)", (effect_id, state)
            )
            self.connection.execute("COMMIT")
        except Exception:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def ledger(self) -> list[tuple[int, str, str]]:
        return self.connection.execute(
            "SELECT sequence,effect_id,state FROM outbox ORDER BY sequence"
        ).fetchall()
