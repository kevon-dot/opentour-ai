"""Connect private receipt reads to the platform's pinned issuer registry."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from importlib import import_module
from pathlib import Path
from typing import Protocol

from opentour_cell.pilot.intake_mcp import require_ref
from opentour_cell.pilot.recipient_lifecycle import (
    LifecycleAuthority,
    LifecycleBinding,
    LifecyclePolicy,
    RollbackPolicy,
    VerifiedLifecycleRecord,
    _epoch,
)
from opentour_cell.pilot.repair_bundle import _read

POLICY_PURPOSE = "opentour.pilot.handoff-lifecycle-policy.v1"
REGISTRY_FILE = "pilot-issuer-registry.json"
_MAX_REGISTRY_BYTES = 262_144
_MAX_POLICY_BYTES = 16_384
_POLICY_FIELDS = {
    "profile_version",
    "engagement_id",
    "issued_at",
    "not_after",
    "binding",
    "replay_key_id",
    "evaluation_only",
    "rollback",
}
_ROLLBACK_FIELDS = {
    "observer_key_id",
    "authorization_ref",
    "environment_ref",
    "expected_fallback_version",
    "authorized_at",
    "expires_at",
}


class _PilotVerifier(Protocol):
    def verify(self, record: bytes, purpose: str) -> dict[str, object]: ...


class _RegistryLifecycleAuthority:
    def __init__(self, directory: Path, registry: bytes, verifier: _PilotVerifier) -> None:
        self.directory = directory
        self.registry = registry
        self.verifier = verifier

    def _verify_current(self, record: bytes, purpose: str) -> dict[str, object]:
        self._require_current_registry()
        payload = self.verifier.verify(record, purpose)
        self._require_current_registry()
        return payload

    def _require_current_registry(self) -> None:
        try:
            current = _read(self.directory / REGISTRY_FILE, _MAX_REGISTRY_BYTES)
        except (OSError, ValueError):
            raise ValueError("lifecycle_registry_changed") from None
        if current != self.registry:
            raise ValueError("lifecycle_registry_changed")

    def current_policy(self, binding: LifecycleBinding) -> LifecyclePolicy | None:
        raw = _read(
            self.directory / f"recipient-lifecycle-{require_ref(binding.release_ref)}.policy.json",
            _MAX_POLICY_BYTES,
        )
        payload = self._verify_current(raw, POLICY_PURPOSE)
        if (
            payload.keys() != _POLICY_FIELDS
            or payload["profile_version"] != "1.0"
            or payload["engagement_id"] != binding.engagement
            or payload["binding"] != asdict(binding)
        ):
            return None
        evaluation_only = payload["evaluation_only"]
        deadline = int(_epoch(payload["not_after"]))
        if (
            type(evaluation_only) is not bool
            or not _epoch(payload["issued_at"]) <= time.time() < deadline
        ):
            return None
        rollback = _rollback_policy(payload["rollback"], deadline)
        if evaluation_only and rollback is not None:
            return None
        return LifecyclePolicy(
            binding, require_ref(payload["replay_key_id"]), evaluation_only, deadline, rollback
        )

    def verify(self, record: bytes, purpose: str) -> VerifiedLifecycleRecord:
        payload = self._verify_current(record, purpose)
        # The platform verifies the exact bytes above, including canonical encoding, every
        # signature, its purpose, role and issuer key in the current pinned registry.
        signatures = json.loads(record)["signatures"]
        if not isinstance(signatures, list) or len(signatures) != 1:
            raise ValueError("lifecycle_signature_invalid")
        signer = signatures[0]
        if not isinstance(signer, dict) or signer.get("purpose") != purpose:
            raise ValueError("lifecycle_signature_invalid")
        return VerifiedLifecycleRecord(
            payload, require_ref(signer.get("signer_role")), require_ref(signer.get("key_id"))
        )


def _rollback_policy(value: object, deadline: int) -> RollbackPolicy | None:
    if value is None:
        return None
    if not isinstance(value, dict) or value.keys() != _ROLLBACK_FIELDS:
        raise ValueError("lifecycle_rollback_policy_invalid")
    authorized_at = value["authorized_at"]
    expires_at = value["expires_at"]
    if (
        type(authorized_at) is not int
        or type(expires_at) is not int
        or not 0 < authorized_at < expires_at <= deadline
    ):
        raise ValueError("lifecycle_rollback_policy_invalid")
    return RollbackPolicy(
        require_ref(value["observer_key_id"]),
        require_ref(value["authorization_ref"]),
        require_ref(value["environment_ref"]),
        require_ref(value["expected_fallback_version"]),
        authorized_at,
        expires_at,
    )


def open_lifecycle_authority(directory: Path) -> LifecycleAuthority | None:
    """Initialize once for the installed worker. The published root pin governs trust."""
    try:
        registry = _read(directory / REGISTRY_FILE, _MAX_REGISTRY_BYTES)
        platform = import_module("opentour_cell.pilot.signed_records")
        verifier: _PilotVerifier = platform.PilotVerifier(registry)
    except Exception:
        # An unprovisioned registry or unavailable platform verifier cannot admit receipts.
        return None
    return _RegistryLifecycleAuthority(directory, registry, verifier)
