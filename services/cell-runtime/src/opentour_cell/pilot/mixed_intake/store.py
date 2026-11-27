"""Customer-local durable state for mixed-source intake sessions (sqlite, file backed).

Simulated/not proven: this is a local single-writer store. It is not the control-plane
database, takes no distributed locks and does not prove PostgreSQL behavior.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from opentour_cell.pilot.mixed_intake.model import Json, Occurrence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, binding TEXT NOT NULL,
  binding_digest TEXT NOT NULL, status TEXT NOT NULL, fingerprint TEXT,
  change_token TEXT, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS frontier(
  session_id TEXT NOT NULL, seq INTEGER NOT NULL, parent_id TEXT NOT NULL,
  cursor TEXT NOT NULL, depth INTEGER NOT NULL, ancestors TEXT NOT NULL,
  done INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(session_id, parent_id, cursor));
CREATE TABLE IF NOT EXISTS pages(
  session_id TEXT NOT NULL, page_key TEXT NOT NULL, summary TEXT NOT NULL,
  PRIMARY KEY(session_id, page_key));
CREATE TABLE IF NOT EXISTS occurrences(
  session_id TEXT NOT NULL, occurrence_id TEXT NOT NULL, native_id TEXT NOT NULL,
  revision TEXT NOT NULL, payload TEXT NOT NULL, payload_digest TEXT NOT NULL,
  PRIMARY KEY(session_id, occurrence_id));
CREATE TABLE IF NOT EXISTS latest(
  session_id TEXT NOT NULL, native_id TEXT NOT NULL, occurrence_id TEXT NOT NULL,
  PRIMARY KEY(session_id, native_id));
CREATE TABLE IF NOT EXISTS parse_cache(
  tenant TEXT NOT NULL, scope TEXT NOT NULL, content_digest TEXT NOT NULL,
  parser_revision TEXT NOT NULL, variant TEXT NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY(tenant, scope, content_digest, parser_revision, variant));
CREATE TABLE IF NOT EXISTS consumers(
  session_id TEXT NOT NULL, consumer_id TEXT NOT NULL, depends TEXT NOT NULL,
  valid INTEGER NOT NULL, PRIMARY KEY(session_id, consumer_id));
CREATE TABLE IF NOT EXISTS ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, kind TEXT NOT NULL,
  payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drafts(
  draft_key TEXT PRIMARY KEY, session_id TEXT NOT NULL, payload TEXT NOT NULL,
  dispatched INTEGER NOT NULL DEFAULT 0, supersedes TEXT);
CREATE TABLE IF NOT EXISTS jobs(job_key TEXT PRIMARY KEY, draft_key TEXT NOT NULL);
"""


class IntakeStore:
    """Thin typed wrapper. Every mutation runs inside one explicit transaction."""

    def __init__(self, path: Path):
        self.path = path
        with self.tx() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        try:
            db.row_factory = sqlite3.Row
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    # ------------------------------------------------------------------ ledger

    @staticmethod
    def log(db: sqlite3.Connection, session_id: str, kind: str, payload: Json) -> None:
        db.execute(
            "INSERT INTO ledger(session_id, kind, payload) VALUES (?,?,?)",
            (session_id, kind, json.dumps(payload, sort_keys=True)),
        )

    def ledger(self, session_id: str, kind: str | None = None) -> list[Json]:
        with self.tx() as db:
            rows = db.execute(
                "SELECT kind, payload FROM ledger WHERE session_id=? ORDER BY id", (session_id,)
            ).fetchall()
        return [
            {"kind": r["kind"], **json.loads(r["payload"])}
            for r in rows
            if kind is None or r["kind"] == kind
        ]

    # ------------------------------------------------------------------ occurrences

    def latest(self, session_id: str) -> list[Occurrence]:
        with self.tx() as db:
            rows = db.execute(
                "SELECT o.payload FROM latest l JOIN occurrences o ON o.session_id=l.session_id "
                "AND o.occurrence_id=l.occurrence_id WHERE l.session_id=? ORDER BY l.native_id",
                (session_id,),
            ).fetchall()
        return [Occurrence(**json.loads(r["payload"])) for r in rows]

    def occurrence(self, session_id: str, native_id: str, revision: str) -> Occurrence | None:
        with self.tx() as db:
            row = db.execute(
                "SELECT payload FROM occurrences WHERE session_id=? AND native_id=? AND revision=?",
                (session_id, native_id, revision),
            ).fetchone()
        return Occurrence(**json.loads(row["payload"])) if row else None

    def all_occurrences(self, session_id: str) -> list[Occurrence]:
        with self.tx() as db:
            rows = db.execute(
                "SELECT payload FROM occurrences WHERE session_id=? ORDER BY native_id, revision",
                (session_id,),
            ).fetchall()
        return [Occurrence(**json.loads(r["payload"])) for r in rows]
