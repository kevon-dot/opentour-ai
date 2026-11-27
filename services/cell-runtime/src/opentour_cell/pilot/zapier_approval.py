"""Bridge an explicitly configured Zapier Slack Request Approval to the owner-local release gate."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from opentour_cell.pilot import release_gate
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.repair_workflow import _record
from opentour_cell.pilot.slack_adapter import _hook_path, hook_fingerprint
from opentour_cell.pilot.training import LocalTrainingStore, _check_directory, _check_file
from opentour_cell.pilot.training_data import identifier

Json = dict[str, Any]
POLICY = "opentour.pilot.zapier-slack-policy.v1"
RECIPE_POLICY = "opentour.pilot.zapier-slack-policy.v2"
REFERENCE = "opentour.pilot.zapier-slack-reference.v1"
EVENT = "opentour.pilot.zapier-slack-decision.v1"
REQUEST = "opentour.pilot.zapier-slack-request.v1"
HEX = re.compile("[0-9a-f]{64}")
OPAQUE = re.compile("[a-zA-Z0-9_.:-]{1,96}")
REQ = re.compile("za-[a-f0-9]{32}")
SCHEDULE_RECEIPT_PREFIX = "last_scheduled_reconcile:"
FIELDS = {
    "schema_version",
    "request_id",
    "nonce",
    "zap_id",
    "zap_version",
    "event_id",
    "decided_at",
    "status",
    "actor_id",
}
BIND = (
    "run_id",
    "run_revision",
    "artifact_sha",
    "reviewer",
    "team",
    "channel",
    "audience",
    "inspection_ref",
)
REVIEW_AUTHORITY = ("generation", "reviewer_grant_revision")
MARKER_STATES = {
    "version_superseded": "superseded",
    "reviewer_replaced": "superseded",
    "request_expired": "expired",
    "current_authority_unavailable": "held",
}


class ApprovalError(ValueError):
    """Fixed public diagnostic from an owner-local operation."""


class ReconciliationJournal:
    """Private recovery state, serialized with the existing approval bridge's effects."""

    def __init__(self, directory: Path, organization: str):
        self.path = directory / "zapier-reconciliation.sqlite3"
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        _check_file(self.path)
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS installation (key TEXT PRIMARY KEY,value TEXT)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS policies "
                "(revision TEXT PRIMARY KEY,document TEXT NOT NULL,signer TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS policy_signers "
                "(revision TEXT PRIMARY KEY,key_file TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS reviewer_projections "
                "(id TEXT PRIMARY KEY,document TEXT NOT NULL,document_sha256 TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS reviewer_projected_requests "
                "(request_id TEXT,projection TEXT,PRIMARY KEY(request_id,projection))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS verified_requests "
                "(id TEXT PRIMARY KEY,wrapper_sha256 TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS request_recovery "
                "(id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, request TEXT NOT NULL, "
                "zap_revision TEXT NOT NULL, epoch TEXT NOT NULL, state TEXT NOT NULL, "
                "reason TEXT, "
                "provider_status TEXT NOT NULL, observed_at INTEGER NOT NULL, "
                "retries INTEGER NOT NULL DEFAULT 0)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, request_id TEXT NOT NULL, "
                "kind TEXT NOT NULL, interval_start INTEGER NOT NULL, state TEXT NOT NULL, "
                "UNIQUE(request_id,kind,interval_start))"
            )
            db.execute(
                "INSERT OR IGNORE INTO installation VALUES ('organization',?)", (organization,)
            )
            require(
                db.execute("SELECT value FROM installation WHERE key='organization'").fetchone()[0]
                == organization,
                "reconciliation_scope_mismatch",
            )
            db.execute(
                "INSERT OR IGNORE INTO installation VALUES ('epoch',?)", (secrets.token_hex(16),)
            )
            db.execute("INSERT OR IGNORE INTO installation VALUES ('paused','false')")
            db.execute("INSERT OR IGNORE INTO installation VALUES ('last_poll','0')")

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        _check_file(self.path)
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    def installation(self) -> Json:
        with self.db() as db:
            return dict(db.execute("SELECT key,value FROM installation"))

    def pin_installation(self, policy: Json, key: bytes) -> None:
        values = {
            "policy_revision": digest(policy),
            "signer_fingerprint": hashlib.sha256(key).hexdigest(),
            "reviewer": policy["reviewer"],
            "zap_version": policy["zap_version"],
        }
        with self.db() as db:
            for name, value in values.items():
                db.execute("INSERT OR IGNORE INTO installation VALUES (?,?)", (name, value))
            current = db.execute(
                "SELECT value FROM installation WHERE key='policy_revision'"
            ).fetchone()[0]
            if current == digest(policy):
                db.execute(
                    "INSERT OR IGNORE INTO policies VALUES (?,?,?)",
                    (current, canonical(policy), hashlib.sha256(key).hexdigest()),
                )

    def record(self, request: Json, policy: Json) -> None:
        with self.db() as db:
            prior = db.execute(
                "SELECT request FROM requests WHERE id=?", (request["request_id"],)
            ).fetchone()
            require(
                prior is None or prior[0] == canonical(request), "reconciliation_identity_conflict"
            )
            epoch = db.execute("SELECT value FROM installation WHERE key='epoch'").fetchone()[0]
            db.execute(
                "INSERT OR IGNORE INTO requests(id,request,zap_revision,epoch,state,"
                "provider_status,observed_at) VALUES (?,?,?,?,'pending','unknown',?)",
                (
                    request["request_id"],
                    canonical(request),
                    policy["zap_version"],
                    epoch,
                    int(time.time()),
                ),
            )

    def request_state(self, request_id: str) -> str | None:
        with self.db() as db:
            row = db.execute("SELECT state FROM requests WHERE id=?", (request_id,)).fetchone()
            return row[0] if row else None

    def transition(
        self,
        request_id: str,
        state: str,
        reason: str | None,
        provider: str,
        *,
        reminder_interval: int | None = None,
        count_retry: bool = False,
        schedule_action: str | None = None,
    ) -> None:
        now = int(time.time())
        with self.db() as db:
            row = db.execute(
                "SELECT state,provider_status,reason FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            require(row is not None, "request_not_in_reconciliation_inventory")
            if schedule_action is not None:
                db.execute(
                    "INSERT OR REPLACE INTO installation VALUES (?,?)",
                    (SCHEDULE_RECEIPT_PREFIX + request_id, schedule_action),
                )
            if row[1] in {"Approved", "Declined"} or provider not in {
                "Pending",
                "Approved",
                "Declined",
            }:
                provider = row[1]
            # A conflict or later outage must not rewrite a completed human decision.
            if row[0] in {"approved", "changes_requested"}:
                state = row[0]
            if (state, provider, reason) != tuple(row) or count_retry:
                db.execute(
                    "UPDATE requests SET state=?,reason=?,provider_status=?,observed_at=?,"
                    "retries=retries+? WHERE id=?",
                    (state, reason, provider, now, int(count_retry), request_id),
                )
            kind = (
                "owner_action"
                if state in {"held", "superseded", "expired"}
                or reason == "conflicting_provider_event"
                else "reminder"
            )
            if kind == "owner_action" or (state == "pending" and reminder_interval is not None):
                last = db.execute(
                    "SELECT max(interval_start) FROM outbox WHERE request_id=? AND kind='reminder'",
                    (request_id,),
                ).fetchone()[0]
                if kind == "reminder" and last is not None and now < last + reminder_interval:
                    return
                bucket = 0 if kind == "owner_action" else now
                reference = "reconcile-" + digest([request_id, kind, bucket])
                db.execute(
                    "INSERT OR IGNORE INTO outbox VALUES (?,?,?,?,'pending')",
                    (reference, request_id, kind, bucket),
                )

    def page(self, key: bytes, *, limit: int, cursor: str | None) -> Json:
        require(type(limit) is int and 1 <= limit <= 50, "invalid_reconciliation_page")
        with self.db() as db:
            meta = dict(db.execute("SELECT key,value FROM installation"))
            after, ceiling = (
                0,
                db.execute("SELECT coalesce(max(rowid),0) FROM requests").fetchone()[0],
            )
            if cursor is not None:
                require(
                    isinstance(cursor, str) and len(cursor) <= 2048, "invalid_reconciliation_cursor"
                )
                try:
                    token = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                    value, signature = token["value"], token["signature"]
                except (ValueError, KeyError, TypeError, UnicodeError):
                    raise ApprovalError("invalid_reconciliation_cursor") from None
                require(
                    isinstance(value, dict)
                    and set(value) == {"organization", "epoch", "after", "ceiling"}
                    and isinstance(signature, str)
                    and hmac.compare_digest(
                        signature, _sig(key, "approval-reconciliation-page.v1", value)
                    )
                    and value["organization"] == digest(meta["organization"])
                    and value["epoch"] == meta["epoch"]
                    and type(value["after"]) is int
                    and type(value["ceiling"]) is int
                    and 0 <= value["after"] <= value["ceiling"],
                    "invalid_reconciliation_cursor",
                )
                after, ceiling = value["after"], value["ceiling"]
            rows = db.execute(
                "SELECT rowid,id,state,reason,provider_status,request,observed_at,retries "
                "FROM requests WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT ?",
                (after, ceiling, limit + 1),
            ).fetchall()
            next_cursor = None
            if len(rows) > limit:
                value = {
                    "organization": digest(meta["organization"]),
                    "epoch": meta["epoch"],
                    "after": rows[limit - 1]["rowid"],
                    "ceiling": ceiling,
                }
                next_cursor = base64.urlsafe_b64encode(
                    canonical(
                        {
                            "value": value,
                            "signature": _sig(key, "approval-reconciliation-page.v1", value),
                        }
                    ).encode()
                ).decode()
            now = int(time.time())
            items = [
                {
                    "request_id": row["id"],
                    "state": row["state"],
                    "reason": row["reason"],
                    "provider_status": row["provider_status"],
                    "pending_age_seconds": max(0, now - json.loads(row["request"])["issued_at"]),
                    "retries": row["retries"],
                }
                for row in rows[:limit]
            ]
            counts = dict(db.execute("SELECT state,count(*) FROM requests GROUP BY state"))
            return {
                "tenant_ref": digest(meta["organization"]),
                "installation_epoch": meta["epoch"],
                "paused": meta["paused"] == "true",
                "items": items,
                "next_cursor": next_cursor,
                "counts": counts,
            }


def require(condition: object, code: str) -> None:
    if not condition:
        raise ApprovalError(code)


def _sig(secret: bytes, context: str, value: Json) -> str:
    return hmac.new(
        secret, context.encode() + b"\n" + canonical(value).encode(), hashlib.sha256
    ).hexdigest()


def _post(hook: str, payload: Json) -> int:
    with closing(http.client.HTTPSConnection("hooks.zapier.com", timeout=5)) as client:
        client.request(
            "POST",
            _hook_path(hook),
            body=canonical(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with closing(client.getresponse()) as response:
            return response.status


class ApprovalBridge:
    def __init__(self, directory: Path, organization: str, config: Path):
        self.directory = directory
        self.organization = identifier(organization)
        self.config = config
        require(
            directory.is_absolute()
            and directory.resolve(strict=True) == directory
            and config.parent == directory,
            "private_configuration_required",
        )
        _check_directory(config)

    @contextmanager
    def _serialized(
        self, *, expected: Json | None = None, deadline: float | None = None
    ) -> Iterator[ReconciliationJournal]:
        require(
            expected is None
            or (
                isinstance(expected, dict)
                and set(expected) == {"installation_epoch", "policy_revision"}
                and isinstance(expected["installation_epoch"], str)
                and re.fullmatch("[0-9a-f]{32}", expected["installation_epoch"])
                and isinstance(expected["policy_revision"], str)
                and HEX.fullmatch(expected["policy_revision"])
            ),
            "invalid_reconciliation_binding",
        )
        require(
            deadline is None
            or (type(deadline) in (int, float) and math.isfinite(deadline) and deadline >= 0),
            "invalid_reconciliation_deadline",
        )
        self._policy()
        path = self.directory / "zapier-reconciliation.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "rb") as lock:
            _check_file(path)
            fcntl.flock(lock, fcntl.LOCK_EX)
            require(deadline is None or time.monotonic() < deadline, "reconciliation_deadline")
            policy, key = self._policy()
            if expected is not None:
                ledger = self.directory / "zapier-reconciliation.sqlite3"
                _check_file(ledger)
                with closing(sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)) as db:
                    meta = dict(
                        db.execute(
                            "SELECT key,value FROM installation WHERE key IN "
                            "('organization','epoch','policy_revision','signer_fingerprint')"
                        )
                    )
                require(
                    meta.get("organization") == self.organization
                    and meta.get("epoch") == expected["installation_epoch"]
                    and meta.get("policy_revision") == expected["policy_revision"]
                    and digest(policy) == expected["policy_revision"]
                    and meta.get("signer_fingerprint") == hashlib.sha256(key).hexdigest(),
                    "reconciliation_binding_changed",
                )
            require(deadline is None or time.monotonic() < deadline, "reconciliation_deadline")
            journal = ReconciliationJournal(self.directory, self.organization)
            journal.pin_installation(policy, key)
            self._inventory(journal, policy, key)
            yield journal

    def _assert_installed_policy(self, journal: ReconciliationJournal) -> None:
        policy, key = self._policy()
        meta = journal.installation()
        require(
            meta["policy_revision"] == digest(policy)
            and meta["signer_fingerprint"] == hashlib.sha256(key).hexdigest(),
            "installation_cutover_required",
        )

    def _invalidate(self, request_id: str, reason: str) -> None:
        marker = self._path(request_id, "reconciled")
        if not os.path.lexists(marker):
            self._record_observation(marker, {"request_id": request_id, "reason": reason})

    def _inventory(self, journal: ReconciliationJournal, policy: Json, key: bytes) -> None:
        for path in sorted(self.directory.glob("zapier-approval-za-*-request.json")):
            request_id = path.name.removeprefix("zapier-approval-").removesuffix("-request.json")
            if journal.request_state(request_id) is None:
                journal.record(self._request(request_id, policy, key, fresh=False), policy)
            try:
                self._archived_request(journal, request_id, key)
            except ApprovalError as error:
                if str(error) not in {
                    "original_request_policy_required",
                    "original_request_signer_required",
                }:
                    raise
                wrapped = _read_private(path, 16_384)
                with journal.db() as db:
                    row = db.execute(
                        "SELECT request,state,reason FROM requests WHERE id=?", (request_id,)
                    ).fetchone()
                    require(
                        row is not None
                        and json.loads(row["request"]) == wrapped.get("request")
                        and wrapped["request"]["request_id"] == request_id
                        and wrapped["request"]["policy_revision"] != digest(policy),
                        "request_seal_mismatch",
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO request_recovery VALUES (?,?,?)",
                        (request_id, row["state"], row["reason"]),
                    )
                    db.execute("UPDATE requests SET state='held' WHERE id=?", (request_id,))
                journal.transition(request_id, "held", str(error), "unknown")
                continue
            with journal.db() as db:
                recovery = db.execute(
                    "SELECT state,reason FROM request_recovery WHERE id=?", (request_id,)
                ).fetchone()
                if recovery is not None:
                    db.execute(
                        "UPDATE requests SET state=?,reason=? WHERE id=?",
                        (*recovery, request_id),
                    )
                    db.execute("DELETE FROM request_recovery WHERE id=?", (request_id,))
            marker = self._path(request_id, "reconciled")
            if os.path.lexists(marker):
                closed = _read_private(marker, 1024)
                reason = closed.get("reason")
                require(
                    closed.get("request_id") == request_id
                    and isinstance(reason, str)
                    and reason in MARKER_STATES,
                    "invalid_reconciliation_marker",
                )
                assert isinstance(reason, str)
                # The marker fences callbacks first. A crash before the DB commit is recoverable.
                with journal.db() as db:
                    status = db.execute(
                        "SELECT provider_status FROM requests WHERE id=?", (request_id,)
                    ).fetchone()[0]
                    projected = (
                        db.execute(
                            "SELECT 1 FROM reviewer_projected_requests WHERE request_id=?",
                            (request_id,),
                        ).fetchone()
                        is not None
                    )
                replaced = reason == "reviewer_replaced" or projected
                unknown = self._unknown_outcome(request_id, status)
                conflicts = sorted(
                    self.directory.glob(f"zapier-approval-{request_id}-status-conflict-*.json")
                )
                for conflict in conflicts:
                    evidence = _read_private(conflict, 1024)
                    require(
                        set(evidence) == {"request_id", "event_sha256", "status"}
                        and evidence["request_id"] == request_id
                        and isinstance(evidence["event_sha256"], str)
                        and HEX.fullmatch(evidence["event_sha256"])
                        and evidence["status"] in {"Pending", "Approved", "Declined"}
                        and conflict
                        == self._path(
                            request_id, "status-conflict-" + evidence["event_sha256"][:16]
                        ),
                        "invalid_provider_conflict_receipt",
                    )
                journal.transition(
                    request_id,
                    "held"
                    if unknown or conflicts
                    else "superseded"
                    if replaced
                    else MARKER_STATES[reason],
                    "conflicting_provider_event"
                    if conflicts
                    else "remote_outcome_unknown"
                    if unknown
                    else "reviewer_replaced"
                    if replaced
                    else reason,
                    "reconciled",
                )

    def _unknown_outcome(self, request_id: str, provider_status: str) -> bool:
        admitted = self._path(request_id, "admitted")
        refused = self._path(request_id, "not-sent")
        if os.path.lexists(refused):
            require(
                _read_private(refused, 1024)
                == {"request_id": request_id, "reason": "request_expired_before_submission"},
                "invalid_local_submission_receipt",
            )
            return False
        if provider_status == "Pending" and os.path.lexists(self._path(request_id, "event")):
            # A verified terminal reservation may precede a stopped local effect.
            # An earlier Pending observation does not settle that later event.
            return True
        # Catch Hook HTTP acceptance is not an observation of the approval action.
        return provider_status not in {"Pending", "Approved", "Declined"} and os.path.lexists(
            admitted
        )

    def pending_summary(self, *, limit: int = 25, cursor: str | None = None) -> Json:
        """Owner-local surface; an installed adapter must derive tenant from its connection."""
        with self._serialized() as journal:
            policy, key = self._policy()
            self._inventory(journal, policy, key)
            return journal.page(key, limit=limit, cursor=cursor)

    def recover_request_authority(
        self, request_id: str, *, original_policy: Path, original_key: Path | None = None
    ) -> Json:
        """Restore original private verification material; grant no current authority."""
        with self._serialized() as journal:
            historical = ApprovalBridge(self.directory, self.organization, original_policy)
            try:
                policy, key = historical._policy(fresh=False, signer_path=original_key)
            except FileNotFoundError:
                require(original_policy.exists(), "original_request_policy_required")
                raise ApprovalError("original_request_signer_required") from None
            request = self._request(request_id, policy, key, fresh=False)
            with journal.db() as db:
                recorded = db.execute(
                    "SELECT request FROM requests WHERE id=?", (request_id,)
                ).fetchone()
                require(
                    recorded is not None and json.loads(recorded[0]) == request,
                    "reconciliation_identity_conflict",
                )
                prior = db.execute(
                    "SELECT document,signer FROM policies WHERE revision=?", (digest(policy),)
                ).fetchone()
                fingerprint = hashlib.sha256(key).hexdigest()
                require(
                    prior is None or tuple(prior) == (canonical(policy), fingerprint),
                    "original_request_signer_mismatch",
                )
                db.execute(
                    "INSERT OR IGNORE INTO policies VALUES (?,?,?)",
                    (digest(policy), canonical(policy), fingerprint),
                )
                if original_key is not None:
                    db.execute(
                        "INSERT OR REPLACE INTO policy_signers VALUES (?,?)",
                        (digest(policy), original_key.name),
                    )
            current_policy, current_key = self._policy()
            self._inventory(journal, current_policy, current_key)
            return {"request_id": request_id, "state": "original_authority_restored"}

    def _archived_request(
        self, journal: ReconciliationJournal, request_id: str, key: bytes
    ) -> tuple[Json, Json, Json, bytes | None]:
        wrapped = _read_private(self._path(request_id, "request"), 16_384)
        request = wrapped.get("request")
        require(isinstance(request, dict), "request_seal_mismatch")
        assert isinstance(request, dict)
        with journal.db() as db:
            row = db.execute(
                "SELECT document,signer FROM policies WHERE revision=?",
                (request.get("policy_revision"),),
            ).fetchone()
            cached = db.execute(
                "SELECT wrapper_sha256 FROM verified_requests WHERE id=?", (request_id,)
            ).fetchone()
            recorded = db.execute(
                "SELECT state,request FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            signer_path = db.execute(
                "SELECT key_file FROM policy_signers WHERE revision=?",
                (request.get("policy_revision"),),
            ).fetchone()
        require(row is not None, "original_request_policy_required")
        assert row is not None
        policy = json.loads(row["document"])
        require(
            policy["organization"] == self.organization
            and digest(policy) == request["policy_revision"],
            "request_policy_unavailable",
        )
        original_key = key if row["signer"] == hashlib.sha256(key).hexdigest() else None
        wrapper_sha = digest(wrapped)
        verified_closed = (
            cached is not None
            and cached[0] == wrapper_sha
            and recorded is not None
            and recorded["state"] in {"approved", "changes_requested", "superseded", "expired"}
            and json.loads(recorded["request"]) == request
        )
        if original_key is None:
            key_file = signer_path[0] if signer_path is not None else policy["key_file"]
            require(
                isinstance(key_file, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}\.json", key_file),
                "invalid_signer_reference",
            )
            try:
                secret = _read_private(self.directory / key_file, 1024)
            except FileNotFoundError:
                pass
            else:
                require(
                    set(secret) == {"secret_hex"}
                    and isinstance(secret["secret_hex"], str)
                    and HEX.fullmatch(secret["secret_hex"]),
                    "original_request_signer_mismatch",
                )
                candidate = bytes.fromhex(secret["secret_hex"])
                if verified_closed and candidate == key and signer_path is None:
                    # An authorized rotation may reuse the selected signer filename.
                    # Exact already-verified closures retain identity, never event authority.
                    return request, policy, wrapped, None
                require(
                    hashlib.sha256(candidate).hexdigest() == row["signer"],
                    "original_request_signer_mismatch",
                )
                original_key = candidate
        if original_key is None:
            # A past authenticated, closed request may retain its audit identity
            # after key retirement. This receipt never authenticates a new event.
            require(
                verified_closed,
                "original_request_signer_required",
            )
            return request, policy, wrapped, None
        verified = self._request(request_id, policy, original_key, fresh=False)
        require(verified == request, "request_seal_mismatch")
        require(cached is None or cached[0] == wrapper_sha, "request_seal_mismatch")
        with journal.db() as db:
            db.execute(
                "INSERT OR IGNORE INTO verified_requests VALUES (?,?)", (request_id, wrapper_sha)
            )
        return verified, policy, wrapped, original_key

    def reconcile_reviewers(self, *, authority: Path, actor: str, limit: int = 50) -> Json:
        """Project signed settings fences locally; never assert a provider outcome."""
        from opentour_cell.pilot.owner_settings import OwnerSettings
        from opentour_cell.pilot.reviewer_assignment import ReviewerAssignments

        require(authority.parent == self.directory, "private_configuration_required")
        with self._serialized() as journal:
            self._assert_installed_policy(journal)
            policy, key = self._policy()
            service = ReviewerAssignments(
                OwnerSettings(
                    self.directory,
                    self.organization,
                    policy["engagement"],
                    authority_file=authority,
                )
            )
            pending = service.pending_reconciliations(actor=actor, limit=limit)
            with service.settings.ledger._db(write=False) as db:
                current = service._current(db, service._operator(actor, db))
            if (
                isinstance(current.get("reviewer_replacement"), dict)
                and journal.installation().get("generation") != current["revision"]
            ):
                # A saved replacement can have no outstanding proposal because
                # all prior effects completed. Its installation still adopts once.
                with journal.db() as db:
                    db.execute("UPDATE installation SET value='true' WHERE key='paused'")
            projected = []
            for item in pending["records"]:
                document = item["document"]
                require(
                    document["organization"] == self.organization
                    and document["engagement"] == policy["engagement"]
                    and digest(document) == item["document_sha256"]
                    and item["id"] == "review-reconcile-" + digest(document),
                    "replacement_projection_scope_mismatch",
                )
                requests = []
                for request_id in document["expired_request_ids"]:
                    request, previous, wrapped, _ = self._archived_request(journal, request_id, key)
                    require(
                        digest(wrapped) == document["request_fingerprints"].get(request_id)
                        and request["engagement"] == policy["engagement"]
                        and all(
                            request[k] == document[k]
                            for k in ("run_id", "run_revision", "artifact_sha")
                        ),
                        "replacement_request_changed",
                    )
                    journal.record(request, previous)
                    requests.append(request)
                # Pausing is durable before any projection. Current saved authority already
                # fences effects, even if this process stops before writing the first marker.
                with journal.db() as db:
                    db.execute("UPDATE installation SET value='true' WHERE key='paused'")
                for request in requests:
                    request_id = request["request_id"]
                    with journal.db() as db:
                        row = db.execute(
                            "SELECT state,provider_status FROM requests WHERE id=?", (request_id,)
                        ).fetchone()
                    assert row is not None
                    if row["state"] in {"approved", "changes_requested"}:
                        continue
                    # The producer's observed uncertainty flag cannot clear later local
                    # uncertainty. Only the bridge's authenticated status history can do so.
                    unknown = self._unknown_outcome(request_id, row["provider_status"])
                    self._invalidate(request_id, "reviewer_replaced")
                    journal.transition(
                        request_id,
                        "held" if unknown else "superseded",
                        "remote_outcome_unknown" if unknown else "reviewer_replaced",
                        row["provider_status"],
                    )
                with journal.db() as db:
                    previous = db.execute(
                        "SELECT document_sha256 FROM reviewer_projections WHERE id=?", (item["id"],)
                    ).fetchone()
                    require(
                        previous is None or previous[0] == item["document_sha256"],
                        "replacement_projection_conflict",
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO reviewer_projections VALUES (?,?,?)",
                        (item["id"], canonical(document), item["document_sha256"]),
                    )
                    db.executemany(
                        "INSERT OR IGNORE INTO reviewer_projected_requests VALUES (?,?)",
                        [(request["request_id"], item["id"]) for request in requests],
                    )
                # No reconciliation DB transaction remains open when the producer obtains
                # its training/mandate writers. ACK grants no send or inspection authority.
                receipt = service.acknowledge_reconciliation(
                    item["id"], item["document_sha256"], actor=actor
                )
                projected.append({"id": item["id"], "receipt_sha256": digest(receipt)})
            return {
                "state": "projected",
                "records": projected,
                "remaining": service.pending_reconciliations(actor=actor, limit=limit)[
                    "pending_count"
                ],
                "provider_outcome": "not_qualified",
            }

    def observe_replaced_status(self, envelope: Json) -> Json:
        """Record authenticated old-provider state without accepting an old vote."""
        return self._observe_fenced_status(envelope, replaced=True)

    def observe_fenced_status(self, envelope: Json) -> Json:
        """Resolve an expired or paused ordinary request without importing its vote."""
        return self._observe_fenced_status(envelope, replaced=False)

    def _record_observation(self, path: Path, value: Json) -> None:
        # Every caller holds the approval writer. Rename keeps the published file
        # single-linked even if the process stops immediately after publication.
        require(path.parent == self.directory, "private_configuration_required")
        if os.path.lexists(path):
            require(_read_private(path, 16_384) == value, "existing_result_conflict")
        else:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=self.directory, prefix=".zapier-reconcile-", delete=False
                ) as pending:
                    temporary = Path(pending.name)
                    pending.write((canonical(value) + "\n").encode())
                    pending.flush()
                    os.fsync(pending.fileno())
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _observe_fenced_status(self, envelope: Json, *, replaced: bool) -> Json:
        with self._serialized() as journal:
            _, key = self._policy()
            require(
                set(envelope) == {"event", "signature"}
                and isinstance(envelope["event"], dict)
                and isinstance(envelope["signature"], str),
                "invalid_provider_envelope",
            )
            event = envelope["event"]
            require(
                set(event) == FIELDS and event["schema_version"] == EVENT,
                "provider_signature_invalid",
            )
            request_id = event["request_id"]
            request, policy, _, original_key = self._archived_request(journal, request_id, key)
            require(original_key is not None, "original_provider_signer_required")
            assert original_key is not None
            require(
                hmac.compare_digest(envelope["signature"], _sig(original_key, EVENT, event)),
                "provider_signature_invalid",
            )
            marker_path = self._path(request_id, "reconciled")
            marker = _read_private(marker_path, 1024) if os.path.lexists(marker_path) else None
            with journal.db() as db:
                projected = db.execute(
                    "SELECT 1 FROM reviewer_projected_requests WHERE request_id=?", (request_id,)
                ).fetchone()
                row = db.execute(
                    "SELECT state,provider_status FROM requests WHERE id=?", (request_id,)
                ).fetchone()
            require(row is not None, "request_not_in_reconciliation_inventory")
            assert row is not None
            require(
                marker is None
                or (
                    marker.get("request_id") == request_id and marker.get("reason") in MARKER_STATES
                ),
                "invalid_reconciliation_marker",
            )
            if replaced:
                require(projected is not None and marker is not None, "provider_scope_mismatch")
            else:
                require(projected is None, "reviewer_replacement_observation_required")
                require(
                    row["state"] not in {"approved", "changes_requested"}
                    and self._completed_decision(request) is None,
                    "completed_request_observation_not_required",
                )
                require(
                    marker is not None
                    or int(time.time()) >= request["expires_at"]
                    or journal.installation()["paused"] == "true",
                    "request_not_fenced",
                )
            require(
                event["nonce"] == request["nonce"]
                and event["zap_id"] == policy["zap_id"]
                and event["zap_version"] == policy["zap_version"],
                "provider_scope_mismatch",
            )
            _check_file(self._path(request_id, "admitted"))
            require(
                not os.path.lexists(self._path(request_id, "not-sent")),
                "request_was_not_submitted",
            )
            require(
                type(event["decided_at"]) is int
                and request["issued_at"] <= event["decided_at"]
                and event["decided_at"] <= int(time.time())
                and isinstance(event["event_id"], str)
                and OPAQUE.fullmatch(event["event_id"])
                and event["status"] in {"Pending", "Approved", "Declined"},
                "invalid_provider_event",
            )
            require(
                event["status"] == "Pending"
                or event["actor_id"] == policy["reviewer"]
                or (
                    event["actor_id"] is None
                    and policy["approver_mode"] == "verified_single_approver"
                ),
                "named_approver_unverified",
            )
            reservation = self._path(request_id, "event")
            reservation_conflict = False
            if os.path.lexists(reservation):
                stamp = _read_private(reservation, 1024)
                require(
                    set(stamp) == {"event_identity", "first_event_sha256", "request_id"}
                    and stamp["request_id"] == request_id
                    and all(
                        isinstance(stamp[k], str) and HEX.fullmatch(stamp[k])
                        for k in ("event_identity", "first_event_sha256")
                    ),
                    "invalid_provider_event_reservation",
                )
                reservation_conflict = stamp["event_identity"] != digest(
                    {k: v for k, v in event.items() if k != "decided_at"}
                )
            reason = (
                "reviewer_replaced"
                if replaced
                else marker["reason"]
                if marker is not None
                else "request_expired"
                if int(time.time()) >= request["expires_at"]
                else "version_superseded"
            )
            # Authenticate first, then fence durably before recording the observation.
            # If the worker stops here, inventory retains any unknown remote outcome.
            self._invalidate(request_id, reason)
            with journal.db() as db:
                row = db.execute(
                    "SELECT state,provider_status FROM requests WHERE id=?", (request_id,)
                ).fetchone()
                require(row is not None, "request_not_in_reconciliation_inventory")
                assert row is not None
                conflicting = reservation_conflict or (
                    row["provider_status"] in {"Approved", "Declined"}
                    and row["provider_status"] != event["status"]
                )
            if conflicting:
                reference = digest(event)
                self._record_observation(
                    self._path(request_id, "status-conflict-" + reference[:16]),
                    {
                        "request_id": request_id,
                        "event_sha256": reference,
                        "status": event["status"],
                    },
                )
                journal.transition(
                    request_id, "held", "conflicting_provider_event", row["provider_status"]
                )
                raise ApprovalError("conflicting_provider_event")
            require(
                not any(
                    self.directory.glob(f"zapier-approval-{request_id}-status-conflict-*.json")
                ),
                "conflicting_provider_event",
            )
            reference = digest(event)
            prefix = "replacement-observation-" if replaced else "fenced-observation-"
            self._record_observation(
                self._path(request_id, prefix + reference[:16]),
                {"event_sha256": reference, "status": event["status"]},
            )
            journal.transition(request_id, MARKER_STATES[reason], reason, event["status"])
            return {
                "request_id": request_id,
                "provider_status": event["status"],
                "observation_sha256": reference,
                "release_decision": "not_recorded",
            }

    def complete_reviewer_replacement(self, *, authority: Path, actor: str) -> Json:
        """Adopt only the saved current reviewer after local and remote reconciliation."""
        from opentour_cell.pilot.owner_settings import OwnerSettings
        from opentour_cell.pilot.reviewer_assignment import (
            ReviewerAssignments,
            current_review_authority,
        )

        require(authority.parent == self.directory, "private_configuration_required")
        with self._serialized() as journal:
            policy, key = self._policy()
            service = ReviewerAssignments(
                OwnerSettings(
                    self.directory,
                    self.organization,
                    policy["engagement"],
                    authority_file=authority,
                )
            )
            with service.settings.store.engagement_guard(engagement_id=policy["engagement"]):
                require(
                    service.pending_reconciliations(actor=actor)["pending_count"] == 0,
                    "replacement_projection_required",
                )
                with service.settings.ledger._db(write=False) as db:
                    current = service._current(db, service._operator(actor, db))
                replacement = current.get("reviewer_replacement")
                require(isinstance(replacement, dict), "saved_reviewer_replacement_required")
                assert isinstance(replacement, dict)
                require(
                    replacement["generation"] == current["revision"]
                    and all(
                        policy[k] == current["policy"]["agreement"]["release_review"][k]
                        for k in ("reviewer", "team", "channel", "audience")
                    ),
                    "current_review_authority_required",
                )
                with journal.db() as db:
                    meta = dict(db.execute("SELECT key,value FROM installation"))
                    previous = db.execute(
                        "SELECT document,signer FROM policies WHERE revision=?",
                        (meta["policy_revision"],),
                    ).fetchone()
                    require(previous is not None, "original_request_policy_required")
                    assert previous is not None
                    old_policy = json.loads(previous["document"])
                    require(
                        previous["signer"] == hashlib.sha256(key).hexdigest()
                        and {k: v for k, v in old_policy.items() if k != "reviewer"}
                        == {k: v for k, v in policy.items() if k != "reviewer"},
                        "reviewer_refresh_scope_changed",
                    )
                    projections = {
                        row["id"]: json.loads(row["document"])
                        for row in db.execute("SELECT id,document FROM reviewer_projections")
                    }
                    requests = db.execute(
                        "SELECT id,request,state,provider_status FROM requests"
                    ).fetchall()
                for proposal in replacement["proposals"]:
                    reference = proposal["reconciliation_ref"]
                    require(reference in projections, "replacement_projection_required")
                    document = projections[reference]
                    actual = current_review_authority(
                        self.directory,
                        self.organization,
                        policy["engagement"],
                        document["run_id"],
                        document["run_revision"],
                        document["artifact_sha"],
                    )
                    require(
                        actual is not None
                        and actual == document
                        and actual["generation"] == current["revision"],
                        "current_review_authority_required",
                    )
                if (
                    meta.get("generation") == current["revision"]
                    and meta["policy_revision"] == digest(policy)
                    and meta.get("reviewer_grant_revision") == current["reviewer_grant_revision"]
                    and meta["paused"] == "false"
                ):
                    return {
                        "state": "fresh_successor_inspection_required",
                        "installation_epoch": meta["epoch"],
                        "generation": current["revision"],
                    }
                for row in requests:
                    with journal.db() as db:
                        recovery = db.execute(
                            "SELECT 1 FROM request_recovery WHERE id=?", (row["id"],)
                        ).fetchone()
                    require(recovery is None, "original_request_authority_recovery_required")
                    if row["state"] in {
                        "approved",
                        "changes_requested",
                        "superseded",
                        "expired",
                    } and not self._unknown_outcome(row["id"], row["provider_status"]):
                        continue
                    if self._completed_decision(json.loads(row["request"])) is not None:
                        continue
                    require(
                        not self._unknown_outcome(row["id"], row["provider_status"]),
                        "remote_reconciliation_required",
                    )
                    require(
                        row["state"] in {"approved", "changes_requested", "superseded", "expired"},
                        "replacement_projection_required",
                    )
                if meta.get("generation") != current["revision"]:
                    require(meta["paused"] == "true", "dispatch_must_be_paused")
                    epoch = secrets.token_hex(16)
                else:
                    epoch = meta["epoch"]
                require(self._policy() == (policy, key), "plugin_policy_changed")
                with journal.db() as db:
                    values = {
                        "epoch": epoch,
                        "paused": "false",
                        "reviewer": policy["reviewer"],
                        "policy_revision": digest(policy),
                        "generation": current["revision"],
                        "reviewer_grant_revision": current["reviewer_grant_revision"],
                    }
                    for name, value in values.items():
                        db.execute(
                            "INSERT OR REPLACE INTO installation VALUES (?,?)", (name, value)
                        )
                journal.pin_installation(policy, key)
                return {
                    "state": "fresh_successor_inspection_required",
                    "installation_epoch": epoch,
                    "generation": current["revision"],
                }

    def reconcile(
        self,
        *,
        request_id: str | None = None,
        poll_interval: int = 60,
        reminder_interval: int = 3600,
    ) -> Json:
        require(
            type(poll_interval) is int
            and 1 <= poll_interval <= 86400
            and type(reminder_interval) is int
            and 1 <= reminder_interval <= 86400,
            "invalid_reconciliation_interval",
        )
        with self._serialized() as journal:
            return self._reconcile(
                journal,
                request_id=request_id,
                poll_interval=poll_interval,
                reminder_interval=reminder_interval,
            )

    def _reconcile(
        self,
        journal: ReconciliationJournal,
        *,
        request_id: str | None = None,
        poll_interval: int = 60,
        reminder_interval: int = 3600,
        schedule_action: str | None = None,
    ) -> Json:
        """Shared core; caller holds this bridge writer and no SQLite transaction."""
        require(
            type(poll_interval) is int
            and 1 <= poll_interval <= 86400
            and type(reminder_interval) is int
            and 1 <= reminder_interval <= 86400,
            "invalid_reconciliation_interval",
        )
        self._assert_installed_policy(journal)
        policy, key = self._policy()
        self._inventory(journal, policy, key)
        now = int(time.time())
        require(
            schedule_action is None
            or (
                request_id is not None
                and isinstance(schedule_action, str)
                and HEX.fullmatch(schedule_action)
            ),
            "invalid_schedule_action",
        )
        schedule_key = (
            SCHEDULE_RECEIPT_PREFIX + request_id
            if request_id is not None and schedule_action is not None
            else None
        )
        if schedule_key is not None:
            with journal.db() as db:
                prior = db.execute(
                    "SELECT value FROM installation WHERE key=?", (schedule_key,)
                ).fetchone()
            if prior is not None and prior[0] == schedule_action:
                return journal.page(key, limit=50, cursor=None) | {"state": "reconciled"}
        with journal.db() as db:
            if request_id is not None:
                require(
                    isinstance(request_id, str)
                    and REQ.fullmatch(request_id)
                    and db.execute("SELECT 1 FROM requests WHERE id=?", (request_id,)).fetchone()
                    is not None,
                    "request_not_in_reconciliation_inventory",
                )
            poll_key = "last_poll" if request_id is None else "last_poll:" + request_id
            db.execute("INSERT OR IGNORE INTO installation VALUES (?,'0')", (poll_key,))
            last = int(
                db.execute("SELECT value FROM installation WHERE key=?", (poll_key,)).fetchone()[0]
            )
            if now < last + poll_interval:
                return {"state": "throttled", "retry_after_seconds": last + poll_interval - now}
            db.execute("UPDATE installation SET value=? WHERE key=?", (str(now), poll_key))
            rows = db.execute(
                "SELECT id,request,state,provider_status FROM requests ORDER BY id"
            ).fetchall()
        for row in rows:
            if request_id is not None and row["id"] != request_id:
                continue
            target_id, req = row["id"], json.loads(row["request"])
            with journal.db() as db:
                if db.execute("SELECT 1 FROM request_recovery WHERE id=?", (target_id,)).fetchone():
                    continue
            if row["state"] in {
                "approved",
                "changes_requested",
                "superseded",
                "expired",
            } or os.path.lexists(self._path(target_id, "reconciled")):
                continue
            try:
                status = self.decision_status(
                    target_id, team=policy["team"], actor=policy["reviewer"]
                )
                if now < req["expires_at"]:
                    self._inspect(req)
            except (OSError, KeyError, sqlite3.Error):
                journal.transition(
                    target_id,
                    "held",
                    "local_state_unavailable",
                    row["provider_status"],
                    schedule_action=schedule_action,
                )
                continue
            except ValueError:
                self._invalidate(target_id, "current_authority_unavailable")
                journal.transition(
                    target_id,
                    "held",
                    "current_authority_unavailable",
                    "unknown",
                    schedule_action=schedule_action,
                )
                continue
            state = status["state"]
            unknown = self._unknown_outcome(target_id, row["provider_status"])
            if state in {"approved", "changes_requested"}:
                journal.transition(target_id, state, None, state, schedule_action=schedule_action)
            elif unknown:
                journal.transition(
                    target_id,
                    "held",
                    "remote_outcome_unknown",
                    "unknown",
                    count_retry=True,
                    schedule_action=schedule_action,
                )
            elif int(time.time()) >= req["expires_at"]:
                self._invalidate(target_id, "request_expired")
                journal.transition(
                    target_id,
                    "expired",
                    "request_expired",
                    "expired",
                    schedule_action=schedule_action,
                )
            else:
                if state == "uncertain" and row["provider_status"] == "Pending":
                    state = "accepted"
                journal.transition(
                    target_id,
                    "pending",
                    None,
                    row["provider_status"] if row["provider_status"] == "Pending" else state,
                    reminder_interval=reminder_interval if state == "accepted" else None,
                    schedule_action=schedule_action,
                )
        if schedule_key is not None:
            # Terminal/fenced/recovery rows can complete without a new transition.
            with journal.db() as db:
                db.execute(
                    "INSERT OR REPLACE INTO installation VALUES (?,?)",
                    (schedule_key, schedule_action),
                )
        result = journal.page(key, limit=50, cursor=None)
        result["state"] = "reconciled"
        return result

    def prepare_cutover(self) -> Json:
        """Fence dispatch before inventory; callers must drain known signed decisions first."""
        with self._serialized() as journal:
            self._assert_installed_policy(journal)
            policy, key = self._policy()
            with journal.db() as db:
                db.execute("UPDATE installation SET value='true' WHERE key='paused'")
                for name in ("reviewer", "zap_version"):
                    db.execute(
                        "INSERT OR IGNORE INTO installation VALUES (?,?)", (name, policy[name])
                    )
            self._inventory(journal, policy, key)
            with journal.db() as db:
                rows = db.execute(
                    "SELECT id,state,provider_status,reason FROM requests ORDER BY id"
                ).fetchall()
            for row in rows:
                if row["reason"] == "conflicting_provider_event":
                    continue
                if row["state"] in {"approved", "changes_requested", "superseded", "expired"}:
                    continue
                request_id = row["id"]
                with journal.db() as db:
                    if db.execute(
                        "SELECT 1 FROM request_recovery WHERE id=?", (request_id,)
                    ).fetchone():
                        continue
                try:
                    status = self.decision_status(
                        request_id, team=policy["team"], actor=policy["reviewer"]
                    )["state"]
                except (OSError, ValueError, KeyError, sqlite3.Error):
                    status = "uncertain"
                if status in {"approved", "changes_requested"}:
                    journal.transition(request_id, status, None, status)
                elif self._unknown_outcome(request_id, row["provider_status"]):
                    journal.transition(request_id, "held", "remote_outcome_unknown", "unknown")
                else:
                    self._invalidate(request_id, "version_superseded")
                    journal.transition(request_id, "superseded", "version_superseded", status)
            return journal.page(key, limit=50, cursor=None)

    def complete_cutover(self) -> Json:
        with self._serialized() as journal:
            policy, key = self._policy()
            meta = journal.installation()
            require(meta["paused"] == "true", "dispatch_must_be_paused")
            require(
                meta.get("reviewer") == policy["reviewer"], "reviewer_replacement_settings_required"
            )
            require(meta.get("zap_version") != policy["zap_version"], "new_zap_revision_required")
            with journal.db() as db:
                require(
                    db.execute("SELECT count(*) FROM request_recovery").fetchone()[0] == 0,
                    "original_request_authority_recovery_required",
                )
                unresolved = db.execute(
                    "SELECT count(*) FROM requests WHERE "
                    "state NOT IN ('approved','changes_requested','superseded','expired')"
                ).fetchone()[0]
                require(unresolved == 0, "remote_reconciliation_required")
                epoch = secrets.token_hex(16)
                db.execute("UPDATE installation SET value=? WHERE key='epoch'", (epoch,))
                db.execute("UPDATE installation SET value='false' WHERE key='paused'")
                db.execute(
                    "UPDATE installation SET value=? WHERE key='zap_version'",
                    (policy["zap_version"],),
                )
                db.execute(
                    "UPDATE installation SET value=? WHERE key='policy_revision'", (digest(policy),)
                )
                db.execute(
                    "UPDATE installation SET value=? WHERE key='signer_fingerprint'",
                    (hashlib.sha256(key).hexdigest(),),
                )
            return {
                "state": "ready",
                "installation_epoch": epoch,
                "zap_revision": policy["zap_version"],
            }

    def claim_notification(self) -> Json | None:
        """Reserve one opaque local outbox item; an unknown send is never automatically retried."""
        with self._serialized() as journal:
            self._assert_installed_policy(journal)
            policy, key = self._policy()
            with journal.db() as db:
                pending = db.execute(
                    "SELECT id,request_id,kind FROM outbox WHERE state='pending' "
                    "ORDER BY interval_start,id"
                ).fetchall()
            for row in pending:
                if row["kind"] == "reminder":
                    try:
                        req = self._request(row["request_id"], policy, key)
                        self._inspect(req)
                        require(
                            journal.request_state(row["request_id"]) == "pending",
                            "reminder_no_longer_current",
                        )
                    except (OSError, KeyError, sqlite3.Error):
                        continue
                    except ValueError:
                        with journal.db() as db:
                            db.execute(
                                "UPDATE outbox SET state='cancelled' WHERE id=?", (row["id"],)
                            )
                        continue
                with journal.db() as db:
                    db.execute("UPDATE outbox SET state='uncertain' WHERE id=?", (row["id"],))
                return {
                    "notification_ref": row["id"],
                    "request_id": row["request_id"],
                    "kind": row["kind"],
                    "tenant_ref": digest(self.organization),
                }
            return None

    def _assert_no_unknown_proposal(self, binding: Json) -> None:
        journal = ReconciliationJournal(self.directory, self.organization)
        with journal.db() as db:
            prior = db.execute(
                "SELECT id,request,provider_status,reason FROM requests "
                "WHERE state NOT IN ('approved','changes_requested')"
            ).fetchall()
            recovery = {row[0] for row in db.execute("SELECT id FROM request_recovery")}
        for row in prior:
            request = json.loads(row["request"])
            if not all(
                request[name] == binding[name]
                for name in ("run_id", "run_revision", "artifact_sha")
            ):
                continue
            require(row["reason"] != "conflicting_provider_event", "conflicting_provider_event")
            require(row["id"] not in recovery, "original_request_authority_recovery_required")
            require(
                not self._unknown_outcome(row["id"], row["provider_status"]),
                "remote_reconciliation_required",
            )

    def confirm_notification(self, notification_ref: str) -> Json:
        with self._serialized() as journal:
            self._assert_installed_policy(journal)
            self._policy()
            with journal.db() as db:
                row = db.execute(
                    "SELECT state FROM outbox WHERE id=?", (notification_ref,)
                ).fetchone()
                require(
                    row is not None and row[0] in {"uncertain", "confirmed"},
                    "notification_claim_required",
                )
                db.execute("UPDATE outbox SET state='confirmed' WHERE id=?", (notification_ref,))
            return {"notification_ref": notification_ref, "state": "confirmed"}

    def _policy(self, *, fresh: bool = True, signer_path: Path | None = None) -> tuple[Json, bytes]:
        p = _read_private(self.config, 16_384)
        require(
            set(p)
            == {
                "schema_version",
                "enabled",
                "organization",
                "engagement",
                "team",
                "channel",
                "reviewer",
                "audience",
                "zap_id",
                "zap_version",
                "approver_mode",
                "setup_attestation",
                "key_file",
                "expires_at",
                "hook_fingerprint",
            },
            "invalid_plugin_policy",
        )
        require(
            p["schema_version"] in {POLICY, RECIPE_POLICY}
            and p["enabled"] is True
            and p["organization"] == self.organization,
            "plugin_not_authorized",
        )
        require(
            all(
                isinstance(p[k], str) and OPAQUE.fullmatch(p[k])
                for k in (
                    "engagement",
                    "team",
                    "channel",
                    "reviewer",
                    "audience",
                    "zap_id",
                    "zap_version",
                )
            ),
            "invalid_plugin_scope",
        )
        require(
            re.fullmatch(r"T[A-Z0-9]+", p["team"])
            and re.fullmatch(r"[CG][A-Z0-9]+", p["channel"])
            and re.fullmatch(r"[UW][A-Z0-9]+", p["reviewer"]),
            "invalid_slack_scope",
        )
        require(
            p["approver_mode"] in {"verified_actor", "verified_single_approver"}
            and isinstance(p["setup_attestation"], str)
            and re.fullmatch(r"att-[a-f0-9]{16,64}", p["setup_attestation"]),
            "unverified_plugin_configuration",
        )
        require(
            type(p["expires_at"]) is int
            and 0 < p["expires_at"] < 2**63
            and (not fresh or time.time() < p["expires_at"]),
            "plugin_policy_expired",
        )
        require(
            isinstance(p["key_file"], str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}\.json", p["key_file"]),
            "invalid_signer_reference",
        )
        require(
            isinstance(p["hook_fingerprint"], str) and HEX.fullmatch(p["hook_fingerprint"]),
            "invalid_plugin_destination",
        )
        if signer_path is not None:
            require(
                not fresh
                and signer_path.parent == self.directory
                and re.fullmatch(r"[A-Za-z0-9_-]{1,64}\.json", signer_path.name),
                "invalid_signer_reference",
            )
        key = _read_private(signer_path or self.directory / p["key_file"], 1024)
        require(
            set(key) == {"secret_hex"}
            and isinstance(key["secret_hex"], str)
            and HEX.fullmatch(key["secret_hex"]),
            "private_signer_required",
        )
        store = LocalTrainingStore(
            self.directory / "training.sqlite3", self.organization, existing_only=True
        )
        require(
            store.engagement_active() and store.engagement_id == p["engagement"],
            "engagement_unavailable",
        )
        return p, bytes.fromhex(key["secret_hex"])

    def check(self) -> Json:
        p, _ = self._policy()
        return {"state": "configured", "provider": "zapier_slack", "policy_revision": digest(p)}

    def uses_recipe(self) -> bool:
        policy, _ = self._policy()
        version: str = policy["schema_version"]
        return version == RECIPE_POLICY

    def details(self, request_id: str, *, team: str, actor: str) -> Json:
        """Called only with the authenticated identity from the tenant's private viewer."""
        policy, key = self._policy()
        require(team == policy["team"] and actor == policy["reviewer"], "reviewer_required")
        req = self._request(request_id, policy, key)
        self._inspect(req)
        return {k: req[k] for k in ("run_revision", "artifact_sha", "audience", "expires_at")}

    def existing_status(
        self,
        *,
        run_id: str,
        run_revision: str,
        artifact_sha: str,
        team: str,
        actor: str,
        handoff_destination: tuple[str, str] | None = None,
    ) -> Json | None:
        policy, key = self._policy()
        require(team == policy["team"] and actor == policy["reviewer"], "reviewer_required")
        if handoff_destination is not None:
            require(
                handoff_destination == (policy["channel"], policy["audience"]),
                "request_scope_mismatch",
            )
        scope = {
            "run_id": run_id,
            "run_revision": run_revision,
            "artifact_sha": artifact_sha,
            **{k: policy[k] for k in ("engagement", "team", "channel", "reviewer", "audience")},
            "policy_revision": digest(policy),
        }
        scope.update(self._review_scope(scope))
        prefix = "za-" + _sig(key, REQUEST + "/scope", scope)[:16]
        active = []
        past = []
        now = int(time.time())
        for path in self.directory.glob(f"zapier-approval-{prefix}*-request.json"):
            rid = path.name.removeprefix("zapier-approval-").removesuffix("-request.json")
            req = self._request(rid, policy, key, fresh=False)
            require(all(req[k] == v for k, v in scope.items()), "request_scope_mismatch")
            if handoff_destination is None:
                if req["expires_at"] > int(time.time()) or self._path(rid, "event").exists():
                    return self.decision_status(rid, team=team, actor=actor)
            elif req["expires_at"] > now and not self._path(rid, "reconciled").exists():
                active.append(req)
            else:
                past.append(req)
        if active:
            # A superseded nonce or interrupted old callback cannot hide a current request.
            if len(active) != 1:
                return None
            return self._handoff_request_status(active[0], policy, key)
        if not past:
            return None
        past.sort(key=lambda req: req["issued_at"], reverse=True)
        if len(past) > 1 and past[0]["issued_at"] == past[1]["issued_at"]:
            return None
        return self._handoff_request_status(past[0], policy, key)

    def _handoff_request_status(self, req: Json, policy: Json, key: bytes) -> Json | None:
        """Project the selected request without initializing a reconciliation ledger."""
        from opentour_cell.pilot.receipt_recovery import _open_db

        reference = req["request_id"]
        with _open_db(self.directory / "zapier-reconciliation.sqlite3") as db:
            if db is None:
                return self._legacy_handoff_status(req, policy)
            installation = dict(db.execute("SELECT key,value FROM installation"))
            if (
                installation.get("organization") != self.organization
                or installation.get("policy_revision") != digest(policy)
                or installation.get("signer_fingerprint") != hashlib.sha256(key).hexdigest()
            ):
                return None
            row = db.execute(
                "SELECT request,epoch,state,reason,provider_status,observed_at FROM requests "
                "WHERE id=? AND NOT EXISTS "
                "(SELECT 1 FROM reviewer_projected_requests WHERE request_id=?)",
                (reference, reference),
            ).fetchone()
            if (
                row is None
                or row["epoch"] != installation.get("epoch")
                or row["state"] == "superseded"
            ):
                return None
            require(row["request"] == canonical(req), "reconciliation_identity_conflict")
            pending = {
                "request_id": reference,
                "state": "approval_unresolved",
                "recorded_at": row["observed_at"],
            }
            try:
                status = self.decision_status(
                    reference, team=policy["team"], actor=policy["reviewer"]
                )
            except ApprovalError as error:
                if str(error) == "receipt_scope_mismatch":
                    return pending
                raise
            decision = status["state"]
            if decision in {"approved", "changes_requested"}:
                return status if self._owns_handoff_decision(db, req, row, decision) else pending

        return self._unresolved_or_expired_status(reference, status, row, pending)

    def _legacy_handoff_status(self, req: Json, policy: Json) -> Json | None:
        """Verify pre-journal approvals using the existing sealed request and receipt."""
        reference = req["request_id"]
        if self._path(reference, "reconciled").exists():
            return None
        pending = {"request_id": reference, "state": "approval_unresolved"}
        try:
            status = self.decision_status(reference, team=policy["team"], actor=policy["reviewer"])
        except ApprovalError as error:
            if str(error) == "receipt_scope_mismatch":
                return pending
            raise
        if status["state"] == "expired":
            return None
        if status["state"] not in {"approved", "changes_requested"}:
            return status
        if not self._has_decision_event(reference):
            return pending
        for path in self.directory.glob("zapier-approval-za-*-request.json"):
            if path == self._path(reference, "request"):
                continue
            other = _read_private(path, 16_384).get("request")
            if not isinstance(other, dict) or self._same_presentation(req, other):
                # An archived legacy matching request is evidence of ambiguity only.
                return pending
        return status

    @staticmethod
    def _same_presentation(req: Json, other: Json) -> bool:
        return all(other.get(k) == req.get(k) for k in BIND + REVIEW_AUTHORITY)

    def _owns_handoff_decision(
        self, db: sqlite3.Connection, req: Json, row: sqlite3.Row, decision: str
    ) -> bool:
        provider = "Approved" if decision == "approved" else "Declined"
        reference = req["request_id"]
        # Restart may recover a completed decision after the callback stopped
        # before writing its terminal provider polarity to the journal.
        if row["state"] != decision or row["provider_status"] not in {
            "unknown",
            "Pending",
            provider,
        }:
            return False
        if self._path(reference, "reconciled").exists() or not self._has_decision_event(reference):
            return False
        for (archived,) in db.execute(
            "SELECT request FROM requests WHERE id!=? AND json_extract(request,'$.run_id')=?",
            (reference, req["run_id"]),
        ):
            previous = json.loads(archived)
            if self._same_presentation(req, previous):
                # A legacy run-level decision cannot identify which nonce accepted it.
                return False
        return True

    def _has_decision_event(self, reference: str) -> bool:
        path = self._path(reference, "event")
        if not path.exists():
            return False
        stamp = _read_private(path, 1024)
        return (
            set(stamp) == {"event_identity", "first_event_sha256", "request_id"}
            and stamp["request_id"] == reference
            and all(
                isinstance(stamp[k], str) and HEX.fullmatch(stamp[k])
                for k in ("event_identity", "first_event_sha256")
            )
        )

    def _unresolved_or_expired_status(
        self, reference: str, status: Json, row: sqlite3.Row, pending: Json
    ) -> Json | None:
        state, reason, provider = row["state"], row["reason"], row["provider_status"]
        if (
            state == "held" and reason in {"remote_outcome_unknown", "conflicting_provider_event"}
        ) or any(self.directory.glob(f"zapier-approval-{reference}-status-conflict-*.json")):
            return pending
        marker = self._path(reference, "reconciled")
        if status["state"] != "expired":
            return status if state == "pending" and not marker.exists() else None
        if state != "expired" or reason != "request_expired":
            return None
        require(
            _read_private(marker, 1024) == {"request_id": reference, "reason": reason},
            "invalid_reconciliation_marker",
        )
        if self._unknown_outcome(reference, provider):
            return pending
        if (
            self._path(reference, "admitted").exists()
            and not self._path(reference, "not-sent").exists()
        ):
            if not self._has_expiry_observation(reference, provider):
                return pending
        return {**status, "recorded_at": status["expires_at"]}

    def _has_expiry_observation(self, request_id: str, provider: str) -> bool:
        if provider not in {"Pending", "Approved", "Declined"}:
            return False
        prefix = "fenced-observation-"
        for path in self.directory.glob(f"zapier-approval-{request_id}-{prefix}*.json"):
            observation = _read_private(path, 1024)
            reference = observation.get("event_sha256")
            if (
                set(observation) == {"event_sha256", "status"}
                and observation["status"] == provider
                and isinstance(reference, str)
                and HEX.fullmatch(reference)
                and path == self._path(request_id, prefix + reference[:16])
            ):
                return True
        return False

    def decision_status(self, request_id: str, *, team: str, actor: str) -> Json:
        policy, key = self._policy()
        require(team == policy["team"] and actor == policy["reviewer"], "reviewer_required")
        req = self._request(request_id, policy, key, fresh=False)
        work, frozen = release_gate._binding(
            self.directory,
            self.organization,
            req["run_id"],
            req["run_revision"],
            req["artifact_sha"],
        )
        seen = release_gate._inspection(
            self.directory,
            self.organization,
            req["run_id"],
            req["run_revision"],
            req["artifact_sha"],
            frozen,
            req["inspection_ref"],
            fresh=False,
        )
        require(seen["engagement"] == req["engagement"], "plugin_policy_changed")
        scope = {
            k: req[k]
            for k in ("request_id", "run_revision", "artifact_sha", "audience", "expires_at")
        }
        receipt_path = work / "release-decision.json"
        if not receipt_path.exists():
            transport_path = self._path(request_id, "transport")
            if req["expires_at"] <= int(time.time()):
                state = "expired"
            elif transport_path.exists():
                transport = _read_private(transport_path, 1024)
                state = transport["transport"]
                require(
                    transport["request_id"] == request_id
                    and state in {"accepted", "failed", "uncertain"},
                    "invalid_transport_receipt",
                )
            else:
                state = "uncertain" if self._path(request_id, "admitted").exists() else "staged"
            return {**scope, "state": state}
        receipt = _read_private(receipt_path, 16384)
        require(
            all(receipt.get(k) == req[k] for k in BIND)
            and receipt.get("provider") == "zapier_slack"
            and receipt.get("decision") in {"approved", "changes_requested"},
            "receipt_scope_mismatch",
        )
        return {**scope, "state": receipt["decision"], "receipt_sha256": digest(receipt)}

    def _completed_decision(self, req: Json) -> str | None:
        """Match archived identity only; never reopen evidence or grant current authority."""
        path = self.directory / "managed-outputs" / req["run_id"] / "release-decision.json"
        if not os.path.lexists(path):
            return None
        receipt = _read_private(path, 16_384)
        # _serialized has already authenticated the original request wrapper (or
        # its exact previously verified closure). A different presentation for
        # the same run is not evidence for this request.
        if not (
            all(receipt.get(k) == req[k] for k in BIND + ("run_id",))
            and receipt.get("organization") == self.organization
            and receipt.get("presentation_id") == req["inspection_ref"]
            and receipt.get("provider") == "zapier_slack"
            and receipt.get("decision") in {"approved", "changes_requested"}
            and {k: receipt[k] for k in REVIEW_AUTHORITY if k in receipt}
            == {k: req[k] for k in REVIEW_AUTHORITY if k in req}
        ):
            return None
        journal = ReconciliationJournal(self.directory, self.organization)
        with journal.db() as db:
            prior = db.execute(
                "SELECT request FROM requests WHERE id!=?", (req["request_id"],)
            ).fetchall()
        for row in prior:
            other = json.loads(row[0])
            if all(other.get(k) == req.get(k) for k in BIND + REVIEW_AUTHORITY):
                # Older versions allowed two nonces to share one presentation.
                # The legacy release schema cannot attribute that receipt to one
                # of them, even if a failed callback reserved an event stamp.
                return None
        return str(receipt["decision"])

    def _path(self, request_id: str, kind: str) -> Path:
        require(isinstance(request_id, str) and REQ.fullmatch(request_id), "invalid_request_id")
        return self.directory / f"zapier-approval-{request_id}-{kind}.json"

    def _review_scope(self, req: Json) -> Json:
        # Preserve the shipped legacy route without importing uninstalled producer
        # modules. Once saved replacement history exists, missing code is a hold.
        ledger = self.directory / "learning-mandates.sqlite3"
        if not os.path.lexists(ledger):
            return {}
        _check_file(ledger)
        with closing(sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)) as db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviewer_replacements'"
            ).fetchone():
                return {}
        from opentour_cell.pilot.reviewer_assignment import current_review_authority

        current = current_review_authority(
            self.directory,
            self.organization,
            req["engagement"],
            req["run_id"],
            req["run_revision"],
            req["artifact_sha"],
        )
        if current is None:
            return {}
        require(
            current["release_review"].get("provider") == "zapier_slack"
            and all(
                current["release_review"].get(k) == req[k]
                for k in ("reviewer", "team", "channel", "audience")
            ),
            "current_review_authority_required",
        )
        return {k: current[k] for k in REVIEW_AUTHORITY}

    def _inspect(self, req: Json, *, preparing: bool = False) -> Json:
        scope = self._review_scope(req)
        if scope:
            installed = ReconciliationJournal(self.directory, self.organization).installation()
            require(
                all(installed.get(k) == v for k, v in scope.items())
                and installed["paused"] == "false",
                "reviewer_generation_adoption_required",
            )
        require(
            preparing or {k: req[k] for k in REVIEW_AUTHORITY if k in req} == scope,
            "review_generation_changed",
        )
        inspection = release_gate.check_release_inspection(
            self.directory, self.organization, **{k: req[k] for k in BIND}
        )
        require(
            inspection["provider"] == "zapier_slack"
            and inspection["engagement"] == req["engagement"],
            "plugin_not_selected",
        )
        require(
            {k: inspection[k] for k in REVIEW_AUTHORITY if k in inspection} == scope,
            "fresh_evidence_inspection_required",
        )
        return inspection

    def _request(self, request_id: str, p: Json, key: bytes, *, fresh: bool = True) -> Json:
        wrapped = _read_private(self._path(request_id, "request"), 16_384)
        req = wrapped.get("request")
        require(
            isinstance(req, dict)
            and isinstance(wrapped.get("seal"), str)
            and hmac.compare_digest(wrapped["seal"], _sig(key, REQUEST, req)),
            "request_seal_mismatch",
        )
        assert isinstance(req, dict)
        require(
            req["request_id"] == request_id
            and req["policy_revision"] == digest(p)
            and req["engagement"] == p["engagement"],
            "plugin_policy_changed",
        )
        require(not fresh or int(time.time()) < req["expires_at"], "request_expired")
        require(
            not fresh or not os.path.lexists(self._path(request_id, "reconciled")),
            "request_reconciled",
        )
        return req

    @staticmethod
    def _wire(req: Json, key: bytes, policy: Json) -> Json:
        if policy["schema_version"] == RECIPE_POLICY:
            reference = {
                "schema_version": REFERENCE,
                **{k: req[k] for k in ("request_id", "nonce", "expires_at")},
            }
            scope = {k: policy[k] for k in ("team", "channel", "reviewer", "zap_id", "zap_version")}
            return reference | {"request_signature": _sig(key, REFERENCE, reference | scope)}

        payload = {
            k: req[k]
            for k in (
                "schema_version",
                "request_id",
                "nonce",
                "expires_at",
                "team",
                "channel",
                "reviewer",
                "run_revision",
                "artifact_sha",
                "audience",
            )
        }
        return payload | {"request_signature": _sig(key, REQUEST + "/outbound", payload)}

    def issue(self, **arguments: Any) -> Json:
        with self._serialized() as journal:
            self._assert_installed_policy(journal)
            require(journal.installation()["paused"] == "false", "approval_dispatch_paused")
            policy, key = self._policy()
            self._inventory(journal, policy, key)
            wire = self._issue(**arguments)
            # Preview is deliberately read-only with respect to the request inventory.
            if not arguments.get("preview_only", False):
                policy, key = self._policy()
                journal.record(self._request(wire["request_id"], policy, key), policy)
            return wire

    def _issue(
        self,
        *,
        run_id: str,
        run_revision: str,
        artifact_sha: str,
        inspection_ref: str,
        ttl_seconds: int = 900,
        expected_policy: str | None = None,
        preview_only: bool = False,
        confirmed_preview: Json | None = None,
    ) -> Json:
        p, key = self._policy()
        require(expected_policy in (None, p["schema_version"]), "plugin_policy_changed")
        require(not preview_only or p["schema_version"] == RECIPE_POLICY, "recipe_required")
        require(
            isinstance(run_id, str)
            and re.fullmatch("run-[a-f0-9]{32}", run_id)
            and HEX.fullmatch(run_revision)
            and HEX.fullmatch(artifact_sha)
            and OPAQUE.fullmatch(inspection_ref),
            "invalid_release_binding",
        )
        require(type(ttl_seconds) is int and 1 <= ttl_seconds <= 86400, "invalid_request_lifetime")
        binding = {
            "run_id": run_id,
            "run_revision": run_revision,
            "artifact_sha": artifact_sha,
            "inspection_ref": inspection_ref,
            **{k: p[k] for k in ("engagement", "team", "channel", "reviewer", "audience")},
            "policy_revision": digest(p),
        }
        store = LocalTrainingStore(
            self.directory / "training.sqlite3", self.organization, existing_only=True
        )
        with store.engagement_guard(engagement_id=p["engagement"]):
            require(_read_private(self.config, 16_384) == p, "plugin_policy_changed")
            inspection = self._inspect(binding, preparing=True)
            binding["inspection_ref"] = inspection["presentation_id"]
            binding.update({k: inspection[k] for k in REVIEW_AUTHORITY if k in inspection})
            journal = ReconciliationJournal(self.directory, self.organization)
            with journal.db() as db:
                epoch = db.execute("SELECT value FROM installation WHERE key='epoch'").fetchone()[0]
                previous = db.execute(
                    "SELECT request FROM requests WHERE epoch!=?", (epoch,)
                ).fetchall()
            require(
                not any(
                    all(
                        json.loads(row[0]).get(k) == binding[k]
                        for k in ("run_id", "run_revision", "artifact_sha", "inspection_ref")
                    )
                    for row in previous
                ),
                "fresh_epoch_presentation_required",
            )
            scope = {k: v for k, v in binding.items() if k != "inspection_ref"}
            prefix = "za-" + _sig(key, REQUEST + "/scope", scope)[:16]
            now = int(time.time())
            expires = min(p["expires_at"], inspection["expires_at"], now + ttl_seconds)
            rid = prefix + _sig(key, REQUEST + "/presentation", binding)[:16]
            prior = None
            for known in self.directory.glob(f"zapier-approval-{prefix}*-request.json"):
                prior_id = known.name.removeprefix("zapier-approval-").removesuffix("-request.json")
                candidate = self._request(prior_id, p, key, fresh=False)
                require(all(candidate[k] == v for k, v in scope.items()), "request_scope_mismatch")
                if os.path.lexists(self._path(prior_id, "reconciled")):
                    continue
                if candidate["expires_at"] > now:
                    self._inspect(candidate)
                    prior = candidate
                    expires = prior["expires_at"]
                    rid = prior_id
                    break
            if p["schema_version"] == RECIPE_POLICY:
                work, _ = release_gate._binding(
                    self.directory, self.organization, run_id, run_revision, artifact_sha
                )
                require(not (work / "release-decision.json").exists(), "review_already_decided")
                if confirmed_preview is not None and prior is None:
                    pinned = confirmed_preview.get("expires_at")
                    require(type(pinned) is int and now < pinned <= expires, "preview_expired")
                    expires = pinned
                preview = {
                    "request_id": rid,
                    "run_revision": run_revision,
                    "artifact_sha": artifact_sha,
                    "audience": p["audience"],
                    "operation": "evaluation handoff only",
                    "fallback": "keep the incumbent",
                    "expires_at": expires,
                    "policy_revision": digest(p),
                }
                if preview_only:
                    return preview
                require(preview == confirmed_preview, "confirmed_preview_required")
            if prior is not None:
                return self._wire(prior, key, p)
            path = self._path(rid, "request")
            if not os.path.lexists(path):
                self._assert_no_unknown_proposal(binding)
                req = binding | {
                    "schema_version": REQUEST,
                    "request_id": rid,
                    "nonce": secrets.token_hex(32),
                    "issued_at": now,
                    "expires_at": expires,
                }
                _record(path, {"request": req, "seal": _sig(key, REQUEST, req)})
            req = self._request(rid, p, key)
        return self._wire(req, key, p)

    def flush(self, request_id: str, hook: str, post: Callable[[str, Json], int] = _post) -> Json:
        with self._serialized() as journal:
            self._assert_installed_policy(journal)
            require(journal.installation()["paused"] == "false", "approval_dispatch_paused")
            return self._flush(request_id, hook, post)

    def _flush(self, request_id: str, hook: str, post: Callable[[str, Json], int]) -> Json:
        p, key = self._policy()
        require(hook_fingerprint(hook) == p["hook_fingerprint"], "plugin_destination_mismatch")
        path = self._path(request_id, "request")
        _check_file(path)
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            req = self._request(request_id, p, key)
            self._inspect(req)
            final = self._path(request_id, "transport")
            admitted = self._path(request_id, "admitted")
            if os.path.lexists(final):
                return _read_private(final, 1024)
            if os.path.lexists(admitted):
                return {"request_id": request_id, "transport": "uncertain"}
            store = LocalTrainingStore(
                self.directory / "training.sqlite3", self.organization, existing_only=True
            )
            with store.engagement_guard(engagement_id=p["engagement"]):
                self._inspect(req)
                current = _read_private(self.config, 16_384)
                require(current == p, "plugin_policy_changed")
                require(int(time.time()) < req["expires_at"], "request_expired")
                _record(admitted, {"request_id": request_id, "admitted_at": int(time.time())})
                dir_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
                if int(time.time()) >= req["expires_at"]:
                    _record(
                        self._path(request_id, "not-sent"),
                        {"request_id": request_id, "reason": "request_expired_before_submission"},
                    )
                    directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                    raise ApprovalError("request_expired")
                try:
                    status = post(hook, self._wire(req, key, p))
                    disposition = "accepted" if 200 <= status < 300 else "failed"
                except (OSError, http.client.HTTPException):
                    disposition = "uncertain"
            receipt = {"request_id": request_id, "transport": disposition}
            _record(final, receipt)
            return receipt

    def flush_ready(self, hook: str, post: Callable[[str, Json], int] = _post) -> Json:
        """One supervised pass. Uncertain submissions need the original downstream receipt."""
        self._policy()
        sent = held = attempted = 0
        for path in sorted(self.directory.glob("zapier-approval-za-*-request.json")):
            request_id = path.name.removeprefix("zapier-approval-").removesuffix("-request.json")
            if not REQ.fullmatch(request_id) or os.path.lexists(self._path(request_id, "admitted")):
                continue
            if attempted >= 25:
                break
            try:
                receipt = self.flush(request_id, hook, post)
            except (ValueError, OSError, sqlite3.Error):
                held += 1
                continue
            attempted += 1
            sent += int(receipt["transport"] == "accepted")
            held += int(receipt["transport"] != "accepted")
        return {"accepted": sent, "held": held}

    def consume(self, envelope: Json) -> Json:
        with self._serialized() as journal:
            try:
                result, request, policy = self._consume(envelope)
            except ApprovalError as error:
                if str(error) == "conflicting_provider_event":
                    request_id = envelope["event"]["request_id"]
                    if journal.request_state(request_id) is not None:
                        event = envelope["event"]
                        _record(
                            self._path(request_id, "conflict-" + digest(event)[:16]),
                            {
                                "request_id": request_id,
                                "event_sha256": digest(event),
                                "status": event["status"],
                                "observed_at": int(time.time()),
                            },
                        )
                        journal.transition(request_id, "held", str(error), event["status"])
                raise
            if result["state"] == "pending":
                require(int(time.time()) < request["expires_at"], "request_expired")
            # A terminal receipt admitted by the effect writer remains terminal if
            # the request clock expires before bookkeeping. Reuse its verified scope.
            journal.record(request, policy)
            journal.transition(
                result["request_id"], result["state"], None, envelope["event"]["status"]
            )
            return result

    def _consume(self, envelope: Json) -> tuple[Json, Json, Json]:
        p, key = self._policy()
        require(
            set(envelope) == {"event", "signature"}
            and isinstance(envelope["event"], dict)
            and isinstance(envelope["signature"], str)
            and HEX.fullmatch(envelope["signature"]),
            "invalid_provider_envelope",
        )
        event = envelope["event"]
        require(
            set(event) == FIELDS
            and event["schema_version"] == EVENT
            and hmac.compare_digest(envelope["signature"], _sig(key, EVENT, event)),
            "provider_signature_invalid",
        )
        request_id = event["request_id"]
        path = self._path(request_id, "request")
        _check_file(path)
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            req = self._request(request_id, p, key)
            _check_file(self._path(request_id, "admitted"))
            require(
                event["nonce"] == req["nonce"]
                and event["zap_id"] == p["zap_id"]
                and event["zap_version"] == p["zap_version"],
                "provider_scope_mismatch",
            )
            require(
                type(event["decided_at"]) is int
                and req["issued_at"] <= event["decided_at"] < req["expires_at"]
                and event["decided_at"] <= int(time.time())
                and isinstance(event["event_id"], str)
                and OPAQUE.fullmatch(event["event_id"]),
                "invalid_provider_event",
            )
            require(
                event["status"] in ("Pending", "Approved", "Declined"), "invalid_provider_status"
            )
            self._inspect(req)
            with ReconciliationJournal(self.directory, self.organization).db() as db:
                prior = db.execute(
                    "SELECT provider_status FROM requests WHERE id=?", (request_id,)
                ).fetchone()
                require(
                    prior is None
                    or prior[0] not in {"Approved", "Declined"}
                    or prior[0] == event["status"],
                    "conflicting_provider_event",
                )
            if event["status"] == "Pending":
                return {"request_id": request_id, "state": "pending"}, req, p
            require(
                event["actor_id"] == p["reviewer"]
                or (event["actor_id"] is None and p["approver_mode"] == "verified_single_approver"),
                "named_approver_unverified",
            )
            current_policy, current_key = self._policy()
            require(
                current_policy == p and hmac.compare_digest(current_key, key),
                "plugin_policy_changed",
            )
            decision = "approved" if event["status"] == "Approved" else "changes_requested"
            reservation = self._path(request_id, "event")
            identity = digest({k: v for k, v in event.items() if k != "decided_at"})
            stamp = {
                "event_identity": identity,
                "first_event_sha256": digest(event),
                "request_id": request_id,
            }

            if os.path.lexists(reservation):
                require(
                    _read_private(reservation, 1024).get("event_identity") == identity,
                    "conflicting_provider_event",
                )
            else:
                _record(reservation, stamp)
            receipt = release_gate.record_release(
                self.directory,
                self.organization,
                **{k: req[k] for k in BIND},
                decision=decision,
                provider="zapier_slack",
                provider_request_expires_at=req["expires_at"],
            )
            return (
                {
                    "request_id": request_id,
                    "state": decision,
                    "release_receipt_sha256": digest(receipt),
                },
                req,
                p,
            )

    def import_ready(self) -> Json:
        """Drain signed files from the customer's private, preconfigured return transport."""
        self._policy()
        inbox = self.directory / "zapier-decisions"
        if not os.path.lexists(inbox):
            return {"processed": 0, "held": 0}
        _check_directory(inbox / "entry.json")
        processed = held = 0
        for path in sorted(inbox.glob("za-*.json")):
            match = re.fullmatch(r"(za-[a-f0-9]{32})\.(pending|approved|declined)\.json", path.name)
            if not match:
                continue
            request_id, status = match.groups()
            marker = self._path(request_id, "import-" + status)
            try:
                envelope = _read_private(path, 16_384)
                message_hash = digest(envelope)
                prior = _read_private(marker, 1024) if os.path.lexists(marker) else None
                if prior and prior["envelope_sha256"] == message_hash:
                    continue
                event = envelope.get("event")
                require(
                    isinstance(event, dict)
                    and event.get("request_id") == request_id
                    and event.get("status") == status.capitalize(),
                    "return_filename_mismatch",
                )
                self.consume(envelope)
                if not prior:
                    _record(marker, {"envelope_sha256": message_hash})
                processed += 1
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                held += 1
                continue
            if processed >= 25:
                break
        return {"processed": processed, "held": held}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--config", type=Path, required=True)
    actions = parser.add_subparsers(dest="operation", required=True)
    actions.add_parser("check-config")
    issue = actions.add_parser("issue")
    for name in ("run-id", "run-revision", "artifact-sha", "inspection-ref"):
        issue.add_argument("--" + name, required=True)
    issue.add_argument("--ttl-seconds", type=int, default=900)
    selector = actions.add_parser("flush").add_mutually_exclusive_group(required=True)
    selector.add_argument("--request-id")
    selector.add_argument("--all", action="store_true")
    actions.add_parser("import").add_argument("--input", type=Path, required=True)
    actions.add_parser("import-ready")
    pending = actions.add_parser("pending-summary")
    pending.add_argument("--limit", type=int, default=25)
    pending.add_argument("--cursor")
    reconcile = actions.add_parser("reconcile")
    reconcile.add_argument("--request-id")
    reconcile.add_argument("--poll-interval", type=int, default=60)
    reconcile.add_argument("--reminder-interval", type=int, default=3600)
    actions.add_parser("prepare-cutover")
    actions.add_parser("complete-cutover")
    replacements = actions.add_parser("reconcile-reviewers")
    replacements.add_argument("--authority", type=Path, required=True)
    replacements.add_argument("--actor", required=True)
    replacements.add_argument("--limit", type=int, default=50)
    adoption = actions.add_parser("complete-reviewer-replacement")
    adoption.add_argument("--authority", type=Path, required=True)
    adoption.add_argument("--actor", required=True)
    actions.add_parser("observe-replaced-status").add_argument("--input", type=Path, required=True)
    actions.add_parser("observe-fenced-status").add_argument("--input", type=Path, required=True)
    recovery = actions.add_parser("recover-request-authority")
    recovery.add_argument("--request-id", required=True)
    recovery.add_argument("--original-policy", type=Path, required=True)
    recovery.add_argument("--original-key", type=Path)
    actions.add_parser("claim-notification")
    actions.add_parser("confirm-notification").add_argument("--notification-ref", required=True)
    args = parser.parse_args()
    try:
        bridge = ApprovalBridge(args.directory, args.organization, args.config)
        if args.operation == "check-config":
            value = bridge.check()
        elif args.operation == "issue":
            value = bridge.issue(
                run_id=args.run_id,
                run_revision=args.run_revision,
                artifact_sha=args.artifact_sha,
                inspection_ref=args.inspection_ref,
                ttl_seconds=args.ttl_seconds,
            )
        elif args.operation == "flush":
            hook = os.environ.get("OPENTOUR_ZAPIER_APPROVAL_HOOK_URL", "")
            value = bridge.flush_ready(hook) if args.all else bridge.flush(args.request_id, hook)
        elif args.operation == "import-ready":
            value = bridge.import_ready()
        elif args.operation == "pending-summary":
            value = bridge.pending_summary(limit=args.limit, cursor=args.cursor)
        elif args.operation == "reconcile":
            value = bridge.reconcile(
                request_id=args.request_id,
                poll_interval=args.poll_interval,
                reminder_interval=args.reminder_interval,
            )
        elif args.operation == "prepare-cutover":
            value = bridge.prepare_cutover()
        elif args.operation == "complete-cutover":
            value = bridge.complete_cutover()
        elif args.operation == "reconcile-reviewers":
            value = bridge.reconcile_reviewers(
                authority=args.authority, actor=args.actor, limit=args.limit
            )
        elif args.operation == "complete-reviewer-replacement":
            value = bridge.complete_reviewer_replacement(authority=args.authority, actor=args.actor)
        elif args.operation == "observe-replaced-status":
            require(args.input.is_absolute(), "private_signed_decision_required")
            value = bridge.observe_replaced_status(_read_private(args.input, 16_384))
        elif args.operation == "observe-fenced-status":
            require(args.input.is_absolute(), "private_signed_decision_required")
            value = bridge.observe_fenced_status(_read_private(args.input, 16_384))
        elif args.operation == "recover-request-authority":
            value = bridge.recover_request_authority(
                args.request_id,
                original_policy=args.original_policy,
                original_key=args.original_key,
            )
        elif args.operation == "claim-notification":
            value = {"notification": bridge.claim_notification()}
        elif args.operation == "confirm-notification":
            value = bridge.confirm_notification(args.notification_ref)
        else:
            require(args.input.is_absolute(), "private_signed_decision_required")
            value = bridge.consume(_read_private(args.input, 16_384))
        print(canonical(value))
        return 0
    except ApprovalError as error:
        print(str(error), file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        print("approval_unavailable", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
