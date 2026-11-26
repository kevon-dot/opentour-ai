"""Bridge an explicitly configured Zapier Slack Request Approval to the owner-local release gate."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import sqlite3
import sys
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
    ) -> None:
        now = int(time.time())
        with self.db() as db:
            row = db.execute(
                "SELECT state,provider_status FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            require(row is not None, "request_not_in_reconciliation_inventory")
            if provider not in {"Pending", "Approved", "Declined"}:
                provider = row[1]
            # A conflict or later outage must not rewrite a completed human decision.
            if row[0] in {"approved", "changes_requested"}:
                state = row[0]
            db.execute(
                "UPDATE requests SET state=?,reason=?,provider_status=?,observed_at=?,"
                "retries=retries+? WHERE id=?",
                (state, reason, provider, now, int(reason == "remote_outcome_unknown"), request_id),
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
    def _serialized(self) -> Iterator[ReconciliationJournal]:
        self._policy()
        path = self.directory / "zapier-reconciliation.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "rb") as lock:
            _check_file(path)
            fcntl.flock(lock, fcntl.LOCK_EX)
            policy, key = self._policy()
            journal = ReconciliationJournal(self.directory, self.organization)
            self._inventory(journal, policy, key)
            journal.pin_installation(policy, key)
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
            _record(marker, {"request_id": request_id, "reason": reason})
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    def _inventory(self, journal: ReconciliationJournal, policy: Json, key: bytes) -> None:
        for path in sorted(self.directory.glob("zapier-approval-za-*-request.json")):
            request_id = path.name.removeprefix("zapier-approval-").removesuffix("-request.json")
            if journal.request_state(request_id) is None:
                journal.record(self._request(request_id, policy, key, fresh=False), policy)
            marker = self._path(request_id, "reconciled")
            if os.path.lexists(marker):
                closed = _read_private(marker, 1024)
                reason = closed.get("reason")
                states = {
                    "version_superseded": "superseded",
                    "request_expired": "expired",
                    "current_authority_unavailable": "held",
                }
                require(
                    closed.get("request_id") == request_id
                    and isinstance(reason, str)
                    and reason in states,
                    "invalid_reconciliation_marker",
                )
                assert isinstance(reason, str)
                # The marker fences callbacks first. A crash before the DB commit is recoverable.
                journal.transition(request_id, states[reason], reason, "reconciled")

    def pending_summary(self, *, limit: int = 25, cursor: str | None = None) -> Json:
        """Owner-local surface; an installed adapter must derive tenant from its connection."""
        with self._serialized() as journal:
            policy, key = self._policy()
            self._inventory(journal, policy, key)
            return journal.page(key, limit=limit, cursor=cursor)

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
            policy, key = self._policy()
            self._inventory(journal, policy, key)
            now = int(time.time())
            with journal.db() as db:
                if request_id is not None:
                    require(
                        isinstance(request_id, str)
                        and REQ.fullmatch(request_id)
                        and db.execute(
                            "SELECT 1 FROM requests WHERE id=?", (request_id,)
                        ).fetchone()
                        is not None,
                        "request_not_in_reconciliation_inventory",
                    )
                poll_key = "last_poll" if request_id is None else "last_poll:" + request_id
                db.execute("INSERT OR IGNORE INTO installation VALUES (?,'0')", (poll_key,))
                last = int(
                    db.execute(
                        "SELECT value FROM installation WHERE key=?", (poll_key,)
                    ).fetchone()[0]
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
                        target_id, "held", "local_state_unavailable", row["provider_status"]
                    )
                    continue
                except ValueError:
                    self._invalidate(target_id, "current_authority_unavailable")
                    journal.transition(
                        target_id, "held", "current_authority_unavailable", "unknown"
                    )
                    continue
                state = status["state"]
                transport = self._path(target_id, "transport")
                admitted = self._path(target_id, "admitted")
                unknown = (
                    row["provider_status"] not in {"Pending", "Approved", "Declined"}
                    and os.path.lexists(admitted)
                    and (
                        not os.path.lexists(transport)
                        or _read_private(transport, 1024).get("transport") != "accepted"
                    )
                )
                if state in {"approved", "changes_requested"}:
                    journal.transition(target_id, state, None, state)
                elif unknown:
                    journal.transition(target_id, "held", "remote_outcome_unknown", "unknown")
                elif now >= req["expires_at"]:
                    self._invalidate(target_id, "request_expired")
                    journal.transition(target_id, "expired", "request_expired", "expired")
                else:
                    if state == "uncertain" and row["provider_status"] == "Pending":
                        state = "accepted"
                    journal.transition(
                        target_id,
                        "pending",
                        None,
                        row["provider_status"] if row["provider_status"] == "Pending" else state,
                        reminder_interval=reminder_interval if state == "accepted" else None,
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
                    "SELECT id,state,provider_status FROM requests ORDER BY id"
                ).fetchall()
            for row in rows:
                if row["state"] in {"approved", "changes_requested", "superseded", "expired"}:
                    continue
                request_id = row["id"]
                try:
                    status = self.decision_status(
                        request_id, team=policy["team"], actor=policy["reviewer"]
                    )["state"]
                except (OSError, ValueError, KeyError, sqlite3.Error):
                    status = "uncertain"
                if status in {"approved", "changes_requested"}:
                    journal.transition(request_id, status, None, status)
                elif row["provider_status"] != "Pending" and (
                    status in {"uncertain", "failed"}
                    or (
                        os.path.lexists(self._path(request_id, "admitted"))
                        and (
                            not os.path.lexists(self._path(request_id, "transport"))
                            or _read_private(self._path(request_id, "transport"), 1024).get(
                                "transport"
                            )
                            != "accepted"
                        )
                    )
                ):
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
                "SELECT id,request,provider_status FROM requests "
                "WHERE state NOT IN ('approved','changes_requested')"
            ).fetchall()
        for row in prior:
            request = json.loads(row["request"])
            if not all(
                request[name] == binding[name]
                for name in ("run_id", "run_revision", "artifact_sha")
            ):
                continue
            admitted = self._path(row["id"], "admitted")
            transport = self._path(row["id"], "transport")
            known = row["provider_status"] in {"Pending", "Approved", "Declined"}
            unknown = (
                not known
                and os.path.lexists(admitted)
                and (
                    not os.path.lexists(transport)
                    or _read_private(transport, 1024).get("transport") != "accepted"
                )
            )
            require(not unknown, "remote_reconciliation_required")

    def confirm_notification(self, notification_ref: str) -> Json:
        with self._serialized() as journal:
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

    def _policy(self) -> tuple[Json, bytes]:
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
            type(p["expires_at"]) is int and time.time() < p["expires_at"] < 2**63,
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
        key = _read_private(self.directory / p["key_file"], 1024)
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
        self, *, run_id: str, run_revision: str, artifact_sha: str, team: str, actor: str
    ) -> Json | None:
        policy, key = self._policy()
        require(team == policy["team"] and actor == policy["reviewer"], "reviewer_required")
        scope = {
            "run_id": run_id,
            "run_revision": run_revision,
            "artifact_sha": artifact_sha,
            **{k: policy[k] for k in ("engagement", "team", "channel", "reviewer", "audience")},
            "policy_revision": digest(policy),
        }
        prefix = "za-" + _sig(key, REQUEST + "/scope", scope)[:16]
        for path in self.directory.glob(f"zapier-approval-{prefix}*-request.json"):
            rid = path.name.removeprefix("zapier-approval-").removesuffix("-request.json")
            req = self._request(rid, policy, key, fresh=False)
            require(all(req[k] == v for k, v in scope.items()), "request_scope_mismatch")
            if req["expires_at"] > int(time.time()) or self._path(rid, "event").exists():
                return self.decision_status(rid, team=team, actor=actor)
        return None

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

    def _path(self, request_id: str, kind: str) -> Path:
        require(isinstance(request_id, str) and REQ.fullmatch(request_id), "invalid_request_id")
        return self.directory / f"zapier-approval-{request_id}-{kind}.json"

    def _inspect(self, req: Json) -> Json:
        inspection = release_gate.check_release_inspection(
            self.directory, self.organization, **{k: req[k] for k in BIND}
        )
        require(
            inspection["provider"] == "zapier_slack"
            and inspection["engagement"] == req["engagement"],
            "plugin_not_selected",
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
            inspection = self._inspect(binding)
            binding["inspection_ref"] = inspection["presentation_id"]
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
                _record(admitted, {"request_id": request_id, "admitted_at": int(time.time())})
                dir_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
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
                result = self._consume(envelope)
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
            policy, key = self._policy()
            journal.record(self._request(result["request_id"], policy, key), policy)
            journal.transition(
                result["request_id"], result["state"], None, envelope["event"]["status"]
            )
            return result

    def _consume(self, envelope: Json) -> Json:
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
            if event["status"] == "Pending":
                return {"request_id": request_id, "state": "pending"}
            require(
                event["actor_id"] == p["reviewer"]
                or (event["actor_id"] is None and p["approver_mode"] == "verified_single_approver"),
                "named_approver_unverified",
            )
            self._inspect(req)
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
            )
            return {
                "request_id": request_id,
                "state": decision,
                "release_receipt_sha256": digest(receipt),
            }

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
