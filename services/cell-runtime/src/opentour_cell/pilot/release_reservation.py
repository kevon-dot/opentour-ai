"""Reserve a current sealed release under the customer worker's configured operator."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from opentour_cell.pilot import release_gate
from opentour_cell.pilot.intake_mcp import require_ref
from opentour_cell.pilot.local_handoff_permissions import LocalHandoffPermissions
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.operating_point import canonical
from opentour_cell.pilot.release_authority import (
    HandoffPermission,
    RecordedReleaseAuthority,
    approval_ref,
)
from opentour_cell.pilot.release_outbox import Action, ReleaseOutbox, ReleaseSelection
from opentour_cell.pilot.training import LocalTrainingStore

SCHEMA = "opentour.pilot.release-reservation.v1"


@dataclass(frozen=True)
class _PinnedPermission:
    permissions: LocalHandoffPermissions
    expected: HandoffPermission

    def resolve(self, workflow_ref: str, actor: str, action: Action) -> HandoffPermission | None:
        current = self.permissions.resolve(workflow_ref, actor, action)
        return current if current == self.expected else None


def reserve(configuration: Path, operation_key: str) -> str:
    """Return the opaque reference after the existing outbox transaction commits."""
    operation_key = require_ref(operation_key)
    try:
        canonical_path = configuration.resolve(strict=True)
    except RuntimeError:
        raise ValueError("invalid_release_reservation_configuration") from None
    if not configuration.is_absolute() or canonical_path != configuration:
        raise ValueError("invalid_release_reservation_configuration")
    scope = _read_private(configuration, 4096)
    if (
        set(scope) != {"schema_version", "tenant", "actor", "workflow_ref"}
        or scope["schema_version"] != SCHEMA
    ):
        raise ValueError("invalid_release_reservation_configuration")
    tenant = require_ref(scope["tenant"])
    actor = require_ref(scope["actor"])
    workflow = require_ref(scope["workflow_ref"])
    directory = configuration.parent
    permissions = LocalHandoffPermissions(directory, tenant)
    permit = permissions.resolve(workflow, actor, "reserve")
    if permit is None:
        raise ValueError("release_reservation_unavailable")
    approval = release_gate.inspect_recorded_release(
        directory, tenant, permit.proposal_ref, permit.proposal_revision, permit.artifact_sha256
    )
    if (
        approval is None
        or approval["decision"] != "approved"
        or not isinstance(approval.get("decision_seal"), str)
    ):
        raise ValueError("release_reservation_unavailable")
    selection = ReleaseSelection(workflow, permit.proposal_ref, approval_ref(approval))
    store = LocalTrainingStore(directory / "training.sqlite3", tenant, existing_only=True)
    # The outbox repeats its approval and source checks inside the write
    # transaction. Pin the selected permission so it cannot change destinations
    # between the verified approval read and reservation.
    authority = RecordedReleaseAuthority(directory, tenant, _PinnedPermission(permissions, permit))
    outbox = ReleaseOutbox(store, authority, seal_received=permissions._seal_received)
    return outbox.reserve(selection, actor, operation_key).reference


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument("--operation-key", required=True)
    args = parser.parse_args()
    try:
        release_ref = reserve(args.configuration, args.operation_key)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        print("release_reservation_unavailable", file=sys.stderr)
        return 2
    print(canonical({"release_ref": release_ref}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
