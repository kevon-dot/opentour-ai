"""Read the current exact approved handoff selected by an authorized reviewer."""

from __future__ import annotations

from dataclasses import dataclass

from opentour_cell.pilot import release_gate
from opentour_cell.pilot.local_handoff_permissions import LocalHandoffPermissions, handoff_outbox
from opentour_cell.pilot.release_authority import approval_ref
from opentour_cell.pilot.release_outbox import ReleaseSelection, ReleaseStatus
from opentour_cell.pilot.training import LocalTrainingStore


@dataclass(frozen=True)
class HandoffReadback:
    selection: ReleaseSelection
    destination_ref: str
    approved_at: int
    delivery: ReleaseStatus | None


def read_handoff(
    store: LocalTrainingStore,
    *,
    actor: str,
    workflow_ref: str,
    proposal_ref: str,
    proposal_revision: str,
    artifact_sha256: str,
) -> HandoffReadback | None:
    """Resolve the installed worker's current scoped receipt without claiming delivery."""
    directory = store.path.parent
    permissions = LocalHandoffPermissions(directory, store.organization)
    permit = permissions.resolve(workflow_ref, actor, "inspect")
    if permit is None or (
        permit.proposal_ref,
        permit.proposal_revision,
        permit.artifact_sha256,
    ) != (
        proposal_ref,
        proposal_revision,
        artifact_sha256,
    ):
        return None
    recorded = release_gate.inspect_recorded_release(
        directory, store.organization, proposal_ref, proposal_revision, artifact_sha256
    )
    if recorded is None or recorded["decision"] != "approved" or "decision_seal" not in recorded:
        return None
    selection = ReleaseSelection(workflow_ref, proposal_ref, approval_ref(recorded))
    delivery = handoff_outbox(store).status(selection, actor)
    if permissions.resolve(workflow_ref, actor, "inspect") != permit:
        return None
    return HandoffReadback(selection, permit.destination_ref, recorded["recorded_at"], delivery)
