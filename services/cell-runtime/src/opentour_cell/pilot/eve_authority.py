"""Authorize the one preallocated EVE fit against the current plan and issuer-signed cap."""

from __future__ import annotations

import os
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from opentour_cell.pilot import fit_dispatch as fd
from opentour_cell.pilot import learning_runs as lr
from opentour_cell.pilot.eve_exchange import Exchange, digest, run_ref
from opentour_cell.pilot.learning_mandate import LearningMandates
from opentour_cell.pilot.mandate_fit_ledger import MandateFitLedger
from opentour_cell.pilot.repair_bundle import _read
from opentour_cell.pilot.slack_learning import LearningScope

CAP_FILE = "eve-spend-mandate.json"
SELECTION_FILE = "eve-learning-selection.json"
REGISTRY_FILE = "pilot-issuer-registry.json"
EVE = "b4965c4acbb27299078e4f97171530644fcc839f"
REF_FIELDS = {
    "tenant",
    "engagement_id",
    "workspace",
    "channel",
    "reviewer",
    "workflow",
    "dataset",
    "mandate_id",
    "reservation_ref",
    "payer_ref",
    "plan_ref",
    "benchmark_ref",
    "host_ref",
}
HASH_FIELDS = {
    "dataset_revision",
    "mandate_revision",
    "development_sha256",
    "protected_sha256",
    "fit_intent_digest",
}
INT_FIELDS = {"starts_at", "deadline", "max_cents", "hourly_cents", "steps", "max_rows"}


@dataclass(frozen=True)
class Grant:
    plan_ref: str
    reservation_ref: str
    benchmark_ref: str
    reviewer: str
    max_cents: int
    spec: dict[str, Any]
    can_control: bool = False


def parse_grant(payload: dict[str, object], now: float, *, read_only: bool = False) -> Grant:
    spec = payload.get("reservation")
    if (
        set(payload)
        != {"profile_version", "tenant", "engagement_id", "issued_at", "not_after", "reservation"}
        or payload["profile_version"] != "1.0"
        or not isinstance(spec, dict)
        or set(spec) != REF_FIELDS | HASH_FIELDS | INT_FIELDS | {"model_revision", "image_id"}
    ):
        raise ValueError("invalid_eve_spend_mandate")
    if (
        any(
            not isinstance(spec[k], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", spec[k])
            for k in REF_FIELDS
        )
        or any(
            not isinstance(spec[k], str) or not re.fullmatch(r"[0-9a-f]{64}", spec[k])
            for k in HASH_FIELDS
        )
        or any(type(spec[k]) is not int for k in INT_FIELDS)
        or spec["model_revision"] != EVE
        or not isinstance(spec["image_id"], str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", spec["image_id"])
        or spec["tenant"] != payload["tenant"]
        or spec["engagement_id"] != payload["engagement_id"]
        or spec["development_sha256"] == spec["protected_sha256"]
    ):
        raise ValueError("invalid_eve_reservation")
    try:
        start = datetime.fromisoformat(str(payload["issued_at"]))
        end = datetime.fromisoformat(str(payload["not_after"]))
        valid_time = (
            start.utcoffset() is not None
            and end.utcoffset() is not None
            and start.timestamp() <= spec["starts_at"] <= now
            and spec["starts_at"] < spec["deadline"] <= end.timestamp()
            and (read_only or now < spec["deadline"])
        )
    except (ValueError, OverflowError, OSError) as error:
        raise ValueError("invalid_eve_window") from error
    window = spec["deadline"] - spec["starts_at"]
    if (
        not valid_time
        or not 8 <= spec["steps"] <= 16
        or not 1 <= spec["max_rows"] <= 5000
        or not 0 <= spec["hourly_cents"]
        or spec["max_cents"] < 0
        or (window * spec["hourly_cents"] + 3599) // 3600 > spec["max_cents"]
    ):
        raise ValueError("eve_window_outside_cap")
    return Grant(
        spec["plan_ref"],
        spec["reservation_ref"],
        spec["benchmark_ref"],
        spec["reviewer"],
        spec["max_cents"],
        spec,
    )


class EveAuthority:
    def __init__(self, directory: Path, organization: str, workspace: str, channel: str) -> None:
        self.directory = directory
        self.organization, self.workspace, self.channel = organization, workspace, channel

    @property
    def configured(self) -> bool:
        return os.path.lexists(self.directory / SELECTION_FILE)

    def _grant(self, *, read_only: bool = False) -> Grant:
        from opentour_cell.pilot.signed_records import (
            MAX_CONFIG_BYTES,
            SPEND_MANDATE_PURPOSE,
            PilotVerifier,
        )

        raw = _read(self.directory / CAP_FILE, 32768)
        registry = _read(self.directory / REGISTRY_FILE, MAX_CONFIG_BYTES)
        return parse_grant(
            PilotVerifier(registry).verify(raw, SPEND_MANDATE_PURPOSE),
            time.time(),
            read_only=read_only,
        )

    def _reservation(
        self, grant: Grant, *, read_only: bool = False
    ) -> tuple[MandateFitLedger, fd.LedgerEntry]:
        """Bind the Slack request to the dispatcher's existing priced reservation."""
        spec = grant.spec
        mandates = LearningMandates(self.directory, spec["tenant"], spec["engagement_id"])
        ledger = MandateFitLedger(mandates)
        entry = ledger.get(grant.reservation_ref)
        if entry is None:
            raise ValueError("eve_fit_reservation_required")
        record, execution = entry.record, entry.execution
        bound, model = record["bound"], record["model"]
        if (
            entry.execution_id != grant.reservation_ref
            or record["intent_digest"] != spec["fit_intent_digest"]
            or record["development_digest"] != spec["development_sha256"]
            or record["tenant"] != spec["tenant"]
            or record["engagement"] != spec["engagement_id"]
            or record["mandate_id"] != spec["mandate_id"]
            or record["mandate_revision"] != spec["mandate_revision"]
            or record["owner_id"] != grant.reviewer
            or record["payer"] != spec["payer_ref"]
            or "plan-" + record["plan_revision"][:40] != grant.plan_ref
            or record["benchmark_revision"] != grant.benchmark_ref
            or model["route"] != "local_open_weight"
            or model["immutable_revision"] != EVE
            or model["account_or_host"] != spec["host_ref"]
            or bound["currency"] != "USD"
            or bound["maximum_minor_units"] != grant.max_cents
            or bound["maximum_steps"] != spec["steps"]
            or bound["maximum_rows"] != spec["max_rows"]
            or bound["maximum_seconds"] != spec["deadline"] - spec["starts_at"]
            or (not read_only and execution.charged_minor_units != grant.max_cents)
            or (not read_only and execution.cancel_requested)
            or (
                not read_only
                and execution.stage
                not in {fd.Stage.RESERVED, fd.Stage.SUBMITTING, fd.Stage.RUNNING}
            )
            or (
                execution.stage != fd.Stage.RESERVED
                and execution.job_ref != run_ref(grant.reservation_ref, spec)
            )
        ):
            raise ValueError("eve_fit_reservation_changed")
        return ledger, entry

    def claim(self, grant: Grant) -> None:
        """Claim one already charged execution after the Slack HMAC has been verified."""
        ledger, entry = self._reservation(grant)
        if entry.execution.stage == fd.Stage.RESERVED:
            job = run_ref(grant.reservation_ref, grant.spec)
            ledger.transition(
                entry.execution_id,
                version=entry.execution.version,
                execution=replace(
                    entry.execution,
                    stage=fd.Stage.SUBMITTING,
                    job_ref=job,
                    submissions=1,
                    lease_owner=job,
                    lease_until=grant.spec["deadline"],
                    submitted_at=time.time(),
                ),
                event="eve_slack_approval",
            )

    def acknowledge(self, grant: Grant, job: dict[str, Any], *, finished: bool) -> None:
        ledger, entry = self._reservation(grant, read_only=True)
        ex = entry.execution
        if ex.job_ref != job["run_ref"]:
            raise ValueError("eve_fit_reservation_changed")
        if finished:
            if ex.stage in {fd.Stage.HELD, fd.Stage.SETTLED}:
                return
            # A terminal optimizer receipt does not measure provider charges. Preserve the
            # full charge until the authorized reconciler supplies the provider's bill.
            next_state = replace(
                ex,
                stage=fd.Stage.HELD,
                hold_reason="usage_unverifiable",
                lease_until=0,
                lease_owner="",
                evidence_ref=job["run_ref"],
            )
            event = "eve_trainer_finished_awaiting_usage"
        else:
            if ex.stage != fd.Stage.SUBMITTING or not job["trainer_id"]:
                return
            next_state = replace(ex, stage=fd.Stage.RUNNING, evidence_ref=job["run_ref"])
            event = "eve_trainer_acknowledged"
        ledger.transition(entry.execution_id, version=ex.version, execution=next_state, event=event)

    @contextmanager
    def current(self, scope: LearningScope, *, write: bool) -> Iterator[Grant]:
        grant = self._grant(read_only=not write)
        spec = grant.spec
        if (
            scope != scope_from(spec)
            or self.organization != scope.organization
            or self.workspace != scope.workspace
            or self.channel != spec["channel"]
        ):
            raise ValueError("eve_scope_changed")
        authority = lr.LocalLearningRunGrant(self.directory / SELECTION_FILE)
        principal = authority.principal
        if (
            principal.channel != "slack"
            or principal.actor != scope.actor
            or principal.tenant != scope.organization
            or principal.role != "owner"
        ):
            raise ValueError("eve_reviewer_not_enrolled")
        with authority.guard(principal, write=write) as enrolled:
            if (
                enrolled.engagement_epoch != scope.engagement
                or enrolled.workflow_ref != scope.workflow
                or enrolled.mandate_id != spec["mandate_id"]
                or "read" not in enrolled.permissions
                or (write and not {"start", "cancel"} <= enrolled.permissions)
            ):
                raise ValueError("eve_permission_changed")
            mandate = lr.LocalMandateSource(self.directory).current(enrolled, principal)
            plan = lr.SealedPlanSource(self.directory).current_plan(enrolled, mandate)
            if (
                mandate.state != "active"
                or not mandate.training
                or mandate.max_fits < 1
                or mandate.max_runs < 1
                or mandate.mandate_revision != spec["mandate_revision"]
                or not isinstance(plan, lr.LearningPlan)
                or not plan.training_capable
                or plan.tenant != scope.organization
                or plan.engagement_epoch != scope.engagement
                or plan.workflow_ref != scope.workflow
                or plan.mandate_id != mandate.mandate_id
                or plan.mandate_revision != mandate.mandate_revision
                or plan.plan_ref != grant.plan_ref
                or plan.benchmark_ref != grant.benchmark_ref
                or plan.model_route_ref != "eve-24b-qlora"
            ):
                raise ValueError("eve_proposal_changed")
            _, entry = self._reservation(grant, read_only=not write)
            yield replace(
                grant,
                can_control={"start", "cancel"} <= enrolled.permissions
                and time.time() < spec["deadline"]
                and not entry.execution.cancel_requested
                and entry.execution.stage
                in {fd.Stage.RESERVED, fd.Stage.SUBMITTING, fd.Stage.RUNNING},
            )


def scope_from(spec: dict[str, Any]) -> LearningScope:
    return LearningScope(
        spec["tenant"],
        spec["engagement_id"],
        spec["workspace"],
        spec["reviewer"],
        spec["workflow"],
        spec["dataset"],
        spec["dataset_revision"],
    )


def renew_host_permit(directory: Path, organization: str) -> None:
    """Only the installed worker can renew an admitted fit after rereading its live authorities."""
    if not os.path.lexists(directory / SELECTION_FILE):
        return
    from opentour_cell.pilot.eve_learning_service import EveLearningService
    from opentour_cell.pilot.signed_records import PilotRecordRefused

    try:
        # The untrusted candidate only selects a binding to verify; it cannot start a job.
        ex = Exchange(directory / "eve-exchange")
        for job, finished in ((ex.active(), False), (ex.unreported(), True)):
            if job is None:
                continue
            spec = job["spec"]
            authority = EveAuthority(directory, organization, spec["workspace"], spec["channel"])
            scope = scope_from(spec)
            with authority.current(scope, write=not finished) as grant:
                if digest(grant.spec) != job["spec_digest"]:
                    continue
                EveLearningService(authority, ex, directory, spec["channel"]).verify_start(
                    scope, grant.plan_ref, job["approval_key"], digest(grant.spec)
                )
                authority.acknowledge(grant, job, finished=finished)
                if finished:
                    ex.mark_reported(job)
                else:
                    ex.renew(grant.reservation_ref, digest(grant.spec))
    except (OSError, ValueError, sqlite3.Error, PilotRecordRefused):
        # The offline supervisor stops when the last independently refreshed permit expires.
        return
