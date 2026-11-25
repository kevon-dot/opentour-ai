"""Owner-local admission, execution and aggregate-only lookup for a frozen evaluation."""

from __future__ import annotations

import fcntl
import os
import re
import secrets
import sqlite3
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.receipt_recovery import _open_db
from opentour_cell.pilot.slack_adapter import Refused
from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.training import _check_directory, _check_file, _initialization_guard
from opentour_cell.pilot.training_data import canonical, digest, identifier

PLAN_VERSION = "opentour.pilot.managed-repair.v1"
QUEUE_FILE = "managed-runs.sqlite3"
RESOURCE = "owner_local_cpu"
TARGET_MS = 2 * 3600 * 1000
HEX = re.compile(r"[0-9a-f]{64}")
RUN_ID = re.compile(r"run-[0-9a-f]{32}")
KEY = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_:.\-]{0,95}")
OUTCOMES = {"simple_repair", "model_candidate", "no_change", "hold"}
Inspect = Callable[[Path, str, dict[str, Any]], dict[str, Any]]
Execute = Callable[[Path, str, dict[str, Any], str], dict[str, Any]]

_SCHEMA = """
CREATE TABLE owner (singleton INTEGER PRIMARY KEY CHECK(singleton=1), org TEXT, engagement TEXT);
CREATE TABLE runs (
 id TEXT PRIMARY KEY, revision TEXT UNIQUE NOT NULL, grant_id TEXT NOT NULL, state TEXT NOT NULL,
 ready_ms INTEGER NOT NULL, accepted_ms INTEGER NOT NULL, started_ms INTEGER, finished_ms INTEGER,
 result_ms INTEGER, wait_seconds INTEGER, claim TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 reason TEXT, case_count INTEGER, result_revision TEXT, outcome TEXT
);
CREATE TABLE requests (key_hash TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id));
"""


class ManagedError(ValueError):
    """Only fixed public reason codes may cross the local MCP boundary."""


def plan_revision(manifest: dict[str, Any]) -> str:
    return digest(manifest)


def _now() -> int:
    return time.time_ns() // 1_000_000


def _safe_result(result: dict[str, Any]) -> tuple[str, int, str]:
    if not isinstance(result, dict) or set(result) != {"result_revision", "case_count", "outcome"}:
        raise ManagedError("invalid_worker_result")
    revision, count, outcome = result["result_revision"], result["case_count"], result["outcome"]
    if (
        not isinstance(revision, str)
        or not HEX.fullmatch(revision)
        or type(count) is not int
        or not 0 <= count <= 1_000_000
        or not isinstance(outcome, str)
        or outcome not in OUTCOMES
    ):
        raise ManagedError("invalid_worker_result")
    return revision, count, outcome


_PUBLIC_HOLDS = {
    "engagement_unavailable",
    "source_grant_withdrawn",
    "source_grant_expired",
    "source_permission_required",
    "source_grant_unavailable",
    "open_weight_grant_required",
    "open_weight_grant_scope_mismatch",
    "model_grant_required",
    "model_grant_expired",
    "model_permission_required",
    "learning_permission_required",
    "protected_cohort_consumed",
    "fresh_heldout_required",
    "protected_plan_conflict",
    "model_execution_uncertain",
    "feature_not_authorized",
}


def _hold(error: Exception, fallback: str) -> str:
    code = str(error)
    return code if code in _PUBLIC_HOLDS else fallback


def _projection(row: sqlite3.Row) -> dict[str, Any]:
    # A malformed/restored journal must not disclose user-controlled strings as status or timing.
    if (
        not isinstance(row["id"], str)
        or not RUN_ID.fullmatch(row["id"])
        or not isinstance(row["revision"], str)
        or not HEX.fullmatch(row["revision"])
        or row["state"] not in {"queued", "running", "ready", "held", "failed"}
        or row["reason"]
        not in _PUBLIC_HOLDS
        | {
            None,
            "workflow_constraints_unmet",
            "evaluation_held",
            "invalid_worker_result",
            "worker_failed",
        }
        or any(type(row[k]) is not int or row[k] < 0 for k in ("ready_ms", "accepted_ms"))
        or any(
            row[k] is not None and (type(row[k]) is not int or row[k] < 0)
            for k in ("started_ms", "result_ms", "finished_ms", "wait_seconds")
        )
    ):
        raise ManagedError("private_state_unavailable")
    ready, accepted, start, result = (
        row[k] for k in ("ready_ms", "accepted_ms", "started_ms", "result_ms")
    )
    elapsed = None
    if result is not None and start is not None and ready <= accepted <= start <= result:
        elapsed = result - ready
    result_data = None
    if row["result_revision"] is not None:
        result_data = {k: row[k] for k in ("result_revision", "case_count", "outcome")}
        _safe_result(result_data)
    return {
        "run_id": row["id"],
        "state": row["state"],
        "plan_revision": row["revision"],
        "reason": row["reason"],
        "result": result_data,
        "resource_class": RESOURCE,
        "timing": {
            "timezone": "UTC",
            "prerequisites_ready_unix_ms": ready,
            "accepted_unix_ms": accepted,
            "execution_started_unix_ms": start,
            "finished_unix_ms": row["finished_ms"],
            "first_result_unix_ms": result,
            "ready_to_result_ms": elapsed,
            "target_upper_ms": TARGET_MS,
            "target_met": None if elapsed is None else elapsed <= TARGET_MS,
            "customer_wait_seconds": row["wait_seconds"],
        },
    }


class ManagedSelection:
    def __init__(
        self,
        directory: Path,
        organization: str,
        manifest: Path,
        authorization: Path,
        *,
        prerequisites: Inspect | None = None,
    ):
        if (
            not directory.is_absolute()
            or directory.resolve(strict=True) != directory
            or manifest.parent != directory
            or authorization.parent != directory
            or manifest == authorization
            or any(p.suffix != ".json" for p in (manifest, authorization))
        ):
            raise ManagedError("private_selection_required")
        self.directory = directory
        self.organization = identifier(organization)
        self.manifest = manifest
        self.authorization = authorization
        self.prerequisites = prerequisites
        self.path = directory / QUEUE_FILE
        _check_directory(self.path)

    def read(self) -> dict[str, Any]:
        try:
            data = _read_private(self.manifest, 256 * 1024)
        except (OSError, ValueError):
            raise ManagedError("private_plan_unavailable") from None
        if (
            data.get("schema_version") != PLAN_VERSION
            or data.get("organization") != self.organization
            or not isinstance(data.get("intake_revision"), str)
            or not HEX.fullmatch(data["intake_revision"])
        ):
            raise ManagedError("plan_scope_mismatch")
        try:
            identifier(data.get("engagement"))
        except TrainingError:
            raise ManagedError("plan_scope_mismatch") from None
        return data

    @contextmanager
    def domain(self, manifest: dict[str, Any], *, write: bool = False) -> Iterator[None]:
        # Keep custody before training, including when the working store was restored.
        try:
            with _open_db(self.directory / "custody.sqlite3", reserve=write) as custody:
                if custody is not None:
                    rows = custody.execute(
                        "SELECT organization,engagement,withdrawn_at FROM closeout LIMIT 2"
                    ).fetchall()
                    if rows:
                        raise ManagedError("engagement_withdrawn")
                with _open_db(self.directory / "training.sqlite3", reserve=write) as training:
                    if training is not None:
                        owner = training.execute(
                            "SELECT organization FROM owner WHERE singleton=1"
                        ).fetchone()
                        scope = training.execute(
                            "SELECT id,withdrawn_at FROM engagement WHERE singleton=1"
                        ).fetchone()
                        if (
                            not owner
                            or owner[0] != self.organization
                            or not scope
                            or scope[0] != manifest["engagement"]
                        ):
                            raise ManagedError("engagement_unavailable")
                        if scope[1] is not None:
                            raise ManagedError("engagement_withdrawn")
                    yield
        except (OSError, sqlite3.Error, Refused, TrainingError):
            raise ManagedError("private_state_unavailable") from None

    def _binding(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "opentour.pilot.managed-grant.v1",
            "scope": "start_local_evaluation",
            "directory_binding": digest(str(self.directory)),
            "organization": self.organization,
            "engagement": data["engagement"],
            "plan_revision": plan_revision(data),
            "intake_revision": data["intake_revision"],
            "resource_class": RESOURCE,
        }

    def inspect(self, inspect: Inspect) -> tuple[dict[str, Any], str]:
        data = self.read()
        with self.domain(data):
            pass
        try:
            info = inspect(self.directory, self.organization, data)
            if info.get("state") != "ready":
                raise ManagedError("workflow_prerequisites_unavailable")
        except (ValueError, OSError, sqlite3.Error) as error:
            raise ManagedError(_hold(error, "workflow_prerequisites_unavailable")) from None
        return data, plan_revision(data)

    def current(self, data: dict[str, Any]) -> None:
        # Inspect only the selected intake's current authority before entering a queue transaction.
        with self.domain(data):
            self.grant(data)
        inspect: Inspect
        if self.prerequisites is None:
            from opentour_cell.pilot.repair_workflow import inspect_managed

            inspect = inspect_managed
        else:
            inspect = self.prerequisites
        try:
            if inspect(self.directory, self.organization, data).get("state") != "ready":
                raise ManagedError("workflow_prerequisites_unavailable")
        except (ValueError, OSError, sqlite3.Error) as error:
            raise ManagedError(_hold(error, "workflow_prerequisites_unavailable")) from None

    def check_source_under_guard(self, data: dict[str, Any]) -> None:
        # Source withdrawal uses the same training transaction held by our caller.
        if self.prerequisites is None and (self.directory / "training.sqlite3").exists():
            from opentour_cell.pilot.source_intake import check_current_intake

            try:
                check_current_intake(
                    self.directory, self.organization, data["engagement"], data["intake_revision"]
                )
            except (ValueError, OSError, sqlite3.Error) as error:
                raise ManagedError(_hold(error, "workflow_prerequisites_unavailable")) from None

    def authorize(
        self,
        revision: str,
        attestation: str,
        expiry: int,
        customer_wait: int | None,
        inspect: Inspect,
    ) -> None:
        data, current = self.inspect(inspect)
        if revision != current:
            raise ManagedError("plan_revision_mismatch")
        if not re.fullmatch(r"att-[a-f0-9]{16,64}", attestation):
            raise ManagedError("invalid_custodian_attestation")
        if customer_wait is not None and (
            type(customer_wait) is not int or not 0 <= customer_wait <= 315_576_000
        ):
            raise ManagedError("invalid_customer_wait")
        with self.domain(data, write=True):
            self.check_source_under_guard(data)
            now = _now()
            if type(expiry) is not int or not now // 1000 < expiry <= now // 1000 + 30 * 86400:
                raise ManagedError("invalid_grant_expiry")
            if self.read() != data:
                raise ManagedError("plan_revision_mismatch")
            grant = self._binding(data) | {
                "custodian_attestation": attestation,
                "expires_at": expiry,
                "ready_ms": now,
                "customer_wait_seconds": customer_wait,
            }
            with tempfile.NamedTemporaryFile(dir=self.directory) as staging:
                staging.write((canonical(grant) + "\n").encode())
                staging.flush()
                os.fsync(staging.fileno())
                try:
                    os.link(staging.name, self.authorization)
                except FileExistsError:
                    raise ManagedError("authorization_already_exists") from None

    def grant(self, data: dict[str, Any]) -> dict[str, Any]:
        try:
            grant = _read_private(self.authorization, 4096)
        except FileNotFoundError:
            raise ManagedError("authorization_required") from None
        except (OSError, ValueError):
            raise ManagedError("authorization_invalid") from None
        binding = self._binding(data)
        if set(grant) != set(binding) | {
            "expires_at",
            "custodian_attestation",
            "ready_ms",
            "customer_wait_seconds",
        } or any(grant[k] != value for k, value in binding.items()):
            raise ManagedError("authorization_scope_mismatch")
        now, ready, expires, wait = (
            _now(),
            grant["ready_ms"],
            grant["expires_at"],
            grant["customer_wait_seconds"],
        )
        if (
            type(expires) is not int
            or type(ready) is not int
            or not 0 < ready <= now
            or not 0 < expires * 1000 - ready <= 30 * 86_400_000
            or not isinstance(grant["custodian_attestation"], str)
            or not re.fullmatch(r"att-[0-9a-f]{16,64}", grant["custodian_attestation"])
            or (wait is not None and (type(wait) is not int or not 0 <= wait <= 315_576_000))
        ):
            raise ManagedError("authorization_invalid")
        if now >= expires * 1000:
            raise ManagedError("authorization_expired")
        return grant

    def _create_queue(self, data: dict[str, Any]) -> None:
        if os.path.lexists(self.path):
            return
        if any(os.path.lexists(str(self.path) + s) for s in ("-journal", "-wal", "-shm")):
            raise ManagedError("private_state_unavailable")
        with tempfile.NamedTemporaryFile(dir=self.directory) as staging:
            with closing(sqlite3.connect(staging.name)) as db, db:
                db.executescript(_SCHEMA)
                db.execute(
                    "INSERT INTO owner VALUES (1,?,?)", (self.organization, data["engagement"])
                )
            os.link(staging.name, self.path)

    @contextmanager
    def queue(
        self, data: dict[str, Any], *, write: bool = False, create: bool = False
    ) -> Iterator[tuple[sqlite3.Connection, dict[str, Any]]]:
        with _initialization_guard(self.path, creator=create), self.domain(data, write=write):
            grant = self.grant(data)
            self.check_source_under_guard(data)
            if self.read() != data:
                raise ManagedError("plan_revision_mismatch")
            if create:
                self._create_queue(data)
            with _open_db(self.path, reserve=write) as db:
                if db is None:
                    raise ManagedError("run_unavailable")
                owner = db.execute("SELECT org,engagement FROM owner WHERE singleton=1").fetchone()
                if not owner or tuple(owner) != (self.organization, data["engagement"]):
                    raise ManagedError("engagement_unavailable")
                yield db, grant
                if write:
                    self.check_source_under_guard(data)
                if write and (self.grant(data) != grant or self.read() != data):
                    raise ManagedError("authorization_changed")

    def start(self, revision: Any, key: Any) -> dict[str, Any]:
        if (
            not isinstance(revision, str)
            or not HEX.fullmatch(revision)
            or not isinstance(key, str)
            or not KEY.fullmatch(key)
        ):
            raise ManagedError("invalid_start_arguments")
        data = self.read()
        if revision != plan_revision(data):
            raise ManagedError("plan_revision_mismatch")
        self.current(data)
        with self.queue(data, write=True, create=True) as (db, grant):
            hashed = digest(key)
            prior = db.execute(
                "SELECT r.* FROM requests k JOIN runs r ON r.id=k.run_id WHERE k.key_hash=?",
                (hashed,),
            ).fetchone()
            if prior is not None and prior["revision"] != revision:
                raise ManagedError("idempotency_conflict")
            row = db.execute("SELECT * FROM runs WHERE revision=?", (revision,)).fetchone()
            grant_id = digest(grant)
            if row is None:
                if db.execute("SELECT count(*) FROM runs").fetchone()[0] >= 1000:
                    raise ManagedError("local_capacity_reached")
                run = "run-" + secrets.token_hex(16)
                db.execute(
                    "INSERT INTO runs (id,revision,grant_id,state,ready_ms,"
                    "accepted_ms,wait_seconds) "
                    "VALUES (?,?,?,'queued',?,?,?)",
                    (
                        run,
                        revision,
                        grant_id,
                        grant["ready_ms"],
                        _now(),
                        grant["customer_wait_seconds"],
                    ),
                )
            else:
                run = row["id"]
                if row["grant_id"] != grant_id:
                    # A new custodian grant needs an explicit start before the worker can resume.
                    db.execute("UPDATE runs SET grant_id=? WHERE id=?", (grant_id, run))
            if prior is None:
                if db.execute("SELECT count(*) FROM requests").fetchone()[0] >= 10_000:
                    raise ManagedError("local_capacity_reached")
                db.execute("INSERT INTO requests VALUES (?,?)", (hashed, run))
            return _projection(db.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone())

    def status(self, run_id: Any) -> dict[str, Any]:
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ManagedError("run_unavailable")
        data = self.read()
        self.current(data)
        with self.queue(data) as (db, grant):
            row = db.execute(
                "SELECT * FROM runs WHERE id=? AND revision=? AND grant_id=?",
                (run_id, plan_revision(data), digest(grant)),
            ).fetchone()
            if row is None:
                raise ManagedError("run_unavailable")
            return _projection(row)

    def once(self, execute: Execute) -> dict[str, Any]:
        data = self.read()
        self.current(data)
        try:
            _check_file(self.path)
        except FileNotFoundError:
            return {"state": "idle"}
        # Crash or exit releases this OS lease. A slow live worker retains it.
        with os.fdopen(
            os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb"
        ) as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"state": "busy"}
            if not os.path.samestat(os.fstat(lease.fileno()), self.path.lstat()):
                raise ManagedError("private_state_unavailable")
            with self.queue(data, write=True) as (db, grant):
                row = db.execute(
                    "SELECT * FROM runs WHERE revision=? AND grant_id=?"
                    " AND state IN ('queued','running')",
                    (plan_revision(data), digest(grant)),
                ).fetchone()
                if row is None:
                    return {"state": "idle"}
                run, claim = row["id"], secrets.token_hex(16)
                db.execute(
                    "UPDATE runs SET state='running', claim=?, attempts=attempts+1,"
                    " started_ms=COALESCE(started_ms,?) WHERE id=?",
                    (claim, _now(), run),
                )
                admitted = digest(grant)
            try:
                result = _safe_result(execute(self.directory, self.organization, data, run))
                state = "held" if result[2] == "hold" else "ready"
                reason = "evaluation_held" if state == "held" else None
            except Exception as exc:
                result = None
                if isinstance(exc, ManagedError):
                    state, reason = "failed", "invalid_worker_result"
                elif isinstance(exc, (ValueError, PermissionError)):
                    state, reason = "held", _hold(exc, "workflow_constraints_unmet")
                else:
                    state, reason = "failed", "worker_failed"
            self.current(data)
            with self.queue(data, write=True) as (db, grant):
                if digest(grant) != admitted:
                    raise ManagedError("authorization_changed")
                finished = _now()
                revision, count, outcome = result or (None, None, None)
                updated = db.execute(
                    "UPDATE runs SET state=?,reason=?,finished_ms=?,result_ms=?,result_revision=?,"
                    "case_count=?,outcome=?,claim=NULL "
                    "WHERE id=? AND claim=? AND grant_id=?",
                    (
                        state,
                        reason,
                        finished,
                        finished if state == "ready" else None,
                        revision,
                        count,
                        outcome,
                        run,
                        claim,
                        admitted,
                    ),
                )
                if updated.rowcount != 1:
                    raise ManagedError("worker_claim_changed")
                return _projection(db.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone())
