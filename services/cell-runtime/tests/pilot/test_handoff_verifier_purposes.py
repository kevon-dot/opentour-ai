"""Verify the OPE-504 purpose table with genuinely signed generated envelopes."""

from functools import partial

from opentour_cell.pilot import signed_records as sr
from test_signed_records import ENGAGEMENT, Kit, Signer, canonical, consent, refused, sign, stamp
from test_signed_records import kit as kit  # noqa: F401


def test_handoff_verifier_purpose_role_table() -> None:
    assert sr.HANDOFF_LIFECYCLE_POLICY_PURPOSE == "opentour.pilot.handoff-lifecycle-policy.v1"
    assert sr.RECIPIENT_REPLAY_PURPOSE == "opentour.pilot.recipient-replay.v1"
    assert sr.SERVING_ROLLBACK_PURPOSE == "opentour.pilot.serving-rollback.v1"
    assert sr.REQUIRED_ROLES[sr.HANDOFF_LIFECYCLE_POLICY_PURPOSE] == {
        "customer_owner",
        "operator_custodian",
    }
    assert sr.REQUIRED_ROLES[sr.RECIPIENT_REPLAY_PURPOSE] == {"recipient_replay_issuer"}
    assert sr.REQUIRED_ROLES[sr.SERVING_ROLLBACK_PURPOSE] == {"destination_observer"}
    assert sr.REGISTRY_ROLES[sr.RECIPIENT_REPLAY_ISSUER] == {sr.RECIPIENT_REPLAY_PURPOSE}
    assert sr.REGISTRY_ROLES[sr.DESTINATION_OBSERVER] == {sr.SERVING_ROLLBACK_PURPOSE}


def test_lifecycle_policy_is_consent_from_the_current_customer_and_custodian(kit: Kit) -> None:
    purpose = sr.HANDOFF_LIFECYCLE_POLICY_PURPOSE
    verifier = kit.verifier()
    policy = consent(
        profile_version="1.0",
        binding={"engagement": ENGAGEMENT, "release_ref": "release-one"},
        replay_key_id="sentinel-recipient-issuer",
        evaluation_only=True,
        rollback=None,
    )
    record = sign(policy, purpose, kit.customer, kit.operator)
    refused("customer_anchor_missing", partial(verifier.verify, record, purpose))
    verifier.pin_customer_anchor(kit.enrolment())
    assert verifier.verify(record, purpose) == policy
    foreign = sign(
        policy | {"engagement_id": "eng-sentinel-other"}, purpose, kit.customer, kit.operator
    )
    refused("customer_anchor_missing", partial(verifier.verify, foreign, purpose))
    for signer in (kit.customer, kit.operator):
        refused(
            "signature_role_missing",
            partial(verifier.verify, sign(policy, purpose, signer), purpose),
        )
    narrowed_operator = kit.operator.entry(purposes=[sr.ENROLMENT_PURPOSE, sr.GRANT_PURPOSE])
    narrowed = kit.verifier(kit.registry("2", keys=[narrowed_operator]))
    refused("key_purpose_not_permitted", partial(narrowed.verify, record, purpose))


def test_handoff_observations_require_distinct_purpose_limited_current_keys(kit: Kit) -> None:
    replay = Signer(sr.RECIPIENT_REPLAY_ISSUER, [sr.RECIPIENT_REPLAY_PURPOSE])
    rollback = Signer(sr.DESTINATION_OBSERVER, [sr.SERVING_ROLLBACK_PURPOSE])
    keys = [*kit.issuers(), replay.entry(), rollback.entry()]
    verifier = kit.verifier(kit.registry(keys=keys))
    observation = {
        "profile_version": "1.0",
        "engagement": ENGAGEMENT,
        "release_ref": "release-one",
        "issued_at": stamp(-60),
        "not_after": stamp(300),
    }
    replay_record = sign(observation, sr.RECIPIENT_REPLAY_PURPOSE, replay)
    rollback_payload = observation | {
        "rollback_authorization_ref": "authorization-one",
        "rollback_operation_receipt_ref": "operation-one",
        "serving_readback_ref": "readback-one",
    }
    rollback_record = sign(rollback_payload, sr.SERVING_ROLLBACK_PURPOSE, rollback)
    assert verifier.verify(replay_record, sr.RECIPIENT_REPLAY_PURPOSE) == observation
    assert verifier.verify(rollback_record, sr.SERVING_ROLLBACK_PURPOSE) == rollback_payload
    refused(
        "purpose_mismatch", partial(verifier.verify, replay_record, sr.SERVING_ROLLBACK_PURPOSE)
    )
    confessed = sign(rollback_payload, sr.SERVING_ROLLBACK_PURPOSE, replay)
    refused(
        "signature_role_unexpected",
        partial(verifier.verify, confessed, sr.SERVING_ROLLBACK_PURPOSE),
    )
    spoofed = canonical(
        {
            "payload": rollback_payload,
            "signatures": [
                replay.signature(
                    rollback_payload,
                    sr.SERVING_ROLLBACK_PURPOSE,
                    signer_role=sr.DESTINATION_OBSERVER,
                )
            ],
        }
    )
    refused(
        "key_role_not_permitted", partial(verifier.verify, spoofed, sr.SERVING_ROLLBACK_PURPOSE)
    )
    rotated = kit.verifier(kit.registry("2", keys=[*kit.issuers(), replay.entry()]))
    refused(
        "registry_superseded",
        partial(verifier.verify, rollback_record, sr.SERVING_ROLLBACK_PURPOSE),
    )
    refused("key_unknown", partial(rotated.verify, rollback_record, sr.SERVING_ROLLBACK_PURPOSE))
    assert rotated.verify(replay_record, sr.RECIPIENT_REPLAY_PURPOSE) == observation


def test_registry_requires_separate_keys_and_does_not_widen_observer_roles(kit: Kit) -> None:
    replay = Signer(sr.RECIPIENT_REPLAY_ISSUER, [sr.RECIPIENT_REPLAY_PURPOSE])
    alias = replay.entry(
        key_id="aliased-observer-key",
        role=sr.DESTINATION_OBSERVER,
        purposes=[sr.SERVING_ROLLBACK_PURPOSE],
    )
    refused(
        "trust_key_role_conflict",
        partial(kit.verifier, kit.registry(keys=[*kit.issuers(), replay.entry(), alias])),
    )
    widened = replay.entry(purposes=[sr.RECIPIENT_REPLAY_PURPOSE, sr.SERVING_ROLLBACK_PURPOSE])
    refused(
        "trust_key_purposes_invalid",
        partial(kit.verifier, kit.registry(keys=[*kit.issuers(), widened])),
    )
