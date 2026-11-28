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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from opentour_cell.pilot.intake_mcp import REF_PATTERN, IntakePrincipal
from opentour_cell.pilot.live_evidence import LiveEvidence, LiveScope, binding_candidate
from opentour_cell.pilot.scoped_query_plan import (
    ArmChart,
    ComparisonArm,
    QueryHold,
    ReadoutQuestion,
    WorkflowDiscovery,
    plan_query,
)
from opentour_cell.pilot.scoped_query_report import MonthlyInvoiceReport
from opentour_cell.pilot.starter_setup import StarterBinding, StarterPreview, StarterSetup
from opentour_cell.pilot.status_questions import (
    ApprovalEvidence,
    StatusAnswer,
    StatusTopic,
    read_status,
    resolve_status,
)

if TYPE_CHECKING:
    from opentour_cell.pilot.handoff_readback import HandoffReadback
    from opentour_cell.pilot.map_review import MapReviewService
    from opentour_cell.pilot.recipient_lifecycle import LifecycleAuthority
    from opentour_cell.pilot.starter_evaluation_session import StarterEvaluationSession
    from opentour_cell.pilot.starter_map_confirmation import StarterCaseEntry, StarterCaseSession
    from opentour_cell.pilot.verifier_form import VerifierForm
    from opentour_cell.pilot.workflow_index import (
        Action,
        IndexPage,
        IndexRow,
        IndexScope,
        ProposalAccess,
        WorkflowIndex,
    )

Json = dict[str, Any]
SESSION_SECONDS = 900
RESULT_PAGE_SIZE = 12
WORKFLOW_PAGE_SIZE = 6
SLACK_ID = re.compile(r"[A-Z][A-Z0-9]{2,31}\Z")
LOCAL_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
# Only a complete, read-only question may select a frozen comparison.
REGRESSION_QUESTION = re.compile(
    r"(?:what (?:got|became) worse|(?:which|what) (?:cases|decisions|answers) "
    r"(?:(?:got|became) (?:worse|wrong|incorrect)|regressed))\??"
)
WORKPLACE_QUESTIONS = {
    ReadoutQuestion.WORKPLACE_SUPPORT,
    ReadoutQuestion.SYNTHETIC_BENEFIT,
    ReadoutQuestion.LABEL_SAVINGS,
}
ROLES = {"inspect", "tune", "review"}
CALLBACKS = {
    "ot_dataset_search",
    "ot_tune_preview",
    "ot_tune_start",
    "ot_prompt_enter",
    "ot_live_binding",
    "ot_verify_outcome",
    "ot_nomination",
    "ot_map_branch_save",
    "ot_case_adjudicate",
    "ot_case_freeze",
    "ot_map_assertions_save",
    "ot_map_precedence_save",
}
ACTIONS = {
    "ot_workflow_starter",
    "ot_map_open",
    "ot_case_review",
    "ot_case_generate",
    "ot_case_page_previous",
    "ot_case_page_next",
    "ot_case_edit",
    "ot_case_splits",
    "ot_case_citation",
    "ot_case_back",
    "ot_starter_score_choice",
    "ot_score_next",
    "ot_score_previous",
    "ot_score_reconnect",
    "ot_case_clause_page",
    "ot_case_commit",
    "ot_choose_map",
    "ot_map_page",
    "ot_map_citation",
    "ot_map_priority",
    "ot_map_date_clause",
    "ot_map_clause_page",
    "ot_map_edit",
    "ot_map_assertions",
    "ot_map_back",
    "ot_map_precedence",
    "ot_starter_save",
    "ot_starter_benchmark",
    "ot_connect_live",
    "ot_workflow_historical",
    "ot_home",
    "ot_browse",
    "ot_tune",
    "ot_status",
    "ot_evidence",
    "ot_plugin_continue",
    "ot_managed_review_status",
    "ot_plugin_request",
    "ot_plugin_status",
    "ot_plugin_receipt",
    "ot_charts",
    "ot_approve",
    "ot_changes",
    "ot_dataset_open",
    "ot_status_refresh",
    "ot_dataset_back",
    "ot_tune_dataset",
    "ot_readouts",
    "ot_result_open",
    "ot_current_week",
    "ot_verify_page",
    "ot_nominate_verifier",
    "ot_week_bind",
    "ot_week_scopes",
    "ot_current_training",
    "ot_readiness_proof",
    "ot_training_scopes",
    "ot_result_prev",
    "ot_result_next",
    "ot_repair_confirm",
    "ot_repair_refresh",
    "ot_release_approve",
    "ot_release_receipt",
    "ot_delivery_status",
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
class MonthlyReportPin:
    workflow_ref: str
    publication_file: str
    custodian_key_id: str


@dataclass(frozen=True)
class MonthlyReportSettings:
    timezone: str
    workflows: tuple[MonthlyReportPin, ...]

    @classmethod
    def parse(cls, doc: Json) -> MonthlyReportSettings:
        from opentour_cell.pilot.repair_workflow import _name

        _check(isinstance(doc, dict) and doc.keys() == {"timezone", "workflows"})
        timezone = doc["timezone"]
        _check(isinstance(timezone, str) and 0 < len(timezone) <= 128)
        try:
            ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise Refused() from None
        rows = doc["workflows"]
        _check(isinstance(rows, list) and 1 <= len(rows) <= 20)
        pins = []
        for row in rows:
            _check(
                isinstance(row, dict)
                and row.keys() == {"workflow_ref", "publication_file", "custodian_key_id"}
            )
            pin = MonthlyReportPin(**row)
            _check(
                isinstance(pin.workflow_ref, str) and re.fullmatch(REF_PATTERN, pin.workflow_ref)
            )
            _check(_name(pin.publication_file).endswith(".json"))
            _check(
                isinstance(pin.custodian_key_id, str)
                and re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,126}[a-z0-9]", pin.custodian_key_id)
            )
            pins.append(pin)
        _check(len({pin.workflow_ref for pin in pins}) == len(pins))
        return cls(timezone, tuple(pins))


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
    live_workflow_ref: str = ""
    starter_setup: StarterBinding | None = None
    workflow_index_publication_file: str = ""
    owner_settings_authority_file: str = ""
    monthly_invoice_reports: MonthlyReportSettings | None = None

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
            "starter_setup",
            "workflow_index_publication_file",
            "owner_settings_authority_file",
            "live_workflow_ref",
            "monthly_invoice_reports",
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
        publication_file = ""
        if "workflow_index_publication_file" in doc:
            from opentour_cell.pilot.repair_workflow import _name

            publication_file = _name(doc["workflow_index_publication_file"])
        authority_file = ""
        if "owner_settings_authority_file" in doc:
            from opentour_cell.pilot.repair_workflow import _name

            authority_file = _name(doc["owner_settings_authority_file"])
        live_workflow = doc.get("live_workflow_ref", "")
        if "live_workflow_ref" in doc:
            _check(isinstance(live_workflow, str) and re.fullmatch(REF_PATTERN, live_workflow))
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
            live_workflow_ref=live_workflow,
            workflow_index_publication_file=publication_file,
            owner_settings_authority_file=authority_file,
            monthly_invoice_reports=MonthlyReportSettings.parse(doc["monthly_invoice_reports"])
            if "monthly_invoice_reports" in doc
            else None,
            starter_setup=StarterBinding.parse(doc["starter_setup"])
            if "starter_setup" in doc
            else None,
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

    def admit_view(self, kind: str, *, authorize: Callable[[], None] | None = None) -> str:
        key = secrets.token_urlsafe(18)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.active(db)
            if authorize is not None:
                authorize()
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


def _static_setup_navigation(view: Json, context: str) -> bool:
    """Compare the complete outgoing private view with a stock, data-free setup screen."""
    from opentour_cell.pilot import slack_views

    callback = view.get("callback_id")
    if callback == "ot_app_home":
        expected = slack_views.app_home_view(
            context,
            "OpenTour pilot workspace",
            can_review=True,
            named_reviewer=True,
            can_add_workflow=True,
        )
    elif callback == "ot_workflow_chooser":
        expected = slack_views.workflow_chooser(context)
    elif callback in {"ot_workflow_starter_info", "ot_workflow_historical_info"}:
        route = "starter" if callback == "ot_workflow_starter_info" else "historical"
        expected = slack_views.workflow_instructions(context, route)
    else:
        return False
    return view == expected


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
            slack_possible=n(
                "SELECT COUNT(*) FROM exposures WHERE status IS NOT 'presented' "
                "AND (status IS NOT 'navigation_presented' OR COALESCE(kind,'') NOT IN "
                "('app_home','ot_workflow_chooser','ot_workflow_starter_info',"
                "'ot_workflow_historical_info'))"
            ),
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
        *,
        workflow_index_scope: Callable[
            [Settings, IntakePrincipal, tuple[ProposalAccess, ...]], IndexScope | None
        ]
        | None = None,
        lifecycle_authority: LifecycleAuthority | None = None,
    ):
        self.workflow_index_scope = workflow_index_scope
        self.config = config
        self.lifecycle_authority = lifecycle_authority
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

    def _current_question(self, actor: str, topic: str) -> tuple[str, str, Json | None] | None:
        """Pin one currently inspected live workflow, separately from any frozen readout."""
        try:
            cfg = self.settings(check_engagement=False)
            workflow = self._live_workflow(cfg, actor)
            with self._live_scope(actor, workflow) as (_, _, scope):
                entry = cfg.permission(actor, "inspect", scope.dataset_ref)
                _check(scope.can_inspect and entry["aggregate"])
                receipt = self._readiness(actor)
                if receipt is not None:
                    _check(
                        receipt["workflow_ref"] == workflow
                        and receipt["engagement"] == scope.engagement_epoch
                        and receipt["dataset_ref"] == scope.dataset_ref
                        and receipt["dataset_revision"] == scope.dataset_revision
                    )
                facts = receipt
                if topic == StatusTopic.LIVE_ERRORS:
                    summary = self._live_summary(actor)
                    if summary is not None:
                        _check(
                            summary["workflow"] == workflow
                            and summary["dataset"] == scope.dataset_ref
                        )
                        if receipt:
                            _check(summary["revision"] == receipt["evidence_revision"])
                    today = datetime.now(UTC).date()
                    facts = {
                        "readiness": receipt,
                        "summary": summary,
                        "week_start": int(
                            datetime.combine(
                                today - timedelta(days=today.weekday()), datetime.min.time(), UTC
                            ).timestamp()
                        ),
                    }
                pin = self.journal.opaque(
                    "workflow",
                    [
                        self._policy(cfg, actor),
                        actor,
                        workflow,
                        scope.authority_revision,
                        scope.engagement_epoch,
                        scope.dataset_ref,
                        scope.dataset_revision,
                        topic,
                        facts,
                    ],
                )
                return workflow, pin, facts
        except Exception:
            return None

    def _monthly_discovery(
        self, cfg: Settings, actor: str
    ) -> tuple[tuple[str, MonthlyInvoiceReport, str], ...]:
        from opentour_cell.pilot.live_authority import PrivateLiveAuthority
        from opentour_cell.pilot.scoped_query_report import open_signed_monthly_invoice_report
        from opentour_cell.pilot.workflow_benchmarks import BenchmarkScope

        setup = cfg.monthly_invoice_reports
        if setup is None:
            return ()
        now = datetime.now(UTC)
        principal = IntakePrincipal(cfg.organization, actor, "slack")
        visible = []
        for pin in setup.workflows:
            admitted = False
            try:
                authority = PrivateLiveAuthority(cfg.directory, cfg.organization, pin.workflow_ref)
                with authority.guard(principal, pin.workflow_ref) as scope:
                    _check(
                        scope.can_inspect
                        and scope.tenant == cfg.organization
                        and scope.workflow_ref == pin.workflow_ref
                        and scope.engagement_epoch == self.store.engagement_id
                    )
                    _check(cfg.permission(actor, "inspect", scope.dataset_ref)["aggregate"])
                    admitted = True
                    verified = open_signed_monthly_invoice_report(
                        cfg.directory,
                        pin.publication_file,
                        BenchmarkScope(scope.tenant, scope.engagement_epoch, scope.workflow_ref),
                        timezone=setup.timezone,
                        at=now,
                        custodian_key_id=pin.custodian_key_id,
                    )
                    if verified is None:
                        return ()
                    report, revision = verified
                    protected = self.journal.opaque(
                        "month",
                        [
                            scope.authority_revision,
                            scope.dataset_revision,
                            self._policy(cfg, actor),
                            pin.publication_file,
                            pin.custodian_key_id,
                            revision,
                        ],
                    )
                visible.append((pin.workflow_ref, report, protected))
            except (ImportError, ValueError, OSError, KeyError, sqlite3.Error):
                if admitted:
                    return ()
        return tuple(visible)

    def _monthly_question(
        self, cfg: Settings, actor: str, view: str, plan: WorkflowDiscovery
    ) -> None:
        rows = self._monthly_discovery(cfg, actor) if cfg.monthly_invoice_reports else ()
        token = self.journal.context(actor, cfg.channel, view, "monthly_question")
        if not rows:
            self._show(view, self.views.query_hold_modal(token, plan))
            return
        self._show(
            view,
            self.views.monthly_invoice_answer(
                token, [(workflow, report) for workflow, report, _ in rows]
            ),
            authorize=lambda: _check(
                self._monthly_discovery(self.settings(check_engagement=False), actor) == rows
            ),
        )

    def _chart_match(
        self, cfg: Settings, actor: str, source: str, version: int
    ) -> tuple[str, str, Target] | None:
        from opentour_cell.pilot.source_intake import check_current_intake
        from opentour_cell.pilot.workflow_benchmarks import BenchmarkScope, WorkflowBenchmarks

        try:
            if actor != cfg.reviewer or not cfg.permission(actor, "review")["aggregate"]:
                return None
            target = self._managed(cfg, actor, source)
            audit = target.audit
            cfg.permission(actor, "review", target.dataset)
            if audit.get("comparison_cohorts", {}).get("rule") != audit["cohort_revision"]:
                return None
            intake = check_current_intake(
                cfg.directory, cfg.organization, self.store.engagement_id, target.dataset_revision
            )
            if intake["dataset_id"] != target.dataset:
                return None
            workflow = intake["workflow"]
            scope = BenchmarkScope(cfg.organization, self.store.engagement_id, workflow)
            receipt = WorkflowBenchmarks(cfg.directory).match_scored_comparison(
                scope, version, audit["run_revision"]
            )
            if receipt is None:
                return None
            pin = self.journal.opaque(
                "chart",
                [self._policy(cfg, actor), workflow, source, target.revision, version, receipt],
            )
            return workflow, pin, target
        except (ValueError, KeyError, OSError, sqlite3.Error):
            return None

    def _chart_picker(
        self,
        cfg: Settings,
        actor: str,
        view: str,
        plan: ArmChart,
        old_hash: str = "",
        page: int = 0,
    ) -> None:
        rows = []
        for _, source in self._result_sources(cfg, actor):
            if source.startswith("managed:"):
                match = self._chart_match(cfg, actor, source, plan.version)
                if match is not None:
                    rows.append((match[0], source, match[1]))
        ctx = self.journal.context
        if not rows:
            token = ctx(actor, cfg.channel, view, "query_hold")
            self._show(view, self.views.query_hold_modal(token, plan), old_hash)
            return
        page = max(0, min(page, (len(rows) - 1) // RESULT_PAGE_SIZE))
        start = page * RESULT_PAGE_SIZE
        visible = rows[start : start + RESULT_PAGE_SIZE]
        listing = {
            "version": plan.version,
            "page": page,
            "items": {pin: src for _, src, pin in visible},
        }
        token = ctx(actor, cfg.channel, view, "chart_picker", revision=_json(listing))

        def authorize() -> None:
            current = self.settings(check_engagement=False)
            for workflow, source, pin in visible:
                match = self._chart_match(current, actor, source, plan.version)
                _check(match is not None and match[:2] == (workflow, pin))

        self._show(
            view,
            self.views.regression_picker(
                token,
                visible,
                previous=bool(page),
                more=start + len(visible) < len(rows),
                topic="arm_chart",
            ),
            old_hash,
            authorize=authorize,
        )

    def _selected_chart(
        self,
        cfg: Settings,
        actor: str,
        view: str,
        source: str,
        version: int,
        pin: str,
        old_hash: str,
        proof: bool = False,
    ) -> None:
        match = self._chart_match(cfg, actor, source, version)
        if match is None or match[1] != pin:
            raise Refused()
        workflow, _, target = match
        token = self.journal.context(
            actor,
            cfg.channel,
            view,
            "chart_proof" if proof else "arm_chart",
            revision=_json({"source": source, "version": version, "pin": pin}),
        )

        def authorize() -> None:
            current = self._chart_match(
                self.settings(check_engagement=False), actor, source, version
            )
            _check(current is not None and current[:2] == (workflow, pin))

        self._show(
            view,
            self.views.frozen_rule_chart(token, workflow, version, target.audit, pin, proof),
            old_hash,
            dataset=target.dataset,
            dataset_revision=target.dataset_revision,
            audit=target.audit,
            authorize=authorize,
        )

    def _regression_picker(
        self,
        cfg: Settings,
        actor: str,
        view: str,
        old_hash: str = "",
        page: int = 0,
        topic: str = "changes",
    ) -> None:
        policy = self._policy(cfg, actor)
        workflow_scoped = topic in WORKPLACE_QUESTIONS or topic == StatusTopic.DELIVERY
        eligible = []
        if cfg.permission(actor, "inspect")["aggregate"]:
            for dataset, source in self._result_sources(cfg, actor):
                target = self._readout(cfg, actor, source)
                if target and self.journal.inspected(actor, target.revision, policy):
                    if workflow_scoped and not self._workplace_scope(cfg, target):
                        continue
                    eligible.append((dataset, source, target.revision))
        page = max(0, min(page, (len(eligible) - 1) // RESULT_PAGE_SIZE))
        start = page * RESULT_PAGE_SIZE
        rows = eligible[start : start + RESULT_PAGE_SIZE]
        current = (
            self._current_question(actor, topic)
            if topic in {StatusTopic.TRAINING, StatusTopic.LIVE_ERRORS}
            else None
        )

        def delivery_choices(settings: Settings) -> dict[str, tuple[str, str]]:
            choices = {}
            if topic == StatusTopic.DELIVERY and settings.live_workflow_ref:
                for _, source, revision in rows:
                    target = self._readout(settings, actor, source)
                    if target and target.revision == revision:
                        receipt = self._handoff_receipt(settings, actor, target)
                        if receipt:
                            choices[revision] = (
                                receipt.selection.workflow_ref,
                                self._receipt_pin(actor, revision, receipt),
                            )
            return choices

        deliveries = delivery_choices(cfg)
        listing = {
            "page": page,
            "workplace_scope": cfg.live_workflow_ref if topic in WORKPLACE_QUESTIONS else None,
            "delivery_scope": cfg.live_workflow_ref if topic == StatusTopic.DELIVERY else None,
            "deliveries": {revision: pin for revision, (_, pin) in deliveries.items()},
            "items": {revision: source for _, source, revision in rows},
            "topic": topic,
            "current": current[1] if current else None,
        }
        ctx = self.journal.context(actor, cfg.channel, view, "regressions", revision=_json(listing))

        def authorize() -> None:
            latest = self._authorize_question_rows(
                cfg, actor, rows, workflow_scoped=workflow_scoped
            )
            if deliveries:
                _check(delivery_choices(latest) == deliveries)
            if current is not None:
                _check(self._current_question(actor, topic) == current)

        self._show(
            view,
            self.views.regression_picker(
                ctx,
                rows,
                previous=bool(page),
                more=start + len(rows) < len(eligible),
                topic=topic,
                **({"current_workflow": current[:2]} if current else {}),
                **(
                    {"delivery_workflows": {k: v[0] for k, v in deliveries.items()}}
                    if deliveries
                    else {}
                ),
            ),
            old_hash,
            authorize=authorize,
        )

    def _authorize_question_rows(
        self,
        cfg: Settings,
        actor: str,
        rows: list[tuple[str, str, str]],
        *,
        workflow_scoped: bool = False,
    ) -> Settings:
        # Both the withdrawal guard and the exposure transaction are held at admission.
        current = self.settings()
        current.permission(actor, "inspect")
        policy = self._policy(current, actor)
        _check(policy == self._policy(cfg, actor))
        if workflow_scoped:
            _check(current.live_workflow_ref == cfg.live_workflow_ref)
        for dataset, source, revision in rows:
            _check(current.permission(actor, "inspect", dataset)["aggregate"])
            target = self._readout(current, actor, source)
            _check(target and target.dataset == dataset and target.revision == revision)
            if workflow_scoped:
                _check(target and self._workplace_scope(current, target))
            _check(self.journal.inspected(actor, revision, policy))
        return current

    def _handoff_receipt(self, cfg: Settings, actor: str, target: Target) -> HandoffReadback | None:
        from opentour_cell.pilot.handoff_readback import read_handoff

        if not (
            cfg.live_workflow_ref
            and cfg.explicit_recipient
            and cfg.approval_provider == target.audit.get("release_provider") == "slack"
            and actor == cfg.reviewer == target.audit.get("release_reviewer")
            and cfg.permission(actor, "review", target.dataset)["aggregate"]
        ):
            return None
        # The selector locates a workflow. The independently signed inspection
        # grant and current source intake bind that workflow to this frozen run.
        return read_handoff(
            self.store,
            actor=actor,
            workflow_ref=cfg.live_workflow_ref,
            proposal_ref=target.audit["job_id"],
            proposal_revision=target.audit["run_revision"],
            artifact_sha256=target.audit["model_revision"],
            cohort_revision=target.audit["cohort_revision"],
            lifecycle_authority=self.lifecycle_authority,
        )

    def _receipt_pin(self, actor: str, revision: str, receipt: HandoffReadback) -> str:
        return self.journal.opaque(
            "release-receipt",
            [
                actor,
                revision,
                receipt.selection.workflow_ref,
                receipt.selection.proposal_ref,
                receipt.selection.approval_ref,
                receipt.destination_ref,
            ],
        )

    @staticmethod
    def _workplace_scope(cfg: Settings, target: Target) -> bool:
        if not cfg.live_workflow_ref:
            return True
        if target.audit["mode"] not in {"managed_rule", "managed_model"}:
            return False
        from opentour_cell.pilot.source_intake import check_current_intake

        intake = check_current_intake(
            cfg.directory, cfg.organization, target.audit["engagement"], target.dataset_revision
        )
        return bool(
            intake["workflow"] == cfg.live_workflow_ref and intake["dataset_id"] == target.dataset
        )

    def _workplace_answer(self, cfg: Settings, target: Target, topic: str) -> StatusAnswer:
        _check(self._workplace_scope(cfg, target))
        scope = f"Workflow {cfg.live_workflow_ref}. " if cfg.live_workflow_ref else ""
        mode = target.audit["mode"]
        basis = {
            "evaluation_only": "This result scored supplied outputs.",
            "training": "This result records a completed CPU fit.",
            "managed_rule": "This result evaluates a simple rule.",
            "managed_model": "This result includes a model comparison.",
        }[mode]
        if topic == ReadoutQuestion.WORKPLACE_SUPPORT:
            return StatusAnswer(
                "Independent workplace support is unavailable for this result.",
                f"{scope}{basis} The frozen cohort contains {target.audit['population']} cases. "
                "No receipt links them to independently verified workplace families.",
                "The data custodian can provide verified case lineage for this frozen result.",
            )
        if topic == ReadoutQuestion.LABEL_SAVINGS:
            return StatusAnswer(
                "Label savings not measured.",
                f"{scope}{basis} The frozen evaluation has {target.report['known_references']} "
                f"known references across {target.report['cohort_size']} cases. "
                "Training-label budgets, independent training families and expert minutes "
                "are unavailable for this comparison.",
                "The evaluation operator can supply a matched label-budget comparison at the "
                "same quality target, coverage and regression limits, with measured expert time.",
            )
        pair = target.report["paired"]
        measured = (
            f"The recorded comparison has {pair['comparable']} comparable cases, "
            f"{pair['fixes']} fixed, {pair['regressions']} newly wrong "
            f"and {pair['excluded']} excluded."
        )
        return StatusAnswer(
            "Augmentation benefit is unproven for this result.",
            f"{scope}{basis} {measured} The result has no matched real-only and augmented arms "
            "scored on the same untouched real cases. Incremental cost is unavailable.",
            "The evaluation operator can provide a matched comparison and its cost receipt.",
        )

    def _status_answer(
        self,
        cfg: Settings,
        actor: str,
        target: Target,
        topic: StatusTopic,
        receipt: HandoffReadback | None = None,
    ) -> StatusAnswer:
        approval = None
        if topic == StatusTopic.DELIVERY:
            _check(
                actor == cfg.reviewer
                and cfg.permission(actor, "review", target.dataset)["aggregate"]
            )
            if not cfg.explicit_recipient:
                return read_status(topic, target.audit, destination_configured=False)
            if target.audit.get("release_provider") == "zapier_slack":
                from opentour_cell.pilot.zapier_approval import ApprovalBridge

                bridge = ApprovalBridge(
                    cfg.directory, cfg.organization, cfg.directory / "zapier-approval.json"
                )
                seen = bridge.existing_status(
                    run_id=target.audit["job_id"],
                    run_revision=target.audit["run_revision"],
                    artifact_sha=target.audit["model_revision"],
                    team=cfg.team,
                    actor=actor,
                    handoff_destination=(cfg.channel, cfg.recipient),
                )
                if seen:
                    approval = ApprovalEvidence(
                        seen["state"], seen["request_id"], seen.get("recorded_at")
                    )
            elif target.audit.get("release_provider") == "slack":
                from opentour_cell.pilot.release_gate import inspect_recorded_release

                recorded = inspect_recorded_release(
                    cfg.directory,
                    cfg.organization,
                    target.audit["job_id"],
                    target.audit["run_revision"],
                    target.audit["model_revision"],
                )
                if recorded and recorded["decision"] == "approved" and receipt:
                    from opentour_cell.pilot.slack_views import handoff_status

                    return handoff_status(receipt, target.audit)
                if recorded:
                    stage = (
                        "release_changes_requested"
                        if recorded["decision"] == "changes_requested"
                        else recorded["decision"]
                    )
                    approval = ApprovalEvidence(
                        stage, recorded["presentation_id"], recorded.get("recorded_at")
                    )
            else:
                state, decision_receipt = self._recorded_decision(target.revision)
                if state in {"approved", "changes_requested"}:
                    stage = "readout_changes_requested" if state == "changes_requested" else state
                    approval = ApprovalEvidence(
                        stage, decision_receipt["receipt"], decision_receipt["decided"]
                    )
        answer = read_status(
            topic,
            target.audit,
            managed_outcome=self.views.managed_outcome(target.audit),
            approval=approval,
        )

        # A dataset revision may be shared by several workflows. Only a workflow
        # identity supplied by the verified frozen readout can link its status.
        frozen_workflow = target.audit.get("workflow_ref")
        if topic == StatusTopic.TRAINING and isinstance(frozen_workflow, str) and frozen_workflow:
            readiness = self._readiness(actor)
            if readiness and (
                readiness["dataset_ref"] == target.dataset
                and readiness["dataset_revision"] == target.dataset_revision
                and readiness["engagement"] == target.audit.get("engagement")
                and readiness["organization"] == target.audit.get("organization")
                and frozen_workflow == readiness["workflow_ref"]
            ):
                current = self.views.readiness_answer(readiness)
                return StatusAnswer(
                    answer.finding,
                    f"{answer.basis} Current workflow {readiness['workflow_ref']} · "
                    f"{current.finding} {current.basis}",
                    current.next_action,
                )
        return answer

    def _questions(
        self,
        cfg: Settings,
        actor: str,
        view: str,
        source: str,
        revision: str,
        topic: str,
        old_hash: str = "",
        expected_receipt_pin: str | None = None,
    ) -> None:
        target = self._readout(cfg, actor, source)
        _check(target and target.revision == revision)
        assert target is not None
        entry = cfg.permission(actor, "inspect", target.dataset)
        _check(
            entry["aggregate"] and self.journal.inspected(actor, revision, self._policy(cfg, actor))
        )
        status_topic = StatusTopic(topic) if topic in StatusTopic else None
        _check(
            not topic
            or status_topic
            or topic in WORKPLACE_QUESTIONS
            or topic in self.views.QUESTION_LABELS
        )
        current = self.settings()
        _check(self._policy(cfg, actor) == self._policy(current, actor))
        current.permission(actor, "inspect", target.dataset)
        delivery_scoped = status_topic == StatusTopic.DELIVERY and expected_receipt_pin is not None
        if delivery_scoped:
            _check(
                current.live_workflow_ref == cfg.live_workflow_ref
                and self._workplace_scope(current, target)
            )
        receipt = (
            self._handoff_receipt(current, actor, target)
            if status_topic == StatusTopic.DELIVERY and expected_receipt_pin
            else None
        )
        answer = (
            self._status_answer(current, actor, target, status_topic, receipt)
            if status_topic
            else self._workplace_answer(current, target, topic)
            if topic in WORKPLACE_QUESTIONS
            else None
        )
        pin = self._receipt_pin(actor, revision, receipt) if receipt else ""
        if expected_receipt_pin is not None:
            _check(pin == expected_receipt_pin)
        kind = "handoff_question" if pin else "questions"
        token = self.journal.context(actor, cfg.channel, view, kind, source, revision)

        def authorize() -> None:
            latest = self._authorize_question_rows(
                cfg, actor, [(target.dataset, source, revision)], workflow_scoped=delivery_scoped
            )
            if topic in WORKPLACE_QUESTIONS:
                _check(self._workplace_answer(self.settings(), target, topic) == answer)
            if receipt:
                _check(self._handoff_receipt(latest, actor, target) == receipt)
            if status_topic:
                settings = latest if delivery_scoped else self.settings()
                _check(
                    self._status_answer(settings, actor, target, status_topic, receipt) == answer
                )

        self._show(
            view,
            self.views.status_question_modal(
                token,
                revision,
                target.dataset,
                answer,
                receipt_pin=pin,
                workflow_ref=current.live_workflow_ref if delivery_scoped else None,
            )
            if answer
            else self.views.question_modal(token, revision, target.report, target.audit, topic),
            old_hash,
            dataset=target.dataset,
            dataset_revision=target.dataset_revision,
            audit=target.audit,
            authorize=authorize,
        )

    def _unbound_recipient(self) -> bool:
        # An unavailable current binding cannot justify excluding a confirmed view.
        try:
            return not self.settings().explicit_recipient
        except Exception:
            return False

    def _show(
        self,
        view_id: str,
        modal: Json,
        old_hash: str = "",
        *,
        dataset: str | None = None,
        dataset_revision: str | None = None,
        audit: Json | None = None,
        setup_context: str | None = None,
        authorize: Callable[[], None] | None = None,
    ) -> str:
        # Record every attempted protected disclosure without persisting projected sample rows.
        exposure = ""
        if modal.get("callback_id"):
            with _evidence_guard(
                self.store, dataset, dataset_revision, self.store.engagement_id, audit
            ):
                exposure = self.journal.admit_view(modal["callback_id"], authorize=authorize)
        navigation = (
            setup_context is not None
            and _static_setup_navigation(modal, setup_context)
            and dataset is None
            and dataset_revision is None
            and audit is None
            and self._unbound_recipient()
        )
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
            self.journal.finish_view(
                exposure,
                "navigation_presented" if navigation and self._unbound_recipient() else "presented",
            )
        return exposure

    def _home(
        self,
        cfg: Settings,
        actor: str,
        view_id: str,
        job: str,
        old_hash: str = "",
        *,
        authorize: Callable[[], None] | None = None,
    ) -> None:
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
            decision_allowed=authorize is None
            and actor == cfg.reviewer
            and "review" in entry["roles"],
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
            authorize=authorize,
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

    def _evidence(
        self,
        cfg: Settings,
        actor: str,
        view: str,
        ctx: Json,
        old_hash: str,
        *,
        authorize: Callable[[], None] | None = None,
        queue_handle: str = "",
    ) -> None:
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
            actor,
            cfg.channel,
            view,
            "queued_evidence" if queue_handle else "evidence",
            ctx["job"],
            queue_handle or target.revision,
        )
        named_reviewer = current.reviewer == actor and "review" in entry["roles"]
        charts = bool(target.audit.get("comparison_cohorts") and named_reviewer)
        view_audit = {**target.audit, "viewer": actor}
        if not named_reviewer:
            view_audit.pop("workflow_benchmark", None)
        recipe = False
        request_status = None
        if (
            named_reviewer
            and cfg.approval_provider == "zapier_slack"
            and ctx["job"].startswith("managed:")
        ):
            from opentour_cell.pilot.zapier_approval import ApprovalBridge

            try:
                bridge = ApprovalBridge(
                    current.directory,
                    current.organization,
                    current.directory / "zapier-approval.json",
                )
                recipe = bridge.uses_recipe()
            except (OSError, ValueError):
                pass
            if (
                recipe
                and current.explicit_recipient
                and target.audit.get("release_reviewer") == actor
            ):
                request_status = bridge.existing_status(
                    run_id=target.audit["job_id"],
                    run_revision=target.audit["run_revision"],
                    artifact_sha=target.audit["model_revision"],
                    team=cfg.team,
                    actor=actor,
                )
        modal = self.views.evidence_modal(
            recipe=recipe,
            request_status=request_status,
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
            charts_allowed=charts and not queue_handle,
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
            authorize=authorize,
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
            authorize is None
            and not recipe
            and current.explicit_recipient
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
                    expected_policy="opentour.pilot.zapier-slack-policy.v1",
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

    def _benchmark_proof(
        self,
        cfg: Settings,
        actor: str,
        view: str,
        ctx: Json,
        action: str,
        value: str,
        old_hash: str,
    ) -> None:
        _check(
            ctx["kind"] == ("evidence" if action == "ot_bench_proof" else "bench_proof")
            and value == ctx["revision"]
        )
        target = self._readout(cfg, actor, ctx["job"])
        _check(
            target
            and target.revision == value
            and target.audit.get("mode") in {"managed_rule", "managed_model"}
        )
        assert target is not None
        rows = [(target.dataset, ctx["job"], value)]

        def authorized_audit() -> Json:
            current = self._authorize_question_rows(cfg, actor, rows)
            entry = current.permission(actor, "inspect", target.dataset)
            if current.reviewer == actor and "review" in entry["roles"]:
                return target.audit
            return {}

        audit = authorized_audit()
        token = self.journal.context(actor, cfg.channel, view, "bench_proof", ctx["job"], value)
        self._show(
            view,
            self.views.benchmark_proof_modal(token, value, audit, cfg.workflow_label, action),
            old_hash,
            dataset=target.dataset,
            dataset_revision=target.dataset_revision,
            audit=target.audit,
            authorize=lambda: _check(authorized_audit() == audit),
        )

    def _open_handoff(
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
        _check(bool(old_hash))
        kinds = (
            {"release_recorded", "handoff_question"}
            if action == "ot_release_receipt"
            else {"handoff"}
        )
        _check(ctx["kind"] in kinds)
        target = self._readout(cfg, actor, job)
        _check(target and target.revision == ctx["revision"])
        assert target is not None
        rows = [(target.dataset, job, target.revision)]
        self._authorize_question_rows(cfg, actor, rows)
        receipt = self._handoff_receipt(cfg, actor, target)
        _check(receipt)
        assert receipt is not None
        _check(self._receipt_pin(actor, target.revision, receipt) == value)
        token = self.journal.context(actor, cfg.channel, view, "handoff", job, target.revision)

        def authorize_receipt() -> None:
            self._authorize_question_rows(cfg, actor, rows)
            _check(self._handoff_receipt(self.settings(), actor, target) == receipt)

        self._show(
            view,
            self.views.handoff_modal(token, value, receipt, target.audit),
            old_hash,
            dataset=target.dataset,
            dataset_revision=target.dataset_revision,
            audit=target.audit,
            authorize=authorize_receipt,
        )

    def _release(
        self,
        cfg: Settings,
        actor: str,
        view: str,
        ctx: Json,
        action: str,
        old_hash: str,
        *,
        authorize_vote: Callable[[], None] | None = None,
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
            before_native_publish=authorize_vote,
        )
        receipt = (
            self._handoff_receipt(current, actor, target)
            if saved["decision"] == "approved"
            else None
        )

        def authorize() -> None:
            self._authorize_question_rows(
                current, actor, [(target.dataset, ctx["job"], target.revision)]
            )
            if receipt:
                _check(self._handoff_receipt(self.settings(), actor, target) == receipt)

        token = self.journal.context(
            actor, current.channel, view, "release_recorded", ctx["job"], target.revision
        )
        self._show(
            view,
            self.views.release_recorded(
                token,
                saved["decision"],
                target.audit["model_revision"],
                current.recipient,
                revision=self._receipt_pin(actor, target.revision, receipt) if receipt else "",
            ),
            old_hash,
            dataset=target.dataset,
            dataset_revision=target.dataset_revision,
            audit=target.audit,
            authorize=authorize,
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

    def _live_summary(self, actor: str) -> Json | None:
        cfg = self.settings(check_engagement=False)
        try:
            workflow = self._live_workflow(cfg, actor)
            with self._live_scope(actor, workflow) as (principal, live, scope):
                _check(
                    scope.can_inspect
                    and cfg.permission(actor, "inspect", scope.dataset_ref)["aggregate"]
                )
                snapshot = live.snapshot(principal, workflow)
                receipts = live.read_receipts(principal, workflow)
                last = receipts[-1] if receipts else None
                return {
                    "workflow": workflow,
                    "dataset": scope.dataset_ref,
                    "revision": snapshot.evidence_revision,
                    "decisions": snapshot.decisions,
                    "verified": snapshot.verified,
                    "pending": snapshot.pending,
                    "disputed": snapshot.disputed,
                    "unmatched_outcomes": snapshot.unmatched_outcomes,
                    "last_ingested_at": snapshot.last_ingested_at,
                    "last_page": {
                        key: last[key]
                        for key in (
                            "receipt_ref",
                            "polled_at",
                            "interval_start",
                            "interval_end",
                            "coverage_through",
                            "complete",
                            "row_count",
                        )
                    }
                    if last
                    else None,
                }
        except Exception:
            return None

    def _readiness(self, actor: str) -> Json | None:
        from opentour_cell.pilot.readiness_selection import open_readiness

        cfg = self.settings(check_engagement=False)
        try:
            workflow = self._live_workflow(cfg, actor)
            with self._live_scope(actor, workflow) as (principal, _, scope):
                _check(
                    scope.can_inspect
                    and cfg.permission(actor, "inspect", scope.dataset_ref)["aggregate"]
                )
                components = open_readiness(cfg.directory, cfg.organization, workflow)
                receipt = components.readiness.read(workflow, actor=principal)
                if receipt is None:
                    return None
                _check(
                    receipt["dataset_ref"] == scope.dataset_ref
                    and receipt["dataset_revision"] == scope.dataset_revision
                )
                if receipt["next_action"] == "bind_verifier" and not (
                    scope.can_bind and any(g.kind == "independent_outcome" for g in scope.grants)
                ):
                    return {**receipt, "next_action": "ask_data_custodian_to_bind_verifier"}
                return receipt
        except Exception:
            # Optional worker evidence may be unavailable or no longer authorized.
            return None

    def _live_workflow(self, cfg: Settings, actor: str) -> str:
        cfg.permission(actor, "inspect")
        if cfg.live_workflow_ref:
            return cfg.live_workflow_ref
        preview = self._starter_setup(actor).preview()
        _check(preview.draft)
        assert preview.draft
        return str(preview.draft["workflow_ref"])

    @contextmanager
    def _live_scope(
        self, actor: str, workflow: str
    ) -> Iterator[tuple[IntakePrincipal, LiveEvidence, LiveScope]]:
        cfg = self.settings(check_engagement=False)
        _check(workflow == self._live_workflow(cfg, actor))
        principal = IntakePrincipal(cfg.organization, actor, "slack")
        from opentour_cell.pilot.live_authority import PrivateLiveAuthority

        authority = PrivateLiveAuthority(cfg.directory, cfg.organization, workflow)
        with authority.guard(principal, workflow) as scope:
            _check(scope.can_inspect or scope.can_bind or scope.can_nominate)
            yield principal, LiveEvidence(cfg.directory, authority), scope

    def _verifier_form(self, actor: str) -> VerifierForm:
        from opentour_cell.pilot.live_authority import PrivateLiveAuthority
        from opentour_cell.pilot.verifier_form import VerifierForm

        cfg = self.settings(check_engagement=False)
        workflow = self._live_workflow(cfg, actor)
        authority = PrivateLiveAuthority(cfg.directory, cfg.organization, workflow)
        return VerifierForm(
            LiveEvidence(cfg.directory, authority),
            IntakePrincipal(cfg.organization, actor, "slack"),
            workflow,
        )

    def _verifier_entry(self, actor: str) -> str:
        try:
            form = self._verifier_form(actor)
            choices = form.choices()
            return (
                self.journal.opaque("verify", [actor, form.workflow, [c.token for c in choices]])
                if choices
                else ""
            )
        except Exception:
            return ""

    def _verify_view(self, cfg: Settings, actor: str, view: str, page: int = 0) -> None:
        form = self._verifier_form(actor)
        choices = form.choices()
        page = max(0, min(page, (len(choices) - 1) // 50))
        selected = choices[page * 50 : page * 50 + 50]
        revision = _json({"page": page, "tokens": [c.token for c in selected]})
        ctx = self.journal.context(
            actor, cfg.channel, view, "verify_outcome", form.workflow, revision
        )

        def authorize() -> None:
            current = self._verifier_form(actor)
            _check(current.workflow == form.workflow and current.choices() == choices)

        self._show(
            view,
            self.views.verify_outcome(
                ctx,
                form.workflow,
                [(f"{c.decision} · rev {c.revision} · {c.namespace}", c.token) for c in selected],
                more=(page + 1) * 50 < len(choices),
                previous=page > 0,
            ),
            authorize=authorize,
        )

    def _nomination(
        self, actor: str, workflow: str, *, nominee: str = "", expected: str = ""
    ) -> tuple[str, str]:
        with self.journal.db() as db:
            if nominee:
                db.execute("BEGIN IMMEDIATE")
            with self._live_scope(actor, workflow) as (_, _, scope):
                if not (scope.can_nominate or scope.can_bind):
                    return "", ""
                key = self.journal.opaque(
                    "nomination",
                    [
                        scope.tenant,
                        workflow,
                        scope.engagement_epoch,
                        scope.dataset_ref,
                        scope.dataset_revision,
                        scope.authority_revision,
                    ],
                )
                prior = ""
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='verifier_nominations'"
                ).fetchone():
                    row = db.execute(
                        "SELECT nominee FROM verifier_nominations WHERE scope=?", (key,)
                    ).fetchone()
                    if row:
                        prior = str(row[0])
                token = (
                    self.journal.opaque("nominate", [key, actor, prior])
                    if scope.can_nominate
                    else ""
                )
                if nominee:
                    _check(scope.can_nominate and _slack_id(nominee, "UW") and expected == token)
                    db.execute(
                        "CREATE TABLE IF NOT EXISTS verifier_nominations("
                        "scope TEXT PRIMARY KEY, owner TEXT NOT NULL, nominee TEXT NOT NULL)"
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO verifier_nominations VALUES(?,?,?)",
                        (key, actor, nominee),
                    )
                    prior = nominee
                return token, prior

    def _owner_setup_readback(self, cfg: Settings, actor: str, workflow: str) -> Json | None:
        if not cfg.owner_settings_authority_file:
            return None
        from opentour_cell.pilot.owner_settings import OwnerSettings

        try:
            service = OwnerSettings.open_readback(
                cfg.directory,
                cfg.organization,
                self.store.engagement_id,
                authority_file=cfg.directory / cfg.owner_settings_authority_file,
            )
            result = service.setup_readback(actor=actor, workflow=workflow)
            _check(self.settings() == cfg)
            return result
        except (ValueError, OSError, sqlite3.Error):
            return None

    def _workflow_settings_view(self, cfg: Settings, actor: str, workflow: str, view: str) -> None:
        nomination = self._nomination(actor, workflow)
        setup = self._owner_setup_readback(cfg, actor, workflow)
        token = self.journal.context(actor, cfg.channel, view, "workflow_settings", workflow)
        self._show(
            view,
            self.views.workflow_settings(token, workflow, *nomination, setup=setup),
            authorize=lambda: _check(
                self.settings() == cfg
                and self._nomination(actor, workflow) == nomination
                and self._owner_setup_readback(cfg, actor, workflow) == setup
            ),
        )

    def _live_model(
        self, actor: str, workflow: str, verifier_only: bool = False
    ) -> tuple[str, list[tuple[str, str]], list[tuple[str, str]]]:
        with self._live_scope(actor, workflow) as (principal, live, scope):
            current: list[tuple[str, str]] = [
                (binding.feed_kind, ref)
                for ref, binding in live.current_bindings(principal, workflow)
            ]
            options = []
            if scope.can_bind:
                for grant in scope.grants:
                    if verifier_only and grant.kind != "independent_outcome":
                        continue
                    revision = binding_candidate(principal, scope, grant)
                    label = "Decisions" if grant.kind == "decision" else "Independent outcomes"
                    if grant.resource in dict(scope.slack_verifiers):
                        label = f"Named verifier {grant.verifier}"
                    options.append((f"{label} · {grant.resource[:36]} · {revision[:8]}", revision))
            readiness_revision = ""
            if verifier_only:
                receipt = self._readiness(actor)
                _check(
                    receipt
                    and receipt["workflow_ref"] == workflow
                    and receipt["next_action"] == "bind_verifier"
                )
                assert receipt
                readiness_revision = receipt["revision"]
            revision = self.journal.opaque(
                "scope",
                [
                    workflow,
                    scope.engagement_epoch,
                    scope.dataset_ref,
                    scope.dataset_revision,
                    scope.custodian,
                    readiness_revision,
                    options,
                    current,
                ],
            )
            return revision, current, options

    def _live_view(
        self,
        cfg: Settings,
        actor: str,
        view: str,
        workflow: str,
        old_hash: str = "",
        saved: str = "",
        verifier_only: bool = False,
    ) -> None:
        model = self._live_model(actor, workflow, verifier_only)
        revision, current, choices = model
        _check(not saved or any(ref == saved for _, ref in current))
        token = self.journal.context(
            actor,
            cfg.channel,
            view,
            "verifier_binding" if verifier_only else "live_binding",
            workflow,
            revision,
        )
        self._show(
            view,
            self.views.live_connections(token, workflow, current, choices, saved),
            old_hash,
            authorize=lambda: _check(self._live_model(actor, workflow, verifier_only) == model),
        )

    def _live_save(self, actor: str, ctx: Json, values: Json) -> str:
        workflow = ctx["job"]
        _check(
            ctx["kind"] in {"live_binding", "verifier_binding"} and set(values) == {"live_resource"}
        )
        field = values["live_resource"]
        _check(
            set(field) == {"live_resource"}
            and set(field["live_resource"]) == {"type", "selected_option"}
        )
        selected = field["live_resource"]
        _check(
            selected["type"] == "static_select"
            and set(selected["selected_option"]) == {"text", "value"}
        )
        choice = selected["selected_option"]["value"]
        with self._live_scope(actor, workflow) as (principal, live, scope):
            _check(
                scope.can_bind
                and self._live_model(actor, workflow, ctx["kind"] == "verifier_binding")[0]
                == ctx["revision"]
            )
            matches = [
                grant
                for grant in scope.grants
                if binding_candidate(principal, scope, grant) == choice
            ]
            _check(
                len(matches) == 1
                and (ctx["kind"] != "verifier_binding" or matches[0].kind == "independent_outcome")
            )
            live.initialize()
            return live.save_binding(
                principal, workflow, matches[0].resource, expected_revision=choice
            )

    def _starter_score_session(self, actor: str) -> StarterEvaluationSession:
        from opentour_cell.pilot.starter_evaluation_session import StarterEvaluationSession

        cfg = self.settings(check_engagement=False)
        cfg.permission(actor, "inspect")
        return StarterEvaluationSession(
            lambda: self.settings(check_engagement=False),
            IntakePrincipal(cfg.organization, actor, "slack"),
        )

    def _starter_scores(self, actor: str) -> tuple[str, list[tuple[Json, str]]]:
        try:
            session = self._starter_score_session(actor)
            entries = sorted(session.entries(), key=lambda e: e["execution_ref"])
            items = [
                (entry, self.journal.opaque("starter-result", [actor, entry])) for entry in entries
            ]
            return (
                (
                    self.journal.opaque("starter-results", [actor, [token for _, token in items]]),
                    items,
                )
                if items
                else ("", [])
            )
        except (ImportError, OSError, KeyError, TypeError, ValueError, sqlite3.Error):
            return "", []

    def _open_starter_score(
        self,
        cfg: Settings,
        actor: str,
        view: str,
        entry: Json,
        *,
        page: int = 0,
        previous: Json | None = None,
        old_hash: str = "",
    ) -> None:
        execution = entry["execution_ref"]
        session = self._starter_score_session(actor)
        _check(session.entry(execution) == entry)
        summary = session.summary(execution, expected_revision=entry["revision"])

        def permission() -> Json | None:
            if summary["state"] != "frozen_unscored":
                return None
            try:
                from opentour_cell.pilot.starter_reconnect import StarterReconnect

                return StarterReconnect(session).entry(
                    execution, expected_revision=entry["revision"]
                )
            except (ImportError, OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                return None

        reconnect = permission()
        pin = self.journal.opaque("starter-page", [actor, entry, summary])
        if previous is not None:
            _check(previous["pin"] == pin)
        if page == 2:
            _check(
                previous
                and reconnect
                and reconnect["state"] == "setup_required"
                and previous["reconnect"] == reconnect["revision"]
            )
        state = dict(
            entry=entry, pin=pin, page=page, reconnect=reconnect["revision"] if reconnect else None
        )
        token = self.journal.context(
            actor, cfg.channel, view, "starter_score", execution, _json(state)
        )

        def authorize() -> None:
            current = self._starter_score_session(actor)
            _check(
                current.entry(execution) == entry
                and current.summary(execution, expected_revision=entry["revision"]) == summary
                and (reconnect is None or permission() == reconnect)
            )

        self._show(
            view,
            self.views.starter_score_readout(token, entry, summary, page=page, reconnect=reconnect),
            old_hash,
            authorize=authorize,
        )

    def _case_session(self, actor: str) -> tuple[StarterCaseSession, StarterCaseEntry]:
        from opentour_cell.pilot.starter_map_confirmation import StarterCaseSession

        cfg = self.settings(check_engagement=False)
        cfg.permission(actor, "inspect")
        session = StarterCaseSession(
            lambda: self.settings(check_engagement=False),
            IntakePrincipal(cfg.organization, actor, "slack"),
        )
        entry = session.entry()
        _check(entry is not None)
        assert entry is not None
        return session, entry

    def _case_entry(self, actor: str) -> str:
        try:
            return self._case_session(actor)[1].revision
        except (ImportError, OSError, KeyError, TypeError, ValueError, sqlite3.Error):
            return ""

    def _check_case_state(self, entry: StarterCaseEntry, pack: Json, state: Json) -> None:
        _check(
            entry.revision == state["entry"]
            and pack["revision"] == state["revision"]
            and pack["authority"]["map_confirmation_ref"] == state["map"]
            and pack["authority"]["run_ref"] == entry.run_ref
            and bool(pack["frozen"]) == state["frozen"]
            and 0 <= state["index"] < len(pack["cases"])
            and pack["cases"][state["index"]]["case_id"] == state["case_id"]
        )

    def _current_case_state(self, actor: str, state: Json) -> None:
        session, entry = self._case_session(actor)
        self._check_case_state(entry, session.review(), state)

    def _case_view(self, cfg: Settings, actor: str, view: str, index: int) -> None:
        session, entry = self._case_session(actor)
        pack = session.review()
        _check(0 <= index < len(pack["cases"]))
        state = dict(
            entry=entry.revision,
            revision=pack["revision"],
            map=pack["authority"]["map_confirmation_ref"],
            frozen=bool(pack["frozen"]),
            index=index,
            case_id=pack["cases"][index]["case_id"],
        )
        token = self.journal.context(
            actor, cfg.channel, view, "case_detail", entry.run_ref, _json(state)
        )
        self._show(
            view,
            self.views.starter_case_detail(token, pack, index),
            authorize=lambda: self._current_case_state(actor, state),
        )

    def _case_action(
        self, cfg: Settings, actor: str, view: str, ctx: Json, action: str, value: str
    ) -> None:
        if ctx["kind"] == "case_clause":
            state = json.loads(ctx["revision"])
            self._current_case_state(actor, state)
            if action == "ot_case_back":
                _check(value == "back")
                self._case_view(cfg, actor, view, state["index"])
            else:
                _check(action == "ot_case_clause_page" and value in {"previous", "next"})
                self._case_clause(
                    cfg, actor, view, state, state["clause_page"] + (1 if value == "next" else -1)
                )
            return
        if ctx["kind"] in {"case_entry", "case_detail", "case_preview"}:
            session, entry = self._case_session(actor)
            _check(entry.run_ref == ctx["job"])
            if ctx["kind"] == "case_entry":
                _check(entry.revision == ctx["revision"])
                if action == "ot_case_generate":
                    from opentour_cell.pilot.starter_case_authoring import guided_case_provider

                    _check(value == "generate" and entry.can_propose)
                    session.propose(guided_case_provider())
                else:
                    _check(action == "ot_case_review" and value == "open")
                self._case_view(cfg, actor, view, 0)
            else:
                state = json.loads(ctx["revision"])
                pack = session.review()
                self._check_case_state(entry, pack, state)
                index = state["index"]
                if ctx["kind"] == "case_preview":
                    _check(action == "ot_case_commit" and value == "freeze" and entry.can_freeze)
                    session.freeze(splits=state["splits"], expected_revision=state["revision"])
                    self._case_view(cfg, actor, view, index)
                    return
                if action == "ot_case_citation":
                    _check(value == "citation" and not pack["frozen"])
                    self._case_clause(cfg, actor, view, state, 0)
                    return
                if action in {"ot_case_page_previous", "ot_case_page_next"}:
                    _check(action == f"ot_case_page_{value}")
                    _check(value in {"previous", "next"})
                    self._case_view(cfg, actor, view, index + (1 if value == "next" else -1))
                else:
                    if action == "ot_case_edit":
                        _check(value == "edit" and entry.can_adjudicate and not pack["frozen"])
                        form = self.views.adjudicate_starter_case
                        args = (pack["cases"][index], pack["authority"]["allowed_actions"])
                        kind = "case_edit"
                    else:
                        _check(
                            action == "ot_case_splits"
                            and value == "splits"
                            and entry.can_freeze
                            and not pack["frozen"]
                            and all(c["status"] != "proposed" for c in pack["cases"])
                        )
                        self._case_splits(cfg, actor, view, entry, pack, state, {})
                        return
                    token = self.journal.context(
                        actor, cfg.channel, view, kind, entry.run_ref, ctx["revision"]
                    )
                    self._show(
                        view,
                        form(token, *args),
                        authorize=lambda: self._current_case_state(actor, state),
                    )
            return

    def _case_clause(self, cfg: Settings, actor: str, view: str, state: Json, page: int) -> None:
        session, entry = self._case_session(actor)
        pack = session.review()
        self._check_case_state(entry, pack, state)
        service, principal = self._map_service(actor, entry.workflow_ref)
        ref = pack["cases"][state["index"]]["citation"]
        revision = pack["authority"]["map_review_revision"]
        passage = service.citation(principal, expected_revision=revision, citation_ref=ref)
        tok = self.journal.context(
            actor,
            cfg.channel,
            view,
            "case_clause",
            entry.run_ref,
            _json(state | {"clause_page": page}),
        )

        def authorize() -> None:
            self._current_case_state(actor, state)
            current, p = self._map_service(actor, entry.workflow_ref)
            _check(current.citation(p, expected_revision=revision, citation_ref=ref) == passage)

        self._show(view, self.views.starter_case_clause(tok, passage, page), authorize=authorize)

    def _case_splits(
        self,
        cfg: Settings,
        actor: str,
        view: str,
        entry: StarterCaseEntry,
        pack: Json,
        state: Json,
        splits: dict[str, str],
    ) -> None:
        pending = sorted({c["family_id"] for c in pack["cases"]} - splits.keys())
        page = pending[:20]
        if not page:
            _check(
                "reserved" in splits.values()
                and all(
                    c["status"] == "resolved" or splits[c["family_id"]] == "excluded"
                    for c in pack["cases"]
                )
            )
        token = self.journal.context(
            actor,
            cfg.channel,
            view,
            "case_splits" if page else "case_preview",
            entry.run_ref,
            _json(state | {"splits": splits, "page": page}),
        )
        layout = (
            self.views.starter_family_splits(token, page)
            if page
            else self.views.starter_freeze_preview(token, pack, splits)
        )
        self._show(view, layout, authorize=lambda: self._current_case_state(actor, state))

    def _map_choices(self, actor: str) -> dict[str, tuple[MapReviewService, str]]:
        from opentour_cell.pilot.map_review import open_map_review
        from opentour_cell.pilot.onboarding_mcp import _read_private

        cfg = self.settings(check_engagement=False)
        cfg.permission(actor, "inspect")
        principal = IntakePrincipal(cfg.organization, actor, "slack")
        try:
            registry = _read_private(cfg.directory / "map-inspection-catalog.json", 8192)
        except FileNotFoundError:
            names = ["map-review-selection.json"]
        else:
            _check(
                set(registry) == {"schema_version", "selections"}
                and registry["schema_version"] == "opentour.pilot.map-inspection-catalog.v1"
            )
            names = registry["selections"]
            _check(
                isinstance(names, list)
                and 0 < len(names) <= 12
                and all(
                    isinstance(n, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\.json", n)
                    for n in names
                )
                and len(set(names)) == len(names)
            )
        choices = {}
        for name in names:
            try:
                service = open_map_review(cfg.directory, cfg.organization, selection_file=name)
                revision = service.entry(principal)
            except (KeyError, OSError, TypeError, ValueError, sqlite3.Error):
                continue
            if revision:
                _check(service.workflow_ref not in choices)
                choices[service.workflow_ref] = (
                    service,
                    self.journal.opaque("map", [actor, service.workflow_ref, revision]),
                )
        return choices

    def _map_service(
        self, actor: str, workflow: str | None = None
    ) -> tuple[MapReviewService, IntakePrincipal]:
        choices = self._map_choices(actor)
        if workflow is None:
            _check(len(choices) == 1)
            workflow = next(iter(choices))
        _check(workflow in choices)
        service = choices[workflow][0]
        return service, IntakePrincipal(service.tenant, actor, "slack")

    def _map_entry(self, actor: str, workflow: str | None = None) -> str:
        try:
            choices = self._map_choices(actor)
            if workflow is not None:
                return choices[workflow][1]
            if len(choices) == 1:
                return next(iter(choices.values()))[1]
            if choices:
                return self.journal.opaque(
                    "map-choices",
                    [actor, sorted((wf, choice[1]) for wf, choice in choices.items())],
                )
        except (ImportError, KeyError, OSError, TypeError, ValueError, sqlite3.Error):
            pass
        return ""

    def _map_verifier_plan(self, actor: str, workflow: str) -> str:
        try:
            setup = self._starter_setup(actor)
            service, _ = self._map_service(actor, workflow)
            if service.authority.path != setup.path:
                return ""
            preview = setup.preview()
            if preview.draft and preview.draft["workflow_ref"] == workflow:
                charter = preview.charter
                return (
                    f"{charter.verification_plan} · {charter.verifier or 'Verifier unbound'} · "
                    f"{charter.responsible_person} · next check {charter.next_check}"
                )
        except (KeyError, OSError, TypeError, ValueError, sqlite3.Error):
            pass
        return ""

    def _map_show(
        self,
        actor: str,
        workflow: str,
        revision: str,
        view: str,
        layout: Json,
        *,
        requires_editor: bool,
        verifier_plan: str = "",
    ) -> None:
        def authorize() -> None:
            service, principal = self._map_service(actor, workflow)
            _check(not verifier_plan or self._map_verifier_plan(actor, workflow) == verifier_plan)
            with service.disclosure(principal, expected_revision=revision) as current:
                _check(not requires_editor or current.can_edit)

        self._show(view, layout, authorize=authorize)

    def _map_view(self, cfg: Settings, actor: str, view: str, workflow: str, page: int = 0) -> None:
        service, principal = self._map_service(actor, workflow)
        snap = service.preview(principal)
        from datetime import date

        from opentour_cell.pilot.map_inspection import branch_example

        example_date = (
            date.fromisoformat(snap.effective_from.iso_date) if snap.effective_from else None
        )
        rows = []
        for branch in snap.branches:
            example = branch_example(snap, branch, decision_date=example_date)
            inputs = ", ".join(
                f"{name}={value if value is not None else 'missing'}"
                for name, value in example.inputs
            )
            clauses = [
                " ".join(str(v) for v in (c.field, c.operator, c.threshold) if v is not None)
                for c in branch.conditions
            ]
            rows.append(
                self.views.MapReviewRow(
                    branch.branch_ref,
                    snap.trigger,
                    ", ".join(
                        f"{field} ({snap.field_kinds[field]})"
                        for field in dict.fromkeys(c.field for c in branch.conditions)
                    )
                    or "Not specified",
                    " and ".join(clauses) or "Requires owner clarification",
                    f"Priority {branch.precedence}"
                    if branch.precedence is not None
                    else "Order unresolved",
                    branch.action or "Unresolved decision",
                    branch.fallback,
                    "Policy clause"
                    if branch.source_support == "source"
                    else (
                        f"Owner assertion · {branch.editor_ref or branch.actor_ref} · "
                        f"{branch.reason or ''}"
                    ),
                    ("Source changed · " if branch.branch_ref in snap.changed_branch_refs else "")
                    + branch.disposition.replace("_", " "),
                    (f"Decision date {example_date.isoformat()} · " if example_date else "")
                    + f"{inputs or 'Unavailable'} → {example.action or 'Unresolved'}"
                    f" · {example.explanation}",
                )
            )
        from opentour_cell.pilot.map_review import citation_ref

        priority = citation_ref(snap.precedence_citation) if snap.precedence_citation else ""
        date_ref = citation_ref(snap.effective_from.citation) if snap.effective_from else ""
        allowed = [r.branch_ref for r in rows[page * 5 : (page + 1) * 5]]
        state = _json(
            {
                "rev": snap.review_revision,
                "page": page,
                "branches": allowed,
                "priority": priority,
                "date_ref": date_ref,
            }
        )
        token = self.journal.context(actor, cfg.channel, view, "map_review", workflow, state)
        verifier_plan = self._map_verifier_plan(actor, workflow)
        layout = self.views.decision_map(
            token,
            workflow,
            snap.review_revision,
            rows,
            [snap.question] if snap.question else [],
            page,
            snap.can_edit,
            snap.confirmed_ref or "",
            snap.can_set_precedence,
            version=snap.version,
            source_revision=snap.source_revision,
            draft_revision=snap.map_revision,
            unit_of_work=snap.unit_of_work,
            verifier_plan=verifier_plan,
            assertion_basis=(
                f"Trigger and fallback assertion by {snap.settings_editor_ref} "
                f"({snap.settings_actor_ref}) · {snap.settings_reason}"
                if snap.settings_support == "owner_assertion"
                else ""
            ),
            priority_citation=priority,
            effective_date=snap.effective_from.iso_date if snap.effective_from else "",
            effective_citation=date_ref,
            priority_assertion=(
                f"Priority confirmed by {snap.precedence_actor_ref} · {snap.precedence_reason}"
                if snap.precedence_actor_ref and snap.precedence_reason
                else ""
            ),
        )
        self._map_show(
            actor,
            workflow,
            snap.review_revision,
            view,
            layout,
            requires_editor=snap.can_edit,
            verifier_plan=verifier_plan,
        )

    def _map_action(
        self, cfg: Settings, actor: str, view: str, ctx: Json, action: str, value: str
    ) -> None:
        workflow = ctx["job"]
        state = json.loads(ctx["revision"])
        service, principal = self._map_service(actor, workflow)
        if action == "ot_map_back":
            _check(value == "back")
            self._map_view(cfg, actor, view, workflow)
            return
        snap = service.preview(principal)
        _check(snap.review_revision == state["rev"])
        page = state.get("page", 0)
        if action == "ot_map_page":
            _check(ctx["kind"] == "map_review" and value in ("next", "previous"))
            self._map_view(cfg, actor, view, workflow, page + (1 if value == "next" else -1))
            return
        if action == "ot_map_assertions":
            _check(ctx["kind"] == "map_review" and snap.can_edit and value == state["rev"])
            token = self.journal.context(
                actor,
                cfg.channel,
                view,
                "map_assertions",
                workflow,
                _json(
                    {
                        "rev": snap.review_revision,
                        "source": snap.source_revision,
                        "grant": service.entry(principal),
                        "actions": snap.actions,
                    }
                ),
            )
            layout = self.views.map_assertions(
                token,
                trigger=snap.settings_trigger,
                unit_of_work=snap.unit_of_work,
                fallback=snap.fallback_action,
                permitted=snap.actions,
            )
        elif action == "ot_map_precedence":
            _check(
                ctx["kind"] == "map_review"
                and snap.can_edit
                and snap.can_set_precedence
                and snap.question
                and value == state["rev"]
            )
            ranked = [
                r for r in snap.branches if r.action is not None and r.disposition != "out_of_scope"
            ]
            token = self.journal.context(
                actor,
                cfg.channel,
                view,
                "map_precedence",
                workflow,
                _json(
                    {
                        "rev": snap.review_revision,
                        "grant": service.entry(principal),
                        "source": snap.source_revision,
                        "refs": [r.branch_ref for r in ranked],
                    }
                ),
            )
            layout = self.views.map_precedence(
                token,
                snap.question,
                [(f"{r.action} · {r.branch_ref}", r.branch_ref) for r in ranked],
            )
        else:
            if action == "ot_map_clause_page":
                _check(ctx["kind"] == "map_clause" and value in ("next", "previous"))
                ref = state["branch"]
                page += 1 if value == "next" else -1
            else:
                _check(
                    ctx["kind"] == "map_review"
                    and (
                        (action == "ot_map_priority" and value and value == state["priority"])
                        or (
                            action == "ot_map_date_clause"
                            and value
                            and value == state.get("date_ref")
                        )
                        or (
                            action in {"ot_map_edit", "ot_map_citation"}
                            and value in state["branches"]
                        )
                    )
                )
                ref, page = value, 0
            branch = next((r for r in snap.branches if r.branch_ref == ref), None)
            if action in {
                "ot_map_clause_page",
                "ot_map_citation",
                "ot_map_priority",
                "ot_map_date_clause",
            }:
                from opentour_cell.pilot.map_review import citation_ref

                citation = branch.citation if branch else snap.precedence_citation
                if snap.effective_from and citation_ref(snap.effective_from.citation) == ref:
                    citation = snap.effective_from.citation
                _check(
                    citation is not None and (branch is not None or citation_ref(citation) == ref)
                )
                assert citation is not None
                passage = service.citation(
                    principal,
                    expected_revision=snap.review_revision,
                    citation_ref=citation_ref(citation),
                )
                token = self.journal.context(
                    actor,
                    cfg.channel,
                    view,
                    "map_clause",
                    workflow,
                    _json({"rev": snap.review_revision, "branch": ref, "page": page}),
                )
                layout = self.views.map_clause(
                    token,
                    citation.resource_ref,
                    citation.source_revision,
                    citation.section_ref,
                    f"{citation.line_start}–{citation.line_end}",
                    passage,
                    page,
                    (
                        f"Owner assertion · {branch.editor_ref} · {branch.reason}"
                        if branch is not None and branch.source_support == "owner_assertion"
                        else ""
                    ),
                )
            else:
                _check(action == "ot_map_edit" and snap.can_edit and branch is not None)
                assert branch is not None
                token = self.journal.context(
                    actor,
                    cfg.channel,
                    view,
                    "map_edit",
                    workflow,
                    _json(
                        {
                            "rev": snap.review_revision,
                            "branch": ref,
                            "source": snap.source_revision,
                            "grant": service.entry(principal),
                        }
                    ),
                )
                predicates = "\n".join(
                    " ".join(str(v) for v in (c.field, c.operator, c.threshold) if v is not None)
                    for c in branch.conditions
                )
                layout = self.views.map_branch_edit(
                    token, ref, branch.action, predicates, snap.actions
                )
        self._map_show(
            actor, workflow, snap.review_revision, view, layout, requires_editor=snap.can_edit
        )

    def _starter_setup(self, actor: str) -> StarterSetup:
        # LocalStarterAuthority pins the engagement in its own transaction.
        return StarterSetup(lambda: self.settings(check_engagement=False), actor)

    def _starter_view(
        self, cfg: Settings, actor: str, view: str, preview: StarterPreview, old_hash: str
    ) -> None:
        token = self.journal.context(
            actor,
            cfg.channel,
            view,
            "starter_saved" if preview.draft else "starter_preview",
            job=str(preview.draft["workflow_ref"]) if preview.draft else "",
            revision=preview.revision,
        )

        workflow = str(preview.draft["workflow_ref"]) if preview.draft else ""
        map_entry = self._map_entry(actor, workflow) if workflow else ""

        def authorize() -> None:
            _check(self._starter_setup(actor).preview() == preview)
            _check(not map_entry or self._map_entry(actor, workflow) == map_entry)

        self._show(
            view,
            self.views.starter_charter(token, preview, map_entry),
            old_hash,
            authorize=authorize,
        )

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
        if ctx["kind"] == "queued_evidence":
            handle = ctx["revision"]
            queued_row, queued_target, queued_source = self._resolve_workflow_proposal(
                actor, handle
            )
            _check(queued_row.action == "open_review" and queued_source == ctx["job"])
            ctx = {**ctx, "kind": "evidence", "revision": queued_target.revision}
            if action in {"ot_release_approve", "ot_release_changes"}:
                _check(value == queued_target.revision)

                def authorize_vote() -> None:
                    _check(
                        self._resolve_workflow_proposal(actor, handle)
                        == (queued_row, queued_target, queued_source)
                    )

                self._release(
                    cfg, actor, view, ctx, action, old_hash, authorize_vote=authorize_vote
                )
                return
        job = ctx["job"]
        if ctx["kind"] == "starter_score":
            _check(isinstance(old_hash, str) and bool(old_hash))
            state = json.loads(ctx["revision"])
            page = state["page"]
            if action == "ot_score_next":
                _check(value == "next" and page == 0)
                page_target = 1
            elif action == "ot_score_previous":
                _check(value == "previous" and page in (1, 2))
                page_target = page - 1
            else:
                _check(
                    action == "ot_score_reconnect"
                    and value == "setup"
                    and page == 1
                    and state["reconnect"]
                )
                page_target = 2
            _check(state["entry"]["execution_ref"] == ctx["job"])
            self._open_starter_score(
                cfg,
                actor,
                view,
                state["entry"],
                page=page_target,
                previous=state,
                old_hash=old_hash,
            )
            return
        if ctx["kind"] == "starter_scores":
            version, items = self._starter_scores(actor)
            _check(action == "ot_starter_score_choice" and version and version == ctx["revision"])
            selected = [item for item, token in items if value == token]
            _check(len(selected) == 1)
            self._open_starter_score(cfg, actor, view, selected[0], old_hash=old_hash)
            return
        if ctx["kind"] in {"case_entry", "case_detail", "case_clause", "case_preview"}:
            self._case_action(cfg, actor, view, ctx, action, value)
            return
        if ctx["kind"] in {"map_review", "map_clause", "map_conflict"} or (
            action == "ot_map_back"
            and ctx["kind"] in {"map_edit", "map_precedence", "map_assertions"}
        ):
            self._map_action(cfg, actor, view, ctx, action, value)
            return
        if ctx["kind"] == "map_choices" and action == "ot_choose_map":
            _check(self._map_entry(actor) == ctx["revision"])
            choices = self._map_choices(actor)
            matches = [wf for wf, choice in choices.items() if choice[1] == value]
            _check(len(matches) == 1)
            self._map_view(cfg, actor, view, matches[0])
            return
        if ctx["kind"] == "starter_saved" and action == "ot_map_open":
            workflow = ctx["job"]
            _check(workflow)
            entry = self._map_entry(actor, workflow)
            _check(entry and value == entry)
            self._map_view(cfg, actor, view, workflow)
            return
        if ctx["kind"] == "workflow_settings":
            if action == "ot_nominate_verifier":
                _check(value and self._nomination(actor, ctx["job"])[0] == value)
                token = self.journal.context(
                    actor, cfg.channel, view, "nomination", ctx["job"], value
                )
                self._show(
                    view,
                    self.views.nominate_verifier(token, ctx["job"]),
                    old_hash,
                    authorize=lambda: _check(self._nomination(actor, ctx["job"])[0] == value),
                )
                return
            _check(action == value == "ot_connect_live")
            self._live_view(cfg, actor, view, ctx["job"], old_hash)
        elif ctx["kind"] == "workflow_chooser":
            _check(action in {"ot_workflow_starter", "ot_workflow_historical"})
            current = self.settings()
            _check(action == "ot_workflow_" + value and self._can_add_workflow(current, actor))
            if value == "starter" and current.starter_setup is not None:
                starter = self._starter_setup(actor).preview()
                self._starter_view(current, actor, view, starter, old_hash)
                return
            token = self.journal.context(actor, cfg.channel, view, "workflow_instructions")
            self._show(
                view,
                self.views.workflow_instructions(token, value),
                old_hash,
                setup_context=None
                if cfg.explicit_recipient or current.explicit_recipient
                else token,
            )
        elif ctx["kind"] in {"starter_preview", "starter_saved"}:
            _check(
                (ctx["kind"], action)
                in {
                    ("starter_preview", "ot_starter_save"),
                    ("starter_saved", "ot_starter_benchmark"),
                }
                and value == ctx["revision"]
            )
            saved = self._starter_setup(actor).save(value, self.journal.opaque("save", ctx["id"]))
            self._starter_view(self.settings(), actor, view, saved, old_hash)
        elif ctx["kind"] == "workflow_instructions":
            raise Refused("This setup screen has no actions.")
        elif action == "ot_verify_page":
            _check(ctx["kind"] == "verify_outcome" and value in {"previous", "next"})
            self._verify_view(
                cfg,
                actor,
                view,
                json.loads(ctx["revision"])["page"] + (1 if value == "next" else -1),
            )
        elif action in {"ot_current_week", "ot_week_bind"}:
            open_verifier = action == "ot_week_bind"
            if open_verifier:
                _check(ctx["kind"] == "current_week" and value == ctx["revision"])
            else:
                _check(ctx["kind"] == "regressions")
                listing = json.loads(ctx["revision"])
                _check(
                    listing["topic"] == StatusTopic.LIVE_ERRORS and value == listing.get("current")
                )
            weekly = self._current_question(actor, StatusTopic.LIVE_ERRORS)
            _check(weekly and weekly[1] == value)
            assert weekly is not None and weekly[2] is not None
            workflow, pin, weekly_facts = weekly
            assert weekly_facts is not None
            if open_verifier:
                _check(
                    weekly_facts["readiness"]
                    and weekly_facts["readiness"]["next_action"] == "bind_verifier"
                )
                self._live_view(cfg, actor, view, workflow, old_hash, verifier_only=True)
                return
            token = self.journal.context(actor, cfg.channel, view, "current_week", revision=pin)
            self._show(
                view,
                self.views.weekly_question(token, workflow, weekly_facts, pin),
                old_hash,
                authorize=lambda: _check(
                    self._current_question(actor, StatusTopic.LIVE_ERRORS) == weekly
                ),
            )
        elif action == "ot_week_scopes":
            _check(ctx["kind"] == "current_week" and value == "scopes")
            self._regression_picker(cfg, actor, view, old_hash, topic=StatusTopic.LIVE_ERRORS.value)
        elif action in {"ot_current_training", "ot_readiness_proof"}:
            proof = action == "ot_readiness_proof"
            if proof:
                _check(ctx["kind"] == "current_training" and value == ctx["revision"])
            else:
                _check(ctx["kind"] == "regressions")
                listing = json.loads(ctx["revision"])
                _check(listing["topic"] == StatusTopic.TRAINING and value == listing.get("current"))
            selected_training = self._current_question(actor, StatusTopic.TRAINING)
            _check(selected_training and selected_training[1] == value)
            assert selected_training is not None
            workflow, _, receipt = selected_training
            _check(not proof or receipt is not None)
            token = self.journal.context(
                actor, cfg.channel, view, "current_training", revision=value
            )
            self._show(
                view,
                self.views.current_training_question(
                    token, workflow, receipt, pin=value, proof=proof
                ),
                old_hash,
                authorize=lambda: _check(
                    self._current_question(actor, StatusTopic.TRAINING) == selected_training
                ),
            )
        elif action == "ot_training_scopes":
            _check(ctx["kind"] == "current_training" and value == "scopes")
            self._regression_picker(cfg, actor, view, old_hash, topic=StatusTopic.TRAINING.value)
        elif action == "ot_help":
            self._help(cfg, actor, view, old_hash=old_hash)
        elif action == "ot_readouts":
            self._results(cfg, actor, view, old_hash)
        elif action in {"ot_result_prev", "ot_result_next", "ot_result_open"}:
            if ctx["kind"] in {"arm_chart", "chart_proof"}:
                selected = json.loads(ctx["revision"])
                _check(action == "ot_result_open" and value == selected["pin"])
                self._selected_chart(
                    cfg,
                    actor,
                    view,
                    selected["source"],
                    selected["version"],
                    value,
                    old_hash,
                    proof=ctx["kind"] == "arm_chart",
                )
                return
            if ctx["kind"] == "chart_picker":
                listing = json.loads(ctx["revision"])
                version = listing["version"]
                if action == "ot_result_open":
                    _check(value in listing["items"])
                    self._selected_chart(
                        cfg, actor, view, listing["items"][value], version, value, old_hash
                    )
                else:
                    plan = ArmChart(version, (ComparisonArm.INCUMBENT, ComparisonArm.SIMPLE_RULE))
                    self._chart_picker(
                        cfg,
                        actor,
                        view,
                        plan,
                        old_hash,
                        listing["page"] + (1 if action == "ot_result_next" else -1),
                    )
                return
            if ctx["kind"] == "regressions":
                listing = json.loads(ctx["revision"])
                if listing.get("topic") in WORKPLACE_QUESTIONS:
                    _check(listing.get("workplace_scope") == cfg.live_workflow_ref)
                if listing.get("topic") == StatusTopic.DELIVERY:
                    _check(listing.get("delivery_scope") == cfg.live_workflow_ref)
                if action == "ot_result_open":
                    _check(value in listing["items"])
                    source = listing["items"][value]
                    _check(source in {s for _, s in self._result_sources(cfg, actor)})
                    self._questions(
                        cfg,
                        actor,
                        view,
                        source,
                        value,
                        listing.get("topic", "changes"),
                        old_hash,
                        expected_receipt_pin=listing.get("deliveries", {}).get(value, ""),
                    )
                else:
                    self._regression_picker(
                        cfg,
                        actor,
                        view,
                        old_hash,
                        listing["page"] + (1 if action == "ot_result_next" else -1),
                        topic=listing.get("topic", "changes"),
                    )
                return
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
        elif action == "ot_managed_review_status":
            _check(ctx["kind"] == "evidence" and value == ctx["revision"] and actor == cfg.reviewer)
            self.private_channel(cfg, actor)
            target = self._managed(cfg, actor, job)
            _check(target.revision == value)
            cfg.permission(actor, "review", target.dataset)
            self._show(
                view,
                self.views.managed_review_status(ctx["id"], target.audit),
                old_hash,
                dataset=target.dataset,
                dataset_revision=target.dataset_revision,
                audit=target.audit,
            )
        elif action in {"ot_plugin_continue", "ot_plugin_request"}:
            from opentour_cell.pilot.zapier_approval import RECIPE_POLICY, ApprovalBridge

            target = self._managed(cfg, actor, job)
            _check(
                cfg.explicit_recipient
                and cfg.reviewer == actor
                and target.audit.get("release_reviewer") == actor
            )
            cfg.permission(actor, "review", target.dataset)
            self.private_channel(cfg, actor)
            bridge = ApprovalBridge(
                cfg.directory, cfg.organization, cfg.directory / "zapier-approval.json"
            )
            binding = {
                "run_id": target.audit["job_id"],
                "run_revision": target.audit["run_revision"],
                "artifact_sha": target.audit["model_revision"],
                "inspection_ref": target.revision,
            }
            if action == "ot_plugin_continue":
                _check(ctx["kind"] == "evidence" and value == ctx["revision"] == target.revision)
                preview = bridge.issue(**binding, expected_policy=RECIPE_POLICY, preview_only=True)
                token = self.journal.context(
                    actor, cfg.channel, view, "plugin_preview", job, _json(preview)
                )
                self._show(
                    view,
                    self.views.plugin_preview_modal(token, preview),
                    old_hash,
                    dataset=target.dataset,
                    dataset_revision=target.dataset_revision,
                    audit=target.audit,
                )
                return
            _check(ctx["kind"] == "plugin_preview")
            confirmed = json.loads(ctx["revision"])
            _check(value == confirmed["request_id"])
            request = bridge.issue(
                **binding, expected_policy=RECIPE_POLICY, confirmed_preview=confirmed
            )
            token = self.journal.context(
                actor, cfg.channel, view, "plugin_request", job, request["request_id"]
            )
            self._show(
                view, self.views.plugin_waiting_modal(token, request["request_id"]), old_hash
            )
        elif action in {"ot_plugin_status", "ot_plugin_receipt"}:
            from opentour_cell.pilot.zapier_approval import ApprovalBridge

            if ctx["kind"] == "queued_provider_status":
                _check(value == ctx["job"])
                self.private_channel(cfg, actor)
                self._open_workflow_proposal(
                    cfg,
                    actor,
                    view,
                    ctx["revision"],
                    receipt_request=value if action == "ot_plugin_receipt" else "",
                )
                return
            _check(ctx["kind"] in {"evidence", "plugin_request"})
            self.private_channel(cfg, actor)
            bridge = ApprovalBridge(
                cfg.directory, cfg.organization, cfg.directory / "zapier-approval.json"
            )
            target = self._managed(cfg, actor, job)
            if ctx["kind"] == "evidence":
                _check(
                    target.revision == ctx["revision"]
                    and target.audit.get("release_reviewer") == actor
                )
                existing = bridge.existing_status(
                    run_id=target.audit["job_id"],
                    run_revision=target.audit["run_revision"],
                    artifact_sha=target.audit["model_revision"],
                    team=cfg.team,
                    actor=actor,
                )
                _check(existing and existing["request_id"] == value)
            else:
                _check(value == ctx["revision"])
            status = bridge.decision_status(value, team=cfg.team, actor=actor)
            self._show(
                view,
                self.views.plugin_waiting_modal(
                    ctx["id"], value, status, show_receipt=action == "ot_plugin_receipt"
                ),
                old_hash,
                dataset=target.dataset,
                dataset_revision=target.dataset_revision,
                audit=target.audit,
            )
        elif action == "ot_evidence":
            _check(
                ctx["kind"]
                in {"home", "evidence", "status", "eval_detail", "questions", "handoff_question"}
                and value == ctx["revision"]
            )
            self._evidence(cfg, actor, view, ctx, old_hash)
        elif action == "ot_question" or action.startswith("ot_question_"):
            _check(ctx["kind"] in {"evidence", "questions"} and value == ctx["revision"])
            topic = "" if action == "ot_question" else action.removeprefix("ot_question_")
            self._questions(cfg, actor, view, job, ctx["revision"], topic, old_hash)
        elif action in {"ot_release_receipt", "ot_delivery_status"}:
            self._open_handoff(cfg, actor, view, ctx, action, value, old_hash)
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

    def _map_submit(
        self, cfg: Settings, actor: str, view: str, ctx: Json, callback: str, values: Json
    ) -> None:
        from opentour_cell.pilot.guided_decision_map import Condition
        from opentour_cell.pilot.map_review import BranchEdit, MapSettingsEdit, StaleMapError

        workflow = ctx["job"]
        state = json.loads(ctx["revision"])
        service, principal = self._map_service(actor, workflow)
        current_grant = service.entry(principal)
        _check(current_grant and current_grant == state["grant"])
        operation = self.journal.opaque("map-save", [ctx["id"], actor])
        changes: tuple[BranchEdit, ...] = ()
        order = None
        order_reason = None
        settings_edit = None
        reason = values["reason"]["reason"].get("value")
        _check(isinstance(reason, str) and 0 < len(reason.strip()) <= 1000)
        if callback == "ot_map_assertions_save":
            _check(
                ctx["kind"] == "map_assertions"
                and set(values)
                in ({"trigger", "fallback", "reason"}, {"trigger", "fallback", "reason", "unit"})
            )
            trigger = values["trigger"]["trigger"].get("value")
            _check(
                trigger is None
                or (
                    isinstance(trigger, str)
                    and len(trigger) <= 160
                    and (not trigger or trigger.isprintable())
                )
            )
            fallback = self._field(values, "fallback", select=True)
            _check(fallback == "unresolved" or fallback in state["actions"])
            unit = values.get("unit", {}).get("unit", {}).get("value")
            _check(unit is None or isinstance(unit, str))
            settings_edit = MapSettingsEdit(
                (trigger or "").strip() or None,
                None if fallback == "unresolved" else fallback,
                reason,
                (unit or "").strip() or None,
            )
        elif callback == "ot_map_precedence_save":
            _check(ctx["kind"] == "map_precedence")
            refs = state["refs"]
            if len(refs) > 30:
                _check(set(values) == {"reason", "order"})
                raw_order = values["order"]["order"]["value"]
                _check(isinstance(raw_order, str) and len(raw_order) <= 1200)
                positions = [p.strip() for p in raw_order.split(",")]
                _check(
                    len(positions) == len(refs)
                    and all(
                        re.fullmatch("[1-9][0-9]{0,2}", p) and int(p) <= len(refs)
                        for p in positions
                    )
                )
                order = tuple(refs[int(p) - 1] for p in positions)
            else:
                _check(set(values) == {"reason", *(f"rank_{i}" for i in range(len(refs)))})
                order = tuple(
                    self._field(values, f"rank_{i}", select=True) for i in range(len(refs))
                )
            _check(set(order) == set(refs) and len(set(order)) == len(refs))
            order_reason = reason
        else:
            _check(
                ctx["kind"] == "map_edit"
                and set(values) == {"reason", "action", "disposition", "conditions"}
            )
            branch = state["branch"]
            disposition = self._field(values, "disposition", select=True)
            _check(disposition in {"accepted", "corrected", "unresolved", "out_of_scope"})
            selected_action = values["action"]["action"].get("selected_option")
            action = self._field(values, "action", select=True) if selected_action else None
            _check(disposition != "corrected" or action)
            raw = values["conditions"]["conditions"].get("value") or ""
            _check(isinstance(raw, str) and len(raw) <= 3000)
            conditions = []
            if disposition == "corrected":
                for line in raw.splitlines():
                    terms = line.split()
                    _check(len(terms) in (2, 3))
                    _check(
                        (terms[1] in {"present", "missing"} and len(terms) == 2)
                        or (terms[1] in {"<", "<=", ">", ">=", "=="} and len(terms) == 3)
                    )
                    conditions.append(
                        Condition(terms[0], terms[1], terms[2] if len(terms) == 3 else None)
                    )
            changes = (
                BranchEdit(
                    branch,
                    disposition,
                    reason,
                    action if disposition == "corrected" else None,
                    tuple(conditions) if disposition == "corrected" else None,
                ),
            )
        try:
            service.save(
                principal,
                expected_revision=state["rev"],
                operation_ref=operation,
                changes=changes,
                precedence=order,
                precedence_reason=order_reason,
                settings=settings_edit,
            )
        except StaleMapError as stale:
            current = stale.current
            _check(service.entry(principal) == current_grant)
            reapply = current.can_edit and state.get("source") == current.source_revision
            before = state["rev"]

            diff: list[str] = []
            previous = stale.previous
            if previous is not None:
                for label, old, new in (
                    ("Unit of work", previous.unit_of_work, current.unit_of_work),
                    ("Trigger", previous.settings_trigger, current.settings_trigger),
                    ("Fallback", previous.fallback_action, current.fallback_action),
                    ("Assertion basis", previous.settings_reason, current.settings_reason),
                    ("Assertion owner", previous.settings_editor_ref, current.settings_editor_ref),
                    ("Priority basis", previous.precedence_reason, current.precedence_reason),
                    ("Priority owner", previous.precedence_actor_ref, current.precedence_actor_ref),
                ):
                    if old != new:
                        diff.append(f"{label} · {old or 'Unspecified'} → {new or 'Unspecified'}")
            for change in stale.diff:
                older, newer = change.before, change.after
                if older is None or newer is None:
                    status = "Current branch" if newer else "Absent from current map"
                    diff.append(f"{change.branch_ref} · {status}")
                    continue
                old_predicate, new_predicate = (
                    " and ".join(
                        " ".join(
                            str(v) for v in (c.field, c.operator, c.threshold) if v is not None
                        )
                        for c in row.conditions
                    )
                    for row in (older, newer)
                )
                branch_diff = []
                for label, before_value, after_value in (
                    ("Conditions", old_predicate, new_predicate),
                    ("Decision", older.action, newer.action),
                    ("Review", older.disposition, newer.disposition),
                    ("Priority", older.precedence, newer.precedence),
                    ("Support", older.source_support, newer.source_support),
                    ("Owner", older.editor_ref, newer.editor_ref),
                    ("Reason", older.reason, newer.reason),
                    ("Source", older.citation_ref, newer.citation_ref),
                ):
                    if before_value != after_value:
                        branch_diff.append(
                            f"{label} · {before_value or 'Unspecified'} → "
                            f"{after_value or 'Unspecified'}"
                        )
                if branch_diff:
                    diff.append(f"{change.branch_ref} · " + " | ".join(branch_diff))
            eligible = [
                r
                for r in current.branches
                if r.action is not None and r.disposition != "out_of_scope"
            ]
            reapply_branch = (
                reapply
                and bool(changes)
                and any(r.branch_ref == changes[0].branch_ref for r in current.branches)
            )
            reapply_order = (
                reapply and order is not None and set(order) == {r.branch_ref for r in eligible}
            )
            reapply_settings = reapply and settings_edit is not None
            if reapply_settings and settings_edit is not None:
                diff.extend(
                    [
                        f"Current trigger · {current.trigger}",
                        f"Current fallback · {current.fallback_action or 'Unresolved'}",
                        f"Your trigger · {settings_edit.trigger or 'Unspecified'}",
                        f"Your fallback · {settings_edit.fallback_action or 'Unresolved'}",
                    ]
                )
            reapply = reapply_branch or reapply_order or reapply_settings
            token = self.journal.context(
                actor,
                cfg.channel,
                view,
                ctx["kind"] if reapply else "map_conflict",
                workflow,
                _json({**state, "rev": current.review_revision}),
            )
            layout = self.views.map_conflict(token, before, current.review_revision, diff)
            if reapply:
                if reapply_settings and settings_edit is not None:
                    form = self.views.map_assertions(
                        token,
                        trigger=settings_edit.trigger,
                        unit_of_work=settings_edit.unit_of_work,
                        fallback=settings_edit.fallback_action,
                        permitted=current.actions,
                    )
                elif reapply_order:
                    form = self.views.map_precedence(
                        token,
                        current.question
                        or "Review the current order before reapplying your changes.",
                        [(f"{r.action} · {r.branch_ref}", r.branch_ref) for r in eligible],
                    )
                else:
                    form = self.views.map_branch_edit(
                        token, state["branch"], action, raw, current.actions
                    )
                layout = self.views.map_reapply(layout, form, values)
            self._map_show(
                actor, workflow, current.review_revision, view, layout, requires_editor=reapply
            )
            return
        self._map_view(cfg, actor, view, workflow)

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
        if callback in {"ot_case_adjudicate", "ot_case_freeze"}:
            state = json.loads(ctx["revision"])
            session, entry = self._case_session(actor)
            _check(entry.run_ref == ctx["job"])
            pack = session.review()
            self._check_case_state(entry, pack, state)
            if callback == "ot_case_adjudicate":
                _check(
                    ctx["kind"] == "case_edit"
                    and entry.can_adjudicate
                    and set(values) == {"target", "reason"}
                )
                choice = self._field(values, "target", select=True)
                options = pack["authority"]["allowed_actions"]
                _check(choice == "unresolved" or choice in {str(i) for i in range(len(options))})
                reason = self._field(values, "reason").strip()
                session.adjudicate(
                    case_id=state["case_id"],
                    target=None if choice == "unresolved" else options[int(choice)],
                    reason=reason,
                    expected_revision=state["revision"],
                    operation_ref=self.journal.opaque("case-save", [ctx["id"], actor]),
                )
            else:
                _check(ctx["kind"] == "case_splits" and entry.can_freeze)
                families = state["page"]
                _check(set(values) == {f"family_{i}" for i in range(len(families))})
                splits = {
                    family: self._field(values, f"family_{i}", select=True)
                    for i, family in enumerate(families)
                }
                _check(set(splits.values()) <= {"development", "reserved", "excluded"})
                self._case_splits(
                    cfg, actor, view, entry, pack, state, state.get("splits", {}) | splits
                )
                return
            self._case_view(cfg, actor, view, state["index"])
        elif callback in {"ot_map_assertions_save", "ot_map_branch_save", "ot_map_precedence_save"}:
            self._map_submit(cfg, actor, view, ctx, callback, values)
        elif callback == "ot_nomination":
            _check(ctx["kind"] == "nomination" and set(values) == {"nominee"})
            self._nomination(
                actor, ctx["job"], nominee=self._field(values, "nominee"), expected=ctx["revision"]
            )
            self._workflow_settings_view(cfg, actor, ctx["job"], view)
        elif callback == "ot_verify_outcome":
            _check(
                ctx["kind"] == "verify_outcome"
                and set(values) == {"target", "finality", "basis", "effective", "action"}
            )
            form = self._verifier_form(actor)
            target = self._field(values, "target", select=True)
            _check(form.workflow == ctx["job"] and target in json.loads(ctx["revision"])["tokens"])
            action = values["action"]["action"].get("value")
            _check(action is None or isinstance(action, str))
            stamp = self._field(values, "effective")
            _check(stamp.isascii() and stamp.isdecimal() and len(stamp) <= 19)
            receipt = form.submit(
                target,
                self._field(values, "finality", select=True),
                action or "",
                self._field(values, "basis"),
                int(stamp),
            )
            token = self.journal.context(
                actor, cfg.channel, view, "verified_outcome", form.workflow
            )
            self._show(
                view,
                self.views.verification_saved(token, receipt),
                authorize=lambda: _check(self._verifier_form(actor).permits_receipt(receipt)),
            )
        elif callback == "ot_live_binding":
            saved = self._live_save(actor, ctx, values)
            self._live_view(cfg, actor, view, ctx["job"], saved=saved)
        elif callback == "ot_prompt_enter":
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
        status_topic = resolve_status(prompt)
        if status_topic:
            self._regression_picker(cfg, actor, view, topic=status_topic.value)
            return
        plan = plan_query(prompt)
        if isinstance(plan, ArmChart):
            self._chart_picker(cfg, actor, view, plan)
            return
        if isinstance(plan, WorkflowDiscovery):
            self._monthly_question(cfg, actor, view, plan)
            return
        if plan == QueryHold.VERSION_REQUIRED:
            ctx = self.journal.context(actor, cfg.channel, view, "query_hold")
            self._show(view, self.views.query_hold_modal(ctx, plan))
            return
        if isinstance(plan, ReadoutQuestion) and plan in WORKPLACE_QUESTIONS:
            self._regression_picker(cfg, actor, view, topic=plan.value)
            return
        if REGRESSION_QUESTION.fullmatch(" ".join(prompt.casefold().split())):
            self._regression_picker(cfg, actor, view)
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

    def _can_add_workflow(self, cfg: Settings, actor: str) -> bool:
        entry = cfg.permission(actor, "inspect")
        return (
            (
                (actor == cfg.reviewer and "review" in entry["roles"])
                or (cfg.starter_setup is not None and cfg.starter_setup.slack_owner == actor)
            )
            and not cfg.workflow_label
            and not entry["datasets"]
        )

    def _workflow_index(self, cfg: Settings, actor: str) -> WorkflowIndex | None:
        from opentour_cell.pilot.workflow_benchmarks import WorkflowBenchmarks
        from opentour_cell.pilot.workflow_index import CurrentIndexAuthority, WorkflowIndex

        provider = self.workflow_index_scope
        if provider is None:
            return None

        def current(principal: IntakePrincipal) -> IndexScope | None:
            latest = self.settings()
            _check(latest == cfg and principal == IntakePrincipal(cfg.organization, actor, "slack"))
            latest.permission(actor, "inspect")
            proposals = self._workflow_proposals(latest, actor)
            scope = provider(latest, principal, proposals)
            if scope is not None:
                _check(scope.epoch == self.store.engagement_id and scope.proposals == proposals)
            return scope

        return WorkflowIndex(WorkflowBenchmarks(cfg.directory), CurrentIndexAuthority(current))

    def _workflow_proposals(self, cfg: Settings, actor: str) -> tuple[ProposalAccess, ...]:
        from opentour_cell.pilot.operating_point import digest
        from opentour_cell.pilot.receipt_recovery import _open_db
        from opentour_cell.pilot.release_gate import inspect_recorded_release
        from opentour_cell.pilot.source_intake import check_current_intake
        from opentour_cell.pilot.workflow_benchmarks import BenchmarkScope
        from opentour_cell.pilot.workflow_index import ProposalAccess

        entry = cfg.permission(actor, "inspect")
        if not entry["aggregate"] or not cfg.explicit_recipient:
            return ()
        epoch = self.store.engagement_id
        output = []
        with (
            self.journal.db() as db,
            _open_db(cfg.directory / "learning-mandates.sqlite3") as ledger,
        ):
            self.journal.active(db)
            replacements = (
                ledger is not None
                and ledger.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='reviewer_replacements'"
                ).fetchone()
            )
            for _, source in self._result_sources(cfg, actor):
                if not source.startswith("managed:"):
                    continue
                try:
                    target = self._managed(cfg, actor, source)
                    audit = target.audit
                    if audit.get("release_provider") not in {"slack", "zapier_slack"}:
                        continue
                    run = audit["job_id"]
                    if (
                        ledger is not None
                        and replacements
                        and ledger.execute(
                            "SELECT 1 FROM reviewer_replacements WHERE scope=? AND run=?",
                            (digest([cfg.organization, epoch]), run),
                        ).fetchone()
                    ):
                        # Only the replacement verifier can grant access to the successor.
                        continue
                    seen = db.execute(
                        "SELECT p.id, p.presented, p.expires FROM presentations p "
                        "JOIN exposures e ON p.id=e.id WHERE e.status='presented' "
                        "AND p.actor=? AND p.revision=? AND p.policy=? AND p.scope=? "
                        "ORDER BY p.presented DESC,p.rowid DESC LIMIT 1",
                        (actor, target.revision, self._policy(cfg, actor), target.evidence_digest),
                    ).fetchone()
                    assigned = (
                        cfg.reviewer == actor == audit.get("release_reviewer")
                        and "review" in entry["roles"]
                    )
                    if seen is not None:
                        self.journal._seal(db, "presentation", seen["id"])
                    elif not assigned or audit["release_provider"] != "slack":
                        continue
                    intake = check_current_intake(
                        cfg.directory, cfg.organization, epoch, target.dataset_revision
                    )
                    if audit["release_provider"] == "zapier_slack":
                        assert seen is not None
                        proposal = self._provider_workflow_proposal(
                            cfg, actor, target, intake, seen
                        )
                        if proposal is not None:
                            output.append(proposal)
                        continue
                    release = inspect_recorded_release(
                        cfg.directory,
                        cfg.organization,
                        run,
                        audit["run_revision"],
                        audit["model_revision"],
                    )
                    action: Action = "open_status"
                    state: Literal["pending", "approved", "changes_requested"] = "pending"
                    if assigned:
                        window = self._assigned_review_window(cfg, target)
                        if window is None:
                            continue
                        stamped, deadline, grant_revision = window
                    else:
                        assert seen is not None
                        stamped, deadline, grant_revision = (
                            seen["presented"],
                            seen["expires"],
                            seen["id"],
                        )
                    if release:
                        state = release["decision"]
                        decided_at = release.get("recorded_at")
                        if type(decided_at) is not int or decided_at <= 0:
                            continue
                        stamped = decided_at
                        if state == "approved" and self._handoff_receipt(cfg, actor, target):
                            action = "open_receipt"
                    elif assigned:
                        action = "open_review"
                    visibility = min(entry["expires_at"], intake["expires_at"])
                    if not release:
                        visibility = min(visibility, deadline)
                    output.append(
                        ProposalAccess(
                            BenchmarkScope(cfg.organization, epoch, intake["workflow"]),
                            actor,
                            digest(
                                [
                                    grant_revision,
                                    intake["authority_revision"],
                                    self._policy(cfg, actor),
                                ]
                            ),
                            run,
                            target.revision,
                            state,
                            stamped,
                            deadline if not release else None,
                            visibility,
                            action,
                            visibility,
                        )
                    )
                except (ValueError, OSError, KeyError, sqlite3.Error):
                    continue
        return tuple(output)

    def _assigned_review_window(self, cfg: Settings, target: Target) -> tuple[int, int, str] | None:
        from opentour_cell.pilot.managed_run import QUEUE_FILE, _projection
        from opentour_cell.pilot.receipt_recovery import _open_db
        from opentour_cell.pilot.repair_workflow import managed_review_evidence

        audit = target.audit
        checked = managed_review_evidence(
            cfg.directory,
            cfg.organization,
            audit["job_id"],
            cfg.recipient,
            cfg.team,
            cfg.channel,
            include_review_window=True,
        )
        grant = checked["review_window"]
        with _open_db(cfg.directory / QUEUE_FILE) as db:
            if db is None:
                return None
            owner = db.execute("SELECT org, engagement FROM owner WHERE singleton=1").fetchone()
            if owner is None or tuple(owner) != (cfg.organization, self.store.engagement_id):
                return None
            row = db.execute(
                "SELECT * FROM runs WHERE id=? AND revision=?",
                (audit["job_id"], grant["plan_revision"]),
            ).fetchone()
            if row is None:
                return None
            run = _projection(row)
        result = run["result"]
        stamp = run["timing"]["first_result_unix_ms"]
        if (
            run["state"] != "ready"
            or not result
            or result["result_revision"] != audit["run_revision"]
            or not stamp
        ):
            return None
        return stamp // 1000, grant["expires_at"], grant["grant_revision"]

    @staticmethod
    def _provider_queue_status(cfg: Settings, actor: str, target: Target) -> Json | None:
        from opentour_cell.pilot.zapier_approval import ApprovalBridge

        bridge = ApprovalBridge(
            cfg.directory, cfg.organization, cfg.directory / "zapier-approval.json"
        )
        audit = target.audit
        return bridge.existing_status(
            run_id=audit["job_id"],
            run_revision=audit["run_revision"],
            artifact_sha=audit["model_revision"],
            team=cfg.team,
            actor=actor,
            handoff_destination=(cfg.channel, cfg.recipient),
        )

    def _provider_workflow_proposal(
        self,
        cfg: Settings,
        actor: str,
        target: Target,
        intake: Json,
        seen: sqlite3.Row,
    ) -> ProposalAccess | None:
        from opentour_cell.pilot.operating_point import digest
        from opentour_cell.pilot.release_gate import inspect_recorded_release
        from opentour_cell.pilot.workflow_benchmarks import BenchmarkScope
        from opentour_cell.pilot.workflow_index import ProposalAccess
        from opentour_cell.pilot.zapier_approval import ApprovalBridge

        audit = target.audit
        if cfg.reviewer != actor or audit.get("release_reviewer") != actor:
            return None
        entry = cfg.permission(actor, "review", target.dataset)
        bridge = ApprovalBridge(
            cfg.directory, cfg.organization, cfg.directory / "zapier-approval.json"
        )
        status = self._provider_queue_status(cfg, actor, target)
        if not status:
            return None
        policy, key = bridge._policy()
        request = bridge._request(status["request_id"], policy, key, fresh=False)
        deadline = request["expires_at"]
        visible_until = min(entry["expires_at"], intake["expires_at"])
        state: Literal["pending", "expired", "approved", "changes_requested"]
        if status["state"] in {"approved", "changes_requested"}:
            release = inspect_recorded_release(
                cfg.directory,
                cfg.organization,
                audit["job_id"],
                audit["run_revision"],
                audit["model_revision"],
            )
            if (
                not release
                or release.get("decision") != status["state"]
                or not release.get("recorded_at")
            ):
                return None
            state, recorded = status["state"], release["recorded_at"]
        elif status["state"] == "expired" and status.get("recorded_at") == deadline:
            state, recorded = "expired", deadline
        elif status["state"] in {"staged", "accepted"} and time.time() < deadline:
            state, recorded = "pending", request["issued_at"]
            visible_until = min(visible_until, deadline)
        else:
            # An unresolved provider outcome cannot become an expired or approved proposal.
            return None
        return ProposalAccess(
            BenchmarkScope(cfg.organization, self.store.engagement_id, intake["workflow"]),
            actor,
            digest(
                [
                    seen["id"],
                    request["request_id"],
                    intake["authority_revision"],
                    self._policy(cfg, actor),
                ]
            ),
            audit["job_id"],
            target.revision,
            state,
            recorded,
            deadline,
            visible_until,
            "open_status",
            visible_until,
        )

    def _workflow_page(
        self, cfg: Settings, actor: str, cursor: str | None
    ) -> tuple[IndexPage | None, bool]:
        try:
            index = self._workflow_index(cfg, actor)
            if index is None:
                return None, False
            principal = IntakePrincipal(cfg.organization, actor, "slack")
            return index.page(principal, size=WORKFLOW_PAGE_SIZE, cursor=cursor), False
        except (ImportError, OSError, ValueError, sqlite3.Error):
            return None, True

    def _resolve_workflow_proposal(self, actor: str, handle: str) -> tuple[IndexRow, Target, str]:
        from opentour_cell.pilot.source_intake import check_current_intake

        _check(isinstance(handle, str) and re.fullmatch(r"[0-9a-f]{64}", handle))
        principal = IntakePrincipal(self.binding[0], actor, "slack")

        current = self.settings()
        current.permission(actor, "inspect")
        index = self._workflow_index(current, actor)
        _check(index is not None)
        assert index is not None
        row = index.select(principal, handle)
        _check(row.proposal_ref is not None and row.proposal_revision is not None)
        assert row.proposal_ref is not None
        source = "managed:" + row.proposal_ref
        target = self._managed(current, actor, source)
        intake = check_current_intake(
            current.directory,
            current.organization,
            self.store.engagement_id,
            target.dataset_revision,
        )
        _check(target.revision == row.proposal_revision and intake["workflow"] == row.workflow_ref)
        return row, target, source

    def _open_workflow_proposal(
        self, cfg: Settings, actor: str, view: str, handle: str, *, receipt_request: str = ""
    ) -> None:
        selected, target, source = self._resolve_workflow_proposal(actor, handle)

        def authorize() -> None:
            _check(self._resolve_workflow_proposal(actor, handle) == (selected, target, source))

        if selected.action == "open_review":
            _check(
                cfg.reviewer == actor == target.audit.get("release_reviewer")
                and cfg.permission(actor, "review", target.dataset)["aggregate"]
            )
            self._evidence(
                cfg,
                actor,
                view,
                {"job": source, "revision": target.revision},
                "",
                authorize=authorize,
                queue_handle=handle,
            )
        elif (
            selected.action == "open_status"
            and target.audit.get("release_provider") == "zapier_slack"
        ):
            status = self._provider_queue_status(cfg, actor, target)
            _check(
                status
                and status["state"]
                in {"staged", "accepted", "approved", "changes_requested", "expired"}
            )
            assert status
            _check(
                not receipt_request
                or receipt_request == status["request_id"]
                and "receipt_sha256" in status
            )
            token = self.journal.context(
                actor, cfg.channel, view, "queued_provider_status", status["request_id"], handle
            )

            def authorize_status() -> None:
                authorize()
                _check(self._provider_queue_status(self.settings(), actor, target) == status)

            self._show(
                view,
                self.views.plugin_waiting_modal(
                    token, status["request_id"], status, show_receipt=bool(receipt_request)
                ),
                dataset=target.dataset,
                dataset_revision=target.dataset_revision,
                audit=target.audit,
                authorize=authorize_status,
            )
        elif selected.action == "open_status":
            self._home(cfg, actor, view, source, authorize=authorize)
        elif selected.action == "open_receipt":
            receipt = self._handoff_receipt(cfg, actor, target)
            _check(receipt)
            assert receipt is not None
            pin = self._receipt_pin(actor, target.revision, receipt)
            context = self.journal.context(
                actor, cfg.channel, view, "handoff", source, target.revision
            )

            def authorize_receipt() -> None:
                authorize()
                _check(self._handoff_receipt(self.settings(), actor, target) == receipt)

            self._show(
                view,
                self.views.handoff_modal(context, pin, receipt, target.audit),
                dataset=target.dataset,
                dataset_revision=target.dataset_revision,
                audit=target.audit,
                authorize=authorize_receipt,
            )
        else:
            raise Refused()

    def _publish_app_home(self, cfg: Settings, event: Json, *, cursor: str | None = None) -> None:
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
            can_add_workflow = self._can_add_workflow(cfg, actor)
        except Exception:
            # Slack or custody checks may be unavailable; replace any previously personalized Home.
            self.web.views_publish(user_id=actor, view=self.views.app_home_view())
            return
        token = self.journal.opaque("home", [actor, cfg.team, cfg.app, cfg.channel])
        queue, queue_denied = self._workflow_page(cfg, actor, cursor)
        if cursor and queue is None:
            # A stale continuation can only reopen the first currently authorized page.
            cursor = None
            queue, queue_denied = self._workflow_page(cfg, actor, cursor)
        pending = self._pending_release(cfg, actor) if queue is None and not queue_denied else None
        workflow_settings = False
        if cfg.live_workflow_ref or (cfg.starter_setup and cfg.starter_setup.slack_owner == actor):
            try:
                workflow = self._live_workflow(cfg, actor)
                with self._live_scope(actor, workflow):
                    workflow_settings = True
            except Exception:
                # The existing Home remains usable without current live authority.
                pass
        live_summary = self._live_summary(actor) if workflow_settings else None
        readiness = self._readiness(actor) if workflow_settings else None
        map_entry = self._map_entry(actor)
        case_entry = self._case_entry(actor)
        score_entry, _ = self._starter_scores(actor)
        verifier_entry = self._verifier_entry(actor) if cfg.live_workflow_ref else ""
        layout = self.views.app_home_view(
            token,
            cfg.workspace_title,
            cfg.workflow_label,
            can_review=entry["aggregate"],
            can_tune=self._home_repair_access(cfg, actor),
            named_reviewer=cfg.reviewer == actor and "review" in entry["roles"],
            pending=pending.revision if pending else "",
            can_add_workflow=can_add_workflow,
            workflow_settings=workflow_settings,
            readiness=readiness,
            live_summary=live_summary,
            verify_outcome=verifier_entry,
            decision_map_entry=map_entry,
            case_entry=case_entry,
            starter_score_entry=score_entry,
            queue=queue,
            queue_denied=queue_denied,
            queue_continuation=cursor is not None,
        )
        navigation = (
            can_add_workflow
            and not cfg.explicit_recipient
            and _static_setup_navigation(layout, token)
            and self._unbound_recipient()
        )
        try:
            with self.store.engagement_guard(engagement_id=self.store.engagement_id):
                exposure = self.journal.admit_view(
                    "app_home",
                    authorize=(
                        lambda: _check(
                            (
                                queue is None
                                or self._workflow_page(self.settings(), actor, cursor)
                                == (queue, False)
                            )
                            and (not score_entry or self._starter_scores(actor)[0] == score_entry)
                            and (not case_entry or self._case_entry(actor) == case_entry)
                            and (not map_entry or self._map_entry(actor) == map_entry)
                            and (
                                not verifier_entry or self._verifier_entry(actor) == verifier_entry
                            )
                            and (readiness is None or self._readiness(actor) == readiness)
                            and (live_summary is None or self._live_summary(actor) == live_summary)
                        )
                    )
                    if queue is not None
                    or readiness is not None
                    or live_summary is not None
                    or verifier_entry
                    or map_entry
                    or case_entry
                    or score_entry
                    else None,
                )
        except Exception:
            self.web.views_publish(user_id=actor, view=self.views.app_home_view())
            return
        try:
            result = self.web.views_publish(user_id=actor, view=layout)
            _check(result is not None and result.get("ok") is True)
        except Exception:
            self.journal.finish_view(exposure, "uncertain")
            raise
        self.journal.finish_view(
            exposure,
            "navigation_presented" if navigation and self._unbound_recipient() else "presented",
        )

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
        if action.get("action_id") == "ot_app_queue_open":
            opened = self.web.views_open(
                trigger_id=body["trigger_id"], view=self.views.notice_modal("checking")
            )
            view_id = opened["view"]["id"]
            try:
                self.private_channel(cfg, actor)
                self._open_workflow_proposal(cfg, actor, view_id, action.get("value"))
            except (ImportError, OSError, ValueError, KeyError, sqlite3.Error):
                self._safe_notice(view_id)
            return
        if action.get("action_id") in {"ot_app_queue_next", "ot_app_queue_first"}:
            self.private_channel(cfg, actor)
            cursor = None
            if action["action_id"] == "ot_app_queue_next":
                cursor = action.get("value")
                _check(isinstance(cursor, str) and re.fullmatch(r"[0-9a-f]{64}", cursor))
            else:
                _check(action.get("value") == "first")
            self._publish_app_home(
                self.settings(),
                {"type": "app_home_opened", "tab": "home", "user": actor},
                cursor=cursor,
            )
            return
        if action.get("action_id") == "ot_app_starter_score":
            self.private_channel(cfg, actor)
            version, items = self._starter_scores(actor)
            _check(version and action.get("value") == version)
            opened = self.web.views_open(
                trigger_id=body["trigger_id"], view=self.views.notice_modal("checking")
            )
            view_id = opened["view"]["id"]
            if len(items) == 1:
                self._open_starter_score(cfg, actor, view_id, items[0][0])
            else:
                token = self.journal.context(
                    actor, cfg.channel, view_id, "starter_scores", "", version
                )
                self._show(
                    view_id,
                    self.views.starter_score_choices(token, items),
                    authorize=lambda: _check(self._starter_scores(actor)[0] == version),
                )
            return
        if action.get("action_id") == "ot_app_cases":
            self.private_channel(cfg, actor)
            _, case_entry = self._case_session(actor)
            _check(case_entry.revision == action.get("value"))
            view = self.web.views_open(
                trigger_id=body["trigger_id"], view=self.views.notice_modal("checking")
            )["view"]["id"]
            token = self.journal.context(
                actor, cfg.channel, view, "case_entry", case_entry.run_ref, case_entry.revision
            )
            layout = self.views.starter_case_entry(
                token, case_entry.workflow_ref, case_entry.run_ref, case_entry.can_propose
            )
            self._show(
                view,
                layout,
                authorize=lambda: _check(self._case_entry(actor) == case_entry.revision),
            )
            return
        if action.get("action_id") == "ot_app_map":
            self.private_channel(cfg, actor)
            map_entry = self._map_entry(actor)
            _check(map_entry and map_entry == action.get("value"))
            opened = self.web.views_open(
                trigger_id=body["trigger_id"], view=self.views.notice_modal("checking")
            )
            view_id = opened["view"]["id"]
            choices = self._map_choices(actor)
            _check(choices and self._map_entry(actor) == map_entry)
            if len(choices) == 1:
                self._map_view(cfg, actor, view_id, next(iter(choices)))
            else:
                token = self.journal.context(
                    actor, cfg.channel, view_id, "map_choices", "", map_entry
                )
                layout = self.views.map_choices(
                    token, [(wf, item[1]) for wf, item in choices.items()]
                )
                self._show(
                    view_id, layout, authorize=lambda: _check(self._map_entry(actor) == map_entry)
                )
            return
        if action.get("action_id") == "ot_app_verify_outcome":
            self.private_channel(cfg, actor)
            verifier_token = self._verifier_entry(actor)
            _check(verifier_token and action.get("value") == verifier_token)
            opened = self.web.views_open(
                trigger_id=body["trigger_id"], view=self.views.notice_modal("checking")
            )
            self._verify_view(cfg, actor, opened["view"]["id"])
            return
        if action.get("action_id") in {"ot_app_settings", "ot_app_bind_verifier"}:
            bind = action["action_id"] == "ot_app_bind_verifier"
            _check(bind or action.get("value") == "ot_app_settings")
            opened = self.web.views_open(
                trigger_id=body["trigger_id"], view=self.views.notice_modal("checking")
            )
            view_id = opened["view"]["id"]
            try:
                self.private_channel(cfg, actor)
                workflow = self._live_workflow(self.settings(), actor)
                if bind:
                    receipt = self._readiness(actor)
                    _check(
                        receipt
                        and receipt["revision"] == action.get("value")
                        and receipt["next_action"] == "bind_verifier"
                    )
                    self._live_view(cfg, actor, view_id, workflow, verifier_only=True)
                    return

                self._workflow_settings_view(cfg, actor, workflow, view_id)
            except Exception:
                self._safe_notice(view_id)
            return
        if action.get("action_id") == "ot_app_add_workflow":
            _check(action.get("value") == "ot_app_add_workflow")
            opened = self.web.views_open(
                trigger_id=body["trigger_id"], view=self.views.notice_modal("checking")
            )
            view_id = opened["view"]["id"]
            try:
                self.private_channel(cfg, actor)
                current = self.settings()
                _check(self._can_add_workflow(current, actor))
                token = self.journal.context(actor, cfg.channel, view_id, "workflow_chooser")
                self._show(
                    view_id,
                    self.views.workflow_chooser(token),
                    setup_context=None
                    if cfg.explicit_recipient or current.explicit_recipient
                    else token,
                )
            except Exception:
                self._safe_notice(view_id)
            return
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
            actions = body.get("actions", [])
            setup_click = (
                body.get("type") == "block_actions"
                and len(actions) == 1
                and actions[0].get("action_id")
                in {"ot_app_add_workflow", "ot_workflow_starter", "ot_workflow_historical"}
                and body.get("container", {}).get("type", "view") == "view"
            )
            if not setup_click:
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
            initial_context = self.journal.read_context(token, actor, cfg.channel, view)
            if setup_click:
                box = body.get("container", {})
                _check(
                    initial_context["kind"] == "workflow_chooser"
                    and obj.get("type") == "modal"
                    and obj.get("callback_id") == "ot_workflow_chooser"
                    and box.get("type", "view") == "view"
                    and box.get("view_id", view) == view
                )
                authorized_view = view
                authorized_hash = obj.get("hash", "")
                cfg.permission(actor, "inspect")
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
                    if action["action_id"] in {"ot_starter_save", "ot_starter_benchmark"}:
                        _check(
                            (action["action_id"], obj.get("callback_id"))
                            in {
                                ("ot_starter_save", "ot_starter_preview"),
                                ("ot_starter_benchmark", "ot_starter_saved"),
                            }
                            and not obj.get("state", {}).get("values")
                            and not (
                                set(action)
                                - {
                                    "action_id",
                                    "value",
                                    "block_id",
                                    "text",
                                    "type",
                                    "action_ts",
                                    "style",
                                }
                            )
                        )
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
        from opentour_cell.pilot.recipient_lifecycle_authority import open_lifecycle_authority

        bot = os.environ.get("SLACK_BOT_TOKEN", "")
        _check(bot.startswith("xoxb-"))
        # A post accepted before a lost reply must never be sent again by an SDK retry.
        web = sdk.WebClient(token=bot, retry_handlers=[])
        verified = web.auth_test()
        _check(verified["team_id"] == cfg.team and bool(verified["bot_id"]))
        from opentour_cell.pilot.workflow_index_authority import open_workflow_index_scope

        app = Workbench(
            config,
            web,
            LocalTrainingStore(cfg.directory / "training.sqlite3", cfg.organization),
            slack_views,
            workflow_index_scope=open_workflow_index_scope
            if cfg.workflow_index_publication_file
            else None,
            lifecycle_authority=(
                None
                if args.flush_approvals or args.post_launcher
                else open_lifecycle_authority(cfg.directory)
            ),
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
