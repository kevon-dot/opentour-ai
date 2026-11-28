"""Authenticated customer-local control of one bounded learning run.

Slack (Socket Mode or the hosted adapter), Zapier and the installed worker's schedule tick call
the same handlers. One durable canonical intent therefore becomes one run and at most one fit
reservation, whichever trigger arrives first. A caller names only the opaque plan reference that
the worker derived. The worker resolves the workflow, benchmark, incumbent, recipe, model route
and standing mandate itself. Connection, tenant, actor, role, grant and mandate never come from
request arguments, and a learning run never carries or implies release authority.

The fit reservation ledger (OPE-442), the plan producer (OPE-562) and the trainer dispatch
(OPE-561) attach through the narrow ports below. When the ledger is absent, admission of a run
that could fit is held.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Protocol

from opentour_cell.pilot.intake_mcp import REF_PATTERN
from opentour_cell.pilot.learning_mandate import LearningMandates, MandateError
from opentour_cell.pilot.mcp import tool_error, tool_result
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.readout import ManifestError
from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.training import LocalTrainingStore

Json = dict[str, Any]

CONTRACT = "opentour.pilot.learning-runs.v1"
SELECTION_SCHEMA = "opentour.pilot.learning-run-selection.v1"
GRANT_SCHEMA = "opentour.pilot.learning-run-grant.v1"
PLAN_SCHEMA = "opentour.pilot.learning-plan.v1"
GRANT_PURPOSE = "learning_run_control"
KEY_FILE = "learning-run-key.json"
SCHEDULE_SELECTION = "learning-run-schedule.json"
DATABASE = "learning-runs.sqlite3"
RELEASE_AUTHORITY = "separate_exact_version_approval_required"

# The one owning definition of the wire vocabulary. The TLS gateway derives its routes and HTTP
# statuses from these values, and the operator guide is checked against them.
SUBMIT = "submit_learning_run"
STATUS = "get_learning_run"
CANCEL = "cancel_learning_run"
ROUTES = (
    ("POST", "/v1/learning-runs", SUBMIT),
    ("GET", "/v1/learning-runs/{run_ref}", STATUS),
    ("POST", "/v1/learning-runs/{run_ref}/cancel", CANCEL),
)
OPERATIONS = ("benchmark_only", "evaluate_and_improve")
CHANNELS = ("slack", "zapier", "schedule")
ROLE_PERMISSIONS = {
    "owner": ("start", "read", "cancel", "usage"),
    "operator": ("read", "usage"),
    "reviewer": ("read",),
    "scheduler": ("start", "read", "dispatch"),
}
WAITING = ("queued", "benchmark_only")
WORKING = ("running", "verifying")
TERMINAL = ("completed", "failed", "refused", "canceled")
STATES = (*WAITING, *WORKING, "review_ready", "held", "cancellation_pending", *TERMINAL)
STAGES = ("starting", "discovery", "benchmark", "simple_repair", "fit", "verification")
ERRORS = {
    "invalid_arguments": 400,
    "learning_run_unavailable": 403,
    "learning_run_role_required": 403,
    "learning_run_expired": 403,
    "learning_run_capacity_reached": 403,
    "idempotency_conflict": 409,
    "learning_run_rate_limited": 429,
}
# Reason code -> (next action, accountable next actor) for held, refused and zero-fit runs.
REASONS: dict[str, tuple[str, str | None]] = {
    "benchmark_only_requested": ("wait_for_worker", "worker"),
    "training_outside_mandate": ("wait_for_worker", "worker"),
    "training_unavailable_for_route": ("wait_for_worker", "worker"),
    "standing_mandate_unavailable": ("enable_standing_mandate", "owner"),
    "standing_mandate_paused": ("resume_mandate", "owner"),
    "standing_mandate_withdrawn": ("renew_mandate", "owner"),
    "standing_mandate_expired": ("renew_mandate", "owner"),
    "standing_mandate_changed": ("start_new_run", "owner"),
    "manual_learning_only": ("enable_standing_mandate", "owner"),
    "standing_run_budget_exhausted": ("owner_raises_limits", "owner"),
    "standing_fit_budget_exhausted": ("owner_raises_limits", "owner"),
    "fit_ledger_unavailable": ("connect_fit_ledger", "operator"),
    "fit_intent_requires_reconciliation": ("reconcile_fit_reservation", "operator"),
    "plan_source_unavailable": ("connect_plan_source", "operator"),
    "no_eligible_plan": ("wait_for_new_evidence", "worker"),
    "plan_stale": ("refresh_plan", "owner"),
    "trainer_submission_uncertain": ("reconcile_trainer_submission", "worker"),
    "canceled_by_owner": ("none", None),
}
MAX_RUNS = 1_000
MAX_KEYS = 10_000
MAX_TICKS = 1_000
MAX_EVENTS = 64
MAX_EVIDENCE = 16
MAX_UNITS = 10_000_000
MAX_RECORD_BYTES = 8192
LEASE_SECONDS = 600
CALLS_PER_MINUTE = 120
_REF = re.compile(REF_PATTERN)


class LearningRunError(ValueError):
    """An expected failure whose code is safe to show the authenticated caller."""


def _need(check: object, code: str = "learning_run_unavailable") -> None:
    if not check:
        raise LearningRunError(code)


def _ref(value: object, code: str = "invalid_arguments") -> str:
    if not isinstance(value, str) or _REF.fullmatch(value) is None:
        raise LearningRunError(code)
    return value


def _hex(value: object, size: int = 64) -> bool:
    return isinstance(value, str) and re.fullmatch(f"[0-9a-f]{{{size}}}", value) is not None


def _units(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_UNITS


def _seal(key: str, body: Json) -> str:
    return hmac.digest(bytes.fromhex(key), canonical(body).encode(), "sha256").hex()


def _intent(tenant: str, plan_ref: str, operation: str) -> str:
    """Channel, actor, client and idempotency key are deliberately not part of the intent."""
    return digest([CONTRACT, "intent", tenant, plan_ref, operation])


@dataclass(frozen=True)
class LearningPrincipal:
    """The authenticated connection identity, chosen by the trusted launcher."""

    tenant: str
    actor: str
    role: str
    client: str
    channel: str


@dataclass(frozen=True)
class LearningScope:
    """The owner-authorized grant, pinned for the duration of one operation."""

    tenant: str
    engagement_epoch: str
    workflow_ref: str
    mandate_id: str
    permissions: frozenset[str]
    grant_revision: str


@dataclass(frozen=True)
class MandateState:
    """The current OPE-426 standing mandate revision, read fresh for every operation."""

    mandate_id: str
    mandate_revision: str
    owner_id: str
    delegate: str
    state: str
    autonomous: bool
    training: bool
    max_runs: int
    runs_used: int
    max_fits: int
    fits_reserved: int
    lineage: tuple[str, ...] = ()


@dataclass(frozen=True)
class LearningPlan:
    """A server-owned immutable plan. Its reference is the digest of its content."""

    tenant: str
    engagement_epoch: str
    workflow_ref: str
    mandate_id: str
    mandate_revision: str
    source_revision: str
    incumbent_ref: str
    benchmark_ref: str
    baseline_ref: str
    recipe_ref: str | None
    model_route_ref: str
    training_capable: bool

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if name == "training_capable":
                _need(type(value) is bool, "learning_plan_invalid")
            elif name == "mandate_revision":
                _need(_hex(value), "learning_plan_invalid")
            elif not (name == "recipe_ref" and value is None):
                _ref(value, "learning_plan_invalid")

    def document(self) -> Json:
        return {"schema_version": PLAN_SCHEMA, **asdict(self)}

    @property
    def plan_ref(self) -> str:
        return "plan-" + digest(self.document())[:40]

    @classmethod
    def from_document(cls, value: object) -> LearningPlan:
        _need(
            isinstance(value, dict)
            and set(value) == {"schema_version", *cls.__dataclass_fields__}
            and value["schema_version"] == PLAN_SCHEMA,
            "learning_plan_invalid",
        )
        assert isinstance(value, dict)
        return cls(**{name: value[name] for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class PlanUnavailable:
    reason_code: str


@dataclass(frozen=True)
class FitIntent:
    """OPE-442 fit identity: tenant, mandate revision, snapshot, plan and model digest."""

    tenant: str
    mandate_id: str
    mandate_revision: str
    snapshot_ref: str
    plan_ref: str
    model_digest: str
    run_ref: str
    residual_ref: str
    max_units: int

    @property
    def identity(self) -> str:
        return digest(
            [
                CONTRACT,
                "fit",
                self.tenant,
                self.mandate_revision,
                self.snapshot_ref,
                self.plan_ref,
                self.model_digest,
            ]
        )


@dataclass(frozen=True)
class FitReservation:
    reservation_ref: str
    reserved_units: int
    consumed_units: int | None = None


class LedgerHold(Exception):
    """The ledger refused a reservation with a safe reason code."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class FitLedger(Protocol):
    """Narrow port for the OPE-442 customer-local reservation ledger.

    ``reserve`` is unique per ``FitIntent.identity`` across operation keys and returns the
    original reservation on replay. A reused key with changed contents raises ``LedgerHold``.
    ``lookup`` reconciles before any retry. ``settle`` records measured usage once and returns
    the ledger's charge (no refund below the ledger's own policy).
    """

    def lookup(self, intent: FitIntent) -> FitReservation | None: ...

    def reserve(self, intent: FitIntent, *, operation_key: str) -> FitReservation: ...

    def settle(self, reservation_ref: str, *, consumed_units: int) -> FitReservation: ...


class LearningRunAuthority(Protocol):
    def guard(
        self, principal: LearningPrincipal, *, write: bool
    ) -> AbstractContextManager[LearningScope]:
        """Recheck and pin the current grant until the scoped operation finishes."""


class MandateSource(Protocol):
    def current(self, scope: LearningScope, principal: LearningPrincipal) -> MandateState: ...


class PlanSource(Protocol):
    """Derive the current plan from the current verified source revision (OPE-562)."""

    def current_plan(
        self, scope: LearningScope, mandate: MandateState
    ) -> LearningPlan | PlanUnavailable: ...


@dataclass(frozen=True)
class SubmitRequest:
    plan_ref: str
    operation: str
    idempotency_key: str


@dataclass(frozen=True)
class Claim:
    """A fenced worker lease on one admitted run."""

    run_ref: str
    lease: str
    operation: str
    plan: LearningPlan
    cancel_requested: bool
    fit_reservation: str | None
    submission: str | None
    execution_ref: str | None


def parse_submit(arguments: object) -> SubmitRequest:
    _need(
        isinstance(arguments, dict)
        and set(arguments) == {"plan_ref", "operation", "idempotency_key"}
        and arguments["operation"] in OPERATIONS,
        "invalid_arguments",
    )
    assert isinstance(arguments, dict)
    return SubmitRequest(
        _ref(arguments["plan_ref"]), arguments["operation"], _ref(arguments["idempotency_key"])
    )


def parse_run(arguments: object) -> str:
    _need(isinstance(arguments, dict) and set(arguments) == {"run_ref"}, "invalid_arguments")
    assert isinstance(arguments, dict)
    return _ref(arguments["run_ref"])


def parse_cancel(arguments: object) -> tuple[str, str]:
    _need(
        isinstance(arguments, dict) and set(arguments) == {"run_ref", "idempotency_key"},
        "invalid_arguments",
    )
    assert isinstance(arguments, dict)
    return _ref(arguments["run_ref"]), _ref(arguments["idempotency_key"])


def http_routes() -> tuple[tuple[re.Pattern[bytes], str], ...]:
    """Exact HTTP/1.1 request lines for the TLS gateway, derived from ROUTES."""
    ref = REF_PATTERN.removeprefix("^").removesuffix("$")
    routes = []
    for method, template, operation in ROUTES:
        path = re.escape(template).replace(re.escape("{run_ref}"), f"(?P<run_ref>{ref})")
        routes.append((re.compile(f"{method} {path} HTTP/1\\.1".encode("ascii")), operation))
    return tuple(routes)


def _identity(principal: LearningPrincipal) -> None:
    _need(
        isinstance(principal, LearningPrincipal)
        and principal.role in ROLE_PERMISSIONS
        and principal.channel in CHANNELS
    )
    for value in (principal.tenant, principal.actor, principal.client):
        _ref(value, "learning_run_unavailable")


def _private(info: os.stat_result) -> None:
    _need(
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and info.st_nlink == 1
        and not info.st_mode & 0o077,
        "private_learning_runs_required",
    )


class LearningRuns:
    """One durable customer-local ledger of learning runs, shared by every trigger."""

    def __init__(
        self,
        directory: Path,
        authority: LearningRunAuthority,
        *,
        mandates: MandateSource,
        plans: PlanSource | None = None,
        ledger: FitLedger | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.authority, self.mandates, self.plans, self.ledger = authority, mandates, plans, ledger
        self.clock = clock
        try:
            directory = directory.absolute()
            _need(directory.resolve(strict=True) == directory, "private_learning_runs_required")
            info = directory.stat()
            _need(
                stat.S_ISDIR(info.st_mode)
                and info.st_uid == os.getuid()
                and not info.st_mode & 0o077,
                "private_learning_runs_required",
            )
            self.path = directory / DATABASE
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            try:
                _private(os.fstat(fd))
            finally:
                os.close(fd)
            with self._db() as db:
                for statement in _SCHEMA:
                    db.execute(statement)
                if not any(
                    row["name"] == "grant_mandate"
                    for row in db.execute("PRAGMA table_info(lr_runs)")
                ):
                    db.execute("ALTER TABLE lr_runs ADD COLUMN grant_mandate TEXT")
                    db.execute("UPDATE lr_runs SET grant_mandate=mandate_id")
        except (OSError, sqlite3.Error) as error:
            raise LearningRunError("private_learning_runs_required") from error

    @contextmanager
    def _db(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        _private(self.path.lstat())
        db = sqlite3.connect(
            self.path.as_uri() + "?mode=rw", uri=True, timeout=10, isolation_level=None
        )
        db.row_factory = sqlite3.Row
        try:
            if not write:
                db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _now(self) -> int:
        return int(self.clock())

    # ---- shared checks -------------------------------------------------------------------

    @staticmethod
    def _permit(principal: LearningPrincipal, scope: LearningScope, permission: str) -> None:
        _need(
            permission in ROLE_PERMISSIONS[principal.role] and permission in scope.permissions,
            "learning_run_role_required",
        )

    def _mandate(self, scope: LearningScope, principal: LearningPrincipal) -> MandateState | None:
        try:
            mandate = self.mandates.current(scope, principal)
        except (
            LearningRunError,
            MandateError,
            TrainingError,
            ManifestError,
            OSError,
            sqlite3.Error,
        ):
            return None
        if principal.role == "owner":
            _need(principal.actor == mandate.owner_id, "learning_run_role_required")
        if principal.role == "scheduler":
            _need(principal.actor == mandate.delegate)
        return mandate

    @staticmethod
    def _mandate_hold(mandate: MandateState | None) -> str | None:
        if mandate is None:
            return "standing_mandate_unavailable"
        if mandate.state != "active":
            return "standing_mandate_" + mandate.state
        if not mandate.autonomous:
            return "manual_learning_only"
        return None

    def _fence(self, row: sqlite3.Row, mandate: MandateState) -> str | None:
        """Why an admitted run can no longer be dispatched under its frozen mandate revision."""
        if (row["mandate_id"], row["mandate_revision"]) != (
            mandate.mandate_id,
            mandate.mandate_revision,
        ):
            return "standing_mandate_changed"
        return self._mandate_hold(mandate)

    def _plan(self, scope: LearningScope, mandate: MandateState) -> LearningPlan | PlanUnavailable:
        if self.plans is None:
            return PlanUnavailable("plan_source_unavailable")
        try:
            plan = self.plans.current_plan(scope, mandate)
        except (LearningRunError, ManifestError, TrainingError, OSError, sqlite3.Error):
            return PlanUnavailable("plan_source_unavailable")
        if isinstance(plan, PlanUnavailable):
            return plan if plan.reason_code in REASONS else PlanUnavailable("no_eligible_plan")
        if not isinstance(plan, LearningPlan):
            return PlanUnavailable("plan_source_unavailable")
        # The server-owned plan must stay inside this grant and the current mandate revision.
        if (plan.tenant, plan.engagement_epoch, plan.workflow_ref) != (
            scope.tenant,
            scope.engagement_epoch,
            scope.workflow_ref,
        ) or (plan.mandate_id, plan.mandate_revision) != (
            mandate.mandate_id,
            mandate.mandate_revision,
        ):
            return PlanUnavailable("no_eligible_plan")
        return plan

    @staticmethod
    def _effective(
        plan: LearningPlan, mandate: MandateState, operation: str
    ) -> tuple[str, str | None]:
        if operation == "benchmark_only":
            return "benchmark_only", "benchmark_only_requested"
        if not mandate.training:
            return "benchmark_only", "training_outside_mandate"
        if not plan.training_capable:
            return "benchmark_only", "training_unavailable_for_route"
        return "evaluate_and_improve", None

    def _budget_hold(
        self, db: sqlite3.Connection, scope: LearningScope, mandate: MandateState, effective: str
    ) -> str | None:
        # Count the whole renewal lineage, as the mandate's own usage does; renewal never resets.
        lineage = mandate.lineage or (mandate.mandate_id,)
        admitted = db.execute(
            "SELECT count(*) FROM lr_runs WHERE tenant=? "
            "AND json_extract(plan,'$.engagement_epoch')=? "
            f"AND mandate_id IN ({','.join('?' * len(lineage))}) "
            "AND (state IN ('queued','benchmark_only') OR dispatched=1)",
            (scope.tenant, scope.engagement_epoch, *lineage),
        ).fetchone()[0]
        if max(admitted, mandate.runs_used) >= mandate.max_runs:
            return "standing_run_budget_exhausted"
        if effective == "evaluate_and_improve":
            if self.ledger is None:
                return "fit_ledger_unavailable"
            if mandate.fits_reserved >= mandate.max_fits:
                return "standing_fit_budget_exhausted"
        return None

    @staticmethod
    def _hold(reason: str) -> Json:
        action, actor = REASONS.get(reason, ("contact_operator", "operator"))
        return {
            "contract": CONTRACT,
            "state": "held",
            "reason_code": reason,
            "next_action": action,
            "next_actor": actor,
            "release_authority": RELEASE_AUTHORITY,
        }

    @staticmethod
    def _within_scope(row: sqlite3.Row, scope: LearningScope) -> bool:
        plan = LearningPlan.from_document(json.loads(row["plan"]))
        return (
            plan.tenant == scope.tenant
            and plan.engagement_epoch == scope.engagement_epoch
            and plan.workflow_ref == scope.workflow_ref
            and row["grant_mandate"] == scope.mandate_id
        )

    def _run(
        self, db: sqlite3.Connection, principal: LearningPrincipal, scope: LearningScope, run: str
    ) -> sqlite3.Row:
        row: sqlite3.Row | None = db.execute(
            "SELECT * FROM lr_runs WHERE tenant=? AND run_ref=?", (principal.tenant, run)
        ).fetchone()
        _need(row is not None and self._within_scope(row, scope))
        assert row is not None
        return row

    def _event(
        self,
        db: sqlite3.Connection,
        tenant: str,
        run: str,
        kind: str,
        state: str,
        reason: str | None,
    ) -> None:
        seq = db.execute(
            "SELECT coalesce(max(seq),0) FROM lr_events WHERE tenant=? AND run_ref=?", (tenant, run)
        ).fetchone()[0]
        if seq < MAX_EVENTS:
            db.execute(
                "INSERT INTO lr_events VALUES (?,?,?,?,?,?,?)",
                (tenant, run, seq + 1, kind, state, reason, self._now()),
            )

    def _set(self, db: sqlite3.Connection, row: sqlite3.Row, **changes: Any) -> sqlite3.Row:
        assignments = ",".join(f"{name}=?" for name in changes)
        db.execute(
            f"UPDATE lr_runs SET {assignments},updated_at=? WHERE tenant=? AND run_ref=?",
            (*changes.values(), self._now(), row["tenant"], row["run_ref"]),
        )
        updated: sqlite3.Row | None = db.execute(
            "SELECT * FROM lr_runs WHERE tenant=? AND run_ref=?", (row["tenant"], row["run_ref"])
        ).fetchone()
        assert updated is not None
        return updated

    @staticmethod
    def _capacity(db: sqlite3.Connection, tenant: str, table: str, maximum: int) -> None:
        query = {
            "lr_runs": "SELECT count(*) FROM lr_runs WHERE tenant=?",
            "lr_keys": "SELECT count(*) FROM lr_keys WHERE tenant=?",
        }[table]
        _need(db.execute(query, (tenant,)).fetchone()[0] < maximum, "learning_run_capacity_reached")

    def _cancellation(self, db: sqlite3.Connection, tenant: str, run: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = db.execute(
            "SELECT * FROM lr_cancellations WHERE tenant=? AND run_ref=?", (tenant, run)
        ).fetchone()
        return row

    def _view(
        self,
        db: sqlite3.Connection,
        row: sqlite3.Row,
        mandate: MandateState | None,
        principal: LearningPrincipal,
        scope: LearningScope,
    ) -> Json:
        state, reason = str(row["state"]), row["reason_code"]
        if state in WAITING:
            # Report the fence that dispatch will apply; a status read writes nothing.
            fenced = None if mandate is None else self._fence(row, mandate)
            if mandate is None:
                state, reason = "held", "standing_mandate_unavailable"
            elif fenced is not None:
                state, reason = "refused", fenced
        actor: str | None
        if state in (*WAITING, *WORKING):
            action, actor = "wait_for_worker", "worker"
        elif state == "review_ready":
            action, actor = "review_in_slack", "reviewer"
        elif state == "cancellation_pending":
            action, actor = "wait_for_cancellation", "worker"
        elif state in ("held", "refused"):
            action, actor = REASONS.get(str(reason), ("contact_operator", "operator"))
        else:
            action, actor = "none", None
        cancellation = self._cancellation(db, row["tenant"], row["run_ref"])
        view: Json = {
            "contract": CONTRACT,
            "run_ref": row["run_ref"],
            "state": state,
            "stage": row["stage"],
            "operation": row["operation"],
            "effective_operation": row["effective"],
            "reason_code": reason,
            "next_action": action,
            "next_actor": actor,
            "owner": row["owner"],
            "plan_ref": row["plan_ref"],
            "workflow_ref": row["workflow_ref"],
            "progress": json.loads(row["progress"]) if row["progress"] else None,
            "evidence_refs": json.loads(row["evidence"]),
            "cancellation": None
            if cancellation is None
            else {"receipt_ref": cancellation["receipt_ref"], "state": cancellation["state"]},
            "release_authority": RELEASE_AUTHORITY,
        }
        if "usage" in ROLE_PERMISSIONS[principal.role] and "usage" in scope.permissions:
            view["usage"] = {
                "fit_reservations": 0 if row["fit_reservation"] is None else 1,
                "reserved_units": row["reserved_units"] or 0,
                "consumed_units": row["consumed_units"],
            }
        return view

    # ---- endpoint handlers (TLS gateway and local Slack Socket Mode) ---------------------

    def preview(self, principal: LearningPrincipal) -> Json | None:
        """Read the sealed plan and allowed controls without admitting or dispatching work."""
        _identity(principal)
        with self.authority.guard(principal, write=False) as scope:
            self._permit(principal, scope, "read")
            mandate = self._mandate(scope, principal)
            if mandate is None:
                return None
            plan = self._plan(scope, mandate)
            if isinstance(plan, PlanUnavailable):
                return None
            with self._db(write=False) as db:
                latest = db.execute(
                    "SELECT run_ref FROM lr_runs WHERE tenant=? AND plan_ref=? AND grant_mandate=? "
                    "ORDER BY created_at DESC,rowid DESC LIMIT 1",
                    (scope.tenant, plan.plan_ref, scope.mandate_id),
                ).fetchone()
                allowed = (
                    principal.role == "owner"
                    and "start" in scope.permissions
                    and self._mandate_hold(mandate) is None
                )
                return {
                    "plan_ref": plan.plan_ref,
                    "workflow_ref": plan.workflow_ref,
                    "engagement": plan.engagement_epoch,
                    "model_route": plan.model_route_ref,
                    "benchmark_ref": plan.benchmark_ref,
                    "mandate_ref": mandate.mandate_id,
                    "max_fits": mandate.max_fits,
                    "spend_cap_cents": 0,
                    "schedule": "paused" if mandate.state == "paused" else "unverified",
                    "can_benchmark": allowed
                    and self._budget_hold(db, scope, mandate, "benchmark_only") is None,
                    "can_improve": allowed
                    and mandate.training
                    and plan.training_capable
                    and self._budget_hold(db, scope, mandate, "evaluate_and_improve")
                    is None,
                    "can_cancel": "cancel" in scope.permissions,
                    "current_run": latest["run_ref"] if latest else None,
                }

    def submit(self, principal: LearningPrincipal, request: SubmitRequest) -> Json:
        _identity(principal)
        _need(isinstance(request, SubmitRequest), "invalid_arguments")
        parse_submit(asdict(request))
        with self.authority.guard(principal, write=True) as scope:
            self._permit(principal, scope, "start")
            mandate = self._mandate(scope, principal)
            if mandate is None:
                return self._hold("standing_mandate_unavailable")
            return self._submit(principal, scope, request, mandate)

    def _known(
        self,
        db: sqlite3.Connection,
        principal: LearningPrincipal,
        scope: LearningScope,
        intent: str,
        key_hash: str,
    ) -> sqlite3.Row | None:
        key = db.execute(
            "SELECT target FROM lr_keys WHERE tenant=? AND client=? AND kind='submit' "
            "AND key_hash=?",
            (principal.tenant, principal.client, key_hash),
        ).fetchone()
        _need(key is None or key["target"] == intent, "idempotency_conflict")
        run: sqlite3.Row | None = db.execute(
            "SELECT * FROM lr_runs WHERE tenant=? AND intent=?", (principal.tenant, intent)
        ).fetchone()
        _need(key is None or run is not None)
        _need(run is None or self._within_scope(run, scope))
        return run

    def _remember(
        self,
        db: sqlite3.Connection,
        principal: LearningPrincipal,
        run: sqlite3.Row,
        intent: str,
        key_hash: str,
    ) -> None:
        found = db.execute(
            "SELECT 1 FROM lr_keys WHERE tenant=? AND client=? AND kind='submit' AND key_hash=?",
            (principal.tenant, principal.client, key_hash),
        ).fetchone()
        if found is None:
            self._capacity(db, principal.tenant, "lr_keys", MAX_KEYS)
            db.execute(
                "INSERT INTO lr_keys VALUES (?,?,'submit',?,?)",
                (principal.tenant, principal.client, key_hash, intent),
            )
        db.execute(
            "INSERT OR IGNORE INTO lr_triggers VALUES (?,?,?,?,?,?)",
            (
                principal.tenant,
                run["run_ref"],
                principal.channel,
                principal.client,
                key_hash,
                self._now(),
            ),
        )

    def _submit(
        self,
        principal: LearningPrincipal,
        scope: LearningScope,
        request: SubmitRequest,
        mandate: MandateState | None,
    ) -> Json:
        intent = _intent(principal.tenant, request.plan_ref, request.operation)
        key_hash = digest([CONTRACT, "submit-key", request.idempotency_key])
        # A replay or a second trigger for an admitted intent returns the same run. It never
        # re-evaluates admission, so it cannot create a second run or a second reservation.
        with self._db() as db:
            run = self._known(db, principal, scope, intent, key_hash)
            if run is not None:
                self._remember(db, principal, run, intent, key_hash)
                return self._view(db, run, mandate, principal, scope)
        hold = self._mandate_hold(mandate)
        if hold is not None:
            return self._hold(hold)
        assert mandate is not None
        plan = self._plan(scope, mandate)
        if isinstance(plan, PlanUnavailable):
            return self._hold(plan.reason_code)
        if plan.plan_ref != request.plan_ref:
            return self._hold("plan_stale")
        effective, reason = self._effective(plan, mandate, request.operation)
        with self._db() as db:
            run = self._known(db, principal, scope, intent, key_hash)
            if run is None:
                budget = self._budget_hold(db, scope, mandate, effective)
                if budget is not None:
                    return self._hold(budget)
                self._capacity(db, principal.tenant, "lr_runs", MAX_RUNS)
                run_ref, now = "lrun-" + secrets.token_hex(16), self._now()
                state = "queued" if effective == "evaluate_and_improve" else "benchmark_only"
                db.execute(
                    "INSERT INTO lr_runs (tenant,run_ref,intent,workflow_ref,plan_ref,plan,"
                    "operation,effective,mandate_id,mandate_revision,owner,state,reason_code,grant_mandate,"
                    "evidence,cancel_requested,dispatched,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'[]',0,0,?,?)",
                    (
                        principal.tenant,
                        run_ref,
                        intent,
                        plan.workflow_ref,
                        plan.plan_ref,
                        canonical(plan.document()),
                        request.operation,
                        effective,
                        mandate.mandate_id,
                        mandate.mandate_revision,
                        mandate.owner_id,
                        state,
                        reason,
                        scope.mandate_id,
                        now,
                        now,
                    ),
                )
                self._event(db, principal.tenant, run_ref, "admitted", state, reason)
                run = self._run(db, principal, scope, run_ref)
            self._remember(db, principal, run, intent, key_hash)
            return self._view(db, run, mandate, principal, scope)

    def status(self, principal: LearningPrincipal, run_ref: str) -> Json:
        """Read one run. A status read never launches, fences or settles anything."""
        _identity(principal)
        _ref(run_ref)
        with self.authority.guard(principal, write=False) as scope:
            self._permit(principal, scope, "read")
            mandate = self._mandate(scope, principal)
            _need(mandate is not None)
            with self._db(write=False) as db:
                return self._view(
                    db, self._run(db, principal, scope, run_ref), mandate, principal, scope
                )

    def cancel(self, principal: LearningPrincipal, run_ref: str, idempotency_key: str) -> Json:
        _identity(principal)
        _ref(run_ref)
        key_hash = digest([CONTRACT, "cancel-key", _ref(idempotency_key)])
        with self.authority.guard(principal, write=True) as scope:
            self._permit(principal, scope, "cancel")
            mandate = self._mandate(scope, principal)
            _need(mandate is not None)
            with self._db() as db:
                key = db.execute(
                    "SELECT target FROM lr_keys WHERE tenant=? AND client=? AND kind='cancel' "
                    "AND key_hash=?",
                    (principal.tenant, principal.client, key_hash),
                ).fetchone()
                _need(key is None or key["target"] == run_ref, "idempotency_conflict")
                row = self._run(db, principal, scope, run_ref)
                if self._cancellation(db, principal.tenant, run_ref) is None:
                    row = self._request_cancel(db, row, mandate)
                if key is None:
                    self._capacity(db, principal.tenant, "lr_keys", MAX_KEYS)
                    db.execute(
                        "INSERT INTO lr_keys VALUES (?,?,'cancel',?,?)",
                        (principal.tenant, principal.client, key_hash, run_ref),
                    )
        self._settle_cancellation(principal.tenant, run_ref)
        with self.authority.guard(principal, write=False) as scope:
            with self._db(write=False) as db:
                return self._cancel_view(db, self._run(db, principal, scope, run_ref))

    def _request_cancel(
        self, db: sqlite3.Connection, row: sqlite3.Row, mandate: MandateState | None
    ) -> sqlite3.Row:
        tenant, run, now = row["tenant"], row["run_ref"], self._now()
        receipt = "lcancel-" + secrets.token_hex(16)
        state = str(row["state"])
        fenced = None if mandate is None or state not in WAITING else self._fence(row, mandate)
        if fenced is not None:
            row = self._set(db, row, state="refused", reason_code=fenced)
            self._event(db, tenant, run, "fenced", "refused", fenced)
            state = "refused"
        if state in TERMINAL or state == "review_ready":
            # Finished work is not undone here; any release still needs its own approval.
            db.execute(
                "INSERT INTO lr_cancellations VALUES (?,?,?,'not_applicable',NULL,?,?)",
                (tenant, run, receipt, now, now),
            )
            return row
        live = row["lease"] is not None and row["lease_expires"] >= now
        if not live and row["submission"] is None and row["fit_reservation"] is None:
            row = self._set(
                db,
                row,
                state="canceled",
                reason_code="canceled_by_owner",
                stage=None,
                lease=None,
                cancel_requested=1,
            )
            db.execute(
                "INSERT INTO lr_cancellations VALUES (?,?,?,'confirmed',NULL,?,?)",
                (tenant, run, receipt, now, now),
            )
            self._event(db, tenant, run, "canceled", "canceled", "canceled_by_owner")
            return row
        # Backend work or a reservation may exist. Report pending until the worker confirms the
        # terminal state and the ledger has settled the consumed budget.
        changes: Json = {"state": "cancellation_pending", "cancel_requested": 1}
        if not live and row["submission"] is None:
            changes["lease"] = None
        row = self._set(db, row, **changes)
        db.execute(
            "INSERT INTO lr_cancellations VALUES (?,?,?,'pending',NULL,?,NULL)",
            (tenant, run, receipt, now),
        )
        self._event(db, tenant, run, "cancel_requested", "cancellation_pending", None)
        return row

    def _settle_cancellation(self, tenant: str, run: str) -> None:
        """Settle a pending cancellation that has a reservation but no live backend work."""
        with self._db(write=False) as db:
            row = db.execute(
                "SELECT * FROM lr_runs WHERE tenant=? AND run_ref=?", (tenant, run)
            ).fetchone()
            pending = self._cancellation(db, tenant, run)
            if (
                row is None
                or pending is None
                or pending["state"] != "pending"
                or row["state"] != "cancellation_pending"
                or row["lease"] is not None
                or row["submission"] not in (None, "stopped")
                or row["fit_reservation"] is None
                or self.ledger is None
            ):
                return
            reservation = str(row["fit_reservation"])
            measured = int(row["measured_units"] or 0)
        try:
            settled = self._settle(reservation, measured)
        except LearningRunError:
            return
        self._confirm_cancel(tenant, run, reservation, settled)

    def _confirm_cancel(
        self, tenant: str, run: str, reservation: str | None, settled: FitReservation | None
    ) -> None:
        consumed = None if settled is None else settled.consumed_units
        _need(
            settled is None
            or (
                settled.reservation_ref == reservation and consumed is not None and _units(consumed)
            )
        )
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM lr_runs WHERE tenant=? AND run_ref=?", (tenant, run)
            ).fetchone()
            pending = self._cancellation(db, tenant, run)
            if row is None or pending is None or pending["state"] != "pending":
                return
            self._set(
                db,
                row,
                state="canceled",
                reason_code="canceled_by_owner",
                stage=None,
                lease=None,
                consumed_units=consumed,
            )
            db.execute(
                "UPDATE lr_cancellations SET state='confirmed',consumed_units=?,confirmed_at=? "
                "WHERE tenant=? AND run_ref=?",
                (consumed, self._now(), tenant, run),
            )
            self._event(db, tenant, run, "canceled", "canceled", "canceled_by_owner")

    def _cancel_view(self, db: sqlite3.Connection, row: sqlite3.Row) -> Json:
        receipt = self._cancellation(db, row["tenant"], row["run_ref"])
        assert receipt is not None
        return {
            "contract": CONTRACT,
            "run_ref": row["run_ref"],
            "receipt_ref": receipt["receipt_ref"],
            "cancellation": receipt["state"],
            "state": row["state"],
            "release_authority": RELEASE_AUTHORITY,
        }

    # ---- worker schedule tick ------------------------------------------------------------

    def schedule_tick(self, principal: LearningPrincipal) -> Json:
        """Submit at most one scoped scheduled intent, or record no-change or a hold.

        The tick reads pause state, mode, fit count and caps from the current mandate revision
        every time and keeps no copy. Each tick rechecks the scheduler's grant.
        """
        _identity(principal)
        _need(principal.channel == "schedule" and principal.role == "scheduler")
        with self.authority.guard(principal, write=True) as scope:
            self._permit(principal, scope, "start")
            mandate = self._mandate(scope, principal)
            with self._db() as db:
                self._fence_waiting(db, scope, mandate)
                pending = [
                    str(row["run_ref"])
                    for row in db.execute(
                        "SELECT run_ref FROM lr_runs WHERE tenant=? AND workflow_ref=? "
                        "AND json_extract(plan,'$.engagement_epoch')=? AND grant_mandate=? "
                        "AND state='cancellation_pending'",
                        (
                            scope.tenant,
                            scope.workflow_ref,
                            scope.engagement_epoch,
                            scope.mandate_id,
                        ),
                    )
                ]
            outcome = self._tick_outcome(principal, scope, mandate)
        for run in pending:
            self._settle_cancellation(principal.tenant, run)
        return outcome

    def _tick_outcome(
        self, principal: LearningPrincipal, scope: LearningScope, mandate: MandateState | None
    ) -> Json:
        hold = self._mandate_hold(mandate)
        if hold is not None:
            return self._tick(principal, mandate, "held", hold)
        assert mandate is not None
        with self._db(write=False) as db:
            busy = db.execute(
                "SELECT run_ref,state FROM lr_runs WHERE tenant=? AND workflow_ref=? "
                "AND json_extract(plan,'$.engagement_epoch')=? AND grant_mandate=? "
                "AND state NOT IN ('completed','failed','refused','canceled') "
                "ORDER BY created_at,rowid LIMIT 1",
                (scope.tenant, scope.workflow_ref, scope.engagement_epoch, scope.mandate_id),
            ).fetchone()
        plan = self._plan(scope, mandate)
        if isinstance(plan, LearningPlan):
            operation = (
                "evaluate_and_improve"
                if mandate.training and plan.training_capable
                else "benchmark_only"
            )
            with self._db(write=False) as db:
                existing = db.execute(
                    "SELECT run_ref FROM lr_runs WHERE tenant=? AND intent=?",
                    (principal.tenant, _intent(principal.tenant, plan.plan_ref, operation)),
                ).fetchone()
            if existing is not None:
                # The same intent already has a run, whichever trigger admitted it.
                return self._tick(
                    principal, mandate, "no_change", "already_admitted", plan, existing["run_ref"]
                )
        if busy is not None:
            # One run per workflow at a time; a candidate awaiting review also waits.
            reason = "awaiting_review" if busy["state"] == "review_ready" else "run_in_progress"
            return self._tick(principal, mandate, "no_change", reason, run=busy["run_ref"])
        if isinstance(plan, PlanUnavailable):
            outcome = "no_change" if plan.reason_code == "no_eligible_plan" else "held"
            return self._tick(principal, mandate, outcome, plan.reason_code)
        key = "schedule-" + digest([plan.plan_ref, operation])[:40]
        result = self._submit(
            principal, scope, SubmitRequest(plan.plan_ref, operation, key), mandate
        )
        if "run_ref" not in result:
            return self._tick(principal, mandate, "held", result["reason_code"], plan)
        return self._tick(
            principal, mandate, "admitted", result["reason_code"], plan, result["run_ref"]
        )

    def _fence_waiting(
        self, db: sqlite3.Connection, scope: LearningScope, mandate: MandateState | None
    ) -> None:
        """A paused, withdrawn, expired or replaced mandate fences queued intents."""
        if mandate is None:
            return
        for row in db.execute(
            "SELECT * FROM lr_runs WHERE tenant=? AND workflow_ref=? "
            "AND json_extract(plan,'$.engagement_epoch')=? AND grant_mandate=? "
            "AND state IN ('queued','benchmark_only')",
            (scope.tenant, scope.workflow_ref, scope.engagement_epoch, scope.mandate_id),
        ).fetchall():
            reason = self._fence(row, mandate)
            if reason is not None:
                self._set(db, row, state="refused", reason_code=reason)
                self._event(db, row["tenant"], row["run_ref"], "fenced", "refused", reason)

    def _tick(
        self,
        principal: LearningPrincipal,
        mandate: MandateState | None,
        outcome: str,
        reason: str | None,
        plan: LearningPlan | None = None,
        run: str | None = None,
    ) -> Json:
        revision = None if mandate is None else mandate.mandate_revision
        plan_ref = None if plan is None else plan.plan_ref
        with self._db() as db:
            seq = db.execute(
                "SELECT coalesce(max(seq),0) FROM lr_ticks WHERE tenant=?", (principal.tenant,)
            ).fetchone()[0]
            db.execute(
                "INSERT INTO lr_ticks VALUES (?,?,?,?,?,?,?,?)",
                (principal.tenant, seq + 1, revision, plan_ref, outcome, reason, run, self._now()),
            )
            db.execute(
                "DELETE FROM lr_ticks WHERE tenant=? AND seq<=?",
                (principal.tenant, seq + 1 - MAX_TICKS),
            )
        return {
            "contract": CONTRACT,
            "tick": outcome,
            "reason_code": reason,
            "run_ref": run,
            "plan_ref": plan_ref,
            "mandate_revision": revision,
        }

    # ---- worker dispatch API (trusted local executor: OPE-561 / OPE-562) -----------------

    def claim(self, principal: LearningPrincipal) -> Claim | None:
        """Lease the oldest dispatchable run after fencing runs whose authority lapsed."""
        _identity(principal)
        with self.authority.guard(principal, write=True) as scope:
            self._permit(principal, scope, "dispatch")
            mandate = self._mandate(scope, principal)
            with self._db() as db:
                self._fence_waiting(db, scope, mandate)
                now = self._now()
                # Reconcile abandoned work first, even under a lapsed mandate: the next worker
                # must stop it, seal it and settle measured usage. New work needs active authority.
                row = db.execute(
                    "SELECT * FROM lr_runs WHERE tenant=? AND workflow_ref=? "
                    "AND json_extract(plan,'$.engagement_epoch')=? AND grant_mandate=? "
                    "AND state IN ('running','verifying','held','cancellation_pending') "
                    "AND lease IS NOT NULL AND lease_expires<? ORDER BY created_at,rowid LIMIT 1",
                    (
                        scope.tenant,
                        scope.workflow_ref,
                        scope.engagement_epoch,
                        scope.mandate_id,
                        now,
                    ),
                ).fetchone()
                if row is None and self._mandate_hold(mandate) is None:
                    row = db.execute(
                        "SELECT * FROM lr_runs WHERE tenant=? AND workflow_ref=? "
                        "AND json_extract(plan,'$.engagement_epoch')=? AND grant_mandate=? "
                        "AND state IN ('queued','benchmark_only') AND cancel_requested=0 "
                        "ORDER BY created_at,rowid LIMIT 1",
                        (
                            scope.tenant,
                            scope.workflow_ref,
                            scope.engagement_epoch,
                            scope.mandate_id,
                        ),
                    ).fetchone()
                if row is None:
                    return None
                changes: Json = {
                    "lease": "lease-" + secrets.token_hex(16),
                    "lease_expires": now + LEASE_SECONDS,
                    "dispatched": 1,
                }
                if row["state"] in WAITING:
                    changes.update(state="running", stage="starting")
                if row["submission"] == "pending":
                    # A crash between recording intent and the trainer's answer is ambiguous.
                    changes.update(
                        submission="uncertain",
                        state="held",
                        reason_code="trainer_submission_uncertain",
                    )
                row = self._set(db, row, **changes)
                self._event(db, scope.tenant, row["run_ref"], "claimed", row["state"], None)
                return self._claim(row)

    @staticmethod
    def _claim(row: sqlite3.Row) -> Claim:
        return Claim(
            row["run_ref"],
            row["lease"],
            row["effective"],
            LearningPlan.from_document(json.loads(row["plan"])),
            bool(row["cancel_requested"]),
            row["fit_reservation"],
            row["submission"],
            row["execution_ref"],
        )

    @contextmanager
    def _leased(
        self, principal: LearningPrincipal, claim: Claim, *, allow_cancel: bool = False
    ) -> Iterator[tuple[sqlite3.Connection, sqlite3.Row]]:
        """Recheck grant, mandate, lease and cancellation before any worker effect."""
        _identity(principal)
        with self.authority.guard(principal, write=True) as scope:
            self._permit(principal, scope, "dispatch")
            mandate = self._mandate(scope, principal)
            with self._db() as db:
                row = self._run(db, principal, scope, claim.run_ref)
                _need(row["lease"] == claim.lease, "learning_run_lease_lost")
                if not allow_cancel:
                    _need(not row["cancel_requested"], "learning_run_cancel_requested")
                    _need(mandate is not None, "standing_mandate_unavailable")
                    assert mandate is not None
                    fenced = self._fence(row, mandate)
                    _need(fenced is None, fenced or "learning_run_unavailable")
                row = self._set(db, row, lease_expires=self._now() + LEASE_SECONDS)
                yield db, row

    def advance(
        self,
        principal: LearningPrincipal,
        claim: Claim,
        *,
        state: str = "running",
        stage: str,
        progress: tuple[int, int] | None = None,
        evidence_refs: tuple[str, ...] = (),
    ) -> Json:
        _need(state in WORKING and stage in STAGES, "invalid_arguments")
        if progress is not None:
            done, total = progress
            _need(
                type(done) is int and type(total) is int and 0 <= done <= total <= 10**9,
                "invalid_arguments",
            )
        with self._leased(principal, claim) as (db, row):
            _need(row["state"] in WORKING, "learning_run_not_running")
            evidence = self._evidence(row, evidence_refs)
            changes: Json = {"state": state, "stage": stage, "evidence": evidence}
            if progress is not None:
                changes["progress"] = canonical({"completed": progress[0], "total": progress[1]})
            if state != row["state"]:
                self._event(db, row["tenant"], row["run_ref"], "advanced", state, None)
            row = self._set(db, row, **changes)
            return {"run_ref": row["run_ref"], "state": row["state"], "stage": row["stage"]}

    @staticmethod
    def _evidence(row: sqlite3.Row, refs: tuple[str, ...]) -> str:
        evidence = list(json.loads(row["evidence"]))
        for ref in refs:
            if _ref(ref) not in evidence:
                evidence.append(ref)
        _need(len(evidence) <= MAX_EVIDENCE, "invalid_arguments")
        return canonical(evidence)

    def reserve_fit(
        self,
        principal: LearningPrincipal,
        claim: Claim,
        *,
        residual_ref: str,
        model_digest: str,
        max_units: int,
    ) -> Json:
        """Reserve at most one fit for this run through the OPE-442 ledger port."""
        _ref(residual_ref)
        _ref(model_digest)
        _need(_units(max_units) and max_units > 0, "invalid_arguments")
        with self._leased(principal, claim) as (db, row):
            _need(
                row["effective"] == "evaluate_and_improve" and row["state"] in WORKING,
                "fit_outside_run_scope",
            )
            if row["fit_reservation"] is not None:
                return {
                    "state": "reserved",
                    "reservation_ref": row["fit_reservation"],
                    "reserved_units": row["reserved_units"],
                }
            if self.ledger is None:
                self._fit_hold(db, row, "fit_ledger_unavailable")
                return {"state": "held", "reason_code": "fit_ledger_unavailable"}
            plan = LearningPlan.from_document(json.loads(row["plan"]))
            intent = FitIntent(
                row["tenant"],
                row["mandate_id"],
                row["mandate_revision"],
                plan.source_revision,
                plan.plan_ref,
                model_digest,
                row["run_ref"],
                residual_ref,
                max_units,
            )
            prior = row["fit_intent"]
            _need(prior is None or prior == intent.identity, "fit_intent_changed")
            self._set(db, row, fit_intent=intent.identity)
        # Never call the ledger inside the grant guard; reconcile an earlier attempt first.
        try:
            reservation = self.ledger.lookup(intent) or self.ledger.reserve(
                intent, operation_key=row["run_ref"] + "-fit"
            )
        except LedgerHold as hold:
            code = hold.reason_code if hold.reason_code in REASONS else "fit_ledger_unavailable"
            with self._leased(principal, claim, allow_cancel=True) as (db, row):
                self._fit_hold(db, row, code)
            return {"state": "held", "reason_code": code}
        except (OSError, sqlite3.Error, ValueError) as error:
            # Unknown outcome: the next attempt reconciles through lookup before reserving.
            raise LearningRunError("fit_ledger_unavailable") from error
        _ref(reservation.reservation_ref, "fit_ledger_unavailable")
        _need(
            _units(reservation.reserved_units) and 0 < reservation.reserved_units <= max_units,
            "fit_ledger_unavailable",
        )
        # Record the reservation even if cancellation arrived meanwhile; that path settles it.
        with self._leased(principal, claim, allow_cancel=True) as (db, row):
            if row["fit_reservation"] is None:
                self._set(
                    db,
                    row,
                    fit_reservation=reservation.reservation_ref,
                    reserved_units=reservation.reserved_units,
                )
                self._event(db, row["tenant"], row["run_ref"], "fit_reserved", row["state"], None)
            else:
                _need(row["fit_reservation"] == reservation.reservation_ref, "fit_intent_changed")
        return {
            "state": "reserved",
            "reservation_ref": reservation.reservation_ref,
            "reserved_units": reservation.reserved_units,
        }

    def _settle(self, reservation: str, consumed_units: int) -> FitReservation:
        """Settle measured usage once through the ledger; an unknown answer stays pending."""
        _need(self.ledger is not None, "fit_ledger_unavailable")
        assert self.ledger is not None
        try:
            settled = self.ledger.settle(reservation, consumed_units=consumed_units)
        except (LedgerHold, OSError, sqlite3.Error, ValueError) as error:
            raise LearningRunError("fit_ledger_unavailable") from error
        _need(
            settled.reservation_ref == reservation
            and settled.consumed_units is not None
            and _units(settled.consumed_units),
            "fit_ledger_unavailable",
        )
        return settled

    def _fit_hold(self, db: sqlite3.Connection, row: sqlite3.Row, reason: str) -> None:
        if row["reason_code"] != reason:
            self._set(db, row, reason_code=reason)
            self._event(db, row["tenant"], row["run_ref"], "fit_held", row["state"], reason)

    def begin_submission(self, principal: LearningPrincipal, claim: Claim) -> str:
        """Record trainer-submission intent before the call; return its deterministic key."""
        with self._leased(principal, claim) as (db, row):
            _need(row["fit_reservation"] is not None, "fit_reservation_required")
            _need(row["submission"] != "submitted", "trainer_job_exists")
            _need(row["submission"] is None, "trainer_submission_uncertain")
            self._set(db, row, submission="pending")
            return "submit-" + digest([row["run_ref"], row["fit_reservation"]])[:40]

    def record_submission(
        self,
        principal: LearningPrincipal,
        claim: Claim,
        *,
        execution_ref: str | None,
        uncertain: bool = False,
    ) -> Json:
        with self._leased(principal, claim, allow_cancel=True) as (db, row):
            _need(row["submission"] == "pending", "trainer_submission_not_started")
            if uncertain or execution_ref is None:
                row = self._set(
                    db,
                    row,
                    submission="uncertain",
                    state="held",
                    reason_code="trainer_submission_uncertain",
                )
                self._event(
                    db,
                    row["tenant"],
                    row["run_ref"],
                    "held",
                    "held",
                    "trainer_submission_uncertain",
                )
            else:
                row = self._set(db, row, submission="submitted", execution_ref=_ref(execution_ref))
                self._event(db, row["tenant"], row["run_ref"], "submitted", row["state"], None)
            return {
                "run_ref": row["run_ref"],
                "state": row["state"],
                "submission": row["submission"],
            }

    def reconcile_submission(
        self, principal: LearningPrincipal, claim: Claim, *, execution_ref: str | None
    ) -> Json:
        """Resolve an uncertain trainer submission from the backend's own answer."""
        with self._leased(principal, claim, allow_cancel=True) as (db, row):
            _need(row["submission"] in ("pending", "uncertain"), "trainer_submission_not_uncertain")
            if execution_ref is not None:
                changes: Json = {"submission": "submitted", "execution_ref": _ref(execution_ref)}
            else:
                changes = {"submission": None, "execution_ref": None}
            if row["state"] == "held":
                changes.update(
                    state="cancellation_pending" if row["cancel_requested"] else "running",
                    reason_code=None,
                )
            row = self._set(db, row, **changes)
            self._event(db, row["tenant"], row["run_ref"], "reconciled", row["state"], None)
            return {
                "run_ref": row["run_ref"],
                "state": row["state"],
                "submission": row["submission"],
            }

    def finish(
        self,
        principal: LearningPrincipal,
        claim: Claim,
        outcome: str,
        *,
        reason_code: str | None = None,
        consumed_units: int | None = None,
        evidence_refs: tuple[str, ...] = (),
    ) -> Json:
        """Record a terminal or review-ready outcome after settling any reservation."""
        _need(outcome in ("review_ready", "completed", "failed", "refused"), "invalid_arguments")
        _need(reason_code is None or reason_code in REASONS, "invalid_arguments")
        _need(consumed_units is None or _units(consumed_units), "invalid_arguments")
        with self._leased(principal, claim, allow_cancel=outcome in ("failed", "refused")) as (
            db,
            row,
        ):
            _need(not row["cancel_requested"], "learning_run_cancel_requested")
            _need(row["submission"] != "uncertain", "trainer_submission_uncertain")
            reservation = row["fit_reservation"]
            _need(reservation is None or consumed_units is not None, "measured_usage_required")
            self._evidence(row, evidence_refs)
            if consumed_units is not None:
                self._set(db, row, measured_units=consumed_units)
        settled = None
        if reservation is not None:
            assert consumed_units is not None
            settled = self._settle(reservation, consumed_units)
        with self._leased(principal, claim, allow_cancel=True) as (db, row):
            row = self._set(
                db,
                row,
                state=outcome,
                stage=None,
                reason_code=reason_code,
                lease=None,
                evidence=self._evidence(row, evidence_refs),
                consumed_units=None if settled is None else settled.consumed_units,
            )
            self._event(db, row["tenant"], row["run_ref"], "finished", outcome, reason_code)
            # The first terminal transition wins. A cancellation that arrived after the outcome
            # was settled cannot be confirmed as a cancel, so its receipt says so.
            db.execute(
                "UPDATE lr_cancellations SET state='not_applicable',confirmed_at=? "
                "WHERE tenant=? AND run_ref=? AND state='pending'",
                (self._now(), row["tenant"], row["run_ref"]),
            )
            return {"run_ref": row["run_ref"], "state": row["state"]}

    def confirm_cancel(
        self, principal: LearningPrincipal, claim: Claim, *, consumed_units: int
    ) -> Json:
        """Confirm that backend work stopped, then settle measured usage before reporting."""
        _need(_units(consumed_units), "invalid_arguments")
        with self._leased(principal, claim, allow_cancel=True) as (db, row):
            _need(row["cancel_requested"], "learning_run_cancel_not_requested")
            _need(row["submission"] in (None, "submitted"), "trainer_submission_uncertain")
            reservation = row["fit_reservation"]
            # Record that backend work stopped and keep the measured usage first, so a retry
            # after a ledger outage can settle it without a worker.
            self._set(
                db,
                row,
                lease=None,
                measured_units=consumed_units,
                submission=None if row["submission"] is None else "stopped",
            )
        settled = None if reservation is None else self._settle(reservation, consumed_units)
        self._confirm_cancel(principal.tenant, claim.run_ref, reservation, settled)
        with self._db(write=False) as db:
            row = db.execute(
                "SELECT * FROM lr_runs WHERE tenant=? AND run_ref=?",
                (principal.tenant, claim.run_ref),
            ).fetchone()
            return self._cancel_view(db, row)


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS lr_runs (
        tenant TEXT NOT NULL, run_ref TEXT NOT NULL, intent TEXT NOT NULL,
        workflow_ref TEXT NOT NULL, plan_ref TEXT NOT NULL, plan TEXT NOT NULL,
        operation TEXT NOT NULL, effective TEXT NOT NULL,
        mandate_id TEXT NOT NULL, mandate_revision TEXT NOT NULL, owner TEXT NOT NULL,
        state TEXT NOT NULL, stage TEXT, reason_code TEXT, progress TEXT, evidence TEXT NOT NULL,
        lease TEXT, lease_expires INTEGER, dispatched INTEGER NOT NULL,
        fit_intent TEXT, fit_reservation TEXT, reserved_units INTEGER, measured_units INTEGER,
        consumed_units INTEGER, submission TEXT, execution_ref TEXT,
        cancel_requested INTEGER NOT NULL,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
        PRIMARY KEY(tenant, run_ref), UNIQUE(tenant, intent))""",
    """CREATE TABLE IF NOT EXISTS lr_keys (
        tenant TEXT NOT NULL, client TEXT NOT NULL, kind TEXT NOT NULL, key_hash TEXT NOT NULL,
        target TEXT NOT NULL, PRIMARY KEY(tenant, client, kind, key_hash))""",
    """CREATE TABLE IF NOT EXISTS lr_triggers (
        tenant TEXT NOT NULL, run_ref TEXT NOT NULL, channel TEXT NOT NULL, client TEXT NOT NULL,
        key_hash TEXT NOT NULL, at INTEGER NOT NULL,
        PRIMARY KEY(tenant, run_ref, client, key_hash))""",
    """CREATE TABLE IF NOT EXISTS lr_events (
        tenant TEXT NOT NULL, run_ref TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
        state TEXT NOT NULL, reason_code TEXT, at INTEGER NOT NULL,
        PRIMARY KEY(tenant, run_ref, seq))""",
    """CREATE TABLE IF NOT EXISTS lr_cancellations (
        tenant TEXT NOT NULL, run_ref TEXT NOT NULL, receipt_ref TEXT NOT NULL,
        state TEXT NOT NULL, consumed_units INTEGER, requested_at INTEGER NOT NULL,
        confirmed_at INTEGER, PRIMARY KEY(tenant, run_ref), UNIQUE(tenant, receipt_ref))""",
    """CREATE TABLE IF NOT EXISTS lr_ticks (
        tenant TEXT NOT NULL, seq INTEGER NOT NULL, mandate_revision TEXT, plan_ref TEXT,
        outcome TEXT NOT NULL, reason_code TEXT, run_ref TEXT, at INTEGER NOT NULL,
        PRIMARY KEY(tenant, seq))""",
)


# ---- owner-local grant, current mandate and sealed plan handoff ------------------------------


@dataclass(frozen=True)
class LearningSelection:
    tenant: str
    actor: str
    role: str
    client: str
    channel: str
    engagement_epoch: str
    workflow_ref: str
    mandate_id: str
    grant_ref: str

    @classmethod
    def read(cls, path: Path) -> LearningSelection:
        code = "private_learning_selection_required"
        try:
            _need(
                path.is_absolute()
                and path.suffix == ".json"
                and path.parent.resolve(strict=True) == path.parent,
                code,
            )
            data = _read_private(path, MAX_RECORD_BYTES)
        except (OSError, RuntimeError, ManifestError, TrainingError) as error:
            raise LearningRunError(code) from error
        _need(
            set(data) == {"schema_version", *cls.__dataclass_fields__}
            and data["schema_version"] == SELECTION_SCHEMA,
            code,
        )
        values = {name: _ref(data[name], code) for name in cls.__dataclass_fields__}
        _need(
            values["role"] in ROLE_PERMISSIONS
            and values["channel"] in CHANNELS
            and (values["role"] == "scheduler") == (values["channel"] == "schedule")
            and _hex(values["engagement_epoch"], 32),
            code,
        )
        return cls(**values)

    def principal(self) -> LearningPrincipal:
        return LearningPrincipal(self.tenant, self.actor, self.role, self.client, self.channel)


_GRANT_FIELDS = {
    "schema_version",
    "purpose",
    "owner_id",
    "permissions",
    "expires_at",
    *LearningSelection.__dataclass_fields__,
}


def _grant_path(directory: Path, grant_ref: str) -> Path:
    return directory / f"learning-run-grant-{grant_ref}.json"


def _withdrawal_path(directory: Path, grant_ref: str) -> Path:
    return directory / f"learning-run-withdrawn-{grant_ref}.json"


def _key(directory: Path) -> str:
    record = _read_private(directory / KEY_FILE, MAX_RECORD_BYTES)
    key = record.get("key")
    _need(set(record) == {"key"} and _hex(key))
    assert isinstance(key, str)
    return key


class LocalLearningRunGrant:
    """Consume the owner-sealed learning-run grant for exactly one connection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.selected = LearningSelection.read(path)
        self.principal = self.selected.principal()
        try:
            self.store = LocalTrainingStore(
                path.parent / "training.sqlite3", self.selected.tenant, existing_only=True
            )
        except (OSError, sqlite3.Error, TrainingError) as error:
            raise LearningRunError("learning_run_unavailable") from error
        self._pinned = self._read_grant()

    def _read_grant(self) -> tuple[frozenset[str], int, str]:
        directory, wanted = self.path.parent, self.selected
        try:
            key = _key(directory)
            record = _read_private(_grant_path(directory, wanted.grant_ref), MAX_RECORD_BYTES)
        except (OSError, ManifestError, TrainingError) as error:
            raise LearningRunError("learning_run_unavailable") from error
        body, seal = record.get("grant"), record.get("seal_sha256")
        _need(
            set(record) == {"grant", "seal_sha256"}
            and isinstance(body, dict)
            and _hex(seal)
            and set(body) == _GRANT_FIELDS
            and body["schema_version"] == GRANT_SCHEMA
            and body["purpose"] == GRANT_PURPOSE
            and all(body[name] == getattr(wanted, name) for name in wanted.__dataclass_fields__)
        )
        assert isinstance(body, dict) and isinstance(seal, str)
        _need(hmac.compare_digest(_seal(key, body), seal))
        permissions = body["permissions"]
        _need(
            isinstance(permissions, list)
            and permissions
            and len(set(permissions)) == len(permissions)
            and set(permissions) <= set(ROLE_PERMISSIONS[wanted.role])
            and type(body["expires_at"]) is int
            and body["expires_at"] > 0
        )
        _ref(body["owner_id"], "learning_run_unavailable")
        _need(not os.path.lexists(_withdrawal_path(directory, wanted.grant_ref)))
        return frozenset(permissions), body["expires_at"], seal

    @contextmanager
    def guard(self, principal: LearningPrincipal, *, write: bool) -> Iterator[LearningScope]:
        wanted = self.selected
        try:
            # The custody guard comes first, as for intake, so withdrawal serializes with effects.
            with self.store.engagement_guard(
                engagement_id=wanted.engagement_epoch, read_only=not write
            ):
                _need(principal == self.principal and LearningSelection.read(self.path) == wanted)
                grant = self._read_grant()
                _need(grant == self._pinned)
                _need(time.time() < grant[1], "learning_run_expired")
                yield LearningScope(
                    wanted.tenant,
                    wanted.engagement_epoch,
                    wanted.workflow_ref,
                    wanted.mandate_id,
                    grant[0],
                    grant[2],
                )
        except (OSError, sqlite3.Error, ManifestError, TrainingError, MandateError) as error:
            raise LearningRunError("learning_run_unavailable") from error


class LocalMandateSource:
    """Read the current OPE-426 mandate revision on every call. It keeps no copy."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def current(self, scope: LearningScope, principal: LearningPrincipal) -> MandateState:
        try:
            registry = LearningMandates(self.directory, scope.tenant, scope.engagement_epoch)
            mandate_id, head, lineage = self._current(registry, scope.mandate_id)
            details = registry.inspect(mandate_id)
            spec = details["scope"]
            _need(spec["workflow"] == scope.workflow_ref, "standing_mandate_unavailable")
            if head == "pause":
                state = "paused"
            elif head == "revoke" or details["withdrawn"]:
                state = "withdrawn"
            elif details["expired"]:
                state = "expired"
            else:
                state = "active"
            return MandateState(
                mandate_id=str(details["mandate_id"]),
                mandate_revision=str(details["mandate_revision"]),
                owner_id=str(spec["owner_id"]),
                delegate=str(spec["delegate"]),
                state=state,
                autonomous=spec["mode"] == "autonomous",
                training="train" in spec["uses"] and bool(spec["profile"]),
                max_runs=int(spec["max_runs"]),
                runs_used=int(details["cumulative_runs"]),
                max_fits=int(spec["max_fits"]),
                fits_reserved=int(details["cumulative_reserved_fits"]),
                lineage=lineage,
            )
        except (
            MandateError,
            TrainingError,
            ManifestError,
            OSError,
            sqlite3.Error,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            raise LearningRunError("standing_mandate_unavailable") from error

    @staticmethod
    def _current(
        registry: LearningMandates, pinned: str
    ) -> tuple[str, str | None, tuple[str, ...]]:
        """Follow the owner's renewals; the owner-settings head, when present, is current."""
        with registry._db(write=False) as db:
            chain = [pinned]
            while len(chain) <= 64:
                row = db.execute(
                    "SELECT successor FROM renewals WHERE scope=? AND prior=?",
                    (registry.scope, chain[-1]),
                ).fetchone()
                if row is None:
                    break
                chain.append(str(row[0]))
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_settings_heads'"
            ).fetchone():
                head = db.execute(
                    "SELECT r.receipt FROM owner_settings_heads h JOIN owner_settings_revisions r "
                    "ON r.scope=h.scope AND r.revision=h.revision WHERE h.scope=?",
                    (registry.scope,),
                ).fetchone()
                if head is not None:
                    receipt = json.loads(head[0])
                    _need(receipt.get("mandate_id") in chain, "standing_mandate_unavailable")
                    return str(receipt["mandate_id"]), receipt.get("state"), tuple(chain)
            return chain[-1], None, tuple(chain)


def plan_record_path(directory: Path, tenant: str, workflow_ref: str) -> Path:
    return directory / f"learning-plan-{digest([tenant, workflow_ref])[:32]}.json"


class SealedPlanSource:
    """Read the worker-sealed current plan that the discovery stage published (OPE-562)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def current_plan(
        self, scope: LearningScope, mandate: MandateState
    ) -> LearningPlan | PlanUnavailable:
        path = plan_record_path(self.directory, scope.tenant, scope.workflow_ref)
        if not os.path.lexists(path):
            return PlanUnavailable("no_eligible_plan")
        try:
            key = _key(self.directory)
            record = _read_private(path, MAX_RECORD_BYTES)
            body, seal = record.get("plan"), record.get("seal_sha256")
            _need(set(record) == {"plan", "seal_sha256"} and isinstance(body, dict) and _hex(seal))
            assert isinstance(body, dict) and isinstance(seal, str)
            _need(hmac.compare_digest(_seal(key, body), seal))
            return LearningPlan.from_document(body)
        except (LearningRunError, OSError, ManifestError, TrainingError, TypeError):
            return PlanUnavailable("plan_source_unavailable")


def _write_private(path: Path, value: Json, *, replace: bool = True) -> None:
    data = (canonical(value) + "\n").encode()
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def publish_plan(directory: Path, plan: LearningPlan) -> str:
    """Trusted worker-side producer: seal the current plan for one workflow."""
    key = _key(directory)
    body = plan.document()
    _write_private(
        plan_record_path(directory, plan.tenant, plan.workflow_ref),
        {"plan": body, "seal_sha256": _seal(key, body)},
    )
    return plan.plan_ref


def open_learning_runs(
    selection: Path, *, plans: PlanSource | None = None, ledger: FitLedger | None = None
) -> tuple[LearningPrincipal, LearningRuns]:
    """Open one scoped connection. Without an OPE-442 ledger, fit-capable runs are held."""
    authority = LocalLearningRunGrant(selection)
    with authority.guard(authority.principal, write=False):
        pass
    service = LearningRuns(
        selection.parent,
        authority,
        mandates=LocalMandateSource(selection.parent),
        plans=SealedPlanSource(selection.parent) if plans is None else plans,
        ledger=ledger,
    )
    return authority.principal, service


def learning_run_dispatch(
    principal: LearningPrincipal, service: LearningRuns
) -> Callable[[str, Json], Json]:
    """Serve the connection selected by the trusted launcher (TLS or local Socket Mode)."""
    _identity(principal)
    _need(principal.channel in ("slack", "zapier"))
    calls: deque[float] = deque()

    def call(name: str, arguments: Json) -> Json:
        now = monotonic()
        while calls and calls[0] <= now - 60:
            calls.popleft()
        if len(calls) >= CALLS_PER_MINUTE:
            return tool_error("learning_run_rate_limited")
        calls.append(now)
        try:
            if name == SUBMIT:
                return tool_result(service.submit(principal, parse_submit(arguments)))
            if name == STATUS:
                return tool_result(service.status(principal, parse_run(arguments)))
            if name == CANCEL:
                run, key = parse_cancel(arguments)
                return tool_result(service.cancel(principal, run, key))
            return tool_error("invalid_arguments")
        except LearningRunError as error:
            code = str(error)
            return tool_error(code if code in ERRORS else "learning_run_unavailable")
        except (OSError, sqlite3.Error, MandateError, TrainingError, ManifestError, LedgerHold):
            return tool_error("learning_run_unavailable")

    return call


def run_schedule_tick(
    directory: Path,
    organization: str,
    *,
    plans: PlanSource | None = None,
    ledger: FitLedger | None = None,
) -> Json:
    """One worker schedule tick from the owner-authorized scheduler selection."""
    principal, service = open_learning_runs(
        directory / SCHEDULE_SELECTION, plans=plans, ledger=ledger
    )
    _need(principal.tenant == organization and principal.channel == "schedule")
    return service.schedule_tick(principal)


# ---- owner-local setup (never reachable from the TLS gateway or an agent tool) --------------


def _authorize(args: argparse.Namespace) -> Json:
    selection: Path = args.selection
    directory = selection.parent
    _need(
        selection.is_absolute() and selection.suffix == ".json",
        "private_learning_selection_required",
    )
    store = LocalTrainingStore(directory / "training.sqlite3", args.tenant, existing_only=True)
    engagement = store.engagement_id
    registry = LearningMandates(directory, args.tenant, engagement)
    details = registry.inspect(args.mandate, actor=args.owner)
    spec = details["scope"]
    _need(spec["workflow"] == args.workflow, "learning_run_unavailable")
    with registry.guard_registered_owner(args.owner):
        _need(spec["owner_id"] == args.owner, "registered_owner_approval_required")
    if args.role == "owner":
        _need(args.actor == args.owner, "learning_run_role_required")
    if args.role == "scheduler":
        _need(args.actor == spec["delegate"], "standing_delegate_mismatch")
    permissions = sorted(set(args.permission or ROLE_PERMISSIONS[args.role]))
    _need(set(permissions) <= set(ROLE_PERMISSIONS[args.role]), "learning_run_role_required")
    _need(type(args.expires_at) is int and args.expires_at > time.time(), "learning_run_expired")
    values = {
        "tenant": args.tenant,
        "actor": args.actor,
        "role": args.role,
        "client": args.client,
        "channel": args.channel,
        "engagement_epoch": engagement,
        "workflow_ref": args.workflow,
        "mandate_id": args.mandate,
        "grant_ref": args.grant_ref,
    }
    selected = {"schema_version": SELECTION_SCHEMA, **values}
    key_path = directory / KEY_FILE
    if not os.path.lexists(key_path):
        _write_private(key_path, {"key": secrets.token_hex(32)}, replace=False)
    body = {
        **values,
        "schema_version": GRANT_SCHEMA,
        "purpose": GRANT_PURPOSE,
        "owner_id": args.owner,
        "permissions": permissions,
        "expires_at": args.expires_at,
    }
    record = {"grant": body, "seal_sha256": _seal(_key(directory), body)}
    for path, value in ((_grant_path(directory, args.grant_ref), record), (selection, selected)):
        if os.path.lexists(path):
            _need(_read_private(path, MAX_RECORD_BYTES) == value, "learning_grant_exists")
        else:
            _write_private(path, value, replace=False)
    LearningSelection.read(selection)
    return {"grant_ref": args.grant_ref, "role": args.role, "permissions": permissions}


def _withdraw(args: argparse.Namespace) -> Json:
    selected = LearningSelection.read(args.selection)
    directory = args.selection.parent
    registry = LearningMandates(directory, selected.tenant, selected.engagement_epoch)
    with registry.guard_registered_owner(args.owner):
        marker = _withdrawal_path(directory, selected.grant_ref)
        if not os.path.lexists(marker):
            _write_private(marker, {"withdrawn_at": int(time.time())}, replace=False)
    return {"grant_ref": selected.grant_ref, "withdrawn": True}


def main(argv: list[str] | None = None) -> int:
    """Owner-local grant setup. The learning-run endpoints cannot create or widen a grant."""
    parser = argparse.ArgumentParser(description=main.__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    authorize = commands.add_parser("authorize")
    authorize.add_argument("--selection", type=Path, required=True)
    authorize.add_argument("--tenant", required=True)
    authorize.add_argument("--owner", required=True)
    authorize.add_argument("--actor", required=True)
    authorize.add_argument("--role", choices=sorted(ROLE_PERMISSIONS), required=True)
    authorize.add_argument("--channel", choices=CHANNELS, required=True)
    authorize.add_argument("--client", required=True)
    authorize.add_argument("--workflow", required=True)
    authorize.add_argument("--mandate", required=True)
    authorize.add_argument("--grant-ref", required=True)
    authorize.add_argument("--expires-at", type=int, required=True)
    authorize.add_argument("--permission", action="append")
    withdraw = commands.add_parser("withdraw")
    withdraw.add_argument("--selection", type=Path, required=True)
    withdraw.add_argument("--owner", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "authorize":
            for value in (args.tenant, args.owner, args.actor, args.client, args.workflow):
                _ref(value, "invalid_arguments")
            _ref(args.mandate, "invalid_arguments")
            _ref(args.grant_ref, "invalid_arguments")
            result = _authorize(args)
        else:
            result = _withdraw(args)
    except (LearningRunError, MandateError, TrainingError, ManifestError) as error:
        print(canonical({"state": "held", "reason": str(error)}))
        return 2
    except (OSError, sqlite3.Error):
        print(canonical({"state": "held", "reason": "private_learning_setup_unavailable"}))
        return 2
    print(canonical(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
