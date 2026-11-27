"""Resolve the custodian's current release destination on the customer worker."""

from __future__ import annotations

import hmac
import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from opentour_cell.pilot.intake_mcp import require_ref
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.release_authority import HandoffPermission, RecordedReleaseAuthority
from opentour_cell.pilot.release_outbox import Action, ApprovedRelease, ReleaseOutbox, ReleaseStatus
from opentour_cell.pilot.training import LocalTrainingStore

SCHEMA = "opentour.pilot.handoff-permission.v1"
PURPOSE = "deliver_approved_artifact"
_ACTIONS = {"reserve", "claim", "reconcile", "inspect"}
_SCOPE_FIELDS = (
    "tenant",
    "engagement",
    "workflow_ref",
    "proposal_ref",
    "recipient",
    "destination_ref",
    "grant_ref",
)
_DIGEST_FIELDS = ("proposal_revision", "artifact_sha256")
_FIELDS = {
    "schema_version",
    "purpose",
    "expires_at",
    "actors",
    *_SCOPE_FIELDS,
    *_DIGEST_FIELDS,
}
_HEX = re.compile(r"[0-9a-f]{64}")


def _now() -> int:
    return int(time.time())


class LocalHandoffPermissions:
    """Selectors only locate a grant. The seal binds destination, intent and action."""

    def __init__(self, directory: Path, tenant: str, clock: Callable[[], int] = _now):
        if not directory.is_absolute() or directory.resolve(strict=True) != directory:
            raise ValueError("release_destination_unavailable")
        self.directory = directory
        self.tenant = require_ref(tenant)
        self.clock = clock

    def _key(self) -> bytes:
        key_doc = _read_private(self.directory / "handoff-permission-key.json", 4096)
        key = key_doc.get("key")
        if set(key_doc) != {"key"} or not isinstance(key, str) or not _HEX.fullmatch(key):
            raise ValueError("release_destination_unavailable")
        return bytes.fromhex(key)

    def _seal_received(self, release: ApprovedRelease, status: ReleaseStatus) -> str:
        body = ["opentour.pilot.receiver-observation.v1", release.reference, asdict(status)]
        return hmac.digest(self._key(), canonical(body).encode(), "sha256").hex()

    def resolve(self, workflow_ref: str, actor: str, action: Action) -> HandoffPermission | None:
        try:
            scope = digest([self.tenant, require_ref(workflow_ref)])
            require_ref(actor)
            envelope = _read_private(self.directory / f"handoff-permission-{scope}.json", 32768)
            if set(envelope) != {"grant", "seal_sha256"}:
                return None
            grant, seal = envelope["grant"], envelope["seal_sha256"]
            if (
                not isinstance(seal, str)
                or not _HEX.fullmatch(seal)
                or not isinstance(grant, dict)
                or set(grant) != _FIELDS
                or not hmac.compare_digest(
                    seal,
                    hmac.digest(self._key(), canonical(grant).encode(), "sha256").hex(),
                )
            ):
                return None
            if (
                (grant["schema_version"], grant["purpose"], grant["tenant"], grant["workflow_ref"])
                != (SCHEMA, PURPOSE, self.tenant, workflow_ref)
                or type(grant["expires_at"]) is not int
                or not self.clock() < grant["expires_at"] < 2**63
                or any(
                    not isinstance(grant[field], str) or not _HEX.fullmatch(grant[field])
                    for field in _DIGEST_FIELDS
                )
            ):
                return None
            for field in _SCOPE_FIELDS:
                require_ref(grant[field])
            actors = grant["actors"]
            if not isinstance(actors, dict) or not 0 < len(actors) <= 128:
                return None
            for subject, actions in actors.items():
                require_ref(subject)
                if (
                    not isinstance(actions, list)
                    or len(actions) != len(set(actions))
                    or not 0 < len(actions) <= len(_ACTIONS)
                    or not set(actions) <= _ACTIONS
                ):
                    return None
            if action not in actors.get(actor, []):
                return None
            revoked = digest([self.tenant, workflow_ref, grant["engagement"], grant["grant_ref"]])
            if os.path.lexists(self.directory / f"handoff-permission-revoked-{revoked}.json"):
                return None
            return HandoffPermission(
                self.tenant,
                grant["engagement"],
                workflow_ref,
                grant["proposal_ref"],
                grant["proposal_revision"],
                grant["artifact_sha256"],
                grant["recipient"],
                grant["destination_ref"],
                "handoff-" + digest([grant["grant_ref"], seal])[:48],
                grant["expires_at"],
            )
        except (KeyError, OSError, TypeError, ValueError):
            return None


def handoff_outbox(store: LocalTrainingStore, clock: Callable[[], int] = _now) -> ReleaseOutbox:
    """Bind the outbox to the worker's existing store and private destination grant."""
    directory = store.path.parent
    permissions = LocalHandoffPermissions(directory, store.organization, clock)
    return ReleaseOutbox(
        store,
        RecordedReleaseAuthority(directory, store.organization, permissions),
        clock,
        seal_received=permissions._seal_received,
    )
