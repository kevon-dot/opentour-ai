"""Generated policy and receipt boundary vectors for the worker's read-only projection."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import pytest
from opentour_cell.pilot import recipient_lifecycle as lifecycle
from opentour_cell.pilot.recipient_delivery import (
    LocalRecipientDelivery,
    delivery_receipt_binding,
    delivery_receipt_reference,
)
from opentour_cell.pilot.release_authority import HandoffPermission
from opentour_cell.pilot.release_outbox import ReleaseSelection, ReleaseStatus
from test_recipient_test_rollback import GeneratedTarget, generated_target  # noqa: F401, F811
from test_repair_bundle import read, write


@dataclass
class FixtureAuthority:
    """Supply already authenticated fixture values at the canonical verifier port."""

    policy: lifecycle.LifecyclePolicy | None

    def current_policy(self, _binding):
        return self.policy

    def verify(self, raw, purpose):
        envelope = json.loads(raw)
        if envelope.pop("test_purpose") != purpose:
            raise ValueError("fixture_purpose_denied")
        return lifecycle.VerifiedLifecycleRecord(**envelope)


@dataclass(frozen=True)
class Fixture:
    directory: Path
    permission: HandoffPermission
    selection: ReleaseSelection
    delivery: ReleaseStatus
    authority: FixtureAuthority
    cohort_revision: str
    now: int

    def record(self, kind, *, payload_updates=None, **envelope_updates):
        policy = self.authority.policy
        assert policy is not None
        payload = {
            **asdict(policy.binding),
            "profile_version": "1.0",
            **lifecycle._claim_times(self.now - 5, self.now - 2, self.now + 10),
            "package_sha256": "d" * 64,
        }
        if kind == "rollback":
            rollback = policy.rollback
            assert rollback is not None
            key_id = rollback.observer_key_id
            purpose = lifecycle.ROLLBACK_PURPOSE
            role = lifecycle.ROLLBACK_ISSUER
            payload.update(
                rollback_authorization_ref=rollback.authorization_ref,
                environment_ref=rollback.environment_ref,
                restored_version=rollback.expected_fallback_version,
                serving_readback_ref="readback-generated-one",
            )
        else:
            key_id = policy.replay_key_id
            purpose = lifecycle.REPLAY_PURPOSE
            role = lifecycle.REPLAY_ISSUER
            payload.update(evidence_revision="e" * 64, replayed_cases=12)
        path = self.directory / f"recipient-lifecycle-{self.delivery.reference}.{kind}.json"
        write(
            path,
            {
                "test_purpose": purpose,
                "signer_role": role,
                "key_id": key_id,
                "payload": payload | (payload_updates or {}),
                **envelope_updates,
            },
        )
        return path

    def projection(self, **updates):
        return lifecycle.read_lifecycle(
            self.directory,
            **(
                {
                    "authority": self.authority,
                    "permission": self.permission,
                    "selection": self.selection,
                    "delivery": self.delivery,
                    "cohort_revision": self.cohort_revision,
                }
                | updates
            ),
        )


@pytest.fixture
def receipt_fixture(tmp_path):
    private = tmp_path / "worker"
    private.mkdir(mode=0o700)
    now = int(time.time())
    expiry = now + 300
    received_at = now - 10
    reference = "release-" + "1" * 48
    permission = HandoffPermission(
        "generated-tenant",
        "generated-engagement",
        "generated-workflow",
        "generated-proposal",
        "a" * 64,
        "b" * 64,
        "generated-recipient",
        "generated-destination",
        "generated-active-scope",
        expiry,
    )
    selected = ReleaseSelection(permission.workflow_ref, permission.proposal_ref, "approval-one")
    body = delivery_receipt_binding(
        release_ref=reference,
        tenant=permission.tenant,
        engagement=permission.engagement,
        destination_ref=permission.destination_ref,
        artifact_sha256=permission.artifact_sha256,
        package_sha256="d" * 64,
    )
    receiver_ref = delivery_receipt_reference(body | {"received_at": received_at})
    delivery = ReleaseStatus(
        reference, permission.destination_ref, "delivered", now - 15, now, receiver_ref, received_at
    )
    cohort_revision = "c" * 64
    binding = lifecycle.LifecycleBinding(
        permission.tenant,
        permission.engagement,
        permission.workflow_ref,
        selected.approval_ref,
        permission.proposal_ref,
        permission.proposal_revision,
        permission.artifact_sha256,
        permission.recipient,
        permission.destination_ref,
        reference,
        receiver_ref,
        cohort_revision,
    )
    rollback = lifecycle.RollbackPolicy(
        "independent-observer-key",
        "separately-approved-rollback",
        "test-environment-one",
        "e" * 64,
        now - 6,
        expiry,
    )
    authority = FixtureAuthority(
        lifecycle.LifecyclePolicy(binding, "off-worker-replay-key", False, expiry, rollback)
    )
    return Fixture(private, permission, selected, delivery, authority, cohort_revision, now)


def test_reads_exact_authorized_delivered_receipts_without_writes(receipt_fixture):
    replay_file = receipt_fixture.record("replay")
    rollback_file = receipt_fixture.record("rollback")
    artifacts = [
        (file.read_bytes(), file.stat().st_mtime_ns) for file in (replay_file, rollback_file)
    ]
    initial = receipt_fixture.projection()
    assert initial.replay and initial.replay.observed_at == receipt_fixture.now - 5
    assert initial.rollback and initial.rollback.environment_ref == "test-environment-one"
    assert initial.rollback.restored_version == "e" * 64
    assert receipt_fixture.projection() == initial
    assert artifacts == [
        (file.read_bytes(), file.stat().st_mtime_ns) for file in (replay_file, rollback_file)
    ]
    assert receipt_fixture.projection(authority=None) == lifecycle.LifecycleReadback()
    assert receipt_fixture.projection(cohort_revision="f" * 64) == lifecycle.LifecycleReadback()
    pending = replace(receipt_fixture.delivery, stage="delivery_unconfirmed")
    assert receipt_fixture.projection(delivery=pending) == lifecycle.LifecycleReadback()


@pytest.mark.parametrize(
    "field,value",
    [
        ("release_ref", "release-" + "0" * 48),
        ("receiver_receipt_ref", "received-" + "0" * 48),
        ("tenant", "generated-neighbor"),
        ("engagement", "expired-engagement"),
        ("workflow_ref", "generated-foreign-workflow"),
        ("proposal_ref", "generated-successor-proposal"),
        ("approval_ref", "approval-retired"),
        ("proposal_revision", "8" * 64),
        ("artifact_sha256", "8" * 64),
        ("recipient_ref", "changed-recipient"),
        ("destination_ref", "changed-destination"),
        ("cohort_revision", "9" * 64),
        ("package_sha256", "9" * 64),
        ("evidence_revision", "unavailable"),
        ("replayed_cases", 0),
        ("replayed_cases", True),
        ("evaluation_only", True),
        ("not_after", "1970-01-01T00:00:00Z"),
        ("observed_at", "2099-01-01T00:00:00Z"),
    ],
)
def test_replay_denies_unscoped_unbound_or_malformed_assurances(receipt_fixture, field, value):
    receipt_fixture.record("replay", payload_updates={field: value})
    assert receipt_fixture.projection() == lifecycle.LifecycleReadback()


@pytest.mark.parametrize(
    "change",
    [
        {"signer_role": lifecycle.REPLAY_ISSUER},
        {"key_id": "off-worker-replay-key"},
        {"test_purpose": lifecycle.REPLAY_PURPOSE},
    ],
)
def test_wrong_observer_cannot_turn_replay_into_rollback(receipt_fixture, change):
    receipt_fixture.record("replay")
    receipt_fixture.record("rollback", **change)
    projection = receipt_fixture.projection()
    assert projection.replay and projection.rollback is None


@pytest.mark.parametrize(
    "change",
    [
        {"environment_ref": "earlier-environment"},
        {"restored_version": "b" * 64},
        {"rollback_authorization_ref": "newer-authorization"},
        {"serving_readback_ref": "invalid ref"},
        {"observed_at": "1970-01-01T00:00:00Z"},
    ],
)
def test_rollback_checks_independent_operation_and_restored_version(receipt_fixture, change):
    receipt_fixture.record("replay")
    receipt_fixture.record("rollback", payload_updates=change)
    projection = receipt_fixture.projection()
    assert projection.replay and projection.rollback is None


def test_current_policy_can_restrict_evaluation_or_withdraw_during_read(
    receipt_fixture, monkeypatch
):
    receipt_fixture.record("replay")
    receipt_fixture.record("rollback")
    authority = receipt_fixture.authority
    assert authority.policy is not None
    authority.policy = replace(authority.policy, evaluation_only=True, rollback=None)
    evaluation = receipt_fixture.projection()
    assert evaluation.replay and evaluation.replay.evaluation_only and evaluation.rollback is None
    valid = authority.policy
    updates = iter((valid, None))
    monkeypatch.setattr(authority, "current_policy", lambda _: next(updates))
    assert receipt_fixture.projection() == lifecycle.LifecycleReadback()


def test_real_recipient_replay_producer_binds_installed_benchmark(
    generated_target: GeneratedTarget,  # noqa: F811
):
    target = generated_target
    receiver = LocalRecipientDelivery(Path(read(target.config)["recipient_grant"]))
    claim = lifecycle.prepare_recipient_replay_claim(receiver, target.release)
    delivery = read(target.receipt_path)
    assert claim["release_ref"] == target.release.reference
    assert claim["workflow_ref"] == target.release.selection.workflow_ref
    assert claim["approval_ref"] == target.release.selection.approval_ref
    assert claim["proposal_revision"] == target.release.proposal_revision
    assert claim["receiver_receipt_ref"] == target.receipt_ref
    assert claim["package_sha256"] == delivery["package_sha256"]
    assert claim["replayed_cases"] > 0 and len(claim["cohort_revision"]) == 64
    withheld = target.receipt_path.with_name("delivery-receipt-withheld.json")
    target.receipt_path.rename(withheld)
    with pytest.raises((OSError, ValueError)):
        lifecycle.prepare_recipient_replay_claim(receiver, target.release)


def test_rollback_claim_requires_operation_and_independent_restored_readback(receipt_fixture):
    policy = receipt_fixture.authority.policy
    assert policy is not None and policy.rollback is not None
    rollback = policy.rollback
    received = receipt_fixture.delivery.received_at
    assert received is not None
    observed = lifecycle.DestinationRollbackObservation(
        binding=policy.binding,
        authorization_ref=rollback.authorization_ref,
        environment_ref=rollback.environment_ref,
        previous_serving_version=policy.binding.artifact_sha256,
        restored_version=rollback.expected_fallback_version,
        operation_receipt_ref="generated-action-one",
        serving_readback_ref="generated-independent-observation-one",
        package_sha256="d" * 64,
        delivery_received_at=received,
        observed_at=receipt_fixture.now - 5,
    )
    claim = lifecycle.prepare_destination_rollback_claim(policy, observed)
    assert claim["release_ref"] == policy.binding.release_ref
    assert claim["restored_version"] == rollback.expected_fallback_version
    assert claim["serving_readback_ref"] == observed.serving_readback_ref
    receipt_fixture.record("rollback", payload_updates=claim)
    assert receipt_fixture.projection().rollback is not None
    for change in (
        {"authorization_ref": "wrong-authorization"},
        {"environment_ref": "wrong-environment"},
        {"previous_serving_version": rollback.expected_fallback_version},
        {"restored_version": policy.binding.artifact_sha256},
        {"operation_receipt_ref": observed.serving_readback_ref},
        {"package_sha256": "7" * 64},
        {"observed_at": rollback.authorized_at - 1},
    ):
        with pytest.raises(ValueError, match="destination_rollback_claim_unavailable"):
            lifecycle.prepare_destination_rollback_claim(policy, replace(observed, **change))
    with pytest.raises(ValueError, match="destination_rollback_claim_unavailable"):
        lifecycle.prepare_destination_rollback_claim(replace(policy, rollback=None), observed)
