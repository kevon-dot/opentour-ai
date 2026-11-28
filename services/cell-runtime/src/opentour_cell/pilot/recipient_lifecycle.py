"""Project release-scoped receipts through a current canonical authority adapter."""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from opentour_cell.pilot import repair_bundle
from opentour_cell.pilot.intake_mcp import require_ref
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.recipient_delivery import (
    LocalRecipientDelivery,
    delivery_receipt_binding,
    delivery_receipt_reference,
)
from opentour_cell.pilot.release_authority import HandoffPermission
from opentour_cell.pilot.release_outbox import ApprovedRelease, ReleaseSelection, ReleaseStatus

REPLAY_PURPOSE = "opentour.pilot.recipient-replay.v1"
ROLLBACK_PURPOSE = "opentour.pilot.serving-rollback.v1"
REPLAY_ISSUER = "recipient_replay_issuer"
ROLLBACK_ISSUER = "destination_observer"
_MAX_ENVELOPE_BYTES = 16_384
_HEX = re.compile(r"[0-9a-f]{64}")
_RELEASE_REF = re.compile(r"release-[0-9a-f]{48}")
_TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)")


@dataclass(frozen=True)
class LifecycleBinding:
    """The immutable handoff identity a signed later observation must match."""

    tenant: str
    engagement: str
    workflow_ref: str
    approval_ref: str
    proposal_ref: str
    proposal_revision: str
    artifact_sha256: str
    recipient_ref: str
    destination_ref: str
    release_ref: str
    receiver_receipt_ref: str
    cohort_revision: str


@dataclass(frozen=True)
class RollbackPolicy:
    observer_key_id: str
    authorization_ref: str
    environment_ref: str
    expected_fallback_version: str
    authorized_at: int
    expires_at: int


@dataclass(frozen=True)
class LifecyclePolicy:
    """A read policy resolved from separate, current release and rollback authority."""

    binding: LifecycleBinding
    replay_key_id: str
    evaluation_only: bool
    expires_at: int
    rollback: RollbackPolicy | None = None


@dataclass(frozen=True)
class DestinationRollbackObservation:
    """A destination observer's independently read operation and post-change state."""

    binding: LifecycleBinding
    authorization_ref: str
    environment_ref: str
    previous_serving_version: str
    restored_version: str
    operation_receipt_ref: str
    serving_readback_ref: str
    package_sha256: str
    delivery_received_at: int
    observed_at: int


@dataclass(frozen=True)
class VerifiedLifecycleRecord:
    payload: Mapping[str, object]
    signer_role: str
    key_id: str


class LifecycleAuthority(Protocol):
    """Resolve owner policy and authenticate receipts against the canonical registry."""

    def current_policy(self, binding: LifecycleBinding) -> LifecyclePolicy | None: ...

    def verify(self, record: bytes, purpose: str) -> VerifiedLifecycleRecord: ...


@dataclass(frozen=True)
class ReplayReadback:
    receipt_ref: str
    observed_at: int
    evaluation_only: bool


@dataclass(frozen=True)
class RollbackReadback:
    receipt_ref: str
    observed_at: int
    environment_ref: str
    restored_version: str


@dataclass(frozen=True)
class LifecycleReadback:
    replay: ReplayReadback | None = None
    rollback: RollbackReadback | None = None


def _sha(value: object) -> bool:
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _epoch(value: object) -> float:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ValueError("invalid_lifecycle_timestamp")
    return datetime.fromisoformat(value).timestamp()


def _policy_valid(policy: LifecyclePolicy, binding: LifecycleBinding, now: float) -> bool:
    require_ref(policy.replay_key_id)
    return (
        policy.binding == binding
        and type(policy.evaluation_only) is bool
        and (not policy.evaluation_only or policy.rollback is None)
        and type(policy.expires_at) is int
        and now < policy.expires_at < 2**63
    )


def _rollback_valid(policy: LifecyclePolicy, rollback: RollbackPolicy, now: float) -> bool:
    for ref in (
        rollback.observer_key_id,
        rollback.authorization_ref,
        rollback.environment_ref,
        rollback.expected_fallback_version,
    ):
        require_ref(ref)
    return (
        not policy.evaluation_only
        and rollback.observer_key_id != policy.replay_key_id
        and rollback.expected_fallback_version != policy.binding.artifact_sha256
        and type(rollback.authorized_at) is int
        and type(rollback.expires_at) is int
        and 0 < rollback.authorized_at <= now < rollback.expires_at <= policy.expires_at
    )


def _delivery_reference(binding: LifecycleBinding, package_sha256: str, received_at: int) -> str:
    body = delivery_receipt_binding(
        release_ref=binding.release_ref,
        tenant=binding.tenant,
        engagement=binding.engagement,
        destination_ref=binding.destination_ref,
        artifact_sha256=binding.artifact_sha256,
        package_sha256=package_sha256,
    )
    return delivery_receipt_reference(body | {"received_at": received_at})


def _observation(
    directory: Path,
    authority: LifecycleAuthority,
    policy: LifecyclePolicy,
    *,
    received_at: int,
    deadline: int,
    rollback: RollbackPolicy | None = None,
) -> tuple[dict[str, object], int, float] | None:
    binding = policy.binding
    is_rollback = rollback is not None
    kind = "rollback" if is_rollback else "replay"
    purpose = ROLLBACK_PURPOSE if is_rollback else REPLAY_PURPOSE
    expected_role = ROLLBACK_ISSUER if is_rollback else REPLAY_ISSUER
    expected_key = rollback.observer_key_id if rollback is not None else policy.replay_key_id
    exact: dict[str, object] = {**asdict(binding), "profile_version": "1.0"}
    if rollback is not None:
        exact.update(
            rollback_authorization_ref=rollback.authorization_ref,
            environment_ref=rollback.environment_ref,
            restored_version=rollback.expected_fallback_version,
        )
    specific = (
        {"rollback_operation_receipt_ref", "serving_readback_ref"}
        if is_rollback
        else {"evidence_revision", "replayed_cases"}
    )
    try:
        raw = repair_bundle._read(
            directory / f"recipient-lifecycle-{binding.release_ref}.{kind}.json",
            _MAX_ENVELOPE_BYTES,
        )
        record = authority.verify(raw, purpose)
        payload = dict(record.payload)
        if (
            record.signer_role != expected_role
            or record.key_id != expected_key
            or payload.keys()
            != exact.keys() | {"issued_at", "not_after", "observed_at", "package_sha256"} | specific
            or any(payload[key] != value for key, value in exact.items())
            or not _sha(payload["package_sha256"])
        ):
            return None
        if rollback is None:
            cases = payload["replayed_cases"]
            if (
                not _sha(payload["evidence_revision"])
                or type(cases) is not int
                or not 0 < cases < 2**53
            ):
                return None
        else:
            operation = require_ref(payload["rollback_operation_receipt_ref"])
            readback = require_ref(payload["serving_readback_ref"])
            if len({rollback.authorization_ref, operation, readback}) != 3:
                return None
        if binding.receiver_receipt_ref != _delivery_reference(
            binding, str(payload["package_sha256"]), received_at
        ):
            return None
        observed = _epoch(payload["observed_at"])
        issued = _epoch(payload["issued_at"])
        expires = _epoch(payload["not_after"])
        now = time.time()
        if not received_at <= observed <= issued <= now < expires <= deadline:
            return None
        if rollback is not None and not rollback.authorized_at <= observed < rollback.expires_at:
            return None
        return payload, int(observed), expires
    except Exception:
        # A verifier refusal or an unavailable private receipt never changes delivery status.
        return None


def read_lifecycle(
    directory: Path,
    *,
    permission: HandoffPermission,
    selection: ReleaseSelection,
    delivery: ReleaseStatus | None,
    cohort_revision: str,
    authority: LifecycleAuthority | None = None,
) -> LifecycleReadback:
    """Read later receipts without doing the recipient replay, lookup or target change."""
    if (
        authority is None
        or delivery is None
        or delivery.stage != "delivered"
        or delivery.destination_ref != permission.destination_ref
        or (selection.workflow_ref, selection.proposal_ref)
        != (permission.workflow_ref, permission.proposal_ref)
        or not _sha(cohort_revision)
        or not _RELEASE_REF.fullmatch(delivery.reference)
        or delivery.receiver_receipt_ref is None
        or delivery.received_at is None
    ):
        return LifecycleReadback()
    binding = LifecycleBinding(
        permission.tenant,
        permission.engagement,
        selection.workflow_ref,
        selection.approval_ref,
        selection.proposal_ref,
        permission.proposal_revision,
        permission.artifact_sha256,
        permission.recipient,
        permission.destination_ref,
        delivery.reference,
        delivery.receiver_receipt_ref,
        cohort_revision,
    )
    try:
        policy = authority.current_policy(binding)
        if policy is None or not _policy_valid(policy, binding, time.time()):
            return LifecycleReadback()
        deadline = min(permission.expires_at, policy.expires_at)
        replay = _observation(
            directory, authority, policy, received_at=delivery.received_at, deadline=deadline
        )
        rollback = None
        if policy.rollback is not None and _rollback_valid(policy, policy.rollback, time.time()):
            rollback = _observation(
                directory,
                authority,
                policy,
                received_at=delivery.received_at,
                deadline=min(deadline, policy.rollback.expires_at),
                rollback=policy.rollback,
            )
        if authority.current_policy(binding) != policy:
            return LifecycleReadback()
        now = time.time()
        if now >= deadline:
            return LifecycleReadback()
    except Exception:
        # The canonical authority contract treats every dependency exception as refusal.
        return LifecycleReadback()
    replay_readback = (
        ReplayReadback("replay-" + digest(replay[0])[:48], replay[1], policy.evaluation_only)
        if replay is not None and now < replay[2]
        else None
    )
    rollback_readback = None
    if rollback is not None and now < rollback[2] and policy.rollback is not None:
        rollback_readback = RollbackReadback(
            "rollback-" + digest(rollback[0])[:48],
            rollback[1],
            policy.rollback.environment_ref,
            policy.rollback.expected_fallback_version,
        )
    return LifecycleReadback(replay_readback, rollback_readback)


def _claim_times(observed_at: int, issued_at: int, expires_at: int) -> dict[str, str]:
    if not 0 < observed_at <= issued_at < expires_at:
        raise ValueError("invalid_lifecycle_claim_window")
    return {
        field: datetime.fromtimestamp(timestamp, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
        for field, timestamp in (
            ("observed_at", observed_at),
            ("issued_at", issued_at),
            ("not_after", expires_at),
        )
    }


def prepare_recipient_replay_claim(
    receiver: LocalRecipientDelivery, release: ApprovedRelease
) -> dict[str, object]:
    """Actually replay the delivered package in recipient custody, then prepare an unsigned claim.

    Only the purpose-authorized off-worker issuer can turn this claim into a receipt.
    Worker status reads never call this function.
    """
    grant = repair_bundle._grant(receiver.grant_file)
    with repair_bundle._current(receiver.grant_file, grant):
        slot, entitlement = receiver._scope(release, grant)
        delivery = receiver._receipt(slot, release, grant, entitlement)
        spec, members, installed = repair_bundle._load(slot / "artifact.zip", grant)
        evidence = repair_bundle._evidence(spec, members, grant)
        cohort_revision = evidence.get("protected_membership_digest")
        replayed = repair_bundle._replay(spec, members, grant)
        observed_at = int(time.time())
        expires_at = min(release.expires_at, grant["expires_at"], entitlement["expires_at"])
        if (
            grant["actor"] != spec["audience"]
            or repair_bundle._sha(installed) != delivery["package_sha256"]
            or not _sha(cohort_revision)
            or not replayed["fallback_rehearsed"]
            or replayed["cases"] <= 0
            or observed_at < delivery["received_at"]
            or repair_bundle._grant(receiver.grant_file) != grant
            or receiver._receipt(slot, release, grant, entitlement) != delivery
        ):
            raise ValueError("recipient_replay_claim_unavailable")
        binding = LifecycleBinding(
            release.tenant,
            release.engagement,
            release.selection.workflow_ref,
            release.selection.approval_ref,
            release.selection.proposal_ref,
            release.proposal_revision,
            release.artifact_sha256,
            grant["actor"],
            release.destination_ref,
            release.reference,
            delivery["receipt_ref"],
            str(cohort_revision),
        )
        return {
            **asdict(binding),
            "profile_version": "1.0",
            **_claim_times(observed_at, observed_at, expires_at),
            "evidence_revision": evidence["evidence_revision"],
            "replayed_cases": replayed["cases"],
            "package_sha256": delivery["package_sha256"],
        }


def prepare_destination_rollback_claim(
    policy: LifecyclePolicy, observed: DestinationRollbackObservation
) -> dict[str, object]:
    """Prepare an unsigned request from a destination observer's actual independent readback.

    The off-worker observer supplies separately verified policy and observations.
    A matching current version alone cannot supply a rollback operation receipt.
    """
    now = int(time.time())
    rollback = policy.rollback
    require_ref(observed.operation_receipt_ref)
    require_ref(observed.serving_readback_ref)
    if (
        rollback is None
        or not _policy_valid(policy, observed.binding, now)
        or not _rollback_valid(policy, rollback, now)
        or observed.authorization_ref != rollback.authorization_ref
        or observed.environment_ref != rollback.environment_ref
        or observed.previous_serving_version != policy.binding.artifact_sha256
        or observed.restored_version != rollback.expected_fallback_version
        or len(
            {
                rollback.authorization_ref,
                observed.operation_receipt_ref,
                observed.serving_readback_ref,
            }
        )
        != 3
        or not _sha(observed.package_sha256)
        or type(observed.observed_at) is not int
        or type(observed.delivery_received_at) is not int
        or not 0 < observed.delivery_received_at <= observed.observed_at <= now
        or observed.observed_at < rollback.authorized_at
        or _delivery_reference(
            policy.binding, observed.package_sha256, observed.delivery_received_at
        )
        != policy.binding.receiver_receipt_ref
    ):
        raise ValueError("destination_rollback_claim_unavailable")
    return {
        **asdict(policy.binding),
        "profile_version": "1.0",
        **_claim_times(observed.observed_at, now, rollback.expires_at),
        "package_sha256": observed.package_sha256,
        "rollback_authorization_ref": rollback.authorization_ref,
        "environment_ref": rollback.environment_ref,
        "restored_version": rollback.expected_fallback_version,
        "rollback_operation_receipt_ref": observed.operation_receipt_ref,
        "serving_readback_ref": observed.serving_readback_ref,
    }
