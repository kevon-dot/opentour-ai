"""Park and resume one owner-authorized EVE source watcher without dispatching a fit."""

from __future__ import annotations

import fcntl
import hmac
import json
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from opentour_cell.pilot import learning_runs as lr
from opentour_cell.pilot.eve_exchange import digest
from opentour_cell.pilot.repair_bundle import _private_directory, _read

DISCOVERY_FILE = "eve-discovery-scope.json"
SELECTION = "eve-learning-selection.json"
_HEX = re.compile(r"[0-9a-f]{64}")
_ACTIONS = {
    "start_scan": {"ready", "parked"},
    "another_scan": {"watching", "scanning"},
    "done_for_now": {"watching", "scanning"},
}


def _need(value: object) -> None:
    if not value:
        raise ValueError("eve_scan_unavailable")


class EveScanService:
    def __init__(self, directory: Path, organization: str, workspace: str, channel: str) -> None:
        _private_directory(directory)
        self.directory, self.organization = directory, organization
        self.workspace, self.channel = workspace, channel
        self.storage = directory / "eve-discovery"
        self.storage.mkdir(mode=0o700, exist_ok=True)
        _private_directory(self.storage)

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        path = self.storage / "scan.sqlite3"
        fd = os.open(path, os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
        try:
            st = os.fstat(fd)
            import stat

            _need(
                stat.S_ISREG(st.st_mode)
                and st.st_nlink == 1
                and st.st_uid == os.getuid()
                and not st.st_mode & 0o077
            )
        finally:
            os.close(fd)
        with closing(sqlite3.connect(path, timeout=3)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS watch(
                binding TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'ready',
                generation INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0,
                pending INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0, source TEXT,
                last_scan REAL, problem TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS decisions(
                binding TEXT NOT NULL, key TEXT NOT NULL, PRIMARY KEY(binding,key))""")
            db.execute("""CREATE TABLE IF NOT EXISTS seen(
                binding TEXT NOT NULL, source TEXT NOT NULL, first_seen REAL NOT NULL,
                PRIMARY KEY(binding,source))""")
            yield db

    def signed(self) -> dict[str, Any]:
        from opentour_cell.pilot import signed_records as sr

        verifier = sr.PilotVerifier(
            _read(self.directory / "pilot-issuer-registry.json", sr.MAX_CONFIG_BYTES)
        )
        raw = verifier.verify(_read(self.directory / DISCOVERY_FILE, 32768), sr.GRANT_PURPOSE)
        _need(
            set(raw)
            == {
                "profile_version",
                "kind",
                "tenant",
                "engagement_id",
                "workspace",
                "channel",
                "reviewer",
                "workflow",
                "mandate_id",
                "connection_revision",
                "issued_at",
                "not_after",
            }
        )
        _need(
            raw["profile_version"] == "1.0"
            and raw["kind"] == "eve_source_discovery"
            and raw["tenant"] == self.organization
            and raw["workspace"] == self.workspace
            and raw["channel"] == self.channel
        )
        for key in (
            "tenant",
            "engagement_id",
            "workspace",
            "channel",
            "reviewer",
            "workflow",
            "mandate_id",
        ):
            _need(
                isinstance(raw[key], str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", str(raw[key]))
            )
        _need(
            isinstance(raw["connection_revision"], str)
            and _HEX.fullmatch(str(raw["connection_revision"]))
        )
        start, end = (datetime.fromisoformat(str(raw[k])) for k in ("issued_at", "not_after"))
        _need(
            start.utcoffset() is not None
            and end.utcoffset() is not None
            and start.timestamp() <= time.time() < end.timestamp()
        )
        return dict(raw)

    @contextmanager
    def current(self, actor: str, *, write: bool) -> Iterator[dict[str, Any]]:
        grant = self.signed()
        authority = lr.LocalLearningRunGrant(self.directory / SELECTION)
        principal = authority.principal
        _need(
            principal.role == "owner"
            and principal.channel == "slack"
            and principal.tenant == self.organization
            and principal.actor == actor == grant["reviewer"]
        )
        with authority.guard(principal, write=write) as scope:
            _need(
                scope.engagement_epoch == grant["engagement_id"]
                and scope.workflow_ref == grant["workflow"]
                and scope.mandate_id == grant["mandate_id"]
                and "read" in scope.permissions
            )
            mandate = lr.LocalMandateSource(self.directory).current(scope, principal)
            _need(mandate.state == "active" and mandate.owner_id == actor)
            can = {"start", "cancel"} <= scope.permissions
            _need(not write or can)
            yield grant | {
                "can_control": can,
                "authority_revision": digest(
                    [grant, scope.grant_revision, mandate.mandate_revision]
                ),
                "binding": digest(
                    [
                        grant[k]
                        for k in ("tenant", "engagement_id", "workspace", "channel", "workflow")
                    ]
                ),
            }

    @staticmethod
    def row(db: sqlite3.Connection, grant: dict[str, Any]) -> sqlite3.Row:
        db.execute("INSERT OR IGNORE INTO watch(binding) VALUES(?)", (grant["binding"],))
        row = db.execute("SELECT * FROM watch WHERE binding=?", (grant["binding"],)).fetchone()
        assert row is not None
        return row

    @staticmethod
    def project(row: sqlite3.Row, g: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": row["state"],
            "revision": digest([g["authority_revision"], row["generation"], row["version"]]),
            "source_revision": row["source"],
            "can_control": g["can_control"],
            "generation": row["generation"],
            "last_scan": row["last_scan"],
            "reason": row["problem"],
        }

    def status(self, actor: str) -> dict[str, Any]:
        with self.current(actor, write=False) as grant, self.db() as db:
            return self.project(self.row(db, grant), grant)

    def act(self, actor: str, action: str, revision: str, key: str) -> dict[str, Any]:
        from opentour_cell.pilot.receipt_recovery import _JournalSnapshot, _open_db

        _need(action in _ACTIONS)
        with self.current(actor, write=True) as grant:
            path = self.directory / "slack-pilot.sqlite3"
            with _open_db(path) as journal_db:
                _need(journal_db is not None)
                assert journal_db is not None
                identity = journal_db.execute("SELECT binding FROM identity WHERE id=1").fetchone()
                _need(identity is not None)
                bound = json.loads(identity[0])
                _need(
                    isinstance(bound, list)
                    and len(bound) == 5
                    and bound[:2] == [self.organization, self.workspace]
                    and bound[3:] == [self.channel, str(self.directory)]
                )
                journal = _JournalSnapshot(journal_db, path, identity[0])
                journal.active(journal_db)
                expected = journal.opaque(
                    "eve_scan",
                    [self.organization, self.workspace, self.channel, actor, action, revision],
                )
                _need(hmac.compare_digest(expected, key))
            with self.db() as db:
                row = self.row(db, grant)
                if db.execute(
                    "SELECT 1 FROM decisions WHERE binding=? AND key=?", (grant["binding"], key)
                ).fetchone():
                    return self.project(row, grant)
                _need(
                    row["state"] in _ACTIONS[action]
                    and self.project(row, grant)["revision"] == revision
                )
                db.execute("INSERT INTO decisions VALUES(?,?)", (grant["binding"], key))
                if action == "another_scan":
                    if row["state"] == "watching" and not row["pending"]:
                        db.execute(
                            "UPDATE watch SET pending=1 WHERE binding=?", (grant["binding"],)
                        )
                else:
                    state = "parked" if action == "done_for_now" else "watching"
                    db.execute(
                        """UPDATE watch SET state=?,version=version+1,generation=generation+1,
                               pending=?,due=0,source=NULL,problem=NULL WHERE binding=?""",
                        (state, int(state == "watching"), grant["binding"]),
                    )
                return self.project(self.row(db, grant), grant)

    def tick(
        self,
        config: Path,
        interval: int,
        discover: Callable[[Path, str, Path, dict[str, Any]], str | None] | None = None,
    ) -> dict[str, Any]:
        # The OS releases this lock on worker death. An interrupted read may be repeated;
        # its immutable snapshot and the generation fence prevent duplicated proposals.
        grant = self.signed()
        actor = grant["reviewer"]
        lockfd = os.open(
            self.storage / "reader.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(lockfd, "rb") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return self.status(actor)
            with self.current(actor, write=True) as g, self.db() as db:
                row = self.row(db, g)
                if row["state"] in {"ready", "parked"} or (
                    row["state"] == "watching" and not row["pending"] and time.time() < row["due"]
                ):
                    return self.project(row, g)
                generation = row["generation"]
                auth_revision = g["authority_revision"]
                db.execute(
                    "UPDATE watch SET state='scanning',pending=0,version=version+1 WHERE binding=?",
                    (g["binding"],),
                )
            problem = None
            try:
                source = (discover or discover_sources)(
                    self.directory, self.organization, config, g
                )
                _need(source is None or isinstance(source, str) and _HEX.fullmatch(source))
            except (KeyError, ValueError, OSError, sqlite3.Error):
                source, problem = None, "source_unavailable"
            with self.current(actor, write=True) as g, self.db() as db:
                row = self.row(db, g)
                if (
                    row["generation"] != generation
                    or row["state"] != "scanning"
                    or g["authority_revision"] != auth_revision
                ):
                    return self.project(row, g)
                now = time.time()
                if source is not None:
                    db.execute(
                        "INSERT OR IGNORE INTO seen VALUES(?,?,?)", (g["binding"], source, now)
                    )
                db.execute(
                    """UPDATE watch SET state='watching',due=?,last_scan=?,problem=?,
                              source=?,version=version+1 WHERE binding=?""",
                    (now + interval, now, problem, source, g["binding"]),
                )
                return self.project(self.row(db, g), g)


def discover_sources(
    directory: Path, organization: str, config: Path, grant: dict[str, Any]
) -> str | None:
    from opentour_cell.pilot import autonomous_cycle, repair_workflow, workplace_context

    cfg = repair_workflow._private_json(config)
    store, ledger, scope, agreement, connection = autonomous_cycle._context(
        directory, organization, cfg
    )
    _need(
        scope["workflow"] == grant["workflow"]
        and cfg["engagement"] == grant["engagement_id"]
        and cfg["mandate_id"] == grant["mandate_id"]
        and agreement["connection_revision"] == grant["connection_revision"]
    )
    cap = scope["max_rows_per_run"]
    found = workplace_context.discover_workplace(
        store,
        connection,
        max_total_rows=cap,
        expected_connection_revision=grant["connection_revision"],
    )
    if found.get("state") != "ready":
        if found.get("reason") == "workplace_resource_pair_missing":
            return None
        raise ValueError("source_unavailable")
    # Admission collects a verified source snapshot and its correction provenance. Only the
    # separate signed EVE plan and current Slack fit decision can enter the trainer.
    _, acquired, _ = autonomous_cycle._admit(
        directory, cfg, store, ledger, scope, connection, workplace_context, agreement
    )
    return str(acquired["snapshot_revision"])


def open_eve_scan_service(
    directory: Path, organization: str, workspace: str, channel: str
) -> EveScanService | None:
    if not os.path.lexists(directory / DISCOVERY_FILE):
        return None
    return EveScanService(directory, organization, workspace, channel)
