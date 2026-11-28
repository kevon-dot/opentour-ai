"""Project a signed, preallocated EVE fit into the installed private Slack workbench."""

from __future__ import annotations

import hmac
import json
import math
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opentour_cell.pilot.eve_exchange import STALE_SECONDS, TERMINAL, digest
from opentour_cell.pilot.slack_learning import (
    Control,
    LearningPlan,
    LearningScope,
    LearningStatus,
    Operation,
    bind_approval,
)

if TYPE_CHECKING:
    from opentour_cell.pilot.eve_authority import EveAuthority
    from opentour_cell.pilot.eve_exchange import Exchange

_ACTIVE = {"queued", "running", "pausing", "paused", "resuming", "stopping", "held"}
_REF = re.compile(r"[A-Za-z0-9_.:~-]{1,160}")


def _require(condition: object) -> None:
    if not condition:
        raise ValueError("eve_learning_unavailable")


def _status(
    row: dict[str, Any] | None, reservation: str, spec: dict[str, Any], *, reviewer: bool
) -> LearningStatus:
    _require(row is not None)
    assert row is not None
    _require(
        row["reservation_ref"] == reservation
        and row["spec_digest"] == digest(spec)
        and row["deadline"] == spec["deadline"]
        and row["total_steps"] == spec["steps"]
    )
    state, heartbeat, deadline = row["state"], row["heartbeat"], row["deadline"]
    _require(
        state in TERMINAL | _ACTIVE
        and isinstance(row["run_ref"], str)
        and _REF.fullmatch(row["run_ref"])
        and isinstance(row["revision"], str)
        and row["revision"]
    )
    now = time.time()
    _require(type(heartbeat) in (int, float) and math.isfinite(heartbeat) and heartbeat >= 0)
    _require(type(deadline) in (int, float) and math.isfinite(deadline) and deadline > 0)
    stale = state in _ACTIVE and (
        not heartbeat
        or not 0 <= now - heartbeat <= STALE_SECONDS
        or now >= row["permit_until"]
        or now >= deadline
    )
    active = row["active_seconds"]
    _require(
        active is None or type(active) in (int, float) and math.isfinite(active) and active >= 0
    )
    available = reviewer and not stale and now < deadline
    result = LearningStatus(
        run_ref=row["run_ref"],
        state=state,
        can_cancel=False,
        revision=row["revision"],
        completed_steps=row["completed_steps"] if heartbeat else None,
        total_steps=row["total_steps"],
        checkpoint_step=row["checkpoint_step"] if heartbeat else None,
        active_seconds=int(active) if heartbeat and active is not None else None,
        stale=stale,
        can_pause=available and state == "running",
        can_resume=available and state == "paused" and row["checkpoint_step"] is not None,
        can_stop=available
        and state in {"queued", "running", "pausing", "paused", "resuming", "held"},
    )
    result.validate()
    return result


class EveLearningService:
    """Take every projection and mutation under the current customer authority."""

    def __init__(self, authority: EveAuthority, exchange: Exchange, directory: Path, channel: str):
        self.authority = authority
        self.exchange = exchange
        self.directory = directory
        self.channel = channel

    def _verify_decision(
        self,
        scope: LearningScope,
        plan: str,
        action: str,
        supplied: str,
        approval_revision: str,
        *,
        receipt: LearningStatus | None = None,
    ) -> None:
        from opentour_cell.pilot.receipt_recovery import _JournalSnapshot, _open_db

        path = self.directory / "slack-pilot.sqlite3"
        with _open_db(path) as db:
            _require(db is not None)
            assert db is not None
            row = db.execute("SELECT binding FROM identity WHERE id=1").fetchone()
            _require(row)
            assert row is not None
            binding = json.loads(row[0])
            _require(
                isinstance(binding, list)
                and len(binding) == 5
                and binding[:2] == [scope.organization, scope.workspace]
                and binding[3:] == [self.channel, str(self.directory)]
            )
            journal = _JournalSnapshot(db, path, row[0])
            journal.active(db)
            if receipt is None:
                expected = journal.opaque(
                    "learning_start",
                    bind_approval([scope.__dict__, plan, action], approval_revision),
                )
            else:
                run = journal.opaque("learning_run", [scope.__dict__, receipt.run_ref])
                version = journal.opaque(
                    "control", bind_approval([run, plan, receipt.revision], approval_revision)
                )
                expected = journal.opaque("learning_control", [run + "." + version, action])
            _require(hmac.compare_digest(supplied, expected))

    def verify_start(self, scope: LearningScope, plan_ref: str, key: str, revision: str) -> None:
        self._verify_decision(scope, plan_ref, "evaluate_and_improve", key, revision)

    def plan(self, scope: LearningScope) -> LearningPlan | None:
        try:
            with self.authority.current(scope, write=False) as grant:
                row = self.exchange.get(grant.reservation_ref)
                reviewer = scope.actor == grant.reviewer and grant.can_control
                receipt = (
                    _status(row, grant.reservation_ref, grant.spec, reviewer=reviewer)
                    if row is not None
                    else None
                )
                return LearningPlan(
                    scope=scope,
                    plan_ref=grant.plan_ref,
                    model_route="eve-24b-qlora",
                    benchmark_ref=grant.benchmark_ref,
                    mandate_ref=grant.reservation_ref,
                    max_fits=1,
                    spend_cap_cents=grant.max_cents,
                    schedule="off",
                    can_benchmark=False,
                    can_improve=reviewer and receipt is None,
                    current_run=receipt.run_ref if receipt is not None else None,
                    approval_revision=digest(grant.spec),
                )
        except Exception:
            # An unreadable, refused or unverified local enrollment has no disclosable plan.
            return None

    def start(
        self, scope: LearningScope, plan_ref: str, operation: Operation, key: str
    ) -> LearningStatus:
        with self.authority.current(scope, write=True) as grant:
            _require(
                scope.actor == grant.reviewer
                and grant.can_control
                and grant.plan_ref == plan_ref
                and operation == "evaluate_and_improve"
                and re.fullmatch(r"learning_start_[a-f0-9]{26}", key)
            )
            self.verify_start(scope, plan_ref, key, digest(grant.spec))
            # The exchange owns idempotency for the signed reservation and the Slack decision key.
            row = self.exchange.get(grant.reservation_ref)
            if row is not None:
                _status(row, grant.reservation_ref, grant.spec, reviewer=True)
            self.authority.claim(grant)
            row = self.exchange.admit(
                grant.reservation_ref, grant.spec, actor=scope.actor, decision_key=key
            )
            return _status(row, grant.reservation_ref, grant.spec, reviewer=True)

    def status(self, scope: LearningScope, run_ref: str) -> LearningStatus:
        with self.authority.current(scope, write=False) as grant:
            row = self.exchange.get(grant.reservation_ref)
            receipt = _status(
                row,
                grant.reservation_ref,
                grant.spec,
                reviewer=scope.actor == grant.reviewer and grant.can_control,
            )
            _require(receipt.run_ref == run_ref)
            return receipt

    def control(
        self, scope: LearningScope, run_ref: str, action: Control, revision: str, key: str
    ) -> LearningStatus:
        with self.authority.current(scope, write=True) as grant:
            _require(
                scope.actor == grant.reviewer
                and grant.can_control
                and re.fullmatch(r"learning_control_[a-f0-9]{26}", key)
            )
            row = self.exchange.get(grant.reservation_ref)
            receipt = _status(row, grant.reservation_ref, grant.spec, reviewer=True)
            _require(
                receipt.run_ref == run_ref
                and receipt.revision == revision
                and action in receipt.controls()
            )
            self._verify_decision(
                scope, grant.plan_ref, action, key, digest(grant.spec), receipt=receipt
            )
            row = self.exchange.control(grant.reservation_ref, action, revision, key)
            result = _status(row, grant.reservation_ref, grant.spec, reviewer=True)
            _require(result.run_ref == run_ref)
            return result

    def cancel(self, scope: LearningScope, run_ref: str) -> LearningStatus:
        # The legacy operation has no observed revision. EVE requires its versioned Stop action.
        with self.authority.current(scope, write=True):
            raise ValueError("eve_versioned_control_required")


def open_eve_learning_service(
    directory: Path, organization: str, workspace: str, channel: str
) -> EveLearningService | None:
    from opentour_cell.pilot.eve_authority import EveAuthority
    from opentour_cell.pilot.eve_exchange import Exchange

    authority = EveAuthority(directory, organization, workspace, channel)
    if not authority.configured:
        return None
    return EveLearningService(authority, Exchange(directory / "eve-exchange"), directory, channel)
