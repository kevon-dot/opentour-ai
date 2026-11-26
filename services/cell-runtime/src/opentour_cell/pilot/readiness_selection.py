"""Compose the worker's scoped readiness reader from one private installation selection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal, require_ref
from opentour_cell.pilot.live_authority import PrivateLiveAuthority
from opentour_cell.pilot.live_evidence import LiveEvidence
from opentour_cell.pilot.live_readiness import (
    DecisionContext,
    LiveReadinessSource,
    LocalRepairAssessments,
)
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.training_readiness import TrainingReadiness

SCHEMA = "opentour.pilot.readiness-selection.v1"


@dataclass(frozen=True)
class ReadinessComponents:
    readiness: TrainingReadiness
    source: LiveReadinessSource
    assessments: LocalRepairAssessments | None
    worker: IntakePrincipal
    workflow: str
    selection: dict[str, Any]

    def refresh(self) -> dict[str, Any] | None:
        """Explicit worker action after ingestion. Reading in Slack never calls this."""
        with self.source.live.authority.guard(self.worker, self.workflow) as scope:
            if not scope.can_ingest or not scope.can_inspect:
                raise IntakeError("readiness_worker_unavailable")
            if (
                _read_private(self.readiness.directory / "readiness-selection.json", 4096)
                != self.selection
            ):
                raise IntakeError("readiness_selection_changed")
            snapshot = self.source.snapshot(self.worker, self.workflow)
            if snapshot is None:
                return None
            if self.assessments is not None and snapshot.verified:
                self.assessments.refresh(snapshot)
            return self.readiness.refresh(self.workflow, actor=self.worker)


def open_readiness(
    directory: Path, tenant: str, workflow_ref: str, *, contexts: DecisionContext | None = None
) -> ReadinessComponents:
    selection = _read_private(directory / "readiness-selection.json", 4096)
    if (
        set(selection) != {"schema_version", "workflow_ref", "worker_ref", "policy_file"}
        or selection["schema_version"] != SCHEMA
        or selection["workflow_ref"] != workflow_ref
    ):
        raise IntakeError("readiness_selection_unavailable")
    policy = selection["policy_file"]
    if policy is not None and not (
        isinstance(policy, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\.json", policy)
    ):
        raise IntakeError("readiness_policy_unavailable")
    worker = IntakePrincipal(tenant, require_ref(selection["worker_ref"]), "intake")
    authority = PrivateLiveAuthority(directory, tenant, workflow_ref)
    with authority.guard(worker, workflow_ref) as scope:
        if not scope.can_inspect or not scope.can_ingest:
            raise IntakeError("readiness_worker_unavailable")
        live = LiveEvidence(directory, authority)
        source = LiveReadinessSource(live)
        assessments = (
            LocalRepairAssessments(directory, live, worker, directory / policy, contexts=contexts)
            if policy
            else None
        )
        readiness = TrainingReadiness(
            directory, tenant, scope.engagement_epoch, source=source, assessments=assessments
        )
        return ReadinessComponents(readiness, source, assessments, worker, workflow_ref, selection)
