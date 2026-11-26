"""Customer-operated Slack Socket Mode workbench for one private pilot installation."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import importlib
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

Json = dict[str, Any]
SESSION_SECONDS = 900
RESULT_PAGE_SIZE = 12
SLACK_ID = re.compile(r"[A-Z][A-Z0-9]{2,31}\Z")
LOCAL_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
ROLES = {"inspect", "tune", "review"}
CALLBACKS = {"ot_dataset_search", "ot_tune_preview", "ot_tune_start", "ot_prompt_enter"}
ACTIONS = {
    "ot_home",
    "ot_browse",
    "ot_tune",
    "ot_status",
    "ot_evidence",
    "ot_charts",
    "ot_approve",
    "ot_changes",
    "ot_dataset_open",
    "ot_status_refresh",
    "ot_dataset_back",
    "ot_tune_dataset",
    "ot_readouts",
    "ot_result_open",
    "ot_result_prev",
    "ot_result_next",
    "ot_repair_confirm",
    "ot_repair_refresh",
    "ot_release_approve",
    "ot_release_changes",
    "ot_help",
    "ot_question",
    "ot_question_errors",
    "ot_question_coverage",
    "ot_question_changes",
    "ot_question_audience",
}


class Refused(ValueError):
    """A safe generic refusal. Source and callback values are never copied into its message."""


def _check(condition: object) -> None:
    if not condition:
        raise Refused("Request unavailable in this pilot.")


def _slack_id(value: object, prefix: str) -> bool:
    return isinstance(value, str) and bool(SLACK_ID.fullmatch(value)) and value[0] in prefix


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class Settings:
    organization: str
    team: str
    app: str
    channel: str
    reviewer: str
    recipient: str
    directory: Path
    roles: dict[str, Json]
    channel_aggregates: bool
    zapier: bool
    zapier_disposition: bool
    zapier_destination_sha256: str
    approval_requests: bool = False
    approval_provider: str = "slack"
    workspace_title: str = "OpenTour pilot workspace"
    workflow_label: str = ""
    config_path: Path | None = None
    explicit_recipient: bool = True

    @property
    def binding(self) -> str:
        return _json([self.organization, self.team, self.app, self.channel, str(self.directory)])

    @classmethod
    def load(cls, path: Path) -> Settings:
        _check(path.is_absolute())
        parent = path.parent.lstat()
        _check(
            stat.S_ISDIR(parent.st_mode)
            and parent.st_uid == os.getuid()
            and parent.st_mode & 0o077 == 0
        )
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            info = os.fstat(stream.fileno())
            _check(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and info.st_nlink == 1
                and info.st_mode & 0o077 == 0
                and info.st_size <= 65536
            )
            content = stream.read(65537)
        _check(len(content) <= 65536)
        doc = json.loads(content)
        required = {
            "organization",
            "team",
            "app",
            "channel",
            "directory",
            "users",
            "reviewer",
        }
        optional = {
            "recipient",
            "channel_aggregates",
            "zapier",
            "zapier_disposition",
            "zapier_destination_sha256",
            "approval_requests",
            "approval_provider",
            "workspace_title",
            "workflow_label",
        }
        _check(isinstance(doc, dict) and required <= doc.keys() <= required | optional)
        _check(isinstance(doc["organization"], str) and LOCAL_ID.fullmatch(doc["organization"]))
        _check(_slack_id(doc["team"], "T") and _slack_id(doc["app"], "A"))
        _check(_slack_id(doc["channel"], "CG") and isinstance(doc["users"], dict))
        _check(_slack_id(doc["reviewer"], "UW"))
        explicit_recipient = "recipient" in doc
        recipient = doc.get("recipient", doc["reviewer"])
        _check(_slack_id(recipient, "UW") or recipient == doc["channel"])
        directory = Path(doc["directory"])
        _check(directory.is_absolute() and directory.is_dir() and not directory.is_symlink())
        if os.name == "posix":
            _check(directory.stat().st_mode & 0o077 == 0 and directory.stat().st_uid == os.getuid())
        for actor, entry in doc["users"].items():
            _check(_slack_id(actor, "UW") and isinstance(entry, dict))
            _check(entry.keys() == {"roles", "datasets", "preview", "aggregate", "expires_at"})
            _check(isinstance(entry["roles"], list) and all(x in ROLES for x in entry["roles"]))
            _check(
                isinstance(entry["datasets"], list)
                and all(
                    isinstance(x, str) and bool(LOCAL_ID.fullmatch(x)) for x in entry["datasets"]
                )
            )
            _check(type(entry["preview"]) is bool and type(entry["aggregate"]) is bool)
            _check(type(entry["expires_at"]) is int)
        for name in ("channel_aggregates", "zapier", "zapier_disposition", "approval_requests"):
            _check(type(doc.get(name, False)) is bool)
        _check(not doc.get("zapier_disposition") or doc.get("zapier"))
        _check(doc.get("approval_provider", "slack") in {"slack", "zapier_slack"})
        for field, limit in (("workspace_title", 48), ("workflow_label", 64)):
            if field in doc:
                label = doc[field]
                _check(
                    isinstance(label, str)
                    and 0 < len(label) <= limit
                    and label == label.strip()
                    and label.isprintable()
                    and not any(c in label for c in "<>@#&")
                )
        destination = doc.get("zapier_destination_sha256", "")
        _check(
            isinstance(destination, str)
            and (destination == "" or re.fullmatch("[0-9a-f]{64}", destination))
        )
        return cls(
            doc["organization"],
            doc["team"],
            doc["app"],
            doc["channel"],
            doc["reviewer"],
            recipient,
            directory,
            doc["users"],
            doc.get("channel_aggregates", False),
            explicit_recipient and doc.get("zapier", False),
            explicit_recipient and doc.get("zapier_disposition", False),
            destination,
            explicit_recipient and doc.get("approval_requests", False),
            doc.get("approval_provider", "slack"),
            doc.get("workspace_title", "OpenTour pilot workspace"),
            doc.get("workflow_label", ""),
            path,
            explicit_recipient=explicit_recipient,
        )

    def permission(self, actor: str, role: str, dataset: str | None = None) -> Json:
        entry = self.roles.get(actor)
        _check(
            entry and role in entry["roles"] and (dataset is None or dataset in entry["datasets"])
        )
        assert entry is not None
        _check(int(time.time()) < entry["expires_at"])
        return entry


class Journal:
    """Local, tenant-bound receipts. Short-lived contexts contain no raw customer examples."""

    def __init__(self, path: Path, binding: str):
        self.path = path
        self.binding = binding
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            os.close(os.open(path, flags, 0o600))
        except FileExistsError:
            pass
        with self.db() as db:
            legacy = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='identity'"
            ).fetchone()
            if legacy:
                existing = db.execute("SELECT binding FROM identity WHERE id=1").fetchone()
                _check(existing and existing[0] == binding)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS identity (
                    id INTEGER PRIMARY KEY, binding TEXT, key BLOB);
                CREATE TABLE IF NOT EXISTS contexts (
                    id TEXT PRIMARY KEY, actor TEXT, channel TEXT, view_id TEXT,
                    kind TEXT, job TEXT, revision TEXT, expires INTEGER);
                CREATE TABLE IF NOT EXISTS jobs (
                    job TEXT PRIMARY KEY, dataset TEXT, requester TEXT);
                CREATE TABLE IF NOT EXISTS launcher (channel TEXT PRIMARY KEY, ts TEXT);
                CREATE TABLE IF NOT EXISTS inspections (
                    actor TEXT, revision TEXT, policy TEXT, expires INTEGER,
                    PRIMARY KEY(actor,revision,policy));
                CREATE TABLE IF NOT EXISTS decisions (
                    revision TEXT PRIMARY KEY, receipt TEXT UNIQUE, actor TEXT,
                    disposition TEXT, decided INTEGER, exportable INTEGER,
                    audience TEXT, attempts INTEGER DEFAULT 0,
                    delivery TEXT DEFAULT 'pending');
                CREATE TABLE IF NOT EXISTS journal_control (id INTEGER PRIMARY KEY,held INTEGER,
                    legacy_unknown INTEGER);
                CREATE TABLE IF NOT EXISTS approval_posts (
                    receipt TEXT PRIMARY KEY, revision TEXT NOT NULL, state TEXT NOT NULL,
                    ts TEXT, exposure TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS exposures (id TEXT PRIMARY KEY,kind TEXT,
                    status TEXT,admitted INTEGER,completed INTEGER);
                CREATE TABLE IF NOT EXISTS presentations (
                    id TEXT PRIMARY KEY, actor TEXT, revision TEXT, policy TEXT, scope TEXT,
                    presented INTEGER, expires INTEGER, snapshot TEXT, rendered TEXT);
                CREATE INDEX IF NOT EXISTS presentation_lookup
                    ON presentations(actor,revision,policy,expires);
                CREATE TABLE IF NOT EXISTS authorizations (
                    receipt TEXT PRIMARY KEY, presentation TEXT, engagement TEXT,
                    dataset TEXT,dataset_revision TEXT,destination TEXT,fields TEXT,
                    transmitted INTEGER,confirmed INTEGER,confirmation_ref TEXT);
                CREATE TABLE IF NOT EXISTS proposals (
                    job TEXT PRIMARY KEY, audience TEXT,engagement TEXT);
                CREATE TABLE IF NOT EXISTS integrity (
                    kind TEXT, id TEXT, seal TEXT, PRIMARY KEY(kind,id));
            """)
            db.execute("INSERT OR IGNORE INTO journal_control VALUES(1,0,?)", (int(bool(legacy)),))
            db.execute(
                "INSERT OR IGNORE INTO identity VALUES(1,?,?)", (binding, secrets.token_bytes(32))
            )
            row = db.execute("SELECT binding,key FROM identity WHERE id=1").fetchone()
            _check(row and row[0] == binding)
            self.key = bytes(row[1])
            # Older grants have no proof of presentation or agreed export destination.
            db.execute(
                "UPDATE decisions SET delivery='legacy_hold' WHERE delivery='pending' "
                "AND receipt NOT IN (SELECT id FROM integrity WHERE kind='decision')"
            )
            db.execute("UPDATE decisions SET delivery='uncertain' WHERE delivery='sending'")

    @staticmethod
    def active(db: sqlite3.Connection) -> None:
        row = db.execute("SELECT held FROM journal_control WHERE id=1").fetchone()
        _check(row and not row[0])

    def admit_view(self, kind: str) -> str:
        key = secrets.token_urlsafe(18)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.active(db)
            db.execute(
                "INSERT INTO exposures VALUES(?,?,'admitted',?,NULL)", (key, kind, int(time.time()))
            )
        return key

    def finish_view(self, key: str, status: str) -> None:
        with self.db() as db:
            db.execute(
                "UPDATE exposures SET status=?,completed=? WHERE id=?",
                (status, int(time.time()), key),
            )

    def claim_charts(self, context: str) -> str:
        key = self.opaque("chart", context)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.active(db)
            claimed = db.execute(
                "INSERT OR IGNORE INTO exposures VALUES(?,'comparison_charts','admitted',?,NULL)",
                (key, int(time.time())),
            )
            return key if claimed.rowcount == 1 else ""

    @contextmanager
    def db(self, timeout: float = 4) -> Iterator[sqlite3.Connection]:
        info = self.path.lstat()
        _check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
        if os.name == "posix":
            _check(info.st_uid == os.getuid() and info.st_mode & 0o077 == 0)
        with closing(sqlite3.connect(self.path, timeout=timeout)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def opaque(self, kind: str, value: object) -> str:
        return (
            kind + "_" + hmac.new(self.key, _json(value).encode(), hashlib.sha256).hexdigest()[:26]
        )

    def _seal(self, db: sqlite3.Connection, kind: str, key: str, *, create: bool = False) -> None:
        if kind == "presentation":
            row = db.execute(
                "SELECT id,actor,revision,policy,scope,presented,expires,snapshot,rendered "
                "FROM presentations WHERE id=?",
                (key,),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT d.revision,d.receipt,d.actor,d.disposition,d.decided,d.exportable,"
                "d.audience,a.presentation,a.engagement,a.dataset,a.dataset_revision,a.destination,"
                "a.fields FROM decisions d JOIN authorizations a USING(receipt) WHERE receipt=?",
                (key,),
            ).fetchone()
        _check(row)
        assert row is not None
        digest = hmac.new(
            self.key, _json([self.binding, kind, *row]).encode(), hashlib.sha256
        ).hexdigest()
        if create:
            db.execute("INSERT INTO integrity VALUES(?,?,?)", (kind, key, digest))
        else:
            record = db.execute(
                "SELECT seal FROM integrity WHERE kind=? AND id=?", (kind, key)
            ).fetchone()
            _check(record and hmac.compare_digest(record[0], digest))

    def context(
        self, actor: str, channel: str, view_id: str, kind: str, job: str = "", revision: str = ""
    ) -> str:
        token = secrets.token_urlsafe(24)
        now = int(time.time())
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.active(db)
            db.execute("DELETE FROM contexts WHERE expires<=?", (now,))
            db.execute("DELETE FROM inspections WHERE expires<=?", (now,))
            db.execute("DELETE FROM contexts WHERE actor=? AND view_id=?", (actor, view_id))
            db.execute(
                "INSERT INTO contexts VALUES(?,?,?,?,?,?,?,?)",
                (token, actor, channel, view_id, kind, job, revision, now + SESSION_SECONDS),
            )
        return token

    def read_context(self, token: str, actor: str, channel: str, view: str) -> Json:
        with self.db(timeout=0.18) as db:
            self.active(db)
            row = db.execute(
                "SELECT * FROM contexts WHERE id=? AND actor=? AND channel=? "
                "AND view_id=? AND expires>?",
                (token, actor, channel, view, int(time.time())),
            ).fetchone()
        _check(row)
        return dict(row) if row else {}

    def launcher(self, channel: str, ts: str, *, register: bool = False) -> bool:
        with self.db() as db:
            if register:
                db.execute("INSERT OR REPLACE INTO launcher VALUES(?,?)", (channel, ts))
            return (
                db.execute(
                    "SELECT 1 FROM launcher WHERE channel=? AND ts=?", (channel, ts)
                ).fetchone()
                is not None
            )

    def bind_job(self, job: str, dataset: str, actor: str, audience: str, engagement: str) -> None:
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.active(db)
            db.execute("INSERT OR IGNORE INTO jobs VALUES(?,?,?)", (job, dataset, actor))
            item = db.execute("SELECT * FROM jobs WHERE job=?", (job,)).fetchone()
            _check(item and (item["dataset"], item["requester"]) == (dataset, actor))
            db.execute("INSERT OR IGNORE INTO proposals VALUES(?,?,?)", (job, audience, engagement))
            pinned = db.execute(
                "SELECT audience,engagement FROM proposals WHERE job=?", (job,)
            ).fetchone()
            _check(pinned and tuple(pinned) == (audience, engagement))

    def job(self, job: str) -> Json:
        with self.db() as db:
            row = db.execute(
                "SELECT j.*,p.audience,p.engagement FROM jobs j LEFT JOIN proposals p "
                "ON p.job=j.job WHERE j.job=?",
                (job,),
            ).fetchone()
        _check(row)
        return dict(row) if row else {}

    def latest(self, allowed: list[str]) -> str:
        with self.db() as db:
            for row in db.execute("SELECT job,dataset FROM jobs ORDER BY rowid DESC"):
                if row["dataset"] in allowed:
                    return str(row["job"])
        return ""

    def review_jobs(
        self, allowed: set[str], engagement: str, audience: str
    ) -> list[tuple[str, str]]:
        with self.db() as db:
            self.active(db)
            return [
                (row["dataset"], row["job"])
                for row in db.execute(
                    "SELECT j.dataset,j.job FROM jobs j JOIN proposals p ON p.job=j.job "
                    "WHERE p.engagement=? AND p.audience=? ORDER BY j.dataset,j.job",
                    (engagement, audience),
                )
                if row["dataset"] in allowed
            ]

    def present(
        self,
        presentation: str,
        actor: str,
        revision: str,
        policy: str,
        scope: str,
        snapshot: Json,
        rendered: Json,
        granted_until: int,
    ) -> None:
        now = int(time.time())
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.active(db)
            proof = db.execute(
                "SELECT completed FROM exposures WHERE id=? AND status='presented'", (presentation,)
            ).fetchone()
            _check(proof and now < granted_until)
            db.execute(
                "INSERT INTO presentations VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    presentation,
                    actor,
                    revision,
                    policy,
                    scope,
                    proof[0],
                    min(now + SESSION_SECONDS, granted_until),
                    _json(snapshot),
                    _json(rendered),
                ),
            )
            self._seal(db, "presentation", presentation, create=True)

    def inspected(self, actor: str, revision: str, policy: str, *, chart_context: str = "") -> bool:
        with self.db() as db:
            self.active(db)
            row = db.execute(
                "SELECT id FROM presentations WHERE actor=? AND revision=? "
                "AND policy=? AND expires>? "
                "AND (?='' OR json_extract(snapshot,'$.chart_context')=?) "
                "ORDER BY rowid DESC LIMIT 1",
                (actor, revision, policy, int(time.time()), chart_context, chart_context),
            ).fetchone()
            if row:
                self._seal(db, "presentation", row[0])
            return row is not None

    def decision(self, revision: str) -> Json:
        with self.db() as db:
            row = db.execute("SELECT * FROM decisions WHERE revision=?", (revision,)).fetchone()
        return dict(row) if row else {}

    def decide(
        self, cfg: Settings, actor: str, target: Target, policy: str, disposition: str
    ) -> Json:
        _check(disposition in {"approved", "changes_requested"})
        now = int(time.time())
        entry = cfg.permission(actor, "review", target.dataset)
        _check(entry["aggregate"] and cfg.reviewer == actor and cfg.binding == self.binding)
        export = cfg.zapier and bool(cfg.zapier_destination_sha256)
        fields = ["event", "receipt"]
        if cfg.zapier_disposition:
            fields.append("disposition")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.active(db)
            presentation = db.execute(
                "SELECT id,snapshot FROM presentations WHERE actor=? "
                "AND revision=? AND policy=? AND scope=? AND expires>? ORDER BY rowid DESC LIMIT 1",
                (actor, target.revision, policy, target.evidence_digest, now),
            ).fetchone()
            _check(presentation)
            self._seal(db, "presentation", presentation[0])
            receipt = "receipt_" + secrets.token_urlsafe(18)
            created = db.execute(
                "INSERT OR IGNORE INTO decisions(revision,receipt,actor,disposition,"
                "decided,exportable,audience,delivery) VALUES(?,?,?,?,?,?,?,?)",
                (
                    target.revision,
                    receipt,
                    actor,
                    disposition,
                    now,
                    int(export),
                    cfg.recipient,
                    "pending" if export else "local_only",
                ),
            ).rowcount
            if created:
                db.execute(
                    "INSERT INTO authorizations(receipt,presentation,engagement,dataset,"
                    "dataset_revision,destination,fields) VALUES(?,?,?,?,?,?,?)",
                    (
                        receipt,
                        presentation[0],
                        target.audit["engagement"],
                        target.dataset,
                        target.dataset_revision,
                        cfg.zapier_destination_sha256 if export else "",
                        _json(fields),
                    ),
                )
                self._seal(db, "decision", receipt, create=True)
            row = db.execute(
                "SELECT d.* FROM decisions d JOIN authorizations a USING(receipt) "
                "WHERE d.revision=?",
                (target.revision,),
            ).fetchone()
            _check(
                row
                and row["actor"] == actor
                and row["disposition"] == disposition
                and row["audience"] == cfg.recipient
            )
            self._seal(db, "decision", row["receipt"])
            return dict(row)

    def receipt(self, receipt: str) -> Json:
        with self.db() as db:
            decision = db.execute("SELECT * FROM decisions WHERE receipt=?", (receipt,)).fetchone()
            _check(decision)
            assert decision is not None
            authorization = db.execute(
                "SELECT * FROM authorizations WHERE receipt=?", (receipt,)
            ).fetchone()
            if not authorization:
                return {
                    "decision": dict(decision),
                    "evidence": None,
                    "verification": "legacy_unqualified",
                }
            self._seal(db, "decision", receipt)
            self._seal(db, "presentation", authorization["presentation"])
            details = db.execute(
                "SELECT * FROM presentations WHERE id=?", (authorization["presentation"],)
            ).fetchone()
            _check(details)
            assert details is not None
            evidence = json.loads(details["snapshot"])
            rendered = json.loads(details["rendered"])
            _check(
                evidence["rendered_digest"] == hashlib.sha256(_json(rendered).encode()).hexdigest()
            )
            audit = {
                k: v
                for k, v in evidence.items()
                if k
                not in {
                    "evidence_digest",
                    "viewer",
                    "readout_revision",
                    "readout",
                    "rendered_digest",
                    "chart_context",
                    "release_settings_file",
                }
            }
            digest = hashlib.sha256(
                _json({"audit": audit, "readout": evidence["readout"]}).encode()
            ).hexdigest()
            _check(
                digest == evidence["evidence_digest"]
                and details["scope"] == digest
                and self.opaque("rev", digest)
                == decision["revision"]
                == details["revision"]
                == evidence["readout_revision"]
                and details["presented"] <= decision["decided"] < details["expires"]
                and details["actor"] == decision["actor"] == evidence["viewer"]
                and authorization["engagement"] == evidence["engagement"]
                and authorization["dataset"] == evidence["dataset_id"]
                and authorization["dataset_revision"] == evidence["dataset_revision"]
                and decision["audience"] == evidence["recipient"]
            )
            return {
                "decision": dict(decision),
                "evidence": evidence,
                "presented_at": details["presented"],
                "actionable_until": details["expires"],
                "rendered": rendered,
                "presentation": details["id"],
                "delivery": {
                    "destination_sha256": authorization["destination"],
                    "permitted_fields": json.loads(authorization["fields"]),
                    "submitted_at": authorization["transmitted"],
                    "confirmed_at": authorization["confirmed"],
                    "downstream_ref": authorization["confirmation_ref"],
                },
            }

    def confirm_delivery(self, receipt: str, reference: str) -> None:
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.confirm_delivery_in(db, receipt, reference)

    @staticmethod
    def confirm_delivery_in(db: sqlite3.Connection, receipt: str, reference: str) -> None:
        _check(isinstance(reference, str) and LOCAL_ID.fullmatch(reference))
        row = db.execute(
            "SELECT d.delivery,a.confirmation_ref FROM decisions d JOIN "
            "authorizations a USING(receipt) WHERE receipt=? AND a.transmitted IS NOT NULL",
            (receipt,),
        ).fetchone()
        _check(
            row
            and row[0] in {"sending", "uncertain", "failed", "accepted", "downstream_confirmed"}
            and (row[1] is None or row[1] == reference)
        )
        db.execute(
            "UPDATE authorizations SET confirmed=COALESCE(confirmed,?),confirmation_ref=? "
            "WHERE receipt=?",
            (int(time.time()), reference, receipt),
        )
        db.execute(
            "UPDATE decisions SET delivery='downstream_confirmed' WHERE receipt=?", (receipt,)
        )


def _hook_path(hook: str) -> str:
    parsed = urlsplit(hook)
    _check(
        parsed.scheme == "https"
        and parsed.netloc == "hooks.zapier.com"
        and not parsed.query
        and not parsed.fragment
        and re.fullmatch(r"/hooks/catch/[0-9]{1,24}/[A-Za-z0-9_-]{2,100}/?", parsed.path)
    )
    return parsed.path


def hook_fingerprint(hook: str) -> str:
    return hashlib.sha256(("https://hooks.zapier.com" + _hook_path(hook)).encode()).hexdigest()


@contextmanager
def _evidence_guard(
    store: Any,
    dataset: str | None,
    dataset_revision: str | None,
    engagement: str,
    audit: Json | None = None,
) -> Iterator[None]:
    if audit and audit.get("mode") in {"managed_rule", "managed_model"}:
        from opentour_cell.pilot.repair_workflow import check_managed_review

        with store.engagement_guard(engagement_id=engagement):
            try:
                check_managed_review(
                    store.path.parent,
                    store.organization,
                    audit["job_id"],
                    audit["recipient"],
                    audit["team"],
                    audit["channel"],
                    audit["run_revision"],
                )
            except (OSError, ValueError, KeyError):
                raise Refused("Managed readout unavailable.") from None
            yield
    else:
        with store.engagement_guard(dataset, dataset_revision, engagement_id=engagement):
            yield


def flush_receipts(
    journal: Journal,
    hook: str,
    *,
    config: Callable[[], Settings] | None = None,
    training: Any = None,
    include_disposition: bool | None = None,
    stopping: Callable[[], bool] | None = None,
) -> int:
    """Claim at most once, under the engagement lock shared with withdrawal."""
    destination = hook_fingerprint(hook)
    _check(config is not None and training is not None)
    assert config is not None
    initial = config()
    _check(journal.binding == initial.binding and training.organization == initial.organization)
    if initial.zapier and initial.zapier_destination_sha256:
        _check(initial.zapier_destination_sha256 == destination)
    with journal.db() as db:
        pending = db.execute(
            "SELECT d.receipt,d.disposition,d.actor,d.audience,a.engagement,"
            "a.dataset,a.dataset_revision,a.destination,a.fields FROM decisions d JOIN "
            "authorizations a USING(receipt) WHERE d.delivery='pending' AND d.attempts=0 "
            "ORDER BY d.rowid LIMIT 50"
        ).fetchall()
    count = 0
    for row in pending:
        if stopping is not None and stopping():
            break
        receipt = row["receipt"]
        try:
            historical = journal.receipt(receipt)
            with _evidence_guard(
                training,
                row["dataset"],
                row["dataset_revision"],
                row["engagement"],
                historical["evidence"],
            ):
                current = config()
                _check(
                    journal.binding == current.binding
                    and training.organization == current.organization
                )
                entry = current.permission(row["actor"], "review", row["dataset"])
                _check(
                    entry["aggregate"]
                    and "inspect" in entry["roles"]
                    and current.zapier
                    and current.reviewer == row["actor"]
                    and current.recipient == row["audience"]
                    and row["destination"] == current.zapier_destination_sha256 == destination
                )
                fields = json.loads(row["fields"])
                _check(fields in (["event", "receipt"], ["event", "receipt", "disposition"]))
                body = {"event": "pilot_decision_recorded", "receipt": receipt}
                if (
                    "disposition" in fields
                    and current.zapier_disposition
                    and include_disposition is not False
                ):
                    body["disposition"] = {
                        "approved": "approved_for_readout",
                        "changes_requested": "changes_requested",
                    }[row["disposition"]]
                with journal.db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    journal.active(db)
                    claimed = db.execute(
                        "UPDATE decisions SET attempts=1,delivery='sending' "
                        "WHERE receipt=? AND attempts=0 AND delivery='pending'",
                        (receipt,),
                    ).rowcount
                    if claimed:
                        db.execute(
                            "UPDATE authorizations SET transmitted=? WHERE receipt=?",
                            (int(time.time()), receipt),
                        )
        except (ValueError, KeyError):
            with journal.db() as db:
                db.execute(
                    "UPDATE decisions SET delivery='suppressed' "
                    "WHERE receipt=? AND delivery='pending'",
                    (receipt,),
                )
            continue
        if not claimed:
            continue
        # Admission is committed before network I/O. A concurrent withdrawal owns reconciliation.
        try:
            with closing(http.client.HTTPSConnection("hooks.zapier.com", timeout=5)) as connection:
                connection.request(
                    "POST",
                    _hook_path(hook),
                    body=_json(body),
                    headers={"Content-Type": "application/json"},
                )
                status = connection.getresponse().status
            delivery = "accepted" if 200 <= status < 300 else "failed"
        except (OSError, http.client.HTTPException):
            delivery = "uncertain"
        with journal.db() as db:
            db.execute(
                "UPDATE decisions SET delivery=? WHERE receipt=? AND delivery "
                "IN ('sending','uncertain')",
                (delivery, receipt),
            )
        count += delivery == "accepted"
    return count


class ReceiptDrainer:
    """Single optional outbound worker. Its wake-up never performs I/O on the Slack callback."""

    def __init__(
        self,
        journal: Journal,
        config: Callable[[], Settings],
        training: Any,
        hook: Callable[[], str],
        interval: float = 30,
    ):
        self.journal = journal
        self.config = config
        self.training = training
        self.hook = hook
        self.interval = interval
        self._pending = threading.Event()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pilot-receipts", daemon=True)

    def start(self) -> None:
        self.wake()
        self._thread.start()

    def wake(self) -> None:
        self._pending.set()

    def _run(self) -> None:
        while not self._stopped.is_set():
            self._pending.wait(self.interval)
            self._pending.clear()
            if self._stopped.is_set():
                return
            try:
                count = flush_receipts(
                    self.journal,
                    self.hook(),
                    config=self.config,
                    training=self.training,
                    stopping=self._stopped.is_set,
                )
                if count == 50:
                    self.wake()
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                # A missing secret, permission or busy store does not affect a completed decision.
                continue

    def close(self) -> None:
        self._stopped.set()
        self._pending.set()
        if self._thread.is_alive():
            self._thread.join()


def withdraw_for_custody(directory: Path, organization: str) -> dict[str, int]:
    """Invalidate action grants and stop the journal after the organization's training hold."""
    totals = dict.fromkeys(
        (
            "slack_exposed",
            "slack_possible",
            "zapier_accepted",
            "zapier_uncertain",
            "zapier_suppressed",
            "decisions",
            "legacy_unknown",
            "local_journal_records",
        ),
        0,
    )
    _check(directory.is_absolute() and not directory.is_symlink())
    private = directory.stat()
    _check(
        stat.S_ISDIR(private.st_mode)
        and private.st_uid == os.getuid()
        and private.st_mode & 0o077 == 0
    )
    path = directory / "slack-pilot.sqlite3"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return totals
    _check(
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and info.st_uid == os.getuid()
        and info.st_mode & 0o077 == 0
    )
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as read:
        binding = read.execute("SELECT binding FROM identity WHERE id=1").fetchone()
        _check(binding)
        assert binding is not None
        identity = json.loads(binding[0])
        _check(
            len(identity) == 5 and identity[0] == organization and identity[-1] == str(directory)
        )
    journal = Journal(path, binding[0])
    with journal.db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE journal_control SET held=1 WHERE id=1")
        db.execute("DELETE FROM contexts")
        db.execute("UPDATE approval_posts SET state='uncertain' WHERE state='claimed'")
        db.execute("UPDATE decisions SET delivery='suppressed' WHERE delivery='pending'")
        db.execute("UPDATE decisions SET delivery='uncertain' WHERE delivery='sending'")

        def n(query: str) -> int:
            return int(db.execute(query).fetchone()[0])

        totals.update(
            slack_exposed=n("SELECT COUNT(*) FROM exposures WHERE status='presented'"),
            slack_possible=n("SELECT COUNT(*) FROM exposures WHERE status!='presented'"),
            zapier_accepted=n(
                "SELECT COUNT(*) FROM decisions "
                "WHERE delivery IN ('accepted','downstream_confirmed')"
            ),
            zapier_uncertain=n(
                "SELECT COUNT(*) FROM decisions WHERE delivery IN ('uncertain','sending','failed')"
            ),
            zapier_suppressed=n(
                "SELECT COUNT(*) FROM decisions WHERE delivery IN ('suppressed','legacy_hold')"
            ),
            decisions=n("SELECT COUNT(*) FROM decisions"),
            local_journal_records=sum(
                n("SELECT COUNT(*) FROM " + table)
                for table in (
                    "jobs",
                    "presentations",
                    "decisions",
                    "launcher",
                    "inspections",
                    "approval_posts",
                    "exposures",
                )
            ),
            legacy_unknown=n("SELECT legacy_unknown FROM journal_control WHERE id=1"),
        )
    return totals


@dataclass(frozen=True)
class Target:
    revision: str
    dataset: str
    dataset_revision: str
    report: Json
    audit: Json
    evidence_digest: str


class Workbench:
    def __init__(
        self,
        config: Callable[[], Settings],
        web: Any,
        training: Any,
        views: Any,
        submit: Callable[[Callable[[], None]], Any] | None = None,
    ):
        self.config = config
        first = config()
        self.binding = (
            first.organization,
            first.team,
            first.app,
            first.channel,
            str(first.directory),
        )
        self.journal = Journal(first.directory / "slack-pilot.sqlite3", _json(self.binding))
        self.web = web
        self.store = training
        self.views = views
        self.on_decision: Callable[[], None] | None = None
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pilot-training")
        self.submit = submit or self.pool.submit
        self.view_locks = [threading.RLock() for _ in range(31)]

    def settings(self, *, check_engagement: bool = True) -> Settings:
        current = self.config()
        _check(
            self.binding
            == (
                current.organization,
                current.team,
                current.app,
                current.channel,
                str(current.directory),
            )
        )
        if check_engagement:
            _check(self.store.engagement_active())
        return current

    def private_channel(self, cfg: Settings, actor: str | None = None) -> None:
        channel = self.web.conversations_info(channel=cfg.channel)["channel"]
        _check(
            channel.get("id") == cfg.channel
            and channel.get("is_private") is True
            and channel.get("is_shared") is False
            and not any(
                channel.get(k)
                for k in (
                    "is_archived",
                    "is_ext_shared",
                    "is_org_shared",
                    "is_shared",
                    "is_pending_ext_shared",
                )
            )
        )
        if actor is None:
            return
        cursor = ""
        for _ in range(25):
            members = self.web.conversations_members(channel=cfg.channel, limit=200, cursor=cursor)
            if actor in members["members"]:
                return
            cursor = members.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                break
        raise Refused()

    def _authorized_job(self, cfg: Settings, actor: str, job: str) -> Json:
        known = self.journal.job(job)
        cfg.permission(actor, "inspect", known["dataset"])
        _check(
            known["engagement"] == self.store.engagement_id and known["audience"] == cfg.recipient
        )
        return known

    def _managed(self, cfg: Settings, actor: str, source: str) -> Target:
        from opentour_cell.pilot.repair_workflow import (
            managed_review_evidence,
            managed_review_sources,
        )

        entry = cfg.permission(actor, "inspect")
        _check(entry["aggregate"])
        allowed = managed_review_sources(
            cfg.directory, cfg.organization, entry["datasets"], cfg.recipient, cfg.team, cfg.channel
        )
        choices = {key: ds for ds, key in allowed}
        _check(source in choices)
        run = source.removeprefix("managed:")
        value = managed_review_evidence(
            cfg.directory, cfg.organization, run, cfg.recipient, cfg.team, cfg.channel
        )
        dataset, metadata = value["dataset_id"], value["audit"]
        _check(
            dataset == choices[source]
            and metadata.get("run_id") == run
            and metadata["mode"] in {"managed_rule", "managed_model"}
        )
        cfg.permission(actor, "inspect", dataset)
        report = self.views.managed_report(value["readout"])
        recorded = {
            key: metadata[key]
            for key in ("run_revision", "cohort_revision", "aggregate_digest", "brief_digest")
        }
        candidate = metadata.get("candidate_digest")
        _check(
            all(isinstance(d, str) and re.fullmatch(r"[0-9a-f]{64}", d) for d in recorded.values())
            and (
                candidate is None
                or isinstance(candidate, str)
                and re.fullmatch(r"[0-9a-f]{64}", candidate)
            )
            and isinstance(value["dataset_revision"], str)
            and re.fullmatch(r"[0-9a-f]{64}", value["dataset_revision"])
            and metadata["handoff_disposition"] in self.views.MANAGED_OUTCOMES
        )
        audit = {
            **recorded,
            "organization": cfg.organization,
            "engagement": self.store.engagement_id,
            "mode": metadata["mode"],
            "job_id": run,
            "dataset_id": dataset,
            "dataset_revision": value["dataset_revision"],
            "model_revision": candidate,
            "handoff_disposition": metadata["handoff_disposition"],
            "error_ceiling_bps": report["error_ceiling_bps"],
            "population": report["cohort_size"],
            "team": cfg.team,
            "app": cfg.app,
            "channel": cfg.channel,
            "reviewer": cfg.reviewer,
            "recipient": cfg.recipient,
        }
        release = value.get("release_review")
        if isinstance(release, dict) and (
            {k: release.get(k) for k in ("team", "channel", "audience", "reviewer")}
            == {
                "team": cfg.team,
                "channel": cfg.channel,
                "audience": cfg.recipient,
                "reviewer": cfg.reviewer,
            }
            and release.get("provider", "slack") == cfg.approval_provider
            and metadata["handoff_disposition"] in {"simple_repair", "model_candidate"}
        ):
            audit["release_reviewer"] = cfg.reviewer
            audit["release_provider"] = cfg.approval_provider
        reason = metadata.get("decision_reason")
        if isinstance(reason, str) and reason in self.views.MANAGED_REASONS:
            audit["decision_reason"] = reason
        pairs = metadata.get("comparisons", {})
        _check(isinstance(pairs, dict) and pairs.keys() <= {"rule", "model"})
        audit["comparisons"] = {
            name: self.views.managed_report(pair) for name, pair in pairs.items()
        }
        cohorts = metadata.get("comparison_cohorts")
        if (
            isinstance(cohorts, dict)
            and cohorts.keys() == pairs.keys()
            and cohorts
            and all(digest == recorded["cohort_revision"] for digest in cohorts.values())
        ):
            audit["comparison_cohorts"] = dict(cohorts)
        if metadata.get("operating_point") is not None:
            audit["operating_point"] = self.views.managed_operating_point(
                metadata["operating_point"], report["error_ceiling_bps"]
            )
            quality = metadata.get("decision_quality_digest")
            if quality is not None:
                _check(isinstance(quality, str) and re.fullmatch(r"[0-9a-f]{64}", quality))
                audit["decision_quality_digest"] = quality
        if metadata.get("workflow_benchmark"):
            from opentour_cell.pilot.slack_benchmark import validated_benchmark

            audit["workflow_benchmark"] = validated_benchmark(
                metadata["workflow_benchmark"],
                cohort_revision=recorded["cohort_revision"],
                population=report["cohort_size"],
                candidate_arm="model" if metadata["mode"] == "managed_model" else "simple",
                candidate_revision=candidate,
            )
        return self._target(dataset, value["dataset_revision"], report, audit)

    def _target(self, dataset: str, dataset_revision: str, report: Json, audit: Json) -> Target:
        digest = hashlib.sha256(_json({"audit": audit, "readout": report}).encode()).hexdigest()
        revision = self.journal.opaque("rev", digest)
        return Target(
            revision,
            dataset,
            dataset_revision,
            report,
            {**audit, "evidence_digest": digest},
            digest,
        )

    def _readout(self, cfg: Settings, actor: str, job: str) -> Target | None:
        if not job:
            return None
        if job.startswith("managed:"):
            return self._managed(cfg, actor, job)
        evaluation = job.startswith("eval:")
        dataset = job[5:] if evaluation else self._authorized_job(cfg, actor, job)["dataset"]
        cfg.permission(actor, "inspect", dataset)
        registration = self.store.dataset_summary(dataset)
        if evaluation:
            _check(registration["mode"] == "evaluation_only")
            try:
                result = self.store.frozen_evaluation(dataset)
            except ValueError:
                return None
            status = {
                "dataset_revision": result["dataset_revision"],
                "revision": result["aggregate_digest"],
                "profile": "supplied_outputs",
                "epochs": None,
                "cpu_limit_seconds": None,
                "max_train_updates": None,
            }
        else:
            status = self.store.status(job)
            _check(status["dataset_id"] == dataset)
            if status["status"] != "completed":
                return None
            try:
                # Verify the stored model and frozen cohort, rescore locally and reconcile its
                # aggregate with the recorded one. Only the verified digest and counts reach Slack.
                result = self.store.frozen_training(job)
            except ValueError:
                return None
        report = result["readout"]
        _check(registration["dataset_revision"] == status["dataset_revision"])
        _check(
            result["dataset_id"] == dataset
            and result["dataset_revision"] == status["dataset_revision"]
            and result["cohort_digest"] == registration["cohort_digest"]
        )
        audit = {
            "engagement": self.store.engagement_id,
            "organization": cfg.organization,
            "mode": "evaluation_only" if evaluation else "training",
            "job_id": None if evaluation else job,
            "dataset_id": dataset,
            "dataset_revision": status["dataset_revision"],
            "model_revision": result["artifact_digest"],
            "profile": status["profile"],
            "proposal_revision": None if evaluation else status["revision"],
            "cohort_revision": registration["cohort_digest"],
            "aggregate_digest": result["aggregate_digest"],
            "brief_digest": result["brief_digest"],
            "epochs": status["epochs"],
            "cpu_limit_seconds": status["cpu_limit_seconds"],
            "max_train_updates": status["max_train_updates"],
            "error_ceiling_bps": report["error_ceiling_bps"],
            "population": report["cohort_size"],
            "team": cfg.team,
            "app": cfg.app,
            "channel": cfg.channel,
            "reviewer": cfg.reviewer,
            "recipient": cfg.recipient,
        }
        if "operating_point" in result:
            quality_digest = result.get("decision_quality_digest")
            _check(
                isinstance(quality_digest, str) and re.fullmatch(r"[0-9a-f]{64}", quality_digest)
            )
            audit["operating_point"] = result["operating_point"]
            audit["decision_quality_digest"] = quality_digest
        return self._target(dataset, status["dataset_revision"], report, audit)

    def _policy(self, cfg: Settings, actor: str) -> str:
        scope = [cfg.team, cfg.channel, cfg.roles.get(actor), cfg.reviewer, cfg.recipient]
        if not cfg.explicit_recipient:
            scope.append("local-evaluation-only")
        return self.journal.opaque("policy", scope)

    def _catalog(self, cfg: Settings, actor: str, search: str = "") -> list[Json]:
        entry = cfg.permission(actor, "inspect")
        _check(isinstance(search, str) and len(search) <= self.views.SEARCH_LIMIT)
        return list(self.store.catalog(query=search, allowed_ids=entry["datasets"]))

    def _result_sources(self, cfg: Settings, actor: str) -> list[tuple[str, str]]:
        entry = cfg.permission(actor, "inspect")
        active = set()
        sources = []
        for dataset in sorted(set(entry["datasets"])):
            try:
                data = self.store.dataset_summary(dataset)
            except ValueError as error:
                if str(error) in {"dataset_unavailable", "operating_agreement_unverified"}:
                    continue
                raise
            active.add(dataset)
            if data["mode"] == "evaluation_only" and entry["aggregate"]:
                sources.append((dataset, "eval:" + dataset))
        sources.extend(self.journal.review_jobs(active, self.store.engagement_id, cfg.recipient))
        if entry["aggregate"]:
            from opentour_cell.pilot.repair_workflow import managed_review_sources

            sources.extend(
                managed_review_sources(
                    cfg.directory,
                    cfg.organization,
                    entry["datasets"],
                    cfg.recipient,
                    cfg.team,
                    cfg.channel,
                )
            )
        return sorted(sources)

    def _recorded_decision(self, revision: str) -> tuple[str, Json]:
        receipt = self.journal.decision(revision)
        if not receipt:
            return "ready", {}
        try:
            if self.journal.receipt(receipt["receipt"])["evidence"] is not None:
                return str(receipt["disposition"]), receipt
        except Refused:
            pass
        return "uncertain", {}

    def _result_state(self, cfg: Settings, actor: str, source: str) -> tuple[str, str]:
        if not source.startswith(("eval:", "managed:")):
            state = self.store.status(source)
            if state["status"] != "completed":
                label = {
                    "awaiting_approval": "Awaiting trainer confirmation",
                    "running": "Training in progress",
                    "failed": "Training failed",
                }.get(state["status"], "Run requires verification")
                return str(state["revision"]), label
            if not cfg.permission(actor, "inspect", state["dataset_id"])["aggregate"]:
                return str(state["revision"]), "Completed · status access"
        target = self._readout(cfg, actor, source)
        if target is None:
            return "", "Evidence unavailable · hold"
        disposition, _ = self._recorded_decision(target.revision)
        labels = {
            "ready": "Ready for evidence review",
            "uncertain": "Decision requires verification",
            "approved": "Cleared for readout",
            "changes_requested": "Changes requested",
        }
        return target.revision, labels[disposition]

    def _selected(self, cfg: Settings, actor: str, view: str, source: str, old_hash: str) -> None:
        if not source.startswith(("eval:", "managed:")):
            state = self.store.status(source)
            entry = cfg.permission(actor, "inspect", state["dataset_id"])
            if state["status"] != "completed" or not entry["aggregate"]:
                self._status(cfg, actor, view, source, old_hash)
                return
        self._home(cfg, actor, view, source, old_hash)

    def _results(
        self, cfg: Settings, actor: str, view: str, old_hash: str = "", page: int = 0
    ) -> None:
        sources = self._result_sources(cfg, actor)
        if not sources:
            self._home(cfg, actor, view, "", old_hash)
            return
        if len(sources) == 1:
            self._selected(cfg, actor, view, sources[0][1], old_hash)
            return
        page = max(0, min(page, (len(sources) - 1) // RESULT_PAGE_SIZE))
        start = page * RESULT_PAGE_SIZE
        rows = []
        pins = {}
        for dataset, source in sources[start : start + RESULT_PAGE_SIZE]:
            revision, state = self._result_state(cfg, actor, source)
            token = self.journal.opaque("item", [source, revision, state])
            pins[token] = [source, revision, state]
            rows.append(
                {
                    "dataset": dataset,
                    "method": "Managed workflow comparison"
                    if source.startswith("managed:")
                    else (
                        "Supplied comparison"
                        if source.startswith("eval:")
                        else "CPU training job · " + source
                    ),
                    "state": state,
                    "reference": revision or source,
                    "token": token,
                }
            )
        ctx = self.journal.context(
            actor, cfg.channel, view, "results", revision=_json({"page": page, "items": pins})
        )
        self._show(
            view,
            self.views.result_chooser(
                ctx, rows, previous=bool(page), more=start + len(rows) < len(sources)
            ),
            old_hash,
        )

    def _help(
        self, cfg: Settings, actor: str, view: str, message: str = "", old_hash: str = ""
    ) -> None:
        cfg.permission(actor, "inspect")
        ctx = self.journal.context(actor, cfg.channel, view, "help")
        self._show(view, self.views.reviewer_help(ctx, message), old_hash)

    def _repair_preview(self, cfg: Settings, actor: str, view: str, dataset: str) -> None:
        from opentour_cell.pilot.managed_launch import managed_launch_summary

        cfg.permission(actor, "inspect", dataset)
        cfg.permission(actor, "tune", dataset)
        summary = managed_launch_summary(
            cfg.directory, cfg.organization, dataset, actor, cfg.team, cfg.channel
        )
        _check(
            summary["dataset_id"] == dataset
            and summary["trainer"] == actor
            and summary["scope"] in {"simple_repair", "conditional_open_weight"}
            and re.fullmatch(r"[0-9a-f]{64}", summary["plan_revision"])
            and type(summary["expires_at"]) is int
            and summary["expires_at"] > time.time()
        )
        ctx = self.journal.context(
            actor, cfg.channel, view, "repair_preview", dataset, summary["plan_revision"]
        )
        self._show(view, self.views.managed_launch_preview(ctx, summary))

    def _repair_progress(
        self, cfg: Settings, actor: str, view: str, dataset: str, run: str, old_hash: str = ""
    ) -> None:
        from opentour_cell.pilot.managed_launch import managed_status_from_slack

        entry = cfg.permission(actor, "inspect", dataset)
        cfg.permission(actor, "tune", dataset)
        status = managed_status_from_slack(
            cfg.directory, cfg.organization, dataset, actor, cfg.team, cfg.channel, run
        )
        _check(status.get("run_id") == run)
        ctx = self.journal.context(actor, cfg.channel, view, "repair_progress", dataset, run)
        can_review = bool(
            entry["aggregate"]
            and status["state"] in {"ready", "held"}
            and (dataset, "managed:" + run) in self._result_sources(cfg, actor)
        )
        self._show(
            view,
            self.views.managed_launch_status(ctx, run, status["state"], review_visible=can_review),
            old_hash,
        )

    def _repair_lookup(self, cfg: Settings, actor: str, view: str, run: str) -> None:
        entry = cfg.permission(actor, "inspect")
        if entry["aggregate"] and any(
            source == "managed:" + run for _, source in self._result_sources(cfg, actor)
        ):
            self._home(cfg, actor, view, "managed:" + run)
            return
        cfg.permission(actor, "tune")
        for dataset in entry["datasets"]:
            try:
                self._repair_progress(cfg, actor, view, dataset, run)
            except (KeyError, ValueError, OSError):
                continue
            return
        raise Refused()

    def _search(
        self, cfg: Settings, actor: str, view: str, source: str, query: str, old_hash: str = ""
    ) -> None:
        if len(query) > self.views.SEARCH_LIMIT or (query and not query.isprintable()):
            self._help(
                cfg,
                actor,
                view,
                "Enter up to 64 printable characters to search the authorized catalog.",
                old_hash,
            )
            return
        self._browse(cfg, actor, view, source, search=query, old_hash=old_hash)

    def _questions(
        self,
        cfg: Settings,
        actor: str,
        view: str,
        source: str,
        revision: str,
        topic: str,
        old_hash: str = "",
    ) -> None:
        target = self._readout(cfg, actor, source)
        _check(target and target.revision == revision)
        assert target is not None
        entry = cfg.permission(actor, "inspect", target.dataset)
        _check(
            entry["aggregate"] and self.journal.inspected(actor, revision, self._policy(cfg, actor))
        )
        _check(not topic or topic in self.views.QUESTION_LABELS)
        current = self.settings()
        _check(self._policy(cfg, actor) == self._policy(current, actor))
        current.permission(actor, "inspect", target.dataset)
        token = self.journal.context(actor, cfg.channel, view, "questions", source, revision)
        self._show(
            view,
            self.views.question_modal(token, revision, target.report, target.audit, topic),
            old_hash,
            dataset=target.dataset,
            dataset_revision=target.dataset_revision,
            audit=target.audit,
        )

    def _show(
        self,
        view_id: str,
        modal: Json,
        old_hash: str = "",
        *,
        dataset: str | None = None,
        dataset_revision: str | None = None,
        audit: Json | None = None,
    ) -> str:
        # Record every attempted protected disclosure without persisting projected sample rows.
        exposure = ""
        if modal.get("callback_id"):
            with _evidence_guard(
                self.store, dataset, dataset_revision, self.store.engagement_id, audit
            ):
                exposure = self.journal.admit_view(modal["callback_id"])
        params = {"view_id": view_id, "view": modal}
        if old_hash:
            params["hash"] = old_hash
        try:
            response = self.web.views_update(**params)
            _check(
                response is not None
                and response.get("ok") is True
                and response.get("view", {}).get("id") == view_id
            )
        except Exception:
            if exposure:
                self.journal.finish_view(exposure, "uncertain")
            raise
        if exposure:
            self.journal.finish_view(exposure, "presented")
        return exposure

    def _home(self, cfg: Settings, actor: str, view_id: str, job: str, old_hash: str = "") -> None:
        entry = cfg.permission(actor, "inspect")
        target = self._readout(cfg, actor, job) if entry["aggregate"] else None
        revision = target.revision if target else ""
        ctx = self.journal.context(actor, cfg.channel, view_id, "home", job, revision)
        disposition, receipt = self._recorded_decision(revision) if target else ("pending", {})
        if (
            not target
            and job
            and (
                job.startswith(("eval:", "managed:"))
                or self.store.status(job)["status"] == "completed"
            )
        ):
            disposition = "uncertain"
        show = cfg.channel_aggregates and entry["aggregate"]
        inspected = bool(
            target
            and disposition != "uncertain"
            and self.journal.inspected(actor, revision, self._policy(cfg, actor))
        )
        modal = self.views.home_modal(
            context=ctx,
            revision=revision or None,
            reviewer=cfg.reviewer,
            recipient=cfg.recipient,
            state="changes" if disposition == "changes_requested" else disposition,
            report=target.report if target and show else None,
            channel_aggregates=show,
            private_evidence=entry["aggregate"],
            witnessed=False,
            job_id=job if job and not job.startswith(("eval:", "managed:")) else None,
            evidence_reviewed=inspected,
            dataset_revision=target.dataset_revision if target else None,
            receipt=receipt.get("receipt"),
            tune_allowed="tune" in entry["roles"]
            and not job.startswith(("eval:", "managed:"))
            and any(
                row["can_train"] and self.store.can_propose(row["dataset_id"], actor)
                for row in self._catalog(cfg, actor)
            ),
            audit=target.audit if target else None,
            decision_allowed=actor == cfg.reviewer and "review" in entry["roles"],
            reviewer_navigation=True,
            workspace_title=cfg.workspace_title,
            workflow_label=cfg.workflow_label,
        )
        self._show(
            view_id,
            modal,
            old_hash,
            dataset=target.dataset if target else None,
            dataset_revision=target.dataset_revision if target else None,
            audit=target.audit if target else None,
        )

    def _browse(
        self,
        cfg: Settings,
        actor: str,
        view: str,
        job: str,
        search: str = "",
        selected: str = "",
        old_hash: str = "",
        expected_revision: str = "",
    ) -> None:
        rows = self._catalog(cfg, actor, search)
        detail = None
        evaluation_target = None
        if selected:
            entry = cfg.permission(actor, "inspect", selected)
            selected_row = self.store.dataset_summary(selected)
            if expected_revision:
                _check(selected_row["dataset_revision"] == expected_revision)
            if selected_row["mode"] == "evaluation_only":
                job = "eval:" + selected
                if entry["aggregate"]:
                    evaluation_target = self._readout(cfg, actor, job)
            if entry["preview"] and selected_row["can_train"]:
                detail = self.store.preview(selected)
                _check(
                    detail["dataset_revision"] == selected_row["dataset_revision"]
                    and detail["scope"] == "training_only"
                )
            else:
                detail = {
                    **selected_row,
                    "rows": [],
                    "scope": "training_only" if selected_row["can_train"] else "evaluation_only",
                    "example_limit": 0,
                }
            rows = [selected_row]
        # Bind row choices to the exact revisions returned by this particular catalog render.
        pins = {r["dataset_id"]: r["dataset_revision"] for r in rows}
        ctx = self.journal.context(
            actor,
            cfg.channel,
            view,
            "eval_detail" if evaluation_target else "dataset",
            job,
            evaluation_target.revision if evaluation_target else _json(pins),
        )
        self._show(
            view,
            self.views.dataset_modal(
                context=ctx,
                datasets=rows,
                query=search,
                detail=detail,
                tune_allowed=bool(
                    selected
                    and "tune" in cfg.permission(actor, "inspect")["roles"]
                    and self.store.can_propose(selected, actor)
                ),
                evidence_revision=evaluation_target.revision if evaluation_target else None,
            ),
            old_hash,
            dataset=selected or None,
            dataset_revision=expected_revision or None,
        )

    def _status(self, cfg: Settings, actor: str, view: str, job: str, old_hash: str = "") -> None:
        known = self._authorized_job(cfg, actor, job)
        entry = cfg.permission(actor, "inspect", known["dataset"])
        info = self.store.status(job)
        _check(info["dataset_id"] == known["dataset"])
        can_start = (
            actor == known["requester"]
            and known["audience"] == cfg.recipient
            and known["engagement"] == self.store.engagement_id
            and "tune" in entry["roles"]
            and info["status"] == "awaiting_approval"
            and self.store.can_propose(known["dataset"], actor)
        )
        mode = "proposal" if can_start else "status"
        target = (
            self._readout(cfg, actor, job)
            if info["status"] == "completed" and entry["aggregate"]
            else None
        )
        revision = target.revision if target else info["revision"]
        ctx = self.journal.context(actor, cfg.channel, view, mode, job, revision)
        if can_start:
            modal = self.views.proposal_modal(context=ctx, job=info, recipient=cfg.recipient)
        else:
            # A status-only viewer may see execution state, never an unauthorized readout.
            aggregate = info if entry["aggregate"] else {**info, "result": None}
            if entry["aggregate"] and info["status"] == "completed" and target is None:
                aggregate = {**info, "status": "uncertain", "result": None}
            modal = self.views.status_modal(
                context=ctx,
                job=aggregate,
                evidence_revision=target.revision if target else None,
                result_visible=bool(entry["aggregate"]),
            )
        self._show(
            view,
            modal,
            old_hash,
            dataset=known["dataset"],
            dataset_revision=info["dataset_revision"],
        )

    def _evidence(self, cfg: Settings, actor: str, view: str, ctx: Json, old_hash: str) -> None:
        target = self._readout(cfg, actor, ctx["job"])
        _check(target and target.revision == ctx["revision"])
        assert target is not None
        current = self.settings()
        _check(
            current.binding == cfg.binding
            and self._policy(current, actor) == self._policy(cfg, actor)
        )
        entry = current.permission(actor, "inspect", target.dataset)
        _check(entry["aggregate"] and self.views.evidence_available(target.report))
        token = self.journal.context(
            actor, cfg.channel, view, "evidence", ctx["job"], target.revision
        )
        named_reviewer = current.reviewer == actor and "review" in entry["roles"]
        charts = bool(target.audit.get("comparison_cohorts") and named_reviewer)
        view_audit = {**target.audit, "viewer": actor}
        if not named_reviewer:
            view_audit.pop("workflow_benchmark", None)
        modal = self.views.evidence_modal(
            context=token,
            revision=target.revision,
            report=target.report,
            recipient=cfg.recipient,
            dataset_revision=target.dataset_revision,
            audit=view_audit,
            questions=True,
            release_allowed=current.explicit_recipient
            and named_reviewer
            and actor == target.audit.get("release_reviewer"),
            charts_allowed=charts,
            benchmark_allowed=named_reviewer,
            workflow=cfg.workflow_label,
        )
        proof = self._show(
            view,
            modal,
            old_hash,
            dataset=target.dataset,
            dataset_revision=target.dataset_revision,
            audit=target.audit,
        )
        with _evidence_guard(
            self.store,
            target.dataset,
            target.dataset_revision,
            target.audit["engagement"],
            target.audit,
        ):
            current = self.config()
            _check(
                self._policy(cfg, actor) == self._policy(current, actor)
                and current.binding == cfg.binding
            )
            current.permission(actor, "inspect", target.dataset)
            rendered = {k: v for k, v in modal.items() if k != "private_metadata"}
            snapshot = {
                **target.audit,
                "viewer": actor,
                "readout_revision": target.revision,
                "readout": target.report,
                "rendered_digest": hashlib.sha256(_json(rendered).encode()).hexdigest(),
            }
            if charts:
                snapshot["chart_context"] = self.journal.opaque("chartview", token)
            if target.audit.get("release_reviewer") and current.config_path is not None:
                snapshot["release_settings_file"] = str(current.config_path)
            self.journal.present(
                proof,
                actor,
                target.revision,
                self._policy(cfg, actor),
                target.evidence_digest,
                snapshot,
                rendered,
                entry["expires_at"],
            )

        if (
            current.explicit_recipient
            and actor == target.audit.get("release_reviewer")
            and "review" in entry["roles"]
            and (target.audit.get("release_provider") == "zapier_slack")
        ):
            from opentour_cell.pilot.zapier_approval import ApprovalBridge

            # The presentation is durable and the engagement write lock is released before staging.
            try:
                bridge = ApprovalBridge(
                    current.directory,
                    current.organization,
                    current.directory / "zapier-approval.json",
                )
                bridge.issue(
                    run_id=target.audit["job_id"],
                    run_revision=target.audit["run_revision"],
                    artifact_sha=target.audit["model_revision"],
                    inspection_ref=target.revision,
                )
            except (OSError, ValueError, KeyError):
                self._show(
                    view,
                    self.views.plugin_held_modal(modal),
                    old_hash,
                    dataset=target.dataset,
                    dataset_revision=target.dataset_revision,
                    audit=target.audit,
                )

    def _charts(
        self, cfg: Settings, actor: str, view: str, ctx: Json, revision: str, old_hash: str
    ) -> None:
        from opentour_cell.pilot.slack_charts import decision_charts

        _check(
            ctx["kind"] == "evidence"
            and ctx["job"].startswith("managed:")
            and revision == ctx["revision"]
            and cfg.reviewer == actor
            and cfg.permission(actor, "review")["aggregate"]
        )
        context = self.journal.opaque("chartview", ctx["id"])
        policy = self._policy(cfg, actor)
        _check(self.journal.inspected(actor, revision, policy, chart_context=context))
        target = self._managed(cfg, actor, ctx["job"])
        _check(target.revision == revision and target.audit.get("comparison_cohorts"))
        payload = decision_charts(target.report, target.audit, revision)
        self.private_channel(cfg, actor)
        status = "numeric"
        with _evidence_guard(
            self.store,
            target.dataset,
            target.dataset_revision,
            target.audit["engagement"],
            target.audit,
        ):
            current = self.settings(check_engagement=False)
            _check(
                current.reviewer == actor
                and current.permission(actor, "review", target.dataset)["aggregate"]
                and current.permission(actor, "inspect", target.dataset)["aggregate"]
                and policy == self._policy(current, actor)
                and self.journal.inspected(actor, revision, policy, chart_context=context)
            )
            exposure = self.journal.claim_charts(ctx["id"])
            if not exposure:
                return
            if any(block["type"] == "data_visualization" for block in payload["blocks"]):
                try:
                    response = self.web.chat_postEphemeral(
                        channel=current.channel, user=actor, **payload
                    )
                    _check(
                        response is not None
                        and response.get("ok") is True
                        and response.get("message_ts")
                    )
                    status = "accepted"
                except Exception:
                    # Slack may have accepted an unacknowledged delivery. Never repeat this click.
                    status = "uncertain"
            self.journal.finish_view(exposure, status)
        current = self.settings()
        _check(
            current.reviewer == actor
            and policy == self._policy(current, actor)
            and self.journal.inspected(actor, revision, policy, chart_context=context)
        )
        self._show(
            view,
            self.views.chart_status(ctx["id"], revision, payload["text"], status),
            old_hash,
            dataset=target.dataset,
            dataset_revision=target.dataset_revision,
            audit=target.audit,
        )

    def _release(
        self, cfg: Settings, actor: str, view: str, ctx: Json, action: str, old_hash: str
    ) -> None:
        from opentour_cell.pilot.release_gate import record_release

        target = self._readout(cfg, actor, ctx["job"])
        _check(target and target.revision == ctx["revision"])
        assert target is not None
        current = self.settings()
        entry = current.permission(actor, "review", target.dataset)
        current.permission(actor, "inspect", target.dataset)
        _check(
            current.binding == cfg.binding
            and current.reviewer == actor
            and actor == target.audit.get("release_reviewer")
            and current.explicit_recipient
            and current.approval_provider == target.audit.get("release_provider") == "slack"
            and entry["aggregate"]
            and self.journal.inspected(actor, target.revision, self._policy(current, actor))
        )
        saved = record_release(
            current.directory,
            current.organization,
            run_id=target.audit["job_id"],
            run_revision=target.audit["run_revision"],
            artifact_sha=target.audit["model_revision"],
            reviewer=actor,
            team=current.team,
            channel=current.channel,
            audience=current.recipient,
            decision="approved" if action == "ot_release_approve" else "changes_requested",
            inspection_ref=target.revision,
            provider="slack",
        )
        token = self.journal.context(
            actor, current.channel, view, "release_recorded", ctx["job"], target.revision
        )
        self._show(
            view,
            self.views.release_recorded(
                token, saved["decision"], target.audit["model_revision"], current.recipient
            ),
            old_hash,
            dataset=target.dataset,
            dataset_revision=target.dataset_revision,
            audit=target.audit,
        )

    def _decide(
        self, cfg: Settings, actor: str, view: str, ctx: Json, action: str, old_hash: str
    ) -> None:
        target = self._readout(cfg, actor, ctx["job"])
        _check(target and target.revision == ctx["revision"])
        assert target is not None
        with _evidence_guard(
            self.store,
            target.dataset,
            target.dataset_revision,
            target.audit["engagement"],
            target.audit,
        ):
            current = self.config()
            _check(
                cfg.binding == current.binding
                and self._policy(cfg, actor) == self._policy(current, actor)
            )
            recorded = self.journal.decide(
                current,
                actor,
                target,
                self._policy(current, actor),
                "approved" if action == "ot_approve" else "changes_requested",
            )
        if recorded["exportable"] and self.on_decision:
            self.on_decision()
        self._home(cfg, actor, view, ctx["job"], old_hash)

    def _action(
        self,
        cfg: Settings,
        actor: str,
        view: str,
        ctx: Json,
        action: str,
        value: str,
        old_hash: str,
    ) -> None:
        job = ctx["job"]
        if action == "ot_help":
            self._help(cfg, actor, view, old_hash=old_hash)
        elif action == "ot_readouts":
            self._results(cfg, actor, view, old_hash)
        elif action in {"ot_result_prev", "ot_result_next", "ot_result_open"}:
            _check(ctx["kind"] == "results")
            listing = json.loads(ctx["revision"])
            if action == "ot_result_open":
                _check(value in listing["items"])
                source, revision, state = listing["items"][value]
                _check(source in {s for _, s in self._result_sources(cfg, actor)})
                if self._result_state(cfg, actor, source) != (revision, state):
                    self._results(cfg, actor, view, old_hash, listing["page"])
                    return
                self._selected(cfg, actor, view, source, old_hash)
            else:
                self._results(
                    cfg,
                    actor,
                    view,
                    old_hash,
                    listing["page"] + (1 if action == "ot_result_next" else -1),
                )
        elif action == "ot_repair_confirm":
            from opentour_cell.pilot.managed_launch import start_managed_from_slack

            _check(ctx["kind"] == "repair_preview" and value == ctx["revision"])
            current = self.settings()
            current.permission(actor, "inspect", job)
            current.permission(actor, "tune", job)
            self.private_channel(current, actor)
            key = self.journal.opaque("start", [actor, job, current.team, current.channel, value])
            started = start_managed_from_slack(
                current.directory,
                current.organization,
                job,
                actor,
                current.team,
                current.channel,
                value,
                key,
            )
            _check(re.fullmatch(r"run-[0-9a-f]{32}", started["run_id"]))
            self._repair_progress(current, actor, view, job, started["run_id"], old_hash)
        elif action == "ot_repair_refresh":
            _check(ctx["kind"] == "repair_progress" and value == ctx["revision"])
            self._repair_progress(cfg, actor, view, job, value, old_hash)
        elif action == "ot_home":
            if ctx["kind"] in {"repair_preview", "repair_progress"}:
                self._results(cfg, actor, view, old_hash)
            else:
                self._home(cfg, actor, view, job, old_hash)
        elif action in {"ot_browse", "ot_dataset_back"}:
            self._browse(cfg, actor, view, job, old_hash=old_hash)
        elif action == "ot_dataset_open":
            _check(ctx["kind"] == "dataset")
            pins = json.loads(ctx["revision"])
            _check(value in pins)
            self._browse(
                cfg,
                actor,
                view,
                job,
                selected=value,
                old_hash=old_hash,
                expected_revision=pins[value],
            )
        elif action in {"ot_tune", "ot_tune_dataset"}:
            cfg.permission(actor, "tune")
            rows = [
                r
                for r in self._catalog(cfg, actor)
                if r["can_train"] and self.store.can_propose(r["dataset_id"], actor)
            ]
            if action == "ot_tune_dataset":
                _check(ctx["kind"] == "dataset")
                pins = json.loads(ctx["revision"])
                cfg.permission(actor, "tune", value)
                row = self.store.dataset_summary(value)
                _check(
                    row["can_train"]
                    and pins.get(value) == row["dataset_revision"]
                    and self.store.can_propose(value, actor)
                )
                rows = [row]
            pins = {r["dataset_id"]: r["dataset_revision"] for r in rows}
            token = self.journal.context(actor, cfg.channel, view, "tune", job, _json(pins))
            self._show(view, self.views.tune_modal(context=token, datasets=rows), old_hash)
        elif action in {"ot_status", "ot_status_refresh"}:
            _check(job and value in {job, "home", ctx["revision"]})
            self._status(cfg, actor, view, job, old_hash)
        elif action == "ot_evidence":
            _check(
                ctx["kind"] in {"home", "evidence", "status", "eval_detail", "questions"}
                and value == ctx["revision"]
            )
            self._evidence(cfg, actor, view, ctx, old_hash)
        elif action == "ot_question" or action.startswith("ot_question_"):
            _check(ctx["kind"] in {"evidence", "questions"} and value == ctx["revision"])
            topic = "" if action == "ot_question" else action.removeprefix("ot_question_")
            self._questions(cfg, actor, view, job, ctx["revision"], topic, old_hash)
        elif action in {"ot_release_approve", "ot_release_changes"}:
            _check(ctx["kind"] == "evidence" and value == ctx["revision"])
            self._release(cfg, actor, view, ctx, action, old_hash)
        elif action in {"ot_approve", "ot_changes"}:
            _check(ctx["kind"] in {"home", "evidence"} and value == ctx["revision"])
            self._decide(cfg, actor, view, ctx, action, old_hash)
        else:
            raise Refused()

    @staticmethod
    def _field(values: Json, key: str, *, select: bool = False) -> str:
        item = values[key][key]
        value = item["selected_option"]["value"] if select else item.get("value", "")
        _check(isinstance(value, str) and len(value) <= 128)
        return str(value)

    def _submission(
        self,
        cfg: Settings,
        actor: str,
        view: str,
        ctx: Json,
        callback: str,
        values: Json,
        token: str,
    ) -> None:
        if callback == "ot_prompt_enter":
            _check(ctx["kind"] == "prompt")
            raw = values["command"]["command"]["value"]
            _check(isinstance(raw, str) and len(raw) <= 160 and raw.isprintable())
            self._dispatch_prompt(cfg, actor, view, raw.strip())
        elif callback == "ot_dataset_search":
            _check(ctx["kind"] == "dataset")
            query = values.get("query", {}).get("query", {}).get("value")
            _check(query is None or isinstance(query, str))
            self._search(cfg, actor, view, ctx["job"], query or "")
        elif callback == "ot_tune_preview":
            _check(ctx["kind"] == "tune")
            dataset = self._field(values, "dataset", select=True)
            cfg.permission(actor, "tune", dataset)
            pins = json.loads(ctx["revision"])
            current = self.store.dataset_summary(dataset)
            _check(current["can_train"] and pins.get(dataset) == current["dataset_revision"])
            profile = self._field(values, "profile", select=True)
            epoch = self._field(values, "epochs", select=True)
            _check(profile == "cpu_text_v1" and epoch in {"1", "3", "5"})
            info = self.store.propose(
                dataset,
                profile=profile,
                epochs=int(epoch),
                request_id=self.journal.opaque("req", [token, actor, callback]),
                authorized_trainer=actor,
            )
            _check(info["dataset_revision"] == pins[dataset])
            self.journal.bind_job(
                info["job_id"], dataset, actor, cfg.recipient, self.store.engagement_id
            )
            self._status(cfg, actor, view, info["job_id"])
        elif callback == "ot_tune_start":
            _check(ctx["kind"] == "proposal")
            job = ctx["job"]
            known = self.journal.job(job)
            _check(
                known["requester"] == actor
                and known["audience"] == cfg.recipient
                and known["engagement"] == self.store.engagement_id
            )
            cfg.permission(actor, "tune", known["dataset"])
            info = self.store.status(job)
            _check(info["revision"] == ctx["revision"])
            if info["status"] != "awaiting_approval":
                self._status(cfg, actor, view, job)
                return

            def work() -> None:
                try:
                    now = self.settings()
                    self.private_channel(now, actor)
                    now.permission(actor, "tune", known["dataset"])
                    _check(
                        now.recipient == known["audience"]
                        and self.store.engagement_id == known["engagement"]
                    )
                    self.store.run(job, reviewer=actor, expected_revision=ctx["revision"])
                except Exception:
                    self._notice_if_current(token, actor, cfg.channel, view)
                    return
                with self.view_locks[hash(view) % len(self.view_locks)]:
                    try:
                        self.journal.read_context(token, actor, cfg.channel, view)
                    except Refused:
                        return
                    try:
                        now = self.settings()
                        self.private_channel(now, actor)
                        self._home(now, actor, view, job)
                    except Exception:
                        self._safe_notice(view)

            self.submit(work)
        else:
            raise Refused()

    def _safe_notice(self, view: str, old_hash: str = "") -> None:
        try:
            self._show(
                view,
                self.views.notice_modal("denied"),
                old_hash,
            )
        except Exception:
            pass

    def _notice_if_current(self, token: str, actor: str, channel: str, view: str) -> None:
        try:
            self.journal.read_context(token, actor, channel, view)
        except Refused:
            return
        self._safe_notice(view)

    def post_launcher(self) -> None:
        cfg = self.settings()
        self.private_channel(cfg)
        with self.store.engagement_guard(engagement_id=self.store.engagement_id):
            exposure = self.journal.admit_view("launcher")
        posted = self.web.chat_postMessage(
            channel=cfg.channel,
            text="Open the private OpenTour pilot workspace.",
            blocks=[
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": "ot_open_workbench",
                            "text": {"type": "plain_text", "text": "Open pilot workspace"},
                            "value": "open",
                        }
                    ],
                }
            ],
            unfurl_links=False,
            unfurl_media=False,
        )
        _check(
            posted.get("ok") is True
            and posted["channel"] == cfg.channel
            and isinstance(posted["ts"], str)
        )
        self.journal.finish_view(exposure, "presented")
        self.journal.launcher(cfg.channel, posted["ts"], register=True)

    def _launch(self, cfg: Settings, actor: str, trigger: str, prompt: str) -> None:
        cfg.permission(actor, "inspect")
        opened = self.web.views_open(trigger_id=trigger, view=self.views.notice_modal("checking"))
        view = opened["view"]["id"]
        try:
            self.private_channel(cfg, actor)
            self._dispatch_prompt(cfg, actor, view, prompt)
        except Exception:
            self._safe_notice(view)

    def _dispatch_prompt(self, cfg: Settings, actor: str, view: str, prompt: str) -> None:
        cfg.permission(actor, "inspect")
        words = prompt.strip().split(maxsplit=1)
        command = words[0].casefold() if words else ""
        argument = words[1] if len(words) == 2 else ""
        if command in {"prompt", "agent"} and argument in {"", "repair"}:
            if argument == "repair":
                _check(self._home_repair_access(cfg, actor))
            ctx = self.journal.context(actor, cfg.channel, view, "prompt")
            self._show(
                view, self.views.prompt_modal(ctx, cfg.workflow_label, repair=argument == "repair")
            )
            return
        if command == "search":
            self._search(cfg, actor, view, "", argument)
            return
        if command == "repair":
            if not LOCAL_ID.fullmatch(argument):
                self._help(
                    cfg,
                    actor,
                    view,
                    "Use /opentour repair SOURCE to preview one approved workflow.",
                )
                return
            self._repair_preview(cfg, actor, view, argument)
            return
        if command == "data" and not argument:
            self._browse(cfg, actor, view, "")
            return
        if command in {"", "status", "review"} and not argument:
            self._results(cfg, actor, view)
            return
        if command == "ask":
            aliases = {
                "threshold": "coverage",
                "limits": "coverage",
                "fixes": "changes",
                "regressions": "changes",
                "what got worse": "changes",
                "what got worse?": "changes",
                "who receives it": "audience",
                "error rate": "errors",
            }
            topic = aliases.get(argument.casefold(), argument.casefold())
            if topic not in self.views.QUESTION_LABELS:
                self._help(
                    cfg,
                    actor,
                    view,
                    "Ask about errors, coverage, changes or audience from an inspected result.",
                )
                return
            sources = self._result_sources(cfg, actor)
            if len(sources) == 1 and cfg.permission(actor, "inspect")["aggregate"]:
                source = sources[0][1]
                target = self._readout(cfg, actor, source)
                if target and self.journal.inspected(
                    actor, target.revision, self._policy(cfg, actor)
                ):
                    self._questions(cfg, actor, view, source, target.revision, topic)
                    return
            self._help(
                cfg,
                actor,
                view,
                "Choose the exact readout and open its evidence before asking.",
            )
            return
        if command == "status" and argument.startswith("run-") and LOCAL_ID.fullmatch(argument):
            self._repair_lookup(cfg, actor, view, argument)
            return
        if command == "status" and LOCAL_ID.fullmatch(argument):
            self._status(cfg, actor, view, argument)
            return
        if len(words) == 1 and words[0].startswith("job-") and LOCAL_ID.fullmatch(words[0]):
            self._authorized_job(cfg, actor, words[0])
            self._home(cfg, actor, view, words[0])
            return
        self._help(
            cfg, actor, view, "" if command == "help" else "Choose a supported pilot command."
        )

    def _home_repair_access(self, cfg: Settings, actor: str) -> bool:
        from opentour_cell.pilot.managed_launch import managed_launch_summary

        entry = cfg.permission(actor, "inspect")
        if "tune" not in entry["roles"]:
            return False
        for dataset in entry["datasets"]:
            try:
                managed_launch_summary(
                    cfg.directory, cfg.organization, dataset, actor, cfg.team, cfg.channel
                )
            except (ValueError, KeyError, OSError):
                continue
            return True
        return False

    def _pending_release(self, cfg: Settings, actor: str) -> Target | None:
        entry = cfg.permission(actor, "inspect")
        if (
            not cfg.explicit_recipient
            or actor != cfg.reviewer
            or "review" not in entry["roles"]
            or not entry["aggregate"]
        ):
            return None
        for _, source in self._result_sources(cfg, actor):
            if not source.startswith("managed:"):
                continue
            try:
                target = self._managed(cfg, actor, source)
            except (ValueError, KeyError, OSError):
                continue
            if target.audit.get("release_reviewer") != actor:
                continue
            decision = (
                cfg.directory / "managed-outputs" / target.audit["job_id"] / "release-decision.json"
            )
            if not decision.exists():
                return target
        return None

    def _publish_app_home(self, cfg: Settings, event: Json) -> None:
        actor = event["user"]
        _check(
            _slack_id(actor, "UW")
            and event.get("type") == "app_home_opened"
            and event.get("tab") == "home"
        )
        try:
            _check(self.store.engagement_active())
            entry = cfg.permission(actor, "inspect")
            self.private_channel(cfg, actor)
        except Exception:
            # Slack or custody checks may be unavailable; replace any previously personalized Home.
            self.web.views_publish(user_id=actor, view=self.views.app_home_view())
            return
        token = self.journal.opaque("home", [actor, cfg.team, cfg.app, cfg.channel])
        pending = self._pending_release(cfg, actor)
        layout = self.views.app_home_view(
            token,
            cfg.workspace_title,
            cfg.workflow_label,
            can_review=entry["aggregate"],
            can_tune=self._home_repair_access(cfg, actor),
            named_reviewer=cfg.reviewer == actor and "review" in entry["roles"],
            pending=pending.revision if pending else "",
        )
        with self.store.engagement_guard(engagement_id=self.store.engagement_id):
            exposure = self.journal.admit_view("app_home")
        try:
            result = self.web.views_publish(user_id=actor, view=layout)
            _check(result is not None and result.get("ok") is True)
        except Exception:
            self.journal.finish_view(exposure, "uncertain")
            raise
        self.journal.finish_view(exposure, "presented")

    def _open_app_home(self, cfg: Settings, actor: str, body: Json) -> None:
        view = body["view"]
        _check(
            view.get("type") == "home"
            and view.get("callback_id") == "ot_app_home"
            and view.get("private_metadata")
            == self.journal.opaque("home", [actor, cfg.team, cfg.app, cfg.channel])
            and body.get("container", {}).get("type") == "view"
            and body.get("type") == "block_actions"
            and len(body["actions"]) == 1
        )
        action = body["actions"][0]
        if action.get("action_id") == "ot_app_pending":
            _check(actor == cfg.reviewer and cfg.permission(actor, "review")["aggregate"])
            opened = self.web.views_open(
                trigger_id=body["trigger_id"], view=self.views.notice_modal("checking")
            )
            view_id = opened["view"]["id"]
            try:
                self.private_channel(cfg, actor)
                pending = self._pending_release(cfg, actor)
                _check(pending and action.get("value") == pending.revision)
                assert pending is not None
                self._evidence(
                    cfg,
                    actor,
                    view_id,
                    {"job": "managed:" + pending.audit["job_id"], "revision": pending.revision},
                    "",
                )
            except Exception:
                self._safe_notice(view_id)
            return
        commands = {
            "ot_app_data": "data",
            "ot_app_review": "review",
            "ot_app_prompt": "prompt",
            "ot_app_repair": "prompt",
        }
        route = action.get("action_id")
        _check(route in commands and action.get("value") == route)
        entry = cfg.permission(actor, "inspect")
        if route == "ot_app_review":
            _check(entry["aggregate"])
        if route == "ot_app_repair":
            cfg.permission(actor, "tune")
        self._launch(
            cfg,
            actor,
            body["trigger_id"],
            "prompt repair" if route == "ot_app_repair" else commands[route],
        )

    def receive(self, request_type: str, body: Json, ack: Callable[[Json | None], None]) -> None:
        """Dispatch a Slack SDK authenticated Socket Mode envelope, acknowledging exactly once."""
        submission = request_type == "interactive" and body.get("type") == "view_submission"
        if not submission:
            ack(None)
        acknowledged = not submission
        authorized_view = ""
        authorized_hash = ""
        try:
            cfg = self.settings(check_engagement=not submission and request_type != "events_api")
            if request_type == "events_api":
                _check(
                    body.get("team_id") == cfg.team
                    and body.get("api_app_id") == cfg.app
                    and body.get("type") == "event_callback"
                )
                self._publish_app_home(cfg, body["event"])
                return
            if request_type == "slash_commands":
                actor = body["user_id"]
                _check(
                    body["team_id"] == cfg.team
                    and body["api_app_id"] == cfg.app
                    and body["channel_id"] == cfg.channel
                    and body["command"] == "/opentour"
                )
                self._launch(cfg, actor, body["trigger_id"], body.get("text", ""))
                return
            _check(
                request_type == "interactive"
                and body["api_app_id"] == cfg.app
                and body["team"]["id"] == cfg.team
            )
            actor = body["user"]["id"]
            cfg.permission(actor, "inspect")
            surfaced = body.get("channel", {}).get("id")
            _check(not surfaced or surfaced == cfg.channel)
            if body.get("container", {}).get("type") == "message":
                box = body["container"]
                if (
                    body.get("type") == "block_actions"
                    and len(body.get("actions", [])) == 1
                    and body["actions"][0].get("action_id") == "ot_open_approval"
                ):
                    from opentour_cell.pilot.approval_inbox import open_approval

                    open_approval(self, cfg, body, body["actions"][0]["value"])
                    return
                _check(
                    body["type"] == "block_actions"
                    and surfaced == cfg.channel
                    and box["channel_id"] == cfg.channel
                    and len(body["actions"]) == 1
                    and body["actions"][0]["action_id"] == "ot_open_workbench"
                    and self.journal.launcher(cfg.channel, box["message_ts"])
                )
                self._launch(cfg, actor, body["trigger_id"], "")
                return
            obj = body["view"]
            if obj.get("type") == "home":
                self._open_app_home(cfg, actor, body)
                return
            view = obj["id"]
            token = obj["private_metadata"]
            # Only trusted local configuration and a bounded indexed lookup precede a modal receipt.
            self.journal.read_context(token, actor, cfg.channel, view)
            if submission:
                ack(
                    {
                        "response_action": "update",
                        "view": self.views.notice_modal(
                            "working" if obj["callback_id"] == "ot_tune_start" else "checking"
                        ),
                    }
                )
                acknowledged = True
                _check(self.store.engagement_active())
            with self.view_locks[hash(view) % len(self.view_locks)]:
                ctx = self.journal.read_context(token, actor, cfg.channel, view)
                if not submission and any(
                    a.get("action_id") == "ot_charts" for a in body.get("actions", [])
                ):
                    container = body.get("container", {})
                    _check(
                        body.get("type") == "block_actions"
                        and len(body["actions"]) == 1
                        and obj.get("type") == "modal"
                        and obj.get("callback_id") == "ot_evidence_view"
                        and obj.get("team_id", cfg.team) == cfg.team
                        and obj.get("app_id", cfg.app) == cfg.app
                        and container.get("type", "view") == "view"
                        and container.get("view_id", view) == view
                        and container.get("channel_id", cfg.channel) == cfg.channel
                    )
                    # Stale or unauthorized chart requests cannot trigger a remote view update.
                    self._charts(
                        cfg,
                        actor,
                        view,
                        ctx,
                        body["actions"][0].get("value", ""),
                        obj.get("hash", ""),
                    )
                    return
                authorized_view = view
                if not submission:
                    authorized_hash = obj.get("hash", "")
                self.private_channel(cfg, actor)
                if submission:
                    callback = obj["callback_id"]
                    _check(callback in CALLBACKS)
                    self._submission(
                        cfg,
                        actor,
                        view,
                        ctx,
                        callback,
                        obj.get("state", {}).get("values", {}),
                        token,
                    )
                else:
                    _check(body["type"] == "block_actions" and len(body["actions"]) == 1)
                    action = body["actions"][0]
                    _check(action["action_id"] in ACTIONS)
                    self._action(
                        cfg,
                        actor,
                        view,
                        ctx,
                        action["action_id"],
                        action.get("value", ""),
                        obj.get("hash", ""),
                    )
        except Exception:
            if authorized_view:
                self._safe_notice(authorized_view, authorized_hash)
        finally:
            if not acknowledged:
                ack(None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="customer-owned configuration JSON")
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--check-config", action="store_true")
    commands.add_argument("--post-launcher", action="store_true")
    commands.add_argument(
        "--flush-approvals",
        action="store_true",
        help="dispatch eligible opt-in requests for human approval once",
    )
    commands.add_argument(
        "--unresolved-approvals",
        action="store_true",
        help="inspect opaque requests with uncertain Slack delivery",
    )
    commands.add_argument("--flush-zapier", action="store_true")
    commands.add_argument(
        "--auto-flush-zapier",
        action="store_true",
        help="serve Slack and dispatch eligible receipts in the background",
    )
    commands.add_argument("--fingerprint-zapier", action="store_true")
    commands.add_argument("--receipt", metavar="RECEIPT")
    commands.add_argument("--unresolved-receipts", action="store_true")
    commands.add_argument("--confirm-zapier", metavar="RECEIPT")
    parser.add_argument("--downstream-ref", metavar="OWNER_REFERENCE")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--cursor")
    arguments = list(sys.argv[1:] if argv is None else argv)
    for flag in ("--receipt", "--confirm-zapier"):
        if flag in arguments:
            index = arguments.index(flag)
            if index + 1 < len(arguments) and re.fullmatch(
                r"-[A-Za-z0-9_-]{23}", arguments[index + 1]
            ):
                # Older URL-safe 18-byte random identifiers sometimes begin with a hyphen.
                arguments[index : index + 2] = [flag + "=" + arguments[index + 1]]
    args = parser.parse_args(arguments)
    app = None
    drainer = None
    approval_drainer = None
    try:

        def config() -> Settings:
            return Settings.load(args.config)

        cfg = config()
        _check(args.unresolved_receipts or (args.limit is None and args.cursor is None))
        _check(bool(args.confirm_zapier) == bool(args.downstream_ref))
        if args.receipt is not None or args.unresolved_receipts:
            from opentour_cell.pilot.receipt_recovery import inspect_receipt, unresolved_receipts

            record = (
                inspect_receipt(cfg, args.receipt)
                if args.receipt is not None
                else unresolved_receipts(
                    cfg, limit=25 if args.limit is None else args.limit, cursor=args.cursor
                )
            )
            print(_json(record))
            return 0
        if args.unresolved_approvals:
            from opentour_cell.pilot.approval_inbox import unresolved

            print(_json(unresolved(cfg)))
            return 0
        if args.fingerprint_zapier:
            print(hook_fingerprint(os.environ.get("OPENTOUR_ZAPIER_HOOK_URL", "")))
            return 0
        if args.check_config:
            print("Pilot configuration validated.")
            return 0
        if args.confirm_zapier:
            from opentour_cell.pilot.receipt_recovery import confirm_receipt

            confirm_receipt(cfg, args.confirm_zapier, args.downstream_ref)
            print("Recorded the owner's independent downstream verification.")
            return 0
        from opentour_cell.pilot.training import LocalTrainingStore

        if args.flush_zapier:
            _check((cfg.directory / "slack-pilot.sqlite3").is_file())
            journal = Journal(cfg.directory / "slack-pilot.sqlite3", cfg.binding)
            _check((cfg.directory / "training.sqlite3").is_file())
            store = LocalTrainingStore(cfg.directory / "training.sqlite3", cfg.organization)
            count = flush_receipts(
                journal,
                os.environ.get("OPENTOUR_ZAPIER_HOOK_URL", ""),
                config=config,
                training=store,
            )
            print(f"Accepted receipt deliveries: {count}.")
            return 0
        # The official Slack SDK is optional and isolated from the offline pilot kit.
        sdk = importlib.import_module("slack_sdk")
        socket_sdk = importlib.import_module("slack_sdk.socket_mode")
        response = importlib.import_module("slack_sdk.socket_mode.response").SocketModeResponse
        from opentour_cell.pilot import slack_views

        bot = os.environ.get("SLACK_BOT_TOKEN", "")
        _check(bot.startswith("xoxb-"))
        # A post accepted before a lost reply must never be sent again by an SDK retry.
        web = sdk.WebClient(token=bot, retry_handlers=[])
        verified = web.auth_test()
        _check(verified["team_id"] == cfg.team and bool(verified["bot_id"]))
        app = Workbench(
            config,
            web,
            LocalTrainingStore(cfg.directory / "training.sqlite3", cfg.organization),
            slack_views,
        )
        if args.flush_approvals:
            from opentour_cell.pilot.approval_inbox import delivery_cycle

            print(_json(delivery_cycle(app)))
            return 0
        if args.post_launcher:
            app.post_launcher()
            print("Pilot launcher posted.")
            return 0
        token = os.environ.get("SLACK_APP_TOKEN", "")
        _check(token.startswith("xapp-"))
        app.private_channel(cfg)
        socket = socket_sdk.SocketModeClient(app_token=token, web_client=web)
        workbench = app

        def listener(client: Any, request: Any) -> None:
            workbench.receive(
                request.type,
                request.payload,
                lambda payload: client.send_socket_mode_response(
                    response(envelope_id=request.envelope_id, payload=payload)
                ),
            )

        socket.socket_mode_request_listeners.append(listener)
        try:
            socket.connect()
            if cfg.approval_requests:
                from opentour_cell.pilot.approval_inbox import ApprovalDrainer

                approval_drainer = ApprovalDrainer(app)
                approval_drainer.start()
            if args.auto_flush_zapier:
                drainer = ReceiptDrainer(
                    app.journal,
                    config,
                    app.store,
                    lambda: os.environ.get("OPENTOUR_ZAPIER_HOOK_URL", ""),
                )
                app.on_decision = drainer.wake
                drainer.start()
            threading.Event().wait()
        finally:
            socket.close()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        print(
            "Pilot operation unavailable. Verify the local configuration, grants and connection.",
            file=sys.stderr,
        )
        return 1
    finally:
        if approval_drainer:
            approval_drainer.close()
        if drainer:
            drainer.close()
        if app:
            app.pool.shutdown(wait=True)


if __name__ == "__main__":
    raise SystemExit(main())
