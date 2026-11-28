"""Open an explicitly sealed metadata capture grant on the customer worker."""

from __future__ import annotations

import hmac
import os
import re
from pathlib import Path
from typing import cast

from opentour_cell.pilot.capture_streams import BOUNDARIES, Action, Boundary, CaptureGrant
from opentour_cell.pilot.intake_mcp import require_ref
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.operating_point import canonical, digest

SCHEMA = "opentour.pilot.capture-authority.v1"
PURPOSE = "private_decision_capture_metadata"
_BOUNDARIES = BOUNDARIES
_FIELDS = {
    "schema_version",
    "purpose",
    "tenant",
    "engagement",
    "workflow",
    "grant_ref",
    "policy_revision",
    "mode",
    "expires_at",
    "recorders",
    "inspectors",
}


def _require(value: object) -> None:
    if not value:
        raise ValueError("decision_capture_unavailable")


class LocalCaptureAuthority:
    """Resolve the current actor independently of the private grant selector."""

    def __init__(self, directory: Path, tenant: str, workflow: str):
        _require(directory.is_absolute() and directory.resolve(strict=True) == directory)
        self.directory = directory
        self.tenant = require_ref(tenant)
        self.workflow = require_ref(workflow)
        scope = digest([self.tenant, self.workflow])
        self.path = directory / f"capture-authority-{scope}.json"

    def resolve(
        self, actor: str, workflow: str, action: Action, boundary: Boundary | None
    ) -> CaptureGrant | None:
        if workflow != self.workflow:
            return None
        try:
            require_ref(actor)
            key_doc = _read_private(self.directory / "capture-authority-key.json", 4096)
            envelope = _read_private(self.path, 32768)
            _require(set(key_doc) == {"key"} and set(envelope) == {"grant", "seal_sha256"})
            key_ref, grant, signature = key_doc["key"], envelope["grant"], envelope["seal_sha256"]
            _require(isinstance(key_ref, str) and re.fullmatch(r"[0-9a-f]{64}", key_ref))
            _require(isinstance(signature, str) and re.fullmatch(r"[0-9a-f]{64}", signature))
            _require(isinstance(grant, dict) and set(grant) == _FIELDS)
            key = bytes.fromhex(key_ref)
            _require(
                hmac.compare_digest(
                    signature, hmac.digest(key, canonical(grant).encode(), "sha256").hex()
                )
            )
            _require(
                (grant["schema_version"], grant["purpose"], grant["tenant"], grant["workflow"])
                == (SCHEMA, PURPOSE, self.tenant, workflow)
                and grant["mode"] in {"off", "metadata_only"}
            )
            grant_ref = require_ref(grant["grant_ref"])
            revoked_ref = digest([self.tenant, workflow, grant["engagement"], grant_ref])
            _require(not os.path.lexists(self.directory / f"capture-revoked-{revoked_ref}.json"))
            recorders, inspectors = grant["recorders"], grant["inspectors"]
            _require(isinstance(recorders, dict) and recorders.keys() <= _BOUNDARIES)
            _require(isinstance(inspectors, dict) and len(inspectors) <= 128)
            sources: dict[Boundary, str] = {}
            for name, assignment in recorders.items():
                _require(isinstance(assignment, dict) and set(assignment) == {"actor", "source"})
                require_ref(assignment["actor"])
                sources[cast(Boundary, name)] = require_ref(assignment["source"])
            for subject, allowed in inspectors.items():
                require_ref(subject)
                _require(
                    isinstance(allowed, list)
                    and bool(allowed)
                    and len(allowed) == len(set(allowed))
                    and set(allowed) <= sources.keys()
                )
            permitted: tuple[tuple[Boundary, str], ...]
            if action == "record" and boundary in sources:
                assert boundary is not None
                if recorders[boundary]["actor"] != actor:
                    return None
                permitted = ((boundary, sources[boundary]),)
            elif action == "inspect" and boundary is None and actor in inspectors:
                permitted = tuple((name, sources[name]) for name in sorted(inspectors[actor]))
            else:
                return None
            stamp = hmac.digest(
                key,
                canonical(["opentour.pilot.capture-fingerprints.v1", signature]).encode(),
                "sha256",
            )
            return CaptureGrant(
                actor,
                self.tenant,
                grant["engagement"],
                workflow,
                "capture-" + digest([grant_ref, signature])[:48],
                grant["policy_revision"],
                grant["expires_at"],
                grant["mode"],
                permitted,
                stamp,
            )
        except (OSError, KeyError, TypeError, ValueError):
            return None
