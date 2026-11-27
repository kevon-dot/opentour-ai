"""Bind a verified human release to the worker's current recipient permission."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from opentour_cell.pilot import release_gate, repair_workflow, source_intake
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.release_outbox import Action, ApprovedRelease, ReleaseSelection


@dataclass(frozen=True)
class HandoffPermission:
    tenant: str
    engagement: str
    workflow_ref: str
    proposal_ref: str
    proposal_revision: str
    artifact_sha256: str
    recipient: str
    destination_ref: str
    revision: str
    expires_at: int


class CurrentHandoffPermissions(Protocol):
    """Return the custodian's current, actor- and action-bound handoff permission."""

    def resolve(
        self, workflow_ref: str, actor: str, action: Action
    ) -> HandoffPermission | None: ...


def approval_ref(receipt: Mapping[str, object]) -> str:
    """Name the exact durable approval selected by the worker."""
    return "approval-" + digest(dict(receipt))[:48]


class RecordedReleaseAuthority:
    def __init__(self, directory: Path, tenant: str, permissions: CurrentHandoffPermissions):
        self.directory = directory
        self.tenant = tenant
        self.permissions = permissions

    def resolve(
        self, db: sqlite3.Connection, selection: ReleaseSelection, actor: str, action: Action
    ) -> ApprovedRelease | None:
        permit = self.permissions.resolve(selection.workflow_ref, actor, action)
        scope = db.execute("SELECT id,withdrawn_at FROM engagement WHERE singleton=1").fetchone()
        if (
            permit is None
            or scope is None
            or scope[1] is not None
            or (permit.tenant, permit.engagement, permit.workflow_ref, permit.proposal_ref)
            != (self.tenant, scope[0], selection.workflow_ref, selection.proposal_ref)
        ):
            return None
        try:
            approval = release_gate.inspect_recorded_release(
                self.directory,
                self.tenant,
                permit.proposal_ref,
                permit.proposal_revision,
                permit.artifact_sha256,
            )
            if (
                approval is None
                or approval["decision"] != "approved"
                or approval["audience"] != permit.recipient
                or approval_ref(approval) != selection.approval_ref
            ):
                return None
            evidence = repair_workflow.managed_review_evidence(
                self.directory,
                self.tenant,
                permit.proposal_ref,
                permit.recipient,
                approval["team"],
                approval["channel"],
            )
            audit = evidence["audit"]
            if (audit["run_revision"], audit["candidate_digest"]) != (
                permit.proposal_revision,
                permit.artifact_sha256,
            ):
                return None
            intake = source_intake.check_current_intake(
                self.directory, self.tenant, permit.engagement, evidence["dataset_revision"]
            )
            if intake["workflow"] != permit.workflow_ref:
                return None
            return ApprovedRelease(
                self.tenant,
                permit.engagement,
                selection,
                permit.proposal_revision,
                permit.artifact_sha256,
                permit.revision,
                permit.destination_ref,
                approval["recorded_at"],
                permit.expires_at,
            )
        except (OSError, KeyError, TypeError, ValueError, sqlite3.Error):
            return None
